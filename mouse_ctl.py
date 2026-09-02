#!/usr/bin/env python3
"""
Omarchy Mouse Settings Backend Controller (mouse_ctl.py)
Hardened security implementation:
- Atomic file writes via temp-files + fsync + os.replace
- Symlink clobbering protection (refuses to follow symlinks)
- File locking via fcntl.flock to prevent race conditions
- Strict input validation & range clamping (finite numbers, allowlisted enums)
- Selective persistence & conditional hyprctl reload
"""

import argparse
import fcntl
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

MAX_SUBPROCESS_TIMEOUT = 15
MAX_SUBPROCESS_OUTPUT = 1_048_576  # retained bytes per captured stream
MAX_JSON_INPUT_BYTES = 1_048_576
MAX_CONFIG_READ_BYTES = 1_048_576

INPUT_LUA_PATH = Path.home() / ".config" / "hypr" / "input.lua"
BINDINGS_LUA_PATH = Path.home() / ".config" / "hypr" / "bindings.lua"
LOCK_PATH = Path.home() / ".config" / "hypr" / ".mouse_ctl.lock"

START_MARKER = "-- [[ OMARCHY_MOUSE_SETTINGS_START ]]"
END_MARKER = "-- [[ OMARCHY_MOUSE_SETTINGS_END ]]"

BINDINGS_START_MARKER = "-- [[ OMARCHY_MOUSE_BINDINGS_START ]]"
BINDINGS_END_MARKER = "-- [[ OMARCHY_MOUSE_BINDINGS_END ]]"

DEFAULT_BUTTON_MAPPINGS = {
    "side_back": "default",
    "side_forward": "default",
    "middle_click": "default",
    "super_left": "move_window",
    "super_right": "resize_window",
    "super_wheel": "workspace_scroll"
}

ALLOWED_BUTTON_ACTIONS = {
    "side_back": {"default", "prev_workspace", "menu", "prev_window"},
    "side_forward": {"default", "next_workspace", "terminal", "next_window"},
    "middle_click": {"default", "close_window", "toggle_floating", "toggle_fullscreen"},
    "super_left": {"move_window", "disabled"},
    "super_right": {"resize_window", "disabled"},
    "super_wheel": {"workspace_scroll", "disabled"}
}

SIMULATE_BUTTON_CODES = {
    "left": "0xC0",
    "right": "0xC1",
    "middle": "0xC2",
    "side_back": "0xC3",
    "side_forward": "0xC4"
}

def _open_dir_fd(path: Path) -> int:
    """Open a directory file descriptor for the parent of a target config file,
    refusing to follow a symlinked final directory and requiring that the final
    directory is real and owned by the user. Ancestors only need to be real
    directories on the way to it."""
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    # Verify the ancestor chain resolves through real directories.
    parts = [Path("/")] + [Path(p) for p in parent.parts[1:]]
    for comp in parts[:-1]:
        if not comp.exists():
            break
        if not stat.S_ISDIR(os.stat(comp).st_mode):
            raise ValueError(f"refusing non-directory config component: {comp}")
    # The final parent must be a real directory owned by the user.
    try:
        st = os.stat(parent)
    except OSError as e:
        raise ValueError(f"cannot stat config directory: {parent} ({e})") from e
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"refusing non-directory config parent: {parent}")
    if st.st_uid != os.geteuid():
        raise ValueError(f"refusing config directory not owned by user: {parent}")
    # Open it no-follow so a symlink swap at the final hop is rejected.
    try:
        return os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise ValueError(f"cannot open config directory safely: {parent} ({e})") from e


def _safe_stat(path: Path):
    """Return an fstat of path opened no-follow, insisting on a regular file to
    which the effective user owns. Raises ValueError on any unsafe condition."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise FileNotFoundError(str(path)) from None
    except OSError as e:
        raise ValueError(f"refusing non-regular/symlink path: {path} ({e})") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"refusing non-regular file: {path}")
        if st.st_uid != os.geteuid():
            raise ValueError(f"refusing file not owned by user: {path}")
        return fd, st
    except BaseException:
        os.close(fd)
        raise


def safe_read_file(path: Path, max_bytes: int = MAX_CONFIG_READ_BYTES) -> str:
    """Read a config file with size, regular-file, owner, no-follow and
    safe-parent checks. Raises ValueError/FileNotFoundError on unsafe conditions."""
    if not path.parent.is_dir():
        if not path.exists():
            raise FileNotFoundError(str(path))
    fd, st = _safe_stat(path)
    try:
        if st.st_size > max_bytes:
            raise ValueError(f"refusing oversized config file: {path} ({st.st_size} bytes)")
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            content = f.read(max_bytes)
        if len(content.encode("utf-8")) > max_bytes:
            raise ValueError(f"refusing oversized config file: {path}")
        return content
    except BaseException:
        raise


def _lock_fd(fd: int):
    """Acquire an exclusive advisory lock on the given file descriptor."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as e:
        raise RuntimeError(f"failed to acquire config lock: {e}") from e


