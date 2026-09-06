import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Item {
  id: root

  property var shell: null
  property var manifest: null
  property bool closingFromHost: false
  readonly property bool opened: window.visible

  // Theme
  readonly property color foreground: Color.foreground
  readonly property color background: Color.background
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent

  readonly property string backendPath: Quickshell.env("HOME") + "/.config/omarchy/plugins/davedes.mouse-keybind-settings/backend/keybinds_manager.py"
  readonly property string settingsDir: Quickshell.env("HOME") + "/.local/state/omarchy/settings"
  readonly property string settingsPath: root.settingsDir + "/davedes.mouse-keybind-settings.json"

  // State
  property var modelData: ({
    active: [],
    catalog: [],
    conflicts: [],
    total_active: 0,
    total_modified: 0,
    total_conflicts: 0
  })

  property bool loading: false
  property string searchQuery: ""
  property bool recordingSearch: false
  property string currentTab: "active" // "active" | "modified" | "catalog" | "conflicts"
  property string currentCategory: "All"
  property string toastMessage: ""
  property bool toastVisible: false
  property string pendingEditKey: ""
  property string pendingEditId: ""

  // Conflict handling. "rehome" (default): saving onto a taken key displaces
  // the other binding immediately and opens a mandatory rehome dialog for it.
  // "override": displace silently. "ask": probe first and ask every time.
  property string conflictMode: "rehome"
  property bool settingsOpen: false

  // Bindings that lost their key and still need a new one:
  // [{ id, description, lostKey, winner, default_key, action, command }]
  property var rehomeQueue: []
  // Set when the queue should auto-open its next entry after the next refresh.
  property bool rehomeAutoOpen: false

  // Any backend mutator in flight. Never restart a live process: block instead.
  readonly property bool busy: setProc.running || resetProc.running || enableProc.running || disableProc.running || migrateProc.running
  // One-shot: move the old plugin's trailing hl.unbind/o.bind lines into the managed block.
  property bool migrationTried: false

  // In-flight save (needed to re-run with --displace or to queue a rehome)
  property var pendingSave: null

  // "ask" mode conflict card
  property bool askOpen: false
  property var askConflict: null
  property bool askRemember: false

  readonly property var categories: [
    "All",
    "Window Management",
    "Workspaces",
    "Menus & System",
    "Applications",
    "Media & Audio"
  ]

  function open(payloadJson) {
    closingFromHost = false
    window.visible = true
    root.pendingEditKey = ""
    root.pendingEditId = ""
    if (payloadJson && payloadJson.trim().length > 0) {
      try {
        var payload = JSON.parse(payloadJson)
        if (payload && typeof payload === "object") {
          if (payload.edit) root.pendingEditKey = String(payload.edit)
          if (payload.id) root.pendingEditId = String(payload.id)
        }
      } catch (e) {
        console.warn("KeybindsPanel: Failed to parse summon payload:", e)
      }
    }
    loadData()
    Qt.callLater(function() {
      if (searchInput) searchInput.forceActiveFocus()
    })
  }

  onModelDataChanged: {
    if ((root.pendingEditKey || root.pendingEditId) && root.modelData && Array.isArray(root.modelData.active)) {
      var targetKey = root.pendingEditKey
      var targetId = root.pendingEditId
      root.pendingEditKey = ""
      root.pendingEditId = ""
      var row = targetId ? Model.findRowById(root.modelData, targetId) : null
      if (!row && targetKey) {
        var norm = Model.normalizeKey(targetKey)
        for (var i = 0; i < root.modelData.active.length; i++) {
          var item = root.modelData.active[i]
          if (item && item.key && Model.normalizeKey(item.key) === norm) { row = item; break }
        }
      }
      if (row) editDialog.openEdit(row)
    }
  }

  function close() {
    closingFromHost = true
    window.visible = false
  }

  function requestClose() {
    if (root.shell && typeof root.shell.hide === "function") {
      root.shell.hide((root.manifest && root.manifest.id) || "davedes.mouse-keybind-settings")
    } else {
      window.visible = false
    }
  }

  function showToast(msg) {
    root.toastMessage = msg
    root.toastVisible = true
    toastTimer.restart()
  }

  Timer {
    id: toastTimer
    interval: 3500
    onTriggered: root.toastVisible = false
  }

  function focusSearch() {
    Qt.callLater(function() {
      if (!editDialog.opened && !root.askOpen && searchInput) searchInput.forceActiveFocus()
    })
  }

  // ---- Persisted settings (shared file with Panel.qml; read-modify-write) ----

  function applySettings(raw) {
    try {
      var data = JSON.parse(raw)
      if (Util.isPlainObject(data)) {
        var m = data.keybindConflictMode
        if (m === "ask" || m === "rehome" || m === "override") root.conflictMode = m
      }
    } catch (e) { /* missing or corrupt -> keep defaults */ }
  }

  function setConflictMode(mode) {
    if (mode !== "ask" && mode !== "rehome" && mode !== "override") return
    root.conflictMode = mode
    var data = {}
    try {
      var cur = JSON.parse(settingsFile.text())
      if (Util.isPlainObject(cur)) data = cur
    } catch (e) { /* start fresh */ }
    data.keybindConflictMode = mode
    root.writeSettings(JSON.stringify(data, null, 2) + "\n")
  }

  // Writes go through a mkdir -p first so a fresh machine (no state dir yet)
  // still persists; the latest pending text wins if several writes queue up.
  property string pendingSettingsText: ""

  function writeSettings(text) {
    root.pendingSettingsText = text
    if (!settingsDirProc.running) settingsDirProc.running = true
  }

  BoundedProcess {
    id: settingsDirProc
    command: ["mkdir", "-p", root.settingsDir]
    timeoutMs: 5000
    onFinished: {
      if (root.pendingSettingsText.length > 0) {
        settingsFile.setText(root.pendingSettingsText)
        root.pendingSettingsText = ""
      }
    }
  }

  FileView {
    id: settingsFile
    path: root.settingsPath
    watchChanges: true
    atomicWrites: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.applySettings(text())
    onLoadFailed: { /* file absent -> defaults */ }
  }

  Component.onCompleted: settingsDirProc.running = true

  // ---- Backend calls ----

  function parseResult(proc) {
    var text = (proc.stdout || "").trim()
    if (!text) return null
    try {
      var lines = text.split("\n")
      return JSON.parse(lines[lines.length - 1])
    } catch (e) {
      return null
    }
  }

  function failureText(proc, res, fallback) {
    if (res && res.error) return String(res.error)
    if (proc.timedOut) return "backend timed out"
    if (proc.overflowed) return "backend output too large"
    if (proc.startFailed) return "backend could not start"
    var err = (proc.stderr || "").trim()
    if (err) {
      var errLines = err.split("\n")
      return errLines[errLines.length - 1]
    }
    return fallback
  }

  function startProc(proc, args) {
    if (proc.running) return false
    proc.command = [root.backendPath].concat(args)
    root.loading = true
    proc.running = true
    return true
  }

  function loadData() {
    if (listProc.running) {
      root.reloadPending = true
      return
    }
    root.reloadPending = false
    root.loading = true
    listProc.running = true
  }
  property bool reloadPending: false

  function saveKeybinding(key, desc, cmd, action, oldKey, id) {
    if (root.busy) {
      root.showToast("Still applying the previous change — try again in a moment")
      return
    }
    root.pendingSave = {
      key: key, desc: desc, cmd: cmd, action: action || "", oldKey: oldKey || "",
      id: id || String(desc || "").toLowerCase(), mode: root.conflictMode
    }
    // rehome / override: displace immediately. ask: probe without --displace.
    root.runSet(root.conflictMode !== "ask")
  }

  function runSet(displace) {
    var p = root.pendingSave
    if (!p) return
    var args = ["set", p.key, p.desc, p.cmd, p.action, p.oldKey, "--id", p.id]
    if (displace) args.push("--displace")
    p.displace = displace
    root.startProc(setProc, args)
  }

  function resetKeybinding(key, defaultKey, id) {
    if (root.busy) { root.showToast("Still applying the previous change — try again in a moment"); return }
    root.startProc(resetProc, ["reset", key || "", defaultKey || "", "--id", id || ""])
  }

  function enableKeybinding(key, id) {
    if (root.busy) { root.showToast("Still applying the previous change — try again in a moment"); return }
    root.startProc(enableProc, ["enable", key || "", "--id", id || ""])
  }

  function disableKeybinding(key, id) {
    if (root.busy) { root.showToast("Still applying the previous change — try again in a moment"); return false }
    return root.startProc(disableProc, ["disable", key || "", "--id", id || ""])
  }

  // ---- Rehome queue ----

  function queueIndexOf(id) {
    for (var i = 0; i < root.rehomeQueue.length; i++) {
      if (root.rehomeQueue[i].id === id) return i
    }
    return -1
  }

  function queueEntryFor(row) {
    if (!row) return null
    var idx = root.queueIndexOf(Model.rowId(row))
    return idx >= 0 ? root.rehomeQueue[idx] : null
  }

  function queuedRow(entry) {
    if (!entry) return null
    return Model.findRowById(root.modelData, entry.id) || Model.findRow(root.modelData, entry.description, "")
  }

  function enqueueRehome(binding, lostKey, winner) {
    if (!binding) return
    var id = Model.rowId(binding)
    var entry = {
      id: id,
      description: binding.description || "",
      lostKey: lostKey || binding.default_key || "",
      winner: winner || "",
      default_key: binding.default_key || "",
      action: binding.action || "",
      command: binding.command || ""
    }
    var q = root.rehomeQueue.slice()
    var idx = root.queueIndexOf(id)
    if (idx >= 0) q[idx] = entry
    else q.push(entry)
    root.rehomeQueue = q
  }

  function dequeueRehome(id) {
    var idx = root.queueIndexOf(id)
    if (idx < 0) return
    var q = root.rehomeQueue.slice()
    q.splice(idx, 1)
    root.rehomeQueue = q
  }

  function pruneRehomeQueue() {
    // Drop entries whose binding has a key again (or vanished).
    var q = []
    for (var i = 0; i < root.rehomeQueue.length; i++) {
      var e = root.rehomeQueue[i]
      var row = root.queuedRow(e)
      if (row && row.status === "disabled" && !row.key) q.push(e)
    }
    if (q.length !== root.rehomeQueue.length) root.rehomeQueue = q
  }

  function openRehomeEntry(entry) {
    if (!entry) return
    var row = root.queuedRow(entry)
    if (!row) {
      row = { id: entry.id, description: entry.description, default_key: entry.default_key,
              action: entry.action || "", command: entry.command || "", status: "disabled", key: null }
    }
    var lostKey = row.default_key || entry.lostKey
    editDialog.openRehome(row, lostKey, entry.winner)
  }

  function advanceRehomeQueue() {
    if (!root.rehomeAutoOpen) return
    if (editDialog.opened || root.askOpen || root.busy) return
    root.rehomeAutoOpen = false
    if (root.rehomeQueue.length === 0) return
    // Newest displacement first: a chain (A took B's key, B took C's) opens C.
    root.openRehomeEntry(root.rehomeQueue[root.rehomeQueue.length - 1])
  }

  function openRehomeFor(row) {
    if (!row) return
    var info = root.needsKeyInfo(row)
    root.enqueueRehome(row, row.default_key || "", info.winner)
    root.openRehomeEntry(root.queueEntryFor(row))
  }

  function remainingToast() {
    var n = root.rehomeQueue.length
    if (n > 0) root.showToast(n === 1 ? "1 binding still needs a key" : n + " bindings still need a key")
  }

  // { needs, winner, lostKey } for a row: disabled rows whose default key is
  // held by another active row, or rows still sitting in the rehome queue.
  function needsKeyInfo(row) {
    var res = { needs: false, winner: "", lostKey: "" }
    if (!row || row.status !== "disabled") return res
    var id = Model.rowId(row)
    if (row.default_key) {
      var holders = Model.holdersOf(root.modelData, row.default_key, id)
      if (holders.length > 0) {
        res.needs = true
        res.winner = holders[0].description || ""
        res.lostKey = row.default_key
        return res
      }
    }
    var entry = root.queueEntryFor(row)
    if (entry) {
      res.needs = true
      res.winner = entry.winner
      res.lostKey = entry.lostKey
    }
    return res
  }

  // ---- "ask" conflict card ----

  function resolveAsk(choice) {
    // choice: "rehome" | "override" | "cancel"
    root.askOpen = false
    var conflict = root.askConflict
    root.askConflict = null
    if (choice === "cancel") {
      root.pendingSave = null
      root.focusSearch()
      return
    }
    if (root.askRemember) root.setConflictMode(choice)
    root.askRemember = false
    if (!root.pendingSave) return
    root.pendingSave.mode = choice
    if (root.busy) {
      root.showToast("Still applying the previous change — try again in a moment")
      root.pendingSave = null
      return
    }
    root.runSet(true)
  }

  // Filtered active bindings (sorted alphabetically)
  readonly property var filteredActive: {
    var list = root.modelData.active || []
    var q = root.searchQuery.trim().toLowerCase()
    var cat = root.currentCategory

    var res = []
    for (var i = 0; i < list.length; i++) {
      var item = list[i]

      // Category filter
      if (cat !== "All" && item.category !== cat) continue

      // Search filter
      if (q.length > 0) {
        var matchKey = item.key && item.key.toLowerCase().indexOf(q) !== -1
        var matchDesc = item.description && item.description.toLowerCase().indexOf(q) !== -1
        var matchCmd = (item.command && item.command.toLowerCase().indexOf(q) !== -1) ||
                       (item.action && item.action.toLowerCase().indexOf(q) !== -1)
        var matchCat = item.category && item.category.toLowerCase().indexOf(q) !== -1

        if (!matchKey && !matchDesc && !matchCmd && !matchCat) continue
      }

      res.push(item)
    }
    return res
  }

  // Filtered modified & custom bindings
  readonly property var filteredModified: {
    var list = root.modelData.active || []
    var q = root.searchQuery.trim().toLowerCase()
    var cat = root.currentCategory

    var res = []
    for (var i = 0; i < list.length; i++) {
      var item = list[i]
      if (item.status !== "modified" && item.status !== "custom") continue
      if (cat !== "All" && item.category !== cat) continue

      if (q.length > 0) {
        var matchKey = item.key && item.key.toLowerCase().indexOf(q) !== -1
        var matchDesc = item.description && item.description.toLowerCase().indexOf(q) !== -1
        var matchCmd = (item.command && item.command.toLowerCase().indexOf(q) !== -1) ||
                       (item.action && item.action.toLowerCase().indexOf(q) !== -1)
        var matchCat = item.category && item.category.toLowerCase().indexOf(q) !== -1

        if (!matchKey && !matchDesc && !matchCmd && !matchCat) continue
      }

      res.push(item)
    }
    return res
  }

  // Filtered catalog presets
  readonly property var filteredCatalog: {
    var list = root.modelData.catalog || []
    var q = root.searchQuery.trim().toLowerCase()
    var cat = root.currentCategory

    var res = []
    for (var i = 0; i < list.length; i++) {
      var item = list[i]

      if (cat !== "All" && item.category !== cat) continue

      if (q.length > 0) {
        var matchName = item.name && item.name.toLowerCase().indexOf(q) !== -1
        var matchDesc = item.description && item.description.toLowerCase().indexOf(q) !== -1
        var matchKey = item.default_key && item.default_key.toLowerCase().indexOf(q) !== -1
        if (!matchName && !matchDesc && !matchKey) continue
      }

      res.push(item)
    }
    return res
  }

  // Filtered conflicts
  readonly property var filteredConflicts: {
    return root.modelData.conflicts || []
  }

  // Resolve a conflicts-tab entry ({description, action}) to its real model row.
  function conflictRow(key, sub) {
    var row = null
    if (sub && sub.id) row = Model.findRowById(root.modelData, String(sub.id))
    if (!row && sub) row = Model.findRow(root.modelData, sub.description, key)
    if (!row) {
      row = { key: key, description: (sub && sub.description) || "", action: (sub && sub.action) || "", status: "modified", source: "user-file" }
    }
    return row
  }

  // --- Backend Subprocesses (bounded: capped output, wall-clock deadline,
  //     whole-process-group termination on timeout/overflow) ---

  BoundedProcess {
    id: listProc
    command: [root.backendPath, "list"]
    maxBytes: 262144
    timeoutMs: 10000
    onFinished: {
      root.loading = false
      if (success) {
        var text = stdout || ""
        if (text && text.trim().length > 0) {
          try {
            var parsed = JSON.parse(text)
            if (parsed) root.modelData = parsed
          } catch (e) {
            console.warn("KeybindsPanel: Failed to parse backend json:", e)
          }
        }
      } else {
        root.showToast("Could not load keybindings: " + root.failureText(listProc, root.parseResult(listProc), "unknown error"))
      }
      if (root.reloadPending) {
        Qt.callLater(root.loadData)
        return
      }
      if (root.tryMigration()) return
      root.pruneRehomeQueue()
      root.advanceRehomeQueue()
    }
  }

  // Run `migrate` once per session when the backend reports stray trailing
  // lines; the list is reloaded afterwards. Deferred (not consumed) while busy.
  function tryMigration() {
    if (root.migrationTried || root.busy) return false
    if (!(Number(root.modelData.pending_migration) > 0)) return false
    root.migrationTried = true
    return root.startProc(migrateProc, ["migrate"])
  }

  BoundedProcess {
    id: migrateProc
    maxBytes: 262144
    timeoutMs: 30000
    onFinished: {
      root.loading = false
      var res = root.parseResult(migrateProc)
      if (!success || !res || res.success === false) {
        root.showToast("Migration failed: " + root.failureText(migrateProc, res, "unknown error"))
      }
      root.loadData()
    }
  }

  BoundedProcess {
    id: setProc
    maxBytes: 262144
    timeoutMs: 30000
    onFinished: {
      root.loading = false
      var res = root.parseResult(setProc)
      var p = root.pendingSave
      root.pendingSave = null

      if (!success || !res) {
        root.showToast("Failed to save keybinding: " + root.failureText(setProc, res, "unknown error"))
        root.loadData()
        return
      }

      if (res.success === false && res.conflict && p && !p.displace) {
        // "ask" mode probe hit a holder: let the user decide.
        root.pendingSave = p
        root.askConflict = res.conflict
        root.askRemember = false
        root.askOpen = true
        root.loadData()
        return
      }

      if (res.success === false) {
        root.showToast("Save failed: " + root.failureText(setProc, res, "backend refused the change"))
        root.loadData()
        return
      }

      var displaced = res.displaced || null
      var savedKey = res.key || (p ? p.key : "")
      if (displaced && p && p.mode === "rehome") {
        root.enqueueRehome(displaced, savedKey, p.desc)
        root.rehomeAutoOpen = true
        root.showToast("Saved. " + (displaced.description || "The other binding") + " needs a new key.")
      } else if (displaced) {
        root.showToast("Saved. " + (displaced.description || "The other binding") + " was unbound.")
      } else {
        root.showToast("Keybinding saved & applied to Hyprland!")
      }
      var savedEntry = p ? root.queueEntryFor({ id: p.id, description: p.desc }) : null
      if (savedEntry) {
        root.dequeueRehome(savedEntry.id)
        if (root.rehomeQueue.length > 0) root.rehomeAutoOpen = true
      }
      root.loadData()
    }
  }

  BoundedProcess {
    id: resetProc
    maxBytes: 262144
    timeoutMs: 30000
    onFinished: {
      root.loading = false
      var res = root.parseResult(resetProc)
      if (success && res && res.success !== false) root.showToast("Keybinding reset to default!")
      else root.showToast("Reset failed: " + root.failureText(resetProc, res, "unknown error"))
      root.loadData()
    }
  }

  BoundedProcess {
    id: enableProc
    maxBytes: 262144
    timeoutMs: 30000
    onFinished: {
      root.loading = false
      var res = root.parseResult(enableProc)
      if (success && res && res.success !== false) root.showToast("Keybinding re-enabled!")
      else root.showToast("Enable failed: " + root.failureText(enableProc, res, "unknown error"))
      root.loadData()
    }
  }

  BoundedProcess {
    id: disableProc
    maxBytes: 262144
    timeoutMs: 30000
    onFinished: {
      root.loading = false
      var res = root.parseResult(disableProc)
      if (success && res && res.success !== false) root.showToast("Keybinding disabled!")
      else root.showToast("Disable failed: " + root.failureText(disableProc, res, "unknown error"))
      if (root.rehomeQueue.length > 0) root.rehomeAutoOpen = true
      root.loadData()
    }
  }

  // Main Window
  // Start hidden: keepLoaded mounts this panel at every shell startup, and
  // Quickshell windows default to visible. Only open() may reveal it
  // (same contract as first-party overlays' `visible: root.opened`).
  FloatingWindow {
    id: window
    visible: false
    title: "Keybindings"
    color: root.background
    implicitWidth: Style.space(1000)
    implicitHeight: Style.space(760)
    minimumSize: Qt.size(Style.space(700), Style.space(520))

    onVisibleChanged: {
      if (!visible && !root.closingFromHost && root.shell && typeof root.shell.hide === "function") {
        root.shell.hide((root.manifest && root.manifest.id) || "davedes.mouse-keybind-settings")
      }
    }

    FocusScope {
      id: mainContainer
      anchors.fill: parent
      focus: true

      Keys.onPressed: function(event) {
        if (!root.recordingSearch) return

        var isSuper = (event.modifiers & Qt.MetaModifier) !== 0 || event.key === Qt.Key_Meta || event.key === Qt.Key_Super_L || event.key === Qt.Key_Super_R
        var isCtrl = (event.modifiers & Qt.ControlModifier) !== 0 || event.key === Qt.Key_Control
        var isAlt = (event.modifiers & Qt.AltModifier) !== 0 || event.key === Qt.Key_Alt || event.key === Qt.Key_AltGr
        var isShift = (event.modifiers & Qt.ShiftModifier) !== 0 || event.key === Qt.Key_Shift

        // Escape cancels search recording
        if (event.key === Qt.Key_Escape && !isSuper && !isCtrl && !isAlt && !isShift) {
          root.recordingSearch = false
          event.accepted = true
          return
        }

        // Ignore modifier-only keypresses alone
        if (event.key === Qt.Key_Control || event.key === Qt.Key_Shift || event.key === Qt.Key_Alt || event.key === Qt.Key_Meta || event.key === Qt.Key_Super_L || event.key === Qt.Key_Super_R) {
          event.accepted = true
          return
        }

        var keyName = Model.translateQtKey(event)
        if (keyName.length > 0) {
          var mods = []
          if (isSuper) mods.push("SUPER")
          if (isShift) mods.push("SHIFT")
          if (isCtrl) mods.push("CTRL")
          if (isAlt) mods.push("ALT")

          var chord = mods.length > 0 ? (mods.join(" + ") + " + " + keyName) : keyName
          root.searchQuery = chord
          searchInput.text = chord
          root.recordingSearch = false
          event.accepted = true
        }
      }

      // Escape: popover -> conflict card -> dialog -> panel, in that order.
      Keys.onEscapePressed: function(event) {
        if (root.recordingSearch) {
          root.recordingSearch = false
        } else if (root.settingsOpen) {
          root.settingsOpen = false
        } else if (root.askOpen) {
          root.resolveAsk("cancel")
        } else if (editDialog.opened) {
          editDialog.close()
        } else {
          root.requestClose()
        }
        event.accepted = true
      }

      ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.space(22)
        spacing: Style.space(16)

        // 1. Top Header Bar with Clean Layout
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(16)

          // App Icon & Title
          RowLayout {
            spacing: Style.space(12)

            // Clean white logo glyph without box
            Text {
              text: ""
              color: "white"
              font.family: Style.font.family
              font.pixelSize: Style.font.title + 8
            }

            ColumnLayout {
              spacing: 0

              Text {
                text: "Keybindings"
                color: root.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.title
                font.bold: true
              }

              Text {
                text: "Hyprland Shortcut Manager"
                color: Util.alpha(root.foreground, 0.55)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }
          }

          // Search Bar (Expands flexibly to fill available width)
          Item {
            Layout.fillWidth: true
            Layout.preferredHeight: Style.space(38)

            TextField {
              id: searchInput
              anchors.fill: parent
              maximumLength: 256
              placeholderText: root.recordingSearch
                ? "Listening... Press any shortcut combination (e.g. CTRL + O)"
                : "Search shortcuts, actions, commands..."
              text: root.searchQuery
              onTextChanged: root.searchQuery = text

              // Clear button
              Button {
                visible: root.searchQuery.length > 0
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                iconText: "✕"
                horizontalPadding: Style.space(6)
                verticalPadding: Style.space(2)
                onClicked: {
                  root.searchQuery = ""
                  searchInput.text = ""
                  root.recordingSearch = false
                }
              }
            }
          }

          // Action Buttons on Right
          RowLayout {
            spacing: Style.space(8)

            Button {
              text: "Add Keybinding"
              iconText: "➕"
              accent: root.accent
              selected: true
              horizontalPadding: Style.space(14)
              onClicked: editDialog.openCreate(null)
            }

            Button {
              iconText: ""
              tooltipText: "Refresh Keybindings"
              horizontalPadding: Style.space(10)
              onClicked: root.loadData()
            }

            Button {
              iconText: ""
              tooltipText: "Edit bindings.lua in Editor"
              horizontalPadding: Style.space(10)
              onClicked: Util.execDetached("omarchy-launch-config-editor $HOME/.config/hypr/bindings.lua")
            }

            Button {
              id: settingsGear
              iconText: "⚙"
              tooltipText: "Conflict handling: " + Model.conflictModeLabel(root.conflictMode)
              selected: root.settingsOpen
              accent: root.accent
              horizontalPadding: Style.space(10)
              onClicked: root.settingsOpen = !root.settingsOpen
            }
          }
        }

        // 2. Navigation Tabs (All Active vs Modified vs Catalog vs Conflicts)
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(12)

          ButtonGroup {
            id: tabGroup

            Button {
              text: "All Active (" + (root.modelData.total_active || 0) + ")"
              selected: root.currentTab === "active"
              horizontalPadding: Style.space(16)
              onClicked: root.currentTab = "active"
            }

            Button {
              text: "⭐ Modified & Custom (" + (root.modelData.total_modified || 0) + ")"
              selected: root.currentTab === "modified"
              accent: (root.modelData.total_modified > 0) ? "#FF9800" : root.foreground
              horizontalPadding: Style.space(16)
              onClicked: root.currentTab = "modified"
            }

            Button {
              text: "Available Actions Catalog (" + ((root.modelData.catalog && root.modelData.catalog.length) || 0) + ")"
              selected: root.currentTab === "catalog"
              horizontalPadding: Style.space(16)
              onClicked: root.currentTab = "catalog"
            }

            Button {
              text: "⚠️ Conflicts (" + (root.modelData.total_conflicts || 0) + ")"
              selected: root.currentTab === "conflicts"
              accent: (root.modelData.total_conflicts > 0) ? root.urgent : root.foreground
              horizontalPadding: Style.space(16)
              onClicked: root.currentTab = "conflicts"
            }
          }

          Item { Layout.fillWidth: true }
        }

        // 3. Category Filter Chips & Record to Find on the Right
        RowLayout {
          visible: root.currentTab !== "conflicts"
          Layout.fillWidth: true
          spacing: Style.space(12)

          Flow {
            Layout.fillWidth: true
            spacing: Style.space(6)

            Repeater {
              model: root.categories

              BorderSurface {
                id: chip
                required property string modelData
                height: Style.space(26)
                width: chipLabel.implicitWidth + Style.space(20)
                radius: Style.cornerRadius
                readonly property bool isSelected: root.currentCategory === modelData

                color: isSelected
                  ? Util.alpha(root.accent, 0.22)
                  : (chipMouseArea.containsMouse ? Util.alpha(root.foreground, 0.08) : Util.alpha(root.foreground, 0.04))
                borderSpec: Border.flat(
                  isSelected ? root.accent : Util.alpha(root.foreground, 0.15),
                  1
                )

                MouseArea {
                  id: chipMouseArea
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.currentCategory = chip.modelData
                }

                Text {
                  id: chipLabel
                  anchors.centerIn: parent
                  text: chip.modelData
                  color: chip.isSelected ? root.accent : root.foreground
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  font.bold: chip.isSelected
                }
              }
            }
          }

          // Record to Find Shortcut Button pinned to the right hand side
          Button {
            id: recordSearchBtn
            text: root.recordingSearch ? "Listening..." : "Record to Find"
            iconText: root.recordingSearch ? "⏺" : ""
            accent: root.recordingSearch ? root.accent : root.foreground
            selected: root.recordingSearch
            horizontalPadding: Style.space(14)
            verticalPadding: Style.space(4)
            Layout.alignment: Qt.AlignVCenter | Qt.AlignRight
            tooltipText: "Press a key combination on your keyboard to instantly find its assigned action"
            onClicked: {
              if (root.recordingSearch) {
                root.recordingSearch = false
              } else {
                root.recordingSearch = true
                mainContainer.forceActiveFocus()
              }
            }
          }
        }

        PanelSeparator { Layout.fillWidth: true }

        // 4. Main Scrollable Content Area
        ScrollView {
          id: scrollArea
          Layout.fillWidth: true
          Layout.fillHeight: true
          clip: true
          ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

          ColumnLayout {
            width: scrollArea.availableWidth
            spacing: Style.space(8)

            // =========================================================
            // --- TAB 1: ALL ACTIVE KEYBINDINGS (ALPHABETICAL) ---
            // =========================================================
            ColumnLayout {
              visible: root.currentTab === "active"
              Layout.fillWidth: true
              spacing: Style.space(8)

              Repeater {
                model: root.filteredActive

                BorderSurface {
                  id: activeRow
                  required property var modelData
                  readonly property var needsKey: root.needsKeyInfo(activeRow.modelData)
                  readonly property bool isDisabled: Boolean(activeRow.modelData && activeRow.modelData.status === "disabled")
                  readonly property string rowId: Model.rowId(activeRow.modelData)
                  Layout.fillWidth: true
                  Layout.preferredHeight: Style.space(64)
                  radius: Style.cornerRadius
                  color: activeRowMouse.containsMouse
                    ? Util.alpha(root.foreground, 0.06)
                    : Util.alpha(root.foreground, 0.02)
                  borderSpec: Border.flat(
                    (modelData && modelData.is_conflict)
                      ? root.urgent
                      : (activeRow.needsKey.needs
                        ? Util.alpha(root.urgent, 0.6)
                        : (activeRowMouse.containsMouse ? Util.alpha(root.foreground, 0.25) : Util.alpha(root.foreground, 0.1))),
                    (modelData && modelData.is_conflict) ? 1.5 : 1
                  )

                  MouseArea {
                    id: activeRowMouse
                    anchors.fill: parent
                    hoverEnabled: true
                  }

                  RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: Style.space(16)
                    anchors.rightMargin: Style.space(16)
                    spacing: Style.space(14)

                    // Status Badge Pill
                    BorderSurface {
                      Layout.preferredHeight: Style.space(22)
                      Layout.preferredWidth: statusText.implicitWidth + Style.space(14)
                      Layout.alignment: Qt.AlignVCenter
                      radius: Style.cornerRadius

                      readonly property string st: (activeRow.modelData && activeRow.modelData.status) || "default"
                      readonly property bool isConf: Boolean(activeRow.modelData && activeRow.modelData.is_conflict) || activeRow.needsKey.needs

                      color: isConf
                        ? Util.alpha(root.urgent, 0.2)
                        : (st === "custom" ? Util.alpha("#4CAF50", 0.2)
                          : (st === "modified" ? Util.alpha("#FF9800", 0.2)
                            : (st === "disabled" ? Util.alpha(root.foreground, 0.1) : Util.alpha(root.foreground, 0.05))))

                      borderSpec: Border.flat(
                        isConf
                          ? root.urgent
                          : (st === "custom" ? "#4CAF50"
                            : (st === "modified" ? "#FF9800"
                              : (st === "disabled" ? Util.alpha(root.foreground, 0.3) : Util.alpha(root.foreground, 0.15)))),
                        1
                      )

                      Text {
                        id: statusText
                        anchors.centerIn: parent
                        text: (activeRow.modelData && activeRow.modelData.is_conflict)
                          ? "⚠️ CONFLICT"
                          : (activeRow.needsKey.needs
                            ? "NEEDS KEY"
                            : (activeRow.modelData && activeRow.modelData.status ? activeRow.modelData.status.toUpperCase() : "DEFAULT"))
                        color: ((activeRow.modelData && activeRow.modelData.is_conflict) || activeRow.needsKey.needs)
                          ? root.urgent
                          : (activeRow.modelData && activeRow.modelData.status === "custom" ? "#4CAF50"
                            : (activeRow.modelData && activeRow.modelData.status === "modified" ? "#FF9800"
                              : (activeRow.modelData && activeRow.modelData.status === "disabled" ? Util.alpha(root.foreground, 0.4) : root.foreground)))
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption - 2
                        font.bold: true
                      }
                    }

                    // Action Info (Description, Command, Category)
                    ColumnLayout {
                      Layout.fillWidth: true
                      Layout.alignment: Qt.AlignVCenter
                      spacing: Style.space(2)

                      RowLayout {
                        spacing: Style.space(8)

                        Text {
                          text: (activeRow.modelData && activeRow.modelData.description) || "Action"
                          textFormat: Text.PlainText
                          color: (activeRow.modelData && activeRow.modelData.status === "disabled")
                            ? Util.alpha(root.foreground, 0.4)
                            : root.foreground
                          font.family: Style.font.family
                          font.pixelSize: Style.font.body
                          font.bold: true
                          font.strikeout: (activeRow.modelData && activeRow.modelData.status === "disabled")
                        }

                        // Category chip
                        BorderSurface {
                          Layout.preferredHeight: Style.space(16)
                          Layout.preferredWidth: catLabel.implicitWidth + Style.space(8)
                          radius: 3
                          color: Util.alpha(root.foreground, 0.05)
                          borderSpec: Border.flat(Util.alpha(root.foreground, 0.1), 1)

                          Text {
                            id: catLabel
                            anchors.centerIn: parent
                            text: (activeRow.modelData && activeRow.modelData.category) || "General"
                            textFormat: Text.PlainText
                            color: Util.alpha(root.foreground, 0.5)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption - 3
                          }
                        }
                      }

                      Text {
                        Layout.fillWidth: true
                        text: activeRow.needsKey.needs
                          ? ((activeRow.needsKey.lostKey ? activeRow.needsKey.lostKey + " " : "") + "taken by " + (activeRow.needsKey.winner || "another binding"))
                          : ((activeRow.modelData && (activeRow.modelData.command || activeRow.modelData.action)) || "")
                        textFormat: Text.PlainText
                        color: activeRow.needsKey.needs ? root.urgent : Util.alpha(root.foreground, 0.5)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                      }
                    }

                    // Key Badge (disabled rows show their default key, dimmed)
                    KeyBadge {
                      Layout.alignment: Qt.AlignVCenter
                      keyText: (activeRow.modelData && (activeRow.modelData.key || (activeRow.isDisabled ? activeRow.modelData.default_key : ""))) || ""
                      opacity: activeRow.isDisabled ? 0.4 : 1.0
                      highlighted: Boolean(activeRow.modelData && activeRow.modelData.is_conflict)
                      accent: (activeRow.modelData && activeRow.modelData.is_conflict) ? root.urgent : root.accent
                    }

                    // Action Buttons
                    RowLayout {
                      Layout.alignment: Qt.AlignVCenter
                      spacing: Style.space(6)

                      // Pick key (needs-key rows) / Edit
                      Button {
                        visible: activeRow.needsKey.needs
                        text: "Pick key…"
                        accent: root.urgent
                        selected: true
                        horizontalPadding: Style.space(10)
                        verticalPadding: Style.space(4)
                        onClicked: root.openRehomeFor(activeRow.modelData)
                      }

                      Button {
                        visible: !activeRow.needsKey.needs
                        iconText: "✏️"
                        tooltipText: "Modify Keybinding"
                        horizontalPadding: Style.space(8)
                        verticalPadding: Style.space(4)
                        onClicked: editDialog.openEdit(activeRow.modelData)
                      }

                      // Enable (if disabled)
                      Button {
                        visible: activeRow.isDisabled && !activeRow.needsKey.needs
                        text: "Enable"
                        iconText: "✓"
                        accent: "#4CAF50"
                        tooltipText: "Re-enable Keybinding"
                        horizontalPadding: Style.space(8)
                        verticalPadding: Style.space(4)
                        onClicked: root.enableKeybinding(activeRow.modelData.key || activeRow.modelData.default_key || "", activeRow.rowId)
                      }

                      // Reset (if modified)
                      Button {
                        visible: Boolean(activeRow.modelData && activeRow.modelData.status === "modified")
                        iconText: "↺"
                        tooltipText: "Reset to Default (" + ((activeRow.modelData && activeRow.modelData.default_key) || "") + ")"
                        horizontalPadding: Style.space(8)
                        verticalPadding: Style.space(4)
                        onClicked: root.resetKeybinding(activeRow.modelData.key, activeRow.modelData.default_key, activeRow.rowId)
                      }

                      // Disable / Delete
                      Button {
                        visible: !activeRow.isDisabled
                        iconText: (activeRow.modelData && activeRow.modelData.status === "custom") ? "🗑️" : "⊘"
                        tooltipText: (activeRow.modelData && activeRow.modelData.status === "custom") ? "Delete custom binding" : "Disable default binding"
                        horizontalPadding: Style.space(8)
                        verticalPadding: Style.space(4)
                        onClicked: {
                          if (activeRow.modelData.status === "custom") {
                            root.resetKeybinding(activeRow.modelData.key, "", activeRow.rowId)
                          } else {
                            root.disableKeybinding(activeRow.modelData.key, activeRow.rowId)
                          }
                        }
                      }
                    }
                  }
                }
              }

              // Empty state for active list
              BorderSurface {
                visible: root.filteredActive.length === 0
                Layout.fillWidth: true
                Layout.preferredHeight: Style.space(170)
                radius: Style.cornerRadius
                color: Util.alpha(root.foreground, 0.02)
                borderSpec: Border.flat(Util.alpha(root.foreground, 0.1), 1)

                ColumnLayout {
                  anchors.centerIn: parent
                  spacing: Style.space(10)

                  Text {
                    text: root.searchQuery.length > 0
                      ? ("No keybinding found for \"" + root.searchQuery + "\"")
                      : "No keybindings match your filter."
                    textFormat: Text.PlainText
                    color: root.foreground
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    font.bold: true
                    Layout.alignment: Qt.AlignHCenter
                  }

                  Text {
                    visible: root.searchQuery.length > 0
                    text: "This shortcut combination is currently free and unassigned."
                    color: Util.alpha(root.foreground, 0.6)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    Layout.alignment: Qt.AlignHCenter
                  }

                  Button {
                    visible: root.searchQuery.length > 0
                    text: "Create Keybinding with " + root.searchQuery
                    iconText: "➕"
                    accent: root.accent
                    selected: true
                    Layout.alignment: Qt.AlignHCenter
                    horizontalPadding: Style.space(18)
                    verticalPadding: Style.space(6)
                    onClicked: {
                      editDialog.openCreate({ default_key: root.searchQuery })
                    }
                  }
                }
              }
            }

            // =========================================================
            // --- TAB 2: MODIFIED & CUSTOM KEYBINDINGS ---
            // =========================================================
            ColumnLayout {
              visible: root.currentTab === "modified"
              Layout.fillWidth: true
              spacing: Style.space(8)

              Repeater {
                model: root.filteredModified

                BorderSurface {
                  id: modRow
                  required property var modelData
                  Layout.fillWidth: true
                  Layout.preferredHeight: Style.space(64)
                  radius: Style.cornerRadius
                  color: modRowMouse.containsMouse
                    ? Util.alpha(root.foreground, 0.06)
                    : Util.alpha(root.foreground, 0.02)
                  borderSpec: Border.flat(
                    modRowMouse.containsMouse ? Util.alpha(root.foreground, 0.25) : Util.alpha(root.foreground, 0.1),
                    1
                  )

                  MouseArea {
                    id: modRowMouse
                    anchors.fill: parent
                    hoverEnabled: true
                  }

                  RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: Style.space(16)
                    anchors.rightMargin: Style.space(16)
                    spacing: Style.space(14)

                    // Status Badge Pill
                    BorderSurface {
                      Layout.preferredHeight: Style.space(22)
                      Layout.preferredWidth: modStatusText.implicitWidth + Style.space(14)
                      Layout.alignment: Qt.AlignVCenter
                      radius: Style.cornerRadius

                      readonly property string st: (modRow.modelData && modRow.modelData.status) || "modified"
                      color: (st === "custom") ? Util.alpha("#4CAF50", 0.2) : Util.alpha("#FF9800", 0.2)
                      borderSpec: Border.flat((st === "custom") ? "#4CAF50" : "#FF9800", 1)

                      Text {
                        id: modStatusText
                        anchors.centerIn: parent
                        text: (modRow.modelData && modRow.modelData.status ? modRow.modelData.status.toUpperCase() : "MODIFIED")
                        color: (modRow.modelData && modRow.modelData.status === "custom" ? "#4CAF50" : "#FF9800")
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption - 2
                        font.bold: true
                      }
                    }

                    // Action Info
                    ColumnLayout {
                      Layout.fillWidth: true
                      Layout.alignment: Qt.AlignVCenter
                      spacing: Style.space(2)

                      RowLayout {
                        spacing: Style.space(8)

                        Text {
                          text: (modRow.modelData && modRow.modelData.description) || "Action"
                          textFormat: Text.PlainText
                          color: root.foreground
                          font.family: Style.font.family
                          font.pixelSize: Style.font.body
                          font.bold: true
                        }

                        BorderSurface {
                          Layout.preferredHeight: Style.space(16)
                          Layout.preferredWidth: modCatLabel.implicitWidth + Style.space(8)
                          radius: 3
                          color: Util.alpha(root.foreground, 0.05)
                          borderSpec: Border.flat(Util.alpha(root.foreground, 0.1), 1)

                          Text {
                            id: modCatLabel
                            anchors.centerIn: parent
                            text: (modRow.modelData && modRow.modelData.category) || "Custom"
                            textFormat: Text.PlainText
                            color: Util.alpha(root.foreground, 0.5)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption - 3
                          }
                        }
                      }

                      Text {
                        Layout.fillWidth: true
                        text: (modRow.modelData && (modRow.modelData.command || modRow.modelData.action)) || ""
                        textFormat: Text.PlainText
                        color: Util.alpha(root.foreground, 0.5)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                      }
                    }

                    // Key Badge
                    KeyBadge {
                      Layout.alignment: Qt.AlignVCenter
                      keyText: (modRow.modelData && modRow.modelData.key) || ""
                    }

                    // Action Buttons
                    RowLayout {
                      Layout.alignment: Qt.AlignVCenter
                      spacing: Style.space(6)

                      Button {
                        iconText: "✏️"
                        tooltipText: "Modify Keybinding"
                        horizontalPadding: Style.space(8)
                        verticalPadding: Style.space(4)
                        onClicked: editDialog.openEdit(modRow.modelData)
                      }

                      Button {
                        visible: Boolean(modRow.modelData && modRow.modelData.status === "modified")
                        iconText: "↺"
                        tooltipText: "Reset to Default (" + ((modRow.modelData && modRow.modelData.default_key) || "") + ")"
                        horizontalPadding: Style.space(8)
                        verticalPadding: Style.space(4)
                        onClicked: root.resetKeybinding(modRow.modelData.key, modRow.modelData.default_key, Model.rowId(modRow.modelData))
                      }

                      Button {
                        visible: Boolean(modRow.modelData && modRow.modelData.status === "custom")
                        iconText: "🗑️"
                        tooltipText: "Delete custom binding"
                        horizontalPadding: Style.space(8)
                        verticalPadding: Style.space(4)
                        onClicked: root.resetKeybinding(modRow.modelData.key, "", Model.rowId(modRow.modelData))
                      }
                    }
                  }
                }
              }

              // Empty state for modified tab
              Text {
                visible: root.filteredModified.length === 0
                text: "No modified or custom keybindings found. All shortcuts are currently set to Omarchy defaults."
                color: Util.alpha(root.foreground, 0.5)
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                Layout.alignment: Qt.AlignHCenter
                Layout.topMargin: Style.space(40)
              }
            }

            // =========================================================
            // --- TAB 3: AVAILABLE CATALOG ---
            // =========================================================
            ColumnLayout {
              visible: root.currentTab === "catalog"
              Layout.fillWidth: true
              spacing: Style.space(8)

              Repeater {
                model: root.filteredCatalog

                BorderSurface {
                  id: catalogRow
                  required property var modelData
                  Layout.fillWidth: true
                  Layout.preferredHeight: Style.space(60)
                  radius: Style.cornerRadius
                  color: catalogRowMouse.containsMouse
                    ? Util.alpha(root.foreground, 0.06)
                    : Util.alpha(root.foreground, 0.02)
                  borderSpec: Border.flat(
                    catalogRowMouse.containsMouse ? Util.alpha(root.foreground, 0.25) : Util.alpha(root.foreground, 0.1),
                    1
                  )

                  MouseArea {
                    id: catalogRowMouse
                    anchors.fill: parent
                    hoverEnabled: true
                  }

                  RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: Style.space(16)
                    anchors.rightMargin: Style.space(16)
                    spacing: Style.space(14)

                    // Category tag
                    BorderSurface {
                      Layout.preferredHeight: Style.space(22)
                      Layout.preferredWidth: catPill.implicitWidth + Style.space(14)
                      Layout.alignment: Qt.AlignVCenter
                      radius: Style.cornerRadius
                      color: Util.alpha(root.accent, 0.1)
                      borderSpec: Border.flat(Util.alpha(root.accent, 0.3), 1)

                      Text {
                        id: catPill
                        anchors.centerIn: parent
                        text: catalogRow.modelData.category
                        textFormat: Text.PlainText
                        color: root.accent
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption - 2
                        font.bold: true
                      }
                    }

                    // Action Info
                    ColumnLayout {
                      Layout.fillWidth: true
                      Layout.alignment: Qt.AlignVCenter
                      spacing: Style.space(2)

                      Text {
                        text: catalogRow.modelData.name
                        textFormat: Text.PlainText
                        color: root.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                        font.bold: true
                      }

                      Text {
                        Layout.fillWidth: true
                        text: catalogRow.modelData.description
                        textFormat: Text.PlainText
                        color: Util.alpha(root.foreground, 0.5)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                      }
                    }

                    // Current Key or Suggested Key
                    RowLayout {
                      Layout.alignment: Qt.AlignVCenter
                      spacing: Style.space(8)

                      Text {
                        visible: !catalogRow.modelData.is_bound && catalogRow.modelData.default_key
                        text: "Suggested:"
                        color: Util.alpha(root.foreground, 0.4)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                      }

                      KeyBadge {
                        keyText: catalogRow.modelData.current_key || catalogRow.modelData.default_key || ""
                        highlighted: catalogRow.modelData.is_bound
                      }
                    }

                    // "+ Bind" Button
                    Button {
                      Layout.alignment: Qt.AlignVCenter
                      text: catalogRow.modelData.is_bound ? "Rebind" : "+ Bind"
                      accent: root.accent
                      selected: !catalogRow.modelData.is_bound
                      horizontalPadding: Style.space(12)
                      verticalPadding: Style.space(4)
                      onClicked: editDialog.openCreate(catalogRow.modelData)
                    }
                  }
                }
              }

              // Empty state for catalog
              Text {
                visible: root.filteredCatalog.length === 0
                text: "No catalog actions match your search."
                color: Util.alpha(root.foreground, 0.4)
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                Layout.alignment: Qt.AlignHCenter
                Layout.topMargin: Style.space(40)
              }
            }

            // =========================================================
            // --- TAB 4: CONFLICTS VIEW (POLISHED CARDS) ---
            // =========================================================
            ColumnLayout {
              visible: root.currentTab === "conflicts"
              Layout.fillWidth: true
              spacing: Style.space(12)

              Repeater {
                model: root.filteredConflicts

                BorderSurface {
                  id: conflictCard
                  required property var modelData
                  Layout.fillWidth: true
                  radius: Style.cornerRadius
                  color: Util.alpha(root.urgent, 0.06)
                  borderSpec: Border.flat(root.urgent, 1.5)
                  padding: Style.space(18)

                  ColumnLayout {
                    width: parent.width
                    spacing: Style.space(14)

                    // Header of Conflict Card
                    RowLayout {
                      Layout.fillWidth: true
                      spacing: Style.space(12)

                      KeyBadge {
                        keyText: conflictCard.modelData.key
                        highlighted: true
                        accent: root.urgent
                        fontSize: Style.font.body
                      }

                      BorderSurface {
                        Layout.preferredHeight: Style.space(22)
                        Layout.preferredWidth: confBadgeTxt.implicitWidth + Style.space(14)
                        radius: Style.cornerRadius
                        color: Util.alpha(root.urgent, 0.25)
                        borderSpec: Border.flat(root.urgent, 1)

                        Text {
                          id: confBadgeTxt
                          anchors.centerIn: parent
                          text: (conflictCard.modelData.bindings ? conflictCard.modelData.bindings.length : 2) + " ACTIONS COLLIDING"
                          color: root.urgent
                          font.family: Style.font.family
                          font.pixelSize: Style.font.caption - 2
                          font.bold: true
                        }
                      }

                      Item { Layout.fillWidth: true }
                    }

                    Text {
                      text: "The following actions are bound to this same shortcut. Click Rebind on one of them to resolve:"
                      color: Util.alpha(root.foreground, 0.8)
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }

                    PanelSeparator { Layout.fillWidth: true }

                    // Conflicting items list inside card
                    ColumnLayout {
                      Layout.fillWidth: true
                      spacing: Style.space(8)

                      Repeater {
                        model: conflictCard.modelData.bindings

                        BorderSurface {
                          id: confSubRow
                          required property var modelData
                          Layout.fillWidth: true
                          Layout.preferredHeight: Style.space(48)
                          radius: Style.cornerRadius
                          color: Util.alpha(root.foreground, 0.04)
                          borderSpec: Border.flat(Util.alpha(root.foreground, 0.12), 1)

                          RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: Style.space(14)
                            anchors.rightMargin: Style.space(14)
                            spacing: Style.space(12)

                            Text {
                              text: confSubRow.modelData.description || "Action"
                              textFormat: Text.PlainText
                              color: root.foreground
                              font.family: Style.font.family
                              font.pixelSize: Style.font.body
                              font.bold: true
                              Layout.preferredWidth: Style.space(260)
                            }

                            Text {
                              text: confSubRow.modelData.action || ""
                              textFormat: Text.PlainText
                              color: Util.alpha(root.foreground, 0.5)
                              font.family: Style.font.family
                              font.pixelSize: Style.font.caption
                              elide: Text.ElideRight
                              Layout.fillWidth: true
                            }

                            Button {
                              text: "Rebind"
                              iconText: "✏️"
                              accent: root.accent
                              selected: true
                              horizontalPadding: Style.space(12)
                              verticalPadding: Style.space(4)
                              onClicked: editDialog.openEdit(root.conflictRow(conflictCard.modelData.key, confSubRow.modelData))
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }

              // Clean empty state when no conflicts
              BorderSurface {
                visible: root.filteredConflicts.length === 0
                Layout.fillWidth: true
                Layout.preferredHeight: Style.space(100)
                radius: Style.cornerRadius
                color: Util.alpha("#4CAF50", 0.08)
                borderSpec: Border.flat("#4CAF50", 1)

                RowLayout {
                  anchors.centerIn: parent
                  spacing: Style.space(12)

                  Text {
                    text: "✓"
                    color: "#4CAF50"
                    font.family: Style.font.family
                    font.pixelSize: Style.font.title
                  }

                  ColumnLayout {
                    spacing: Style.space(2)

                    Text {
                      text: "No shortcut conflicts found"
                      color: "#4CAF50"
                      font.family: Style.font.family
                      font.pixelSize: Style.font.body
                      font.bold: true
                    }

                    Text {
                      text: "All keybindings are unique and non-overlapping."
                      color: Util.alpha(root.foreground, 0.6)
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                  }
                }
              }
            }
          }
        }
      }

      // Add / Edit / Rehome Modal Dialog (child of mainContainer, fills the window)
      EditKeybindDialog {
        id: editDialog
        anchors.fill: parent
        z: 5
        allBindings: (root.modelData && root.modelData.active) || []
        catalog: (root.modelData && root.modelData.catalog) || []
        conflictMode: root.conflictMode
        saving: root.busy
        onSaved: function(key, desc, cmd, action, oldKey, id) {
          root.saveKeybinding(key, desc, cmd, action, oldKey, id)
        }
        onDisableRequested: function(id, key) {
          if (root.disableKeybinding(key, id)) root.dequeueRehome(id)
          else root.remainingToast()
        }
        onUnboundAccepted: function(id) {
          root.dequeueRehome(id)
          if (root.rehomeQueue.length > 0) {
            root.rehomeAutoOpen = true
            root.advanceRehomeQueue()
          }
        }
        onCanceled: {
          // A rehome dialog closed without a decision: the binding stays queued.
          if (editDialog.isRehome) root.remainingToast()
        }
        onClosed: root.focusSearch()
      }

      // "Ask" conflict card: shown when conflictMode is "ask" and the probe hit a holder.
      Item {
        id: askCard
        anchors.fill: parent
        visible: root.askOpen
        z: 6

        Rectangle {
          anchors.fill: parent
          color: Util.alpha(root.background, 0.7)

          MouseArea { anchors.fill: parent; onClicked: root.resolveAsk("cancel") }

          BorderSurface {
            id: askSurface
            width: Math.min(parent.width - Style.space(64), Style.space(520))
            height: askSurface.contentTopInset + askSurface.contentBottomInset + askLayout.implicitHeight
            anchors.centerIn: parent
            color: root.background
            borderSpec: Border.flat(root.urgent, Style.normalBorderWidth)
            radius: Style.cornerRadius
            padding: Style.space(22)

            MouseArea { anchors.fill: parent; onClicked: {} }

            ColumnLayout {
              id: askLayout
              anchors.top: parent.top
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.topMargin: askSurface.contentTopInset
              anchors.leftMargin: askSurface.contentLeftInset
              anchors.rightMargin: askSurface.contentRightInset
              spacing: Style.space(14)

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                text: {
                  var k = (root.pendingSave && root.pendingSave.key) || ""
                  var d = (root.askConflict && root.askConflict.description) || "another binding"
                  return k + " is already " + d + ". What should happen to it?"
                }
                color: root.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.title
              }

              Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "Rehome keeps the other binding and asks you for its new key. Override unbinds it."
                color: Util.alpha(root.foreground, 0.65)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }

              Toggle {
                Layout.fillWidth: true
                label: "Remember my choice"
                description: "Stops asking; change it later from the gear menu."
                checked: root.askRemember
                foreground: root.foreground
                accent: root.accent
                onClicked: root.askRemember = !root.askRemember
              }

              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(10)

                Item { Layout.fillWidth: true }

                Button {
                  text: "Cancel"
                  horizontalPadding: Style.space(16)
                  verticalPadding: Style.space(6)
                  onClicked: root.resolveAsk("cancel")
                }

                Button {
                  text: "Just override"
                  accent: root.urgent
                  horizontalPadding: Style.space(16)
                  verticalPadding: Style.space(6)
                  onClicked: root.resolveAsk("override")
                }

                Button {
                  text: "Rehome the other binding"
                  accent: root.accent
                  selected: true
                  horizontalPadding: Style.space(16)
                  verticalPadding: Style.space(6)
                  onClicked: root.resolveAsk("rehome")
                }
              }
            }
          }
        }
      }

      // Settings popover (conflict handling mode)
      Item {
        id: settingsPopover
        anchors.fill: parent
        visible: root.settingsOpen
        z: 7

        MouseArea { anchors.fill: parent; onClicked: root.settingsOpen = false }

        BorderSurface {
          id: popoverCard
          anchors.top: parent.top
          anchors.right: parent.right
          anchors.topMargin: Style.space(22) + settingsGear.height + Style.space(8)
          anchors.rightMargin: Style.space(22)
          width: Style.space(360)
          height: popoverCard.contentTopInset + popoverCard.contentBottomInset + popoverLayout.implicitHeight
          color: root.background
          borderSpec: Border.flat(Util.alpha(root.foreground, 0.3), 1)
          radius: Style.cornerRadius
          padding: Style.space(16)

          MouseArea { anchors.fill: parent; onClicked: {} }

          ColumnLayout {
            id: popoverLayout
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.topMargin: popoverCard.contentTopInset
            anchors.leftMargin: popoverCard.contentLeftInset
            anchors.rightMargin: popoverCard.contentRightInset
            spacing: Style.space(8)

            Text {
              text: "When a saved key is already taken"
              color: root.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              font.bold: true
            }

            Repeater {
              model: [
                { value: "rehome", label: "Rehome the other binding", hint: "Take the key, then ask me for the other binding's new key. (default)" },
                { value: "override", label: "Just override", hint: "Take the key and leave the other binding unbound." },
                { value: "ask", label: "Ask every time", hint: "Show a choice before anything is written." }
              ]

              BorderSurface {
                id: modeOption
                required property var modelData
                readonly property bool isSelected: root.conflictMode === modelData.value
                Layout.fillWidth: true
                implicitHeight: modeOptionLayout.implicitHeight + Style.space(16)
                radius: Style.cornerRadius
                color: isSelected
                  ? Util.alpha(root.accent, 0.18)
                  : (modeOptionMouse.containsMouse ? Util.alpha(root.foreground, 0.08) : Util.alpha(root.foreground, 0.03))
                borderSpec: Border.flat(isSelected ? root.accent : Util.alpha(root.foreground, 0.15), 1)

                MouseArea {
                  id: modeOptionMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: {
                    root.setConflictMode(modeOption.modelData.value)
                    root.settingsOpen = false
                  }
                }

                ColumnLayout {
                  id: modeOptionLayout
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.leftMargin: Style.space(12)
                  anchors.rightMargin: Style.space(12)
                  spacing: Style.space(2)

                  Text {
                    text: (modeOption.isSelected ? "● " : "○ ") + modeOption.modelData.label
                    color: modeOption.isSelected ? root.accent : root.foreground
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    font.bold: modeOption.isSelected
                  }

                  Text {
                    Layout.fillWidth: true
                    text: modeOption.modelData.hint
                    wrapMode: Text.WordWrap
                    color: Util.alpha(root.foreground, 0.6)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                  }
                }
              }
            }
          }
        }
      }

      // Toast
      BorderSurface {
        id: toast
        visible: root.toastVisible
        z: 8
        anchors.bottom: parent.bottom
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottomMargin: Style.space(22)
        width: Math.min(parent.width - Style.space(64), toastText.implicitWidth + Style.space(32))
        height: toastText.implicitHeight + Style.space(20)
        radius: Style.cornerRadius
        color: root.background
        borderSpec: Border.flat(root.accent, 1)

        Text {
          id: toastText
          anchors.centerIn: parent
          width: Math.min(implicitWidth, toast.width - Style.space(24))
          text: root.toastMessage
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          horizontalAlignment: Text.AlignHCenter
          color: root.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}