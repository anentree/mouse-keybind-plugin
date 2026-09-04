#!/usr/bin/env python3
"""
Keybindings Manager Backend for Omarchy Hyprland
Provides parsing, catalog discovery, conflict detection, mouse scroll integration, and safe Lua config manipulation.
"""

import os
import sys
import re
import json
import shutil
import signal
import select
import stat
import errno
import subprocess
import time
from datetime import datetime
from pathlib import Path

DEFAULT_BINDINGS_DIR = os.environ.get("OMARCHY_PATH", "/usr/share/omarchy") + "/default/hypr/bindings"
USER_BINDINGS_PATH = os.path.expanduser("~/.config/hypr/bindings.lua")
CACHE_DIR = os.path.expanduser("~/.cache/omarchy")

MODIFIER_ORDER = {"SUPER": 1, "SHIFT": 2, "CTRL": 3, "CONTROL": 3, "ALT": 4}
UNSAFE_KEY_RE = re.compile(r'["\'\\()\n\r\t]')
MAX_KEY_LEN = 64
MAX_TEXT_LEN = 200
MAX_CMD_LEN = 2000
MAX_FILE_BYTES = 65_536
MAX_DIR_FILES = 256
MAX_TOTAL_READ = 2_097_152
MAX_SUBPROCESS_TIMEOUT = 15
MAX_SUBPROCESS_OUTPUT = 1_048_576