def _unlock_fd(fd: int):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as e:
        raise RuntimeError(f"failed to release config lock: {e}") from e


@contextmanager
def file_lock():
    """Acquires an exclusive lock across read-modify-write operations to prevent
    races. Fail-closed: any lock or path error aborts the operation."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_dir_fd = _open_dir_fd(LOCK_PATH)
    lock_ref = os.path.join("/proc/self/fd", str(lock_dir_fd), LOCK_PATH.name)
    try:
        lock_fd = os.open(lock_ref, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        os.close(lock_dir_fd)
        raise RuntimeError(f"cannot open lock file safely: {LOCK_PATH} ({e})") from e
    try:
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid():
            raise RuntimeError(f"lock file is not a user-regular file: {LOCK_PATH}")
        _lock_fd(lock_fd)
    except Exception:
        os.close(lock_fd)
        os.close(lock_dir_fd)
        raise
    try:
        yield
    finally:
        try:
            _unlock_fd(lock_fd)
        finally:
            os.close(lock_fd)
            os.close(lock_dir_fd)


def safe_atomic_write(target_path: Path, content: str) -> bool:
    """Atomically replace target_path with content, refusing symlinks and
    verifying the parent directory descriptor, using rename-by-dirfd so no
    check-then-unlink window exists."""
    try:
        parent_dir_fd = _open_dir_fd(target_path)
        parent_fd = os.dup(parent_dir_fd)
        os.close(parent_dir_fd)
    except ValueError:
        return False
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir="/proc/self/fd/" + str(parent_fd),
            prefix=f"{target_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            replacement_ref = os.path.join("/proc/self/fd", str(parent_fd), os.path.basename(tmp_name))
            os.replace(replacement_ref, os.path.join("/proc/self/fd", str(parent_fd), target_path.name))
            return True
        except Exception:
            try:
                if tmp_name:
                    os.unlink(tmp_name)
            except OSError:
                pass
            return False
    finally:
        os.close(parent_fd)


def run_cmd(cmd) -> tuple:
    """Run a command with a hard timeout, retained-output byte limit and
    process-group cleanup so no child survives a timeout or crash."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=MAX_SUBPROCESS_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            return 1, "", "command timed out"
        code = proc.returncode
        return code, (out or "")[:MAX_SUBPROCESS_OUTPUT], (err or "")[:MAX_SUBPROCESS_OUTPUT]
    except Exception as e:
        return 1, "", str(e)

def validate_float(val, default: float, min_val: float, max_val: float) -> float:
    try:
        v = float(val)
        if not math.isfinite(v):
            return default
        return max(min_val, min(max_val, round(v, 2)))
    except (TypeError, ValueError, OverflowError):
        return default

def validate_int(val, default: int, min_val: int, max_val: int) -> int:
    try:
        v = int(val)
        return max(min_val, min(max_val, v))
    except (TypeError, ValueError, OverflowError):
        return default

