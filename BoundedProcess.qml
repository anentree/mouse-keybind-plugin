import QtQuick
import Quickshell.Io

// BoundedProcess: runs `command` (an argv list) with the same lifecycle
// guarantees as the backend run_cmd() helper:
//   - stdout/stderr are collected incrementally (SplitParser) and hard-capped
//     at maxBytes each; exceeding either cap kills the process group and is
//     reported as overflow.
//   - a wall-clock deadline (timeoutMs) fires SIGTERM then SIGKILL on the whole
//     process group and is reported as a timeout; it never waits forever.
//   - the command is launched through `setsid` so it becomes its own session /
//     process-group leader; termination signals (including kill(1) on the
//     negative pid) then reach every descendant, not just the direct child.
// Usage mirrors Quickshell.Io.Process: set `command`, then `running = true`.
// Rooted in an invisible Item (not QtObject) so the Process/Timer children can
// attach to the default `data` property.
Item {
  id: root
  visible: false
  width: 0
  height: 0

  property var command: []
  property int maxBytes: 262144
  property int timeoutMs: 15000
  property bool running: false

  readonly property string stdout: stdoutData
  readonly property string stderr: stderrData
  readonly property int exitCode: exitCodeData
  readonly property int exitStatus: exitStatusData
  readonly property bool timedOut: timedOutData
  readonly property bool overflowed: overflowedData
  readonly property bool startFailed: startFailedData

  readonly property bool success: finishedData && !startFailedData && !timedOutData
      && !overflowedData && exitStatusData === 0 && exitCodeData === 0

  signal finished()

  property string stdoutData: ""
  property string stderrData: ""
  property bool timedOutData: false
  property bool overflowedData: false
  property bool startFailedData: false
  property int exitCodeData: -1
  property int exitStatusData: -1
  property bool finishedData: false

  onRunningChanged: {
    if (root.running) {
      // Defer until the component tree (proc, timers) is fully constructed so
      // `running: true` in a declaration is safe.
      Qt.callLater(root.begin)
    } else if (!root.finishedData) {
      root.abort()
    }
  }

  Process {
    id: proc
    stdout: SplitParser {
      onRead: function(data) { root.appendStdout(data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendStderr(data) }
    }
    onExited: function(code, status) { root.onExited(code, status) }
  }

  Timer {
    id: deadline
    interval: Math.max(250, root.timeoutMs)
    repeat: false
    onTriggered: {
      if (root.running && !root.finishedData) {
        root.timedOutData = true
        root.terminateGroup(false)
      }
    }
  }

  Timer {
    id: hardKill
    interval: 300
    repeat: false
    onTriggered: {
      if (root.running && !root.finishedData) root.terminateGroup(true)
    }
  }

  Timer {
    id: killProcWatchdog
    interval: 3000
    repeat: false
    onTriggered: {
      if (killProc.running) killProc.signal(9)
    }
  }

  Process {
    id: killProc
    stdout: SplitParser {}
    stderr: SplitParser {}
    onExited: killProcWatchdog.stop()
  }

  function begin() {
    if (!root.running) return
    var cmd = root.command
    if (!cmd || cmd.length === 0) {
      root.reset()
      root.startFailedData = true
      root.finish()
      return
    }
    root.reset()
    // Run under its own session so the launched command is its own
    // process-group leader and a single group signal reaches every descendant.
    proc.command = ["setsid"].concat(cmd)
    deadline.interval = Math.max(250, root.timeoutMs)
    deadline.start()
    proc.running = true
  }

  function reset() {
    stdoutData = ""
    stderrData = ""
    timedOutData = false
    overflowedData = false
    startFailedData = false
    exitCodeData = -1
    exitStatusData = -1
    finishedData = false
    killProcWatchdog.stop()
    if (killProc.running) killProc.running = false
  }

  function appendStdout(data) {
    if (!root.running || root.finishedData || root.overflowedData) return
    if (stdoutData.length < root.maxBytes) stdoutData += data
    if (stdoutData.length >= root.maxBytes) {
      root.overflowedData = true
      deadline.stop()
      root.terminateGroup(false)
    }
  }

  function appendStderr(data) {
    if (!root.running || root.finishedData || root.overflowedData) return
    if (stderrData.length < root.maxBytes) stderrData += data
    if (stderrData.length >= root.maxBytes) {
      root.overflowedData = true
      deadline.stop()
      root.terminateGroup(false)
    }
  }

  function terminateGroup(force) {
    if (!proc.running) {
      root.finish()
      return
    }
    if (force) {
      // Hard kill: SIGKILL the leader directly and the whole group via
      // kill(1) on its negative pid (leader PGID == its PID under setsid).
      proc.signal(9)
      var pid = Number(proc.processId)
      if (pid > 0) {
        killProc.command = ["kill", "-9", "-" + String(pid)]
        killProcWatchdog.start()
        killProc.running = true
      }
      root.finish()
    } else {
      // Gentle phase: SIGTERM the leader, then escalate to the hard kill.
      proc.signal(15)
      hardKill.start()
    }
  }

  function onExited(code, status) {
    if (!root.running || root.finishedData) return
    deadline.stop()
    hardKill.stop()
    killProcWatchdog.stop()
    if (killProc.running) killProc.running = false
    root.exitCodeData = code
    root.exitStatusData = status
    root.finish()
  }

  function abort() {
    deadline.stop()
    hardKill.stop()
    if (proc.running) proc.signal(9)
    killProcWatchdog.stop()
    if (killProc.running) killProc.running = false
    root.finish()
  }

  function finish() {
    if (root.finishedData) return
    root.finishedData = true
    root.running = false
    root.finished()
  }
}