def run_cmd(cmd) -> tuple:
    """Run a command with:
      - a hard wall-clock timeout,
      - a hard aggregate stdout+stderr byte ceiling enforced *while streaming*
        (not after the process exits), and
      - whole-process-group cleanup (SIGTERM -> SIGKILL + reap) on both
        overflow and timeout, so no child survives.
    Returns (returncode, bounded_stdout, bounded_stderr); on timeout or overflow
    the code is None to signal the failure."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception:
        return None, "", ""
    out = bytearray()
    err = bytearray()
    total = 0
    overflow = False
    timed_out = False
    deadline = time.monotonic() + MAX_SUBPROCESS_TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            fds = [fd for fd in (proc.stdout, proc.stderr) if fd is not None and not fd.closed]
            if not fds:
                break
            rlist, _, _ = select.select(fds, [], [], min(remaining, 0.2))
            for fd in rlist:
                data = os.read(fd.fileno(), 8192)
                if not data:
                    fd.close()
                    continue
                if total + len(data) > MAX_SUBPROCESS_OUTPUT:
                    overflow = True
                    continue
                total += len(data)
                if fd is proc.stdout:
                    out += data
                else:
                    err += data
            if overflow:
                break
        if timed_out or overflow:
            _kill_and_reap(proc)
            return None, _decode(out), _decode(err)
        code = proc.wait()
        return code, _decode(out), _decode(err)
    except Exception:
        _kill_and_reap(proc)
        return None, _decode(out), _decode(err)


def _kill_and_reap(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.wait()
        except Exception:
            pass


def _decode(data: bytearray) -> str:
    return bytes(data).decode("utf-8", errors="replace").strip()


def _parent_dir_fd(path):
    """Open path's parent directory via component-wise descriptor-relative
    traversal: every component is opened with openat(..., O_DIRECTORY|O_NOFOLLOW)
    relative to the previously retained dirfd, walking from '/'. No pathname is
    ever followed, so a symlink substitution at any ancestor is rejected."""
    p = Path(path)
    if not p.is_absolute():
        raise ValueError(f"expected absolute path: {path}")
    parent = p.parent
    comps = parent.parts[1:]  # drop the leading root component
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in comps:
            nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise ValueError(f"refusing non-directory component: {comp}")
        return fd
    except FileNotFoundError:
        os.close(fd)
        raise
    except OSError as e:
        os.close(fd)
        raise ValueError(f"cannot traverse path: {path} ({e})") from e
    except BaseException:
        os.close(fd)
        raise


def _open_dir_fd(path):
    """Open path itself as a directory via descriptor-relative O_NOFOLLOW walk.
    Raises FileNotFoundError if the directory is absent and ValueError on any
    unsafe component."""
    p = Path(path)
    comps = p.parts[1:]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in comps:
            nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise ValueError(f"refusing non-directory component: {comp}")
        return fd
    except FileNotFoundError:
        os.close(fd)
        raise
    except OSError as e:
        os.close(fd)
        raise ValueError(f"cannot open directory safely: {path} ({e})") from e
    except BaseException:
        os.close(fd)
        raise


def safe_open_config(path, *, owner_check=True, max_bytes=MAX_FILE_BYTES):
    """Open a config file relative to its descriptor-verified parent dirfd with
    no-follow, regular-file and (optionally) owner checks, returning
    (fd, size). Raises FileNotFoundError when genuinely absent and ValueError
    on unsafe conditions."""
    p = Path(path)
    parent_fd = _parent_dir_fd(path)
    try:
        try:
            fd = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            raise
        except IsADirectoryError:
            raise ValueError(f"refusing directory where file expected: {path}")
        except OSError as e:
            raise ValueError(f"refusing non-regular/symlink path: {path} ({e})") from e
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise ValueError(f"refusing non-regular file: {path}")
        if owner_check and st.st_uid != os.geteuid():
            os.close(fd)
            raise ValueError(f"refusing file not owned by user: {path}")
        if st.st_size > max_bytes:
            os.close(fd)
            raise ValueError(f"refusing oversized config file: {path}")
        return fd, st.st_size
    finally:
        os.close(parent_fd)


def _read_fd_capped(fd, max_bytes):
    """Read up to max_bytes+1 from the current fd offset, raising ValueError
    (cap+1 overflow rejection) if the file exceeds the limit."""
    raw = os.read(fd, max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("config file exceeds size limit")
    return raw.decode("utf-8")

def sanitize_lua_str(s: str) -> str:
    s = (s or "").replace("\r", " ").replace("\n", " ")
    if len(s) > MAX_TEXT_LEN:
        s = s[:MAX_TEXT_LEN]
    return s.replace("\\", "\\\\").replace('"', '\\"')

_BIND_HEADER_RE = re.compile(
    r"o\.bind(?:_toggle)?\s*\(\s*[\"']([^\"']+)[\"']\s*,\s*"
    r"(?:[\"']([^\"']+)[\"']|nil)\s*,\s*"
)

def _parse_bind_line(line: str):
    m = _BIND_HEADER_RE.match(line)
    if not m:
        return None
    raw_key = m.group(1).strip()
    desc = (m.group(2) or "").strip()
    pos = m.end()
    depth_paren = 0
    depth_brace = 0
    in_string = None
    escape_next = False
    action_start = pos
    while pos < len(line):
        ch = line[pos]
        if escape_next:
            escape_next = False
            pos += 1
            continue
        if ch == '\\' and in_string:
            escape_next = True
            pos += 1
            continue
        if ch == '"' or ch == "'":
            if not in_string:
                in_string = ch
            elif in_string == ch:
                in_string = None
            pos += 1
            continue
        if in_string:
            pos += 1
            continue
        if ch == '(':
            depth_paren += 1
        elif ch == ')':
            if depth_paren > 0:
                depth_paren -= 1
            else:
                break
        elif ch == '{':
            depth_brace += 1
        elif ch == '}':
            depth_brace -= 1
        elif ch == ',' and depth_paren == 0 and depth_brace == 0:
            break
        pos += 1
    action = line[action_start:pos].strip()
    opts = None
    if pos < len(line) and line[pos] == ',':
        opts_start = pos + 1
        opts_text = line[opts_start:].rstrip()
        if opts_text.endswith(')'):
            opts_text = opts_text[:-1].strip()
        if opts_text:
            opts = opts_text
    return raw_key, desc, action, opts

PRESET_CATALOG = [
    {"id": "win.close", "category": "Window Management", "name": "Close Window", "description": "Close currently focused window", "dispatcher": "hl.dsp.window.close()", "command": "hyprctl dispatch killactive", "default_key": "SUPER + W"},
    {"id": "win.close_all", "category": "Window Management", "name": "Close All Windows", "description": "Close all open windows on current workspace", "dispatcher": "exec", "command": "omarchy-hyprland-window-close-all", "default_key": "CTRL + ALT + DELETE"},
    {"id": "win.fullscreen", "category": "Window Management", "name": "Toggle Fullscreen", "description": "Toggle fullscreen state for focused window", "dispatcher": "hl.dsp.window.fullscreen()", "command": "hyprctl dispatch fullscreen", "default_key": "SUPER + F"},
    {"id": "win.fullwidth", "category": "Window Management", "name": "Toggle Full Width", "description": "Expand window across the full screen width", "dispatcher": "hl.dsp.window.fullwidth()", "command": "hyprctl dispatch fullscreen 2", "default_key": "SUPER + ALT + F"},
    {"id": "win.floating", "category": "Window Management", "name": "Toggle Window Floating", "description": "Toggle between tiling and floating mode", "dispatcher": "hl.dsp.window.float({ action = \"toggle\" })", "command": "hyprctl dispatch togglefloating", "default_key": "SUPER + T"},
    {"id": "win.split", "category": "Window Management", "name": "Toggle Split Direction", "description": "Toggle window tiling split between horizontal/vertical", "dispatcher": "hl.dsp.layout(\"togglesplit\")", "command": "hyprctl dispatch layoutmsg togglesplit", "default_key": "SUPER + J"},
    {"id": "win.popout", "category": "Window Management", "name": "Pop Window Out (Float & Pin)", "description": "Float focused window and pin it across workspaces", "dispatcher": "exec", "command": "omarchy-hyprland-window-pop", "default_key": "SUPER + O"},
    {"id": "win.transparency", "category": "Window Management", "name": "Toggle Window Transparency", "description": "Toggle transparency for active window", "dispatcher": "exec", "command": "omarchy-hyprland-window-transparency-toggle", "default_key": "SUPER + BACKSPACE"},
    {"id": "win.gaps", "category": "Window Management", "name": "Toggle Window Gaps", "description": "Toggle inner and outer workspace window gaps", "dispatcher": "exec", "command": "omarchy-hyprland-window-gaps-toggle", "default_key": "SUPER + SHIFT + BACKSPACE"},
    {"id": "win.focus_left", "category": "Window Management", "name": "Focus Window Left", "description": "Move focus to the window on the left", "dispatcher": "hl.dsp.focus.left()", "command": "hyprctl dispatch movefocus l", "default_key": "SUPER + LEFT"},
    {"id": "win.focus_right", "category": "Window Management", "name": "Focus Window Right", "description": "Move focus to the window on the right", "dispatcher": "hl.dsp.focus.right()", "command": "hyprctl dispatch movefocus r", "default_key": "SUPER + RIGHT"},
    {"id": "win.focus_up", "category": "Window Management", "name": "Focus Window Up", "description": "Move focus to the window above", "dispatcher": "hl.dsp.focus.up()", "command": "hyprctl dispatch movefocus u", "default_key": "SUPER + UP"},
    {"id": "win.focus_down", "category": "Window Management", "name": "Focus Window Down", "description": "Move focus to the window below", "dispatcher": "hl.dsp.focus.down()", "command": "hyprctl dispatch movefocus d", "default_key": "SUPER + DOWN"},
    {"id": "win.move_left", "category": "Window Management", "name": "Move Window Left", "description": "Move active window to the left in tiling layout", "dispatcher": "hl.dsp.window.move({ direction = \"left\" })", "command": "hyprctl dispatch movewindow l", "default_key": "SUPER + SHIFT + LEFT"},
    {"id": "win.move_right", "category": "Window Management", "name": "Move Window Right", "description": "Move active window to the right in tiling layout", "dispatcher": "hl.dsp.window.move({ direction = \"right\" })", "command": "hyprctl dispatch movewindow r", "default_key": "SUPER + SHIFT + RIGHT"},
    {"id": "win.move_up", "category": "Window Management", "name": "Move Window Up", "description": "Move active window up in tiling layout", "dispatcher": "hl.dsp.window.move({ direction = \"up\" })", "command": "hyprctl dispatch movewindow u", "default_key": "SUPER + SHIFT + UP"},
    {"id": "win.move_down", "category": "Window Management", "name": "Move Window Down", "description": "Move active window down in tiling layout", "dispatcher": "hl.dsp.window.move({ direction = \"down\" })", "command": "hyprctl dispatch movewindow d", "default_key": "SUPER + SHIFT + DOWN"},
    {"id": "ws.next", "category": "Workspaces", "name": "Next Workspace", "description": "Switch to next workspace", "dispatcher": "hl.dsp.focus({ workspace = \"e+1\" })", "command": "hyprctl dispatch workspace e+1", "default_key": "SUPER + TAB"},
    {"id": "ws.prev", "category": "Workspaces", "name": "Previous Workspace", "description": "Switch to previous workspace", "dispatcher": "hl.dsp.focus({ workspace = \"e-1\" })", "command": "hyprctl dispatch workspace e-1", "default_key": "SUPER + SHIFT + TAB"},
    {"id": "ws.scratchpad_toggle", "category": "Workspaces", "name": "Toggle Special Scratchpad", "description": "Toggle overlay special workspace (scratchpad)", "dispatcher": "hl.dsp.special_workspace.focus({ name = \"scratchpad\" })", "command": "hyprctl dispatch togglespecialworkspace scratchpad", "default_key": "SUPER + S"},
    {"id": "ws.scratchpad_moveto", "category": "Workspaces", "name": "Move to Scratchpad", "description": "Move focused window into special scratchpad", "dispatcher": "hl.dsp.special_workspace.move_window({ name = \"scratchpad\" })", "command": "hyprctl dispatch movetoworkspace special:scratchpad", "default_key": "SUPER + ALT + S"},
    {"id": "menu.root", "category": "Menus & System", "name": "Omarchy Menu", "description": "Summon the primary Omarchy launcher menu", "dispatcher": "exec", "command": "omarchy-menu toggle", "default_key": "SUPER + SPACE"},
    {"id": "menu.apps", "category": "Menus & System", "name": "Apps Menu", "description": "Summon the applications list menu", "dispatcher": "exec", "command": "omarchy-menu toggle apps", "default_key": "SUPER + ALT + SPACE"},
    {"id": "menu.system", "category": "Menus & System", "name": "System / Power Menu", "description": "Summon the system power/logout menu", "dispatcher": "exec", "command": "omarchy-menu toggle system", "default_key": "SUPER + ESCAPE"},
    {"id": "menu.keybindings", "category": "Menus & System", "name": "Keybindings Helper", "description": "Open keybindings quick selector", "dispatcher": "exec", "command": "omarchy-menu-keybindings", "default_key": "SUPER + K"},
    {"id": "shell.emojis", "category": "Menus & System", "name": "Emoji Picker", "description": "Open full-screen emoji selector overlay", "dispatcher": "exec", "command": "omarchy-shell shell toggle omarchy.emojis", "default_key": "SUPER + CTRL + E"},
    {"id": "shell.clipboard", "category": "Menus & System", "name": "Clipboard Manager", "description": "Open clipboard history overlay", "dispatcher": "exec", "command": "omarchy-shell shell toggle omarchy.clipboard", "default_key": "SUPER + CTRL + V"},
    {"id": "shell.theme", "category": "Menus & System", "name": "Theme Menu", "description": "Summon the Omarchy theme switcher", "dispatcher": "exec", "command": "omarchy-menu toggle theme", "default_key": "SUPER + SHIFT + CTRL + SPACE"},
    {"id": "shell.background", "category": "Menus & System", "name": "Background Switcher", "description": "Summon wallpaper selector overlay", "dispatcher": "exec", "command": "omarchy-menu toggle background", "default_key": "SUPER + CTRL + SPACE"},
    {"id": "shell.toggle_bar", "category": "Menus & System", "name": "Toggle Menu Bar", "description": "Show or hide the top status bar", "dispatcher": "exec", "command": "omarchy-toggle-bar", "default_key": "SUPER + SHIFT + SPACE"},
    {"id": "sys.lock", "category": "Menus & System", "name": "Lock System", "description": "Trigger session screen lock", "dispatcher": "exec", "command": "omarchy-system-lock", "default_key": "SUPER + CTRL + L"},
    {"id": "sys.screensaver", "category": "Menus & System", "name": "Screensaver", "description": "Start the Omarchy screensaver", "dispatcher": "exec", "command": "omarchy-launch-screensaver force", "default_key": "SUPER + ALT + L"},
    {"id": "app.terminal", "category": "Applications", "name": "Terminal", "description": "Terminal", "dispatcher": "{ omarchy = \"terminal\" }", "command": "omarchy-launch-terminal", "default_key": "SUPER + RETURN"},
    {"id": "app.browser", "category": "Applications", "name": "Browser", "description": "Browser", "dispatcher": "{ omarchy = \"browser\" }", "command": "omarchy-launch-browser", "default_key": "SUPER + SHIFT + RETURN"},
    {"id": "app.file_manager", "category": "Applications", "name": "File Manager", "description": "File manager", "dispatcher": "{ omarchy = \"nautilus\" }", "command": "omarchy-launch-nautilus", "default_key": "SUPER + SHIFT + F"},
    {"id": "app.file_manager_cwd", "category": "Applications", "name": "File Manager (Active CWD)", "description": "File manager (cwd)", "dispatcher": "{ omarchy = \"nautilus-cwd\" }", "command": "omarchy-launch-nautilus-cwd", "default_key": "SUPER + ALT + SHIFT + F"},
    {"id": "app.editor", "category": "Applications", "name": "Code Editor", "description": "Editor", "dispatcher": "{ omarchy = \"editor\" }", "command": "omarchy-launch-editor", "default_key": "SUPER + SHIFT + N"},
    {"id": "app.docker", "category": "Applications", "name": "Docker", "description": "Docker", "dispatcher": "{ tui = \"lazydocker\" }", "command": "lazydocker", "default_key": "SUPER + SHIFT + D"},
    {"id": "app.calculator", "category": "Applications", "name": "Calculator", "description": "Calculator", "dispatcher": "exec", "command": "omacalc", "default_key": "SUPER + CTRL + Q"},
    {"id": "media.volume_up", "category": "Media & Audio", "name": "Volume Up", "description": "Raise audio output volume", "dispatcher": "exec", "command": "omarchy-audio-output-volume raise", "default_key": "XF86AudioRaiseVolume"},
    {"id": "media.volume_down", "category": "Media & Audio", "name": "Volume Down", "description": "Lower audio output volume", "dispatcher": "exec", "command": "omarchy-audio-output-volume lower", "default_key": "XF86AudioLowerVolume"},
    {"id": "media.mute", "category": "Media & Audio", "name": "Mute Audio", "description": "Toggle audio output mute", "dispatcher": "exec", "command": "omarchy-audio-output-volume mute-toggle", "default_key": "XF86AudioMute"},
    {"id": "media.mic_mute", "category": "Media & Audio", "name": "Mute Microphone", "description": "Toggle audio microphone mute", "dispatcher": "exec", "command": "omarchy-audio-input-mute", "default_key": "XF86AudioMicMute"},
    {"id": "media.screenshot", "category": "Media & Audio", "name": "Capture Screenshot", "description": "Capture screen or interactive slurp region", "dispatcher": "exec", "command": "omarchy-capture-screenshot", "default_key": "PRINT"},
    {"id": "media.screenrecord", "category": "Media & Audio", "name": "Toggle Screenrecording", "description": "Start or stop screenrecording", "dispatcher": "exec", "command": "omarchy-capture-screenrecording --stop-recording || omarchy-menu toggle trigger.capture.screenrecord", "default_key": "ALT + PRINT"},
    {"id": "media.color_picker", "category": "Media & Audio", "name": "Color Picker", "description": "Pick color under cursor to clipboard", "dispatcher": "exec", "command": "pkill hyprpicker || hyprpicker -a", "default_key": "SUPER + PRINT"},
    {"id": "media.ocr_text", "category": "Media & Audio", "name": "OCR Text from Screen", "description": "Extract text from selected screen area", "dispatcher": "exec", "command": "omarchy-capture-text", "default_key": "SUPER + CTRL + PRINT"},
]

def normalize_key_chord(key_chord: str) -> str:
    if not key_chord:
        return ""
    raw_parts = [p.strip() for p in key_chord.replace(",", "+").split("+") if p.strip()]
    if not raw_parts:
        return ""
    mods = []
    main_key = ""
    for part in raw_parts:
        upper = part.upper()
        if upper in ("SUPER", "WIN", "LOGO", "SUPER_L", "SUPER_R", "META", "MOD4"):
            if "SUPER" not in mods:
                mods.append("SUPER")
        elif upper in ("SHIFT", "SHIFT_L", "SHIFT_R"):
            if "SHIFT" not in mods:
                mods.append("SHIFT")
        elif upper in ("CTRL", "CONTROL", "CONTROL_L", "CONTROL_R"):
            if "CTRL" not in mods:
                mods.append("CTRL")
        elif upper in ("ALT", "ALT_L", "ALT_R", "MOD1"):
            if "ALT" not in mods:
                mods.append("ALT")
        else:
            main_key = part
    mods.sort(key=lambda m: MODIFIER_ORDER.get(m, 99))
    if main_key:
        upper_k = main_key.upper()
        if upper_k in ("RETURN", "ENTER", "SPACE", "ESCAPE", "TAB", "BACKSPACE", "DELETE", "PRINT"):
            main_key = upper_k
        elif upper_k in ("LEFT", "RIGHT", "UP", "DOWN"):
            main_key = upper_k
        elif upper_k.startswith("F") and upper_k[1:].isdigit():
            main_key = upper_k
        elif main_key in ("comma", "period", "slash", "minus", "equal", "bracketleft", "bracketright", "semicolon", "apostrophe", "backslash"):
            pass
        elif len(main_key) == 1:
            main_key = main_key.upper()
    if mods and main_key:
        out = f"{' + '.join(mods)} + {main_key}"
    elif mods and not main_key:
        out = " + ".join(mods)
    else:
        out = main_key
    if len(out) > MAX_KEY_LEN or UNSAFE_KEY_RE.search(out or ""):
        return ""
    return out

def get_mouse_scroll_settings():
    settings = {"natural_scroll": False, "scroll_factor": 1.0}
    try:
        code, outp, _ = run_cmd(["hyprctl", "getoption", "input:scroll_factor", "-j"])
        if code == 0 and outp:
            data = json.loads(outp)
            if "float" in data:
                settings["scroll_factor"] = float(data["float"])
    except Exception:
        pass
    try:
        code, outp, _ = run_cmd(["hyprctl", "getoption", "input:natural_scroll", "-j"])
        if code == 0 and outp:
            data = json.loads(outp)
            if "bool" in data:
                settings["natural_scroll"] = bool(data["bool"])
    except Exception:
        pass
    return settings

def parse_default_bindings():
    bindings = []
    try:
        dirfd = _open_dir_fd(DEFAULT_BINDINGS_DIR)
    except (FileNotFoundError, ValueError):
        return bindings
    try:
        names = sorted(e.name for e in os.scandir(dirfd))
        file_count = 0
        total_read = 0
        for fname in names:
            if file_count >= MAX_DIR_FILES or total_read > MAX_TOTAL_READ:
                break
            file_count += 1
            if not fname.endswith(".lua"):
                continue
            try:
                src_fd = os.open(fname, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
            except OSError:
                continue
            try:
                st = os.fstat(src_fd)
                if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
                    continue
                content = _read_fd_capped(src_fd, MAX_FILE_BYTES)
            except Exception:
                continue
            finally:
                os.close(src_fd)
            total_read += len(content.encode("utf-8"))
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith('--'):
                    continue
                parsed = _parse_bind_line(stripped)
                if not parsed:
                    continue
                raw_key, desc, action_raw, opts_raw = parsed
                norm_key = normalize_key_chord(raw_key)
                if not desc:
                    desc = action_raw.strip('"\'')
                cat = "General"
                base = fname.replace(".lua", "")
                if base == "applications":
                    cat = "Applications"
                elif base == "tiling":
                    cat = "Window Management"
                elif base == "media":
                    cat = "Media & Audio"
                elif base == "utilities":
                    cat = "Menus & System"
                elif base == "clipboard":
                    cat = "Clipboard"
                elif base == "voxtype":
                    cat = "AI & Voice"
                is_release = opts_raw and ("on_release = true" in opts_raw or "on_release=true" in opts_raw)
                bindings.append({
                    "source": "default",
                    "file": fname,
                    "key": norm_key,
                    "raw_key": raw_key,
                    "description": desc,
                    "action": action_raw,
                    "category": cat,
                    "is_mouse": "mouse" in raw_key.lower() or "mouse" in (opts_raw or ""),
                    "is_release": is_release,
                })
    finally:
        os.close(dirfd)
    return bindings

def parse_user_bindings():
    result = {
        "raw_content": "",
        "unbinds": set(),
        "binds": [],
        "mouse_binds": [],
    }
    try:
        src_fd, _ = safe_open_config(USER_BINDINGS_PATH, owner_check=True)
    except FileNotFoundError:
        return result
    try:
        content = _read_fd_capped(src_fd, MAX_FILE_BYTES)
    finally:
        os.close(src_fd)
    result["raw_content"] = content
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        m_unbind = re.match(r'hl\.unbind\s*\(\s*["\']([^"\']+)["\']\s*\)', stripped)
        if m_unbind:
            raw_key = m_unbind.group(1).strip()
            result["unbinds"].add(normalize_key_chord(raw_key))
            continue
        parsed = _parse_bind_line(stripped)
        if parsed:
            raw_key, desc, action_raw, opts_raw = parsed
            norm_key = normalize_key_chord(raw_key)
            entry = {
                "source": "user",
                "key": norm_key,
                "raw_key": raw_key,
                "description": desc or action_raw.strip('"\''),
                "action": action_raw,
                "is_mouse": "mouse" in raw_key.lower() or "mouse" in (opts_raw or ""),
                "is_release": opts_raw and ("on_release = true" in opts_raw or "on_release=true" in opts_raw),
            }
            if entry["is_mouse"]:
                result["mouse_binds"].append(entry)
            else:
                result["binds"].append(entry)
    return result

def build_complete_model():
    default_binds = parse_default_bindings()
    user_state = parse_user_bindings()
    rebound_defaults_map = {}
    matched_user_bind_keys = set()
    for ub in user_state["binds"]:
        matched = next(
            (d for d in default_binds if d["description"].lower() == ub["description"].lower() or d["action"] == ub["action"]),
            None
        )
        if matched:
            rebound_defaults_map[matched["key"]] = ub
            matched_user_bind_keys.add(ub["key"])
    all_active = []
    for d in default_binds:
        k = d["key"]
        if k in rebound_defaults_map:
            ub = rebound_defaults_map[k]
            item = dict(ub)
            item["status"] = "modified"
            item["category"] = d.get("category", "Custom")
            item["default_key"] = k
            item["default_action"] = d.get("action", "")
            all_active.append(item)
            continue
        same_key_user_bind = next((ub for ub in user_state["binds"] if ub["key"] == k and ub["key"] not in matched_user_bind_keys), None)
        if same_key_user_bind:
            item = dict(same_key_user_bind)
            item["status"] = "modified" if same_key_user_bind["description"].lower() == d["description"].lower() else "custom"
            item["category"] = d.get("category", "Custom")
            item["default_key"] = k
            item["default_action"] = d["action"]
            all_active.append(item)
            matched_user_bind_keys.add(k)
            continue
        if k in user_state["unbinds"]:
            item = dict(d)
            item["status"] = "disabled"
            item["default_key"] = k
            all_active.append(item)
            continue
        item = dict(d)
        item["status"] = "default"
        item["default_key"] = k
        all_active.append(item)
    for ub in user_state["binds"]:
        if ub["key"] in matched_user_bind_keys:
            continue
        item = dict(ub)
        item["status"] = "custom"
        item["category"] = "Custom"
        item["default_key"] = ""
        all_active.append(item)
    all_active.sort(key=lambda x: (x.get("description", "").lower(), x.get("key", "")))
    conflicts_dict = {}
    key_groups = {}
    for b in all_active:
        if b.get("status") == "disabled" or b.get("is_mouse") or b.get("is_release"):
            continue
        k = b["key"]
        if k not in key_groups:
            key_groups[k] = []
        key_groups[k].append(b)
    for k, group in key_groups.items():
        sources = {x.get("source") for x in group}
        files = {x.get("file") for x in group}
        is_intentional_composite = (sources == {"default"} and len(files) == 1 and files != {None})
        if len(group) > 1 and not is_intentional_composite:
            for b in group:
                b["is_conflict"] = True
                b["conflict_with"] = [other["description"] for other in group if other != b]
            conflicts_dict[k] = {
                "key": k,
                "bindings": [
                    {"description": x["description"], "action": x["action"], "status": x.get("status", "unknown")}
                    for x in group
                ],
            }
        else:
            for b in group:
                b["is_conflict"] = False
    conflicts = list(conflicts_dict.values())
    bound_descriptions = {b["description"].lower() for b in all_active if b.get("status") != "disabled"}
    catalog = []
    for preset in PRESET_CATALOG:
        is_bound = preset["name"].lower() in bound_descriptions or preset["description"].lower() in bound_descriptions
        curr_key = ""
        for b in all_active:
            if b.get("status") != "disabled" and (b["description"].lower() == preset["name"].lower() or b["description"].lower() == preset["description"].lower()):
                curr_key = b["key"]
                break
        cat_item = dict(preset)
        cat_item["is_bound"] = is_bound
        cat_item["current_key"] = curr_key
        catalog.append(cat_item)
    catalog.sort(key=lambda x: x.get("name", "").lower())
    total_modified = len([b for b in all_active if b.get("status") in ("modified", "custom")])
    return {
        "active": all_active,
        "catalog": catalog,
        "conflicts": conflicts,
        "total_active": len([b for b in all_active if b.get("status") != "disabled"]),
        "total_modified": total_modified,
        "total_conflicts": len(conflicts),
        "mouse_settings": get_mouse_scroll_settings(),
        "user_bindings_file": USER_BINDINGS_PATH,
    }

def write_user_bindings(lines_to_add=None, unbinds_to_add=None, keys_to_remove=None):
    name = Path(USER_BINDINGS_PATH).name
    parent_fd = _parent_dir_fd(USER_BINDINGS_PATH)
    try:
        existing_content = ""
        prior_mode = 0o600
        try:
            src_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                st = os.fstat(src_fd)
                if not stat.S_ISREG(st.st_mode):
                    raise ValueError(f"refusing non-regular file: {USER_BINDINGS_PATH}")
                prior_mode = stat.S_IMODE(st.st_mode)
                existing_content = _read_fd_capped(src_fd, MAX_FILE_BYTES)
            finally:
                os.close(src_fd)
        except FileNotFoundError:
            pass

        current_lines = existing_content.splitlines() if existing_content else []
        if not current_lines:
            current_lines = [
                "-- Personal keybinding overrides for Omarchy Hyprland",
                "-- Managed graphically by Mouse & Keybind Settings Plugin",
                "",
            ]
        clean_keys = set(normalize_key_chord(k) for k in (keys_to_remove or []))
        new_lines = []
        for line in current_lines:
            stripped = line.strip()
            if stripped.startswith("--"):
                new_lines.append(line)
                continue
            is_targeted = False
            if clean_keys:
                m_unbind = re.match(r'hl\.unbind\s*\(\s*["\']([^"\']+)["\']\s*\)', stripped)
                if m_unbind and normalize_key_chord(m_unbind.group(1)) in clean_keys:
                    is_targeted = True
                m_bind = re.match(r'o\.bind(?:_toggle)?\s*\(\s*["\']([^"\']+)["\']', stripped)
                if m_bind and normalize_key_chord(m_bind.group(1)) in clean_keys:
                    is_targeted = True
            if not is_targeted:
                new_lines.append(line)
        if unbinds_to_add:
            for k in unbinds_to_add:
                norm_k = normalize_key_chord(k)
                line_str = f'hl.unbind("{norm_k}")'
                if line_str not in new_lines:
                    new_lines.append(line_str)
        if lines_to_add:
            for entry in lines_to_add:
                key = normalize_key_chord(entry.get("key", ""))
                desc = sanitize_lua_str(entry.get("description", ""))
                cmd = entry.get("command", "")
                action = entry.get("action", "")
                if action and (action.strip().startswith("{") or action.strip().startswith("hl.")):
                    new_lines.append(f'o.bind("{key}", "{desc}", {action.strip()[:MAX_CMD_LEN]})')
                elif cmd and (cmd.strip().startswith("{") or cmd.strip().startswith("hl.")):
                    new_lines.append(f'o.bind("{key}", "{desc}", {cmd.strip()[:MAX_CMD_LEN]})')
                else:
                    raw_cmd = (cmd or action).strip()
                    if (raw_cmd.startswith('"') and raw_cmd.endswith('"')) or (raw_cmd.startswith("'") and raw_cmd.endswith("'")):
                        raw_cmd = raw_cmd[1:-1]
                    escaped_cmd = sanitize_lua_str(raw_cmd)
                    new_lines.append(f'o.bind("{key}", "{desc}", "{escaped_cmd}")')
        final_output = []
        prev_blank = False
        for line in new_lines:
            if not line.strip():
                if not prev_blank:
                    final_output.append("")
                    prev_blank = True
            else:
                final_output.append(line)
                prev_blank = False
        data = ("\n".join(final_output) + "\n").encode("utf-8")
        tmp_name = f".{name}.{os.getpid()}.{os.urandom(4).hex}.tmp"
        tmp_fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, prior_mode, dir_fd=parent_fd)
        try:
            wrote = 0
            while wrote < len(data):
                wrote += os.write(tmp_fd, data[wrote:])
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1
            os.replace(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException:
            try:
                if tmp_fd != -1:
                    os.close(tmp_fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(parent_fd)

def reload_hyprland():
    code, reload_out, _ = run_cmd(["hyprctl", "reload"])
    code2, cfg_out, _ = run_cmd(["hyprctl", "configerrors"])
    return {
        "success": code == 0,
        "reload_output": (reload_out or "").strip(),
        "config_errors": (cfg_out or "").strip() if code2 == 0 else "",
    }


def _snapshot_bindings():
    """Capture (exists, raw_bytes, mode) of USER_BINDINGS_PATH for rollback, or
    None if the file is genuinely absent. Raises (fails closed) on unsafety."""
    try:
        src_fd, _ = safe_open_config(USER_BINDINGS_PATH, owner_check=True)
    except FileNotFoundError:
        return None
    try:
        raw = os.read(src_fd, MAX_FILE_BYTES)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("user bindings file exceeds size limit")
        return (True, raw, stat.S_IMODE(os.fstat(src_fd).st_mode))
    finally:
        os.close(src_fd)


def _restore_bindings(prior):
    """Atomically restore USER_BINDINGS_PATH to a prior _snapshot_bindings()
    result (exact bytes + mode). A prior of None removes the file if present."""
    name = Path(USER_BINDINGS_PATH).name
    parent_fd = _parent_dir_fd(USER_BINDINGS_PATH)
    try:
        if prior is None:
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            return
        _, raw, mode = prior
        tmp_name = f".{name}.{os.getpid()}.{os.urandom(4).hex}.tmp"
        tmp_fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=parent_fd)
        try:
            wrote = 0
            while wrote < len(raw):
                wrote += os.write(tmp_fd, raw[wrote:])
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1
            os.replace(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException:
            try:
                if tmp_fd != -1:
                    os.close(tmp_fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(parent_fd)


def _verify_and_rollback(prior, key, old_key="", description=""):
    """After a write, reload Hyprland and validate. If reload fails or Hyprland
    reports config errors, atomically restore the exact prior file bytes + mode
    and reload again. The report is truthful about rollback: if restoration
    fails, the returned dict says so (rolled_back False) instead of claiming a
    rollback that did not happen."""
    status = reload_hyprland()
    if status.get("success") and not (status.get("config_errors") or "").strip():
        return status
    rollback_error = ""
    try:
        _restore_bindings(prior)
    except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
        rollback_error = str(exc)
    reload_hyprland()
    err = (status.get("config_errors") or "config validation failed").strip()
    if rollback_error:
        err = err + "; rollback FAILED: " + rollback_error
    else:
        err = err + "; changes rolled back"
    return {
        "success": False,
        "key": key,
        "old_key": old_key,
        "description": description,
        "reload": status,
        "rolled_back": not rollback_error,
        "error": err,
    }

def set_keybinding(key: str, description: str, command: str, action: str = "", old_key: str = "", override_conflicts: bool = True):
    norm_key = normalize_key_chord(key)
    norm_old_key = normalize_key_chord(old_key) if old_key else ""
    if not norm_key:
        return {"success": False, "error": "Invalid key combination"}
    if len(description or "") > MAX_TEXT_LEN or len(command or "") > MAX_CMD_LEN or len(action or "") > MAX_CMD_LEN:
        return {"success": False, "error": "Description or command exceeds allowed length"}
    default_binds = parse_default_bindings()
    keys_to_clean = [norm_key]
    unbinds_to_add = []
    if norm_old_key and norm_old_key != norm_key:
        keys_to_clean.append(norm_old_key)
        if any(d["key"] == norm_old_key for d in default_binds):
            unbinds_to_add.append(norm_old_key)
    if any(d["key"] == norm_key for d in default_binds) or override_conflicts:
        unbinds_to_add.append(norm_key)
    binds = [{
        "key": norm_key,
        "description": description or command,
        "command": command,
        "action": action,
    }]
    prior = _snapshot_bindings()
    write_user_bindings(lines_to_add=binds, unbinds_to_add=unbinds_to_add, keys_to_remove=keys_to_clean)
    status = _verify_and_rollback(prior, norm_key, norm_old_key, description or command)
    return {
        "success": status.get("success", False),
        "key": norm_key,
        "old_key": norm_old_key,
        "description": description,
        "reload": status.get("reload"),
        "error": status.get("error"),
    }

def reset_keybinding(key: str, default_key: str = ""):
    norm_key = normalize_key_chord(key)
    norm_def = normalize_key_chord(default_key) if default_key else ""
    keys_to_remove = [norm_key]
    if norm_def and norm_def != norm_key:
        keys_to_remove.append(norm_def)
    default_binds = parse_default_bindings()
    user_state = parse_user_bindings()
    for ub in user_state["binds"]:
        if ub["key"] == norm_key:
            matched_default = next(
                (d for d in default_binds if d["description"].lower() == ub["description"].lower() or d["action"] == ub["action"]),
                None
            )
            if matched_default and matched_default["key"] not in keys_to_remove:
                keys_to_remove.append(matched_default["key"])
    prior = _snapshot_bindings()
    write_user_bindings(keys_to_remove=keys_to_remove)
    status = _verify_and_rollback(prior, norm_key)
    return {"success": status.get("success", False), "key": norm_key, "reload": status.get("reload"), "error": status.get("error")}

def disable_keybinding(key: str):
    norm_key = normalize_key_chord(key)
    prior = _snapshot_bindings()
    write_user_bindings(unbinds_to_add=[norm_key], keys_to_remove=[norm_key])
    status = _verify_and_rollback(prior, norm_key)
    return {"success": status.get("success", False), "key": norm_key, "reload": status.get("reload"), "error": status.get("error")}

def enable_keybinding(key: str):
    norm_key = normalize_key_chord(key)
    prior = _snapshot_bindings()
    write_user_bindings(keys_to_remove=[norm_key])
    status = _verify_and_rollback(prior, norm_key)
    return {"success": status.get("success", False), "key": norm_key, "reload": status.get("reload"), "error": status.get("error")}

def main():
    if len(sys.argv) < 2:
        print(json.dumps(build_complete_model(), indent=2))
        return
    cmd = sys.argv[1]
    if cmd == "list":
        print(json.dumps(build_complete_model()))
    elif cmd == "set":
        if len(sys.argv) < 5:
            print(json.dumps({"success": False, "error": "Usage: set <key> <desc> <command> [action] [old_key]"}))
            sys.exit(1)
        key = sys.argv[2]
        desc = sys.argv[3]
        command = sys.argv[4]
        action = sys.argv[5] if len(sys.argv) > 5 else ""
        old_key = sys.argv[6] if len(sys.argv) > 6 else ""
        res = set_keybinding(key, desc, command, action, old_key)
        print(json.dumps(res))
    elif cmd == "reset":
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "Usage: reset <key> [default_key]"}))
            sys.exit(1)
        key = sys.argv[2]
        def_key = sys.argv[3] if len(sys.argv) > 3 else ""
        res = reset_keybinding(key, def_key)
        print(json.dumps(res))
    elif cmd == "disable":
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "Usage: disable <key>"}))
            sys.exit(1)
        key = sys.argv[2]
        res = disable_keybinding(key)
        print(json.dumps(res))
    elif cmd == "enable":
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "Usage: enable <key>"}))
            sys.exit(1)
        key = sys.argv[2]
        res = enable_keybinding(key)
        print(json.dumps(res))
    elif cmd == "reload":
        res = reload_hyprland()
        print(json.dumps(res))
    else:
        print(json.dumps({"success": False, "error": f"Unknown command {cmd}"}))
        sys.exit(1)

if __name__ == "__main__":
    main()