def validate_bool(val, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    if isinstance(val, (int, float)):
        return bool(val)
    return default

def validate_accel_profile(val: str, default: str = "adaptive") -> str:
    if str(val) in ("flat", "adaptive", "custom"):
        return str(val)
    return default

def validate_button_mapping(button: str, action: str) -> str:
    default = DEFAULT_BUTTON_MAPPINGS.get(button, "default")
    allowed = ALLOWED_BUTTON_ACTIONS.get(button, set())
    if str(action) in allowed:
        return str(action)
    return default

def run_cmd(cmd):
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except Exception as e:
        return 1, "", str(e)

def get_hypr_option(opt_name):
    code, out, _ = run_cmd(["hyprctl", "getoption", opt_name, "-j"])
    if code != 0 or not out:
        return None
    try:
        data = json.loads(out)
        if "float" in data:
            return data["float"]
        if "int" in data:
            return data["int"]
        if "bool" in data:
            return data["bool"]
        if "str" in data:
            val = data["str"]
            if val == "[[EMPTY]]":
                return ""
            return val
        return None
    except Exception:
        return None

def get_devices():
    code, out, _ = run_cmd(["hyprctl", "devices", "-j"])
    if code != 0 or not out:
        return []
    try:
        data = json.loads(out)
        mice = data.get("mice", [])
        clean_mice = []
        for m in mice:
            name = str(m.get("name", "Unknown Mouse"))
            clean_mice.append({
                "name": name,
                "address": str(m.get("address", "")),
                "defaultSpeed": validate_float(m.get("defaultSpeed"), 0.0, -1.0, 1.0),
                "scrollFactor": validate_float(m.get("scrollFactor"), 1.0, 0.1, 10.0)
            })
        def _mouse_rank(name_l):
            if "mouse" in name_l:
                return 0
            if "keyboard" in name_l or "consumer" in name_l or "virtual" in name_l:
                return 2
            return 1
        clean_mice.sort(key=lambda d: _mouse_rank(d["name"].lower()))
        return clean_mice
    except Exception:
        return []

def get_battery(primary_name=""):
    code, out, _ = run_cmd(["upower", "-e"])
    if code != 0 or not out:
        return None
    dev_tokens = set(re.findall(r"[a-z0-9]+", primary_name.lower()))
    dev_tokens.discard("mouse")
    for hid_path in [ln.strip() for ln in out.splitlines() if "battery_hid" in ln]:
        code, info, _ = run_cmd(["upower", "-i", hid_path])
        if code != 0 or not info:
            continue
        pct_match = re.search(r"percentage:\s*([0-9]*\.?[0-9]+)%", info)
        if not pct_match:
            continue
        state_match = re.search(r"state:\s*(\S+)", info)
        model_match = re.search(r"model:\s*(.+)", info)
        model = model_match.group(1).strip() if model_match else ""
        model_tokens = set(re.findall(r"[a-z0-9]+", model.lower()))
        if dev_tokens and (dev_tokens & model_tokens):
            return {
                "percent": validate_float(pct_match.group(1), 0.0, 0.0, 100.0),
                "state": state_match.group(1) if state_match else "unknown",
                "model": model
            }
    return None


def read_saved_input_settings():
    settings = {}
    if not INPUT_LUA_PATH.exists():
        return settings
    try:
        content = safe_read_file(INPUT_LUA_PATH)
        if START_MARKER in content and END_MARKER in content:
            block = content.split(START_MARKER)[1].split(END_MARKER)[0]
            sens_match = re.search(r"sensitivity\s*=\s*([-+]?[0-9]*\.?[0-9]+)", block)
            if sens_match:
                settings["sensitivity"] = validate_float(sens_match.group(1), 0.0, -1.0, 1.0)
            accel_match = re.search(r'accel_profile\s*=\s*"([^"]+)"', block)
            if accel_match:
                settings["accel_profile"] = validate_accel_profile(accel_match.group(1))
            follow_match = re.search(r"follow_mouse\s*=\s*([0-9]+)", block)
            if follow_match:
                settings["follow_mouse"] = validate_int(follow_match.group(1), 1, 0, 3)
            natural_match = re.search(r"natural_scroll\s*=\s*(true|false)", block)
            if natural_match:
                settings["natural_scroll"] = (natural_match.group(1) == "true")
            left_match = re.search(r"left_handed\s*=\s*(true|false)", block)
            if left_match:
                settings["left_handed"] = (left_match.group(1) == "true")
            scroll_match = re.search(r"scroll_factor\s*=\s*([-+]?[0-9]*\.?[0-9]+)", block)
            if scroll_match:
                settings["scroll_factor"] = validate_float(scroll_match.group(1), 1.0, 0.1, 8.0)
            refocus_match = re.search(r"mouse_refocus\s*=\s*(true|false)", block)
            if refocus_match:
                settings["mouse_refocus"] = (refocus_match.group(1) == "true")
    except Exception:
        pass
    return settings

def read_saved_button_mappings():
    mappings = dict(DEFAULT_BUTTON_MAPPINGS)
    if not BINDINGS_LUA_PATH.exists():
        return mappings
    try:
        content = safe_read_file(BINDINGS_LUA_PATH)
        if BINDINGS_START_MARKER in content and BINDINGS_END_MARKER in content:
            block = content.split(BINDINGS_START_MARKER)[1].split(BINDINGS_END_MARKER)[0]
            if 'o.bind("mouse:275", "Previous workspace"' in block:
                mappings["side_back"] = "prev_workspace"
            elif 'o.bind("mouse:275", "Omarchy menu"' in block:
                mappings["side_back"] = "menu"
            elif 'o.bind("mouse:275", "Previous window"' in block:
                mappings["side_back"] = "prev_window"

            if 'o.bind("mouse:276", "Next workspace"' in block:
                mappings["side_forward"] = "next_workspace"
            elif 'o.bind("mouse:276", "Terminal"' in block:
                mappings["side_forward"] = "terminal"
            elif 'o.bind("mouse:276", "Next window"' in block:
                mappings["side_forward"] = "next_window"

            if 'o.bind("mouse:274", "Close active window"' in block:
                mappings["middle_click"] = "close_window"
            elif 'o.bind("mouse:274", "Toggle floating"' in block:
                mappings["middle_click"] = "toggle_floating"
            elif 'o.bind("mouse:274", "Toggle fullscreen"' in block:
                mappings["middle_click"] = "toggle_fullscreen"

            if 'hl.unbind("SUPER + mouse:272")' in block:
                mappings["super_left"] = "disabled"
            if 'hl.unbind("SUPER + mouse:273")' in block:
                mappings["super_right"] = "disabled"
            if 'hl.unbind("SUPER + mouse_down")' in block:
                mappings["super_wheel"] = "disabled"
    except Exception:
        pass
    return mappings

def get_current_status():
    saved_input = read_saved_input_settings()
    sensitivity = saved_input.get("sensitivity", get_hypr_option("input:sensitivity"))
    sensitivity = validate_float(sensitivity, 0.0, -1.0, 1.0)
    accel_profile = saved_input.get("accel_profile", get_hypr_option("input:accel_profile"))
    accel_profile = validate_accel_profile(accel_profile, "adaptive")
    follow_mouse = saved_input.get("follow_mouse", get_hypr_option("input:follow_mouse"))
    follow_mouse = validate_int(follow_mouse, 1, 0, 3)
    natural_scroll = saved_input.get("natural_scroll", get_hypr_option("input:natural_scroll"))
    natural_scroll = validate_bool(natural_scroll, False)
    left_handed = saved_input.get("left_handed", get_hypr_option("input:left_handed"))
    left_handed = validate_bool(left_handed, False)
    scroll_factor = saved_input.get("scroll_factor", get_hypr_option("input:scroll_factor"))
    scroll_factor = validate_float(scroll_factor, 1.0, 0.1, 8.0)
    mouse_refocus = saved_input.get("mouse_refocus", get_hypr_option("input:mouse_refocus"))
    mouse_refocus = validate_bool(mouse_refocus, True)
    devices = get_devices()
    button_mappings = read_saved_button_mappings()
    return {
        "devices": devices,
        "primaryDevice": devices[0]["name"] if devices else "Standard Mouse",
        "battery": get_battery(devices[0]["name"] if devices else ""),
        "sensitivity": sensitivity,
        "accel_profile": accel_profile,
        "is_flat": (accel_profile == "flat"),
        "follow_mouse": follow_mouse,
        "natural_scroll": natural_scroll,
        "left_handed": left_handed,
        "scroll_factor": scroll_factor,
        "mouse_refocus": mouse_refocus,
        "button_mappings": button_mappings
    }

def apply_hypr_eval(settings) -> bool:
    sensitivity = validate_float(settings.get("sensitivity"), 0.0, -1.0, 1.0)
    accel = validate_accel_profile(settings.get("accel_profile", "adaptive"))
    accel_lua = f'"{accel}"' if accel in ("flat", "adaptive", "custom") else '""'
    follow_mouse = validate_int(settings.get("follow_mouse"), 1, 0, 3)
    natural_scroll = "true" if validate_bool(settings.get("natural_scroll"), False) else "false"
    left_handed = "true" if validate_bool(settings.get("left_handed"), False) else "false"
    scroll_factor = validate_float(settings.get("scroll_factor"), 1.0, 0.1, 8.0)
    mouse_refocus = "true" if validate_bool(settings.get("mouse_refocus"), True) else "false"
    lua_cmd = (
        f"hl.config({{ input = {{ "
        f"sensitivity = {sensitivity:.2f}, "
        f"accel_profile = {accel_lua}, "
        f"follow_mouse = {follow_mouse}, "
        f"natural_scroll = {natural_scroll}, "
        f"left_handed = {left_handed}, "
        f"scroll_factor = {scroll_factor:.2f}, "
        f"mouse_refocus = {mouse_refocus} "
        f"}} }})"
    )
    code, out, err = run_cmd(["hyprctl", "eval", lua_cmd])
    return code == 0

def persist_to_input_lua(settings) -> bool:
    if not INPUT_LUA_PATH.exists():
        original_content = "-- User input overrides\n"
    else:
        try:
            original_content = safe_read_file(INPUT_LUA_PATH)
        except Exception:
            original_content = ""
    sensitivity = validate_float(settings.get("sensitivity"), 0.0, -1.0, 1.0)
    accel = validate_accel_profile(settings.get("accel_profile", "adaptive"))
    accel_lua = f'"{accel}"' if accel in ("flat", "adaptive", "custom") else '""'
    follow_mouse = validate_int(settings.get("follow_mouse"), 1, 0, 3)
    natural_scroll = "true" if validate_bool(settings.get("natural_scroll"), False) else "false"
    left_handed = "true" if validate_bool(settings.get("left_handed"), False) else "false"
    scroll_factor = validate_float(settings.get("scroll_factor"), 1.0, 0.1, 8.0)
    mouse_refocus = "true" if validate_bool(settings.get("mouse_refocus"), True) else "false"
    new_block = (
        f"{START_MARKER}\n"
        f"hl.config({{\n"
        f"  input = {{\n"
        f"    sensitivity = {sensitivity:.2f},\n"
        f"    accel_profile = {accel_lua},\n"
        f"    follow_mouse = {follow_mouse},\n"
        f"    natural_scroll = {natural_scroll},\n"
        f"    left_handed = {left_handed},\n"
        f"    scroll_factor = {scroll_factor:.2f},\n"
        f"    mouse_refocus = {mouse_refocus},\n"
        f"  }},\n"
        f"}})\n"
        f"{END_MARKER}"
    )
    if START_MARKER in original_content and END_MARKER in original_content:
        pattern = re.compile(re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL)
        updated_content = pattern.sub(new_block, original_content)
    else:
        updated_content = original_content.rstrip() + "\n\n" + new_block + "\n"
    if updated_content == original_content:
        return False
    if not safe_atomic_write(INPUT_LUA_PATH, updated_content):
        raise RuntimeError(f"failed to write config safely: {INPUT_LUA_PATH}")
    return True

def persist_to_bindings_lua(mappings) -> bool:
    if not BINDINGS_LUA_PATH.exists():
        original_content = "-- User keybinding overrides\n"
    else:
        try:
            original_content = safe_read_file(BINDINGS_LUA_PATH)
        except Exception:
            original_content = ""
    lines = []
    lines.append(BINDINGS_START_MARKER)
    sb = validate_button_mapping("side_back", mappings.get("side_back", "default"))
    if sb == "prev_workspace":
        lines.append('hl.unbind("mouse:275")')
        lines.append('o.bind("mouse:275", "Previous workspace", hl.dsp.focus({ workspace = "e-1" }), { mouse = true })')
    elif sb == "menu":
        lines.append('hl.unbind("mouse:275")')
        lines.append('o.bind("mouse:275", "Omarchy menu", "omarchy-menu toggle root", { mouse = true })')
    elif sb == "prev_window":
        lines.append('hl.unbind("mouse:275")')
        lines.append('o.bind("mouse:275", "Previous window", hl.dsp.focus({ direction = "l" }), { mouse = true })')
    sf = validate_button_mapping("side_forward", mappings.get("side_forward", "default"))
    if sf == "next_workspace":
        lines.append('hl.unbind("mouse:276")')
        lines.append('o.bind("mouse:276", "Next workspace", hl.dsp.focus({ workspace = "e+1" }), { mouse = true })')
    elif sf == "terminal":
        lines.append('hl.unbind("mouse:276")')
        lines.append('o.bind("mouse:276", "Terminal", { launch = "ghostty" }, { mouse = true })')
    elif sf == "next_window":
        lines.append('hl.unbind("mouse:276")')
        lines.append('o.bind("mouse:276", "Next window", hl.dsp.focus({ direction = "r" }), { mouse = true })')
    mc = validate_button_mapping("middle_click", mappings.get("middle_click", "default"))
    if mc == "close_window":
        lines.append('hl.unbind("mouse:274")')
        lines.append('o.bind("mouse:274", "Close active window", hl.dsp.window.kill(), { mouse = true })')
    elif mc == "toggle_floating":
        lines.append('hl.unbind("mouse:274")')
        lines.append('o.bind("mouse:274", "Toggle floating", hl.dsp.window.toggle_floating(), { mouse = true })')
    elif mc == "toggle_fullscreen":
        lines.append('hl.unbind("mouse:274")')
        lines.append('o.bind("mouse:274", "Toggle fullscreen", hl.dsp.window.fullscreen(), { mouse = true })')
    sl = validate_button_mapping("super_left", mappings.get("super_left", "move_window"))
    if sl == "disabled":
        lines.append('hl.unbind("SUPER + mouse:272")')
    sr = validate_button_mapping("super_right", mappings.get("super_right", "resize_window"))
    if sr == "disabled":
        lines.append('hl.unbind("SUPER + mouse:273")')
    sw = validate_button_mapping("super_wheel", mappings.get("super_wheel", "workspace_scroll"))
    if sw == "disabled":
        lines.append('hl.unbind("SUPER + mouse_down")')
        lines.append('hl.unbind("SUPER + mouse_up")')
    lines.append(BINDINGS_END_MARKER)
    new_block = "\n".join(lines)
    if BINDINGS_START_MARKER in original_content and BINDINGS_END_MARKER in original_content:
        pattern = re.compile(re.escape(BINDINGS_START_MARKER) + r".*?" + re.escape(BINDINGS_END_MARKER), re.DOTALL)
        updated_content = pattern.sub(new_block, original_content)
    else:
        updated_content = original_content.rstrip() + "\n\n" + new_block + "\n"
    if updated_content == original_content:
        return False
    if not safe_atomic_write(BINDINGS_LUA_PATH, updated_content):
        raise RuntimeError(f"failed to write config safely: {BINDINGS_LUA_PATH}")
    return True

def notify_user(title, message, icon="input-mouse"):
    run_cmd(["notify-send", "-a", "Omarchy", "-i", icon, str(title), str(message)])

def main():
    parser = argparse.ArgumentParser(description="Omarchy Mouse Control Helper")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Get JSON status of mouse settings")
    apply_parser = subparsers.add_parser("apply", help="Apply and persist settings")
    apply_parser.add_argument("--json-data", type=str, help="JSON string with settings")
    subparsers.add_parser("toggle-accel", help="Toggle precision (flat) vs desktop (adaptive) acceleration")
    subparsers.add_parser("toggle-natural-scroll", help="Toggle natural scroll direction")
    subparsers.add_parser("reset-defaults", help="Reset mouse settings to default")
    simulate_parser = subparsers.add_parser("simulate-button", help="Emit a synthetic mouse button press via ydotool")
    simulate_parser.add_argument("--button", type=str, required=True, choices=sorted(SIMULATE_BUTTON_CODES.keys()))
    args = parser.parse_args()

    if args.command == "status" or not args.command:
        state = get_current_status()
        print(json.dumps(state, indent=2))
        return

    if args.command == "simulate-button":
        code = SIMULATE_BUTTON_CODES[args.button]
        if shutil.which("ydotool") is None:
            print(json.dumps({"success": False, "error": "ydotool not installed"}))
            return
        ret, out, err_out = run_cmd(["ydotool", "click", "-D", "50", code])
        if ret == 0:
            print(json.dumps({"success": True, "button": args.button}))
        else:
            err_l = (err_out + "\n" + out).lower()
            if "socket" in err_l or "connect" in err_l or "ydotoold" in err_l or "failed to open" in err_l:
                print(json.dumps({"success": False, "error": "ydotoold unavailable"}))
            else:
                print(json.dumps({"success": False, "error": err_out.strip() or f"ydotool exited {ret}"}))
        return

    with file_lock():
        if args.command == "toggle-accel":
            current = get_current_status()
            new_profile = "adaptive" if current["accel_profile"] == "flat" else "flat"
            current["accel_profile"] = new_profile
            current["is_flat"] = (new_profile == "flat")
            eval_ok = apply_hypr_eval(current)
            persist_to_input_lua(current)
            label = "Precision (Raw 1:1)" if new_profile == "flat" else "Desktop (Dynamic)"
            notify_user("Mouse Acceleration", f"Switched to {label}")
            print(json.dumps({"success": eval_ok, "accel_profile": new_profile, "label": label}))
            return

        if args.command == "toggle-natural-scroll":
            current = get_current_status()
            new_val = not current["natural_scroll"]
            current["natural_scroll"] = new_val
            eval_ok = apply_hypr_eval(current)
            persist_to_input_lua(current)
            label = "Natural (Mobile)" if new_val else "Traditional (Classic PC)"
            notify_user("Mouse Scrolling", f"Scroll direction set to {label}")
            print(json.dumps({"success": eval_ok, "natural_scroll": new_val, "label": label}))
            return

        if args.command == "reset-defaults":
            defaults = {
                "sensitivity": 0.0,
                "accel_profile": "adaptive",
                "follow_mouse": 1,
                "natural_scroll": False,
                "left_handed": False,
                "scroll_factor": 1.0,
                "mouse_refocus": True,
            }
            eval_ok = apply_hypr_eval(defaults)
            input_changed = persist_to_input_lua(defaults)
            bindings_changed = persist_to_bindings_lua(DEFAULT_BUTTON_MAPPINGS)
            if bindings_changed or input_changed:
                run_cmd(["hyprctl", "reload"])
            notify_user("Mouse Settings", "Reset to Omarchy defaults")
            print(json.dumps({"success": eval_ok, "status": get_current_status()}))
            return

        if args.command == "apply":
            current = get_current_status()
            has_input_change = False
            has_bindings_change = False
            if args.json_data:
                if len(args.json_data.encode("utf-8")) > MAX_JSON_INPUT_BYTES:
                    print(json.dumps({"success": False, "error": "JSON payload too large"}))
                    return
                try:
                    payload = json.loads(args.json_data)
                    if not isinstance(payload, dict):
                        print(json.dumps({"success": False, "error": "Payload must be a JSON object"}))
                        return
                    if "sensitivity" in payload:
                        current["sensitivity"] = validate_float(payload["sensitivity"], current["sensitivity"], -1.0, 1.0)
                        has_input_change = True
                    if "accel_profile" in payload:
                        current["accel_profile"] = validate_accel_profile(payload["accel_profile"], current["accel_profile"])
                        current["is_flat"] = (current["accel_profile"] == "flat")
                        has_input_change = True
                    if "follow_mouse" in payload:
                        current["follow_mouse"] = validate_int(payload["follow_mouse"], current["follow_mouse"], 0, 3)
                        has_input_change = True
                    if "natural_scroll" in payload:
                        current["natural_scroll"] = validate_bool(payload["natural_scroll"], current["natural_scroll"])
                        has_input_change = True
                    if "left_handed" in payload:
                        current["left_handed"] = validate_bool(payload["left_handed"], current["left_handed"])
                        has_input_change = True
                    if "scroll_factor" in payload:
                        current["scroll_factor"] = validate_float(payload["scroll_factor"], current["scroll_factor"], 0.1, 8.0)
                        has_input_change = True
                    if "mouse_refocus" in payload:
                        current["mouse_refocus"] = validate_bool(payload["mouse_refocus"], current["mouse_refocus"])
                        has_input_change = True
                    if "button_mappings" in payload and isinstance(payload["button_mappings"], dict):
                        bm = payload["button_mappings"]
                        if len(bm) > 64:
                            print(json.dumps({"success": False, "error": "button_mappings collection too large"}))
                            return
                        for btn, act in bm.items():
                            if btn in ALLOWED_BUTTON_ACTIONS and isinstance(act, str) and len(act) <= 64:
                                current["button_mappings"][btn] = validate_button_mapping(btn, act)
                        has_bindings_change = True
                except Exception as e:
                    print(json.dumps({"success": False, "error": f"Invalid JSON: {e}"}))
                    return
            eval_ok = True
            if has_input_change:
                eval_ok = apply_hypr_eval(current)
                persist_to_input_lua(current)
            if has_bindings_change:
                bindings_changed = persist_to_bindings_lua(current.get("button_mappings", {}))
                if bindings_changed:
                    run_cmd(["hyprctl", "reload"])
            _, err_out, _ = run_cmd(["hyprctl", "configerrors"])
            print(json.dumps({
                "success": eval_ok and not err_out,
                "error": err_out if err_out else None,
                "status": get_current_status()
            }))
            return

if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)