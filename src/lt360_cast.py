"""Second-monitor cast: a virtual Hyprland output (or any existing Wayland output) shown live on the panel.

The daemon does the capture itself (lt360_media.CastReel: wf-recorder -> raw frames, newest frame wins, so
latency stays at about one frame). This module only handles the compositor side: creating/removing the
virtual output, parking it on its own workspace, restoring focus and cursor, and launching apps on it.
Shared by `lt360ctl cast ...` and lt360-gui. `call` is any callable that sends one request to the daemon
and returns its response dict (it may raise OSError).
"""
import json
import os
import shlex
import subprocess
import time

STATE_PATH = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                          "lt360", "cast.json")
PANEL_SIZE = (854, 480)  # the LT360 VISION panel in horizontal mode (480x854 in vertical mode)
DEFAULT_ZOOM = 1.0       # UI zoom: 1 virtual pixel = 1 panel pixel; 1.25/1.5/2 = bigger icons and text, less room
DEFAULT_WS = 10


class CastError(Exception):
    pass


def _hypr(*args, timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CastError("hyprctl not found: creating a virtual monitor needs Hyprland (or pass an existing output)")
    except subprocess.TimeoutExpired:
        raise CastError(f"hyprctl {' '.join(args)} timed out")
    if r.returncode != 0:
        raise CastError(f"hyprctl {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def _dispatch(lua: str):
    _hypr("dispatch", lua)


def _monitors(include_inactive: bool = False) -> list[dict]:
    return json.loads(_hypr("monitors", *(["all"] if include_inactive else []), "-j"))


def list_outputs() -> list[str]:
    """Real outputs (not virtual headless/nested ones) that can be mirrored on the panel."""
    return [m["name"] for m in _monitors() if not m["name"].startswith(("HEADLESS-", "WL-", "X11-"))]


def _cursor() -> tuple[int, int]:
    x, y = _hypr("cursorpos").replace(",", " ").split()[:2]
    return int(float(x)), int(float(y))


def _move_cursor(x: int, y: int):
    _dispatch(f"hl.dsp.cursor.move({{ x = {int(x)}, y = {int(y)} }})")


def _wait_for(cond, timeout: float = 1.5, step: float = 0.03) -> bool:
    """Poll `cond()` until it is true (compositor state changes land a moment after the command returns).
    Replaces fixed sleeps: as fast as the compositor allows, never racing it."""
    end = time.monotonic() + timeout
    while True:
        try:
            if cond():
                return True
        except CastError:
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(step)


def _settle(timeout: float = 1.5, quiet: float = 0.25):
    """Return once the monitor list and focus have stopped changing for `quiet` seconds (hotplug blips done)."""
    end, last, since = time.monotonic() + timeout, None, time.monotonic()
    while time.monotonic() < end:
        try:
            snap = [(m["name"], m.get("focused"), m["activeWorkspace"]["id"]) for m in _monitors(True)]
        except CastError:
            return
        now = time.monotonic()
        if snap != last:
            last, since = snap, now
        elif now - since >= quiet:
            return
        time.sleep(0.05)


def _lua_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)  # JSON escapes (\" \\ \n) are valid Lua


def load_state() -> dict | None:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def _teardown(state: dict | None):
    """Remove the virtual output we created (its windows move to the remaining monitor) and put the cursor back:
    removing an output warps it."""
    if state and state.get("created") and state.get("output"):
        try:
            _hypr("output", "remove", state["output"])
            if state.get("cursor"):
                _wait_for(lambda: state["output"] not in [m["name"] for m in _monitors(True)], 1.0)
                time.sleep(0.1)
                _move_cursor(*state["cursor"])
        except CastError:
            pass
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass


def virtual_mode(panel: tuple[int, int], zoom: float) -> tuple[str, int]:
    """(mode, scale) for a virtual monitor whose UI looks `zoom` times bigger on the panel.
    Hyprland snaps any scale that does not divide the mode into whole pixels (854x480 only allows 1 and 2), so
    fractional zoom is done differently: the monitor's logical size is panel/zoom, rendered at a clean 2x scale
    (mode = 2 x logical) and the capture downsamples that smoothly to the panel."""
    pw, ph = panel
    if abs(zoom - 1.0) < 1e-6:
        return f"{pw}x{ph}", 1
    return f"{2 * round(pw / zoom)}x{2 * round(ph / zoom)}", 2


def _is_virtual(name: str) -> bool:
    return name.startswith(("HEADLESS-", "WL-", "X11-"))


def _bind_workspaces(virtual: str | None, ws: int):
    """Pin workspaces 1-10 to monitors with workspace rules. Without this a monitor that gets focus (the new
    virtual one, or any workspace shortcut pressed while the mouse is on it) pulls YOUR workspaces onto itself.
    Workspaces keep the real monitor that shows them now (unused ones go to the focused real monitor); `ws` goes to
    the virtual monitor once it exists (`virtual`). Hyprland cannot clear a rule at runtime (hyprctl reload does);
    for a single real monitor the rules are harmless."""
    mons = _monitors()
    real = [m for m in mons if not _is_virtual(m["name"])]
    if not real:
        return
    default = next((m["name"] for m in real if m.get("focused")), real[0]["name"])
    names = {m["name"] for m in real}
    owner = {}
    for w in json.loads(_hypr("workspaces", "-j")):
        if w.get("monitor") in names and w["id"] > 0:
            owner[w["id"]] = w["monitor"]
    for i in range(1, 11):
        if i == ws:
            continue
        _hypr("eval", f'hl.workspace_rule({{ workspace = "{i}", monitor = "{owner.get(i, default)}" }})')
    if virtual:
        _hypr("eval", f'hl.workspace_rule({{ workspace = "{int(ws)}", monitor = "{virtual}", default = true }})')


def _active_on_real(ws: int) -> str | None:
    """Name of the real monitor currently SHOWING workspace `ws`, if any."""
    return next((m["name"] for m in _monitors()
                 if not _is_virtual(m["name"]) and m["activeWorkspace"]["id"] == int(ws)), None)


def _check_workspace_free(ws: int):
    """The panel can only take a workspace that is empty (or does not exist yet) and not on screen on a real
    monitor. Hyprland will not pull an occupied workspace over by focus, and `workspace.move` silently moves the
    FOCUSED workspace if given a wrong key, so occupied ones are refused instead of risking your windows."""
    mon = _active_on_real(ws)
    if mon:
        raise CastError(f"workspace {ws} is on screen on {mon} right now; switch that screen to another workspace "
                        f"first, or pick a different one")
    for w in json.loads(_hypr("workspaces", "-j")):
        if w["id"] == int(ws) and w.get("windows", 0) > 0 and not _is_virtual(w.get("monitor", "")):
            raise CastError(f"workspace {ws} has {w['windows']} window(s) on {w.get('monitor')}; pick an empty "
                            f"workspace (you can send windows to the panel's workspace afterwards)")


def _focused() -> dict | None:
    return next((m for m in _monitors() if m.get("focused")), None)


def _park_on_workspace(output: str, ws: int, orig: str | None, orig_ws: int | None) -> bool:
    """Make `output` show workspace `ws`, then hand focus back to `orig` showing `orig_ws` again. Every step is
    verified (polled until the compositor has really done it) and retried: a focus step that races the compositor
    would otherwise move YOUR monitor onto `ws`. Returns whether the output ended up on `ws`."""
    ok = False
    for _ in range(4):
        _dispatch(f'hl.dsp.focus({{ monitor = "{output}" }})')
        if not _wait_for(lambda: (_focused() or {}).get("name") == output, 1.0):
            continue
        _dispatch(f"hl.dsp.focus({{ workspace = {int(ws)} }})")
        if _wait_for(lambda: any(m["name"] == output and m["activeWorkspace"]["id"] == int(ws) for m in _monitors()), 1.0):
            ok = True
            break
    def restored() -> bool:
        now = _focused()
        return not orig or bool(now and now["name"] == orig and (orig_ws is None or now["activeWorkspace"]["id"] == orig_ws))
    for _ in range(3):
        if orig:
            _dispatch(f'hl.dsp.focus({{ monitor = "{orig}" }})')
            _wait_for(lambda: (_focused() or {}).get("name") == orig, 1.0)
        if orig_ws is not None:
            _dispatch(f"hl.dsp.focus({{ workspace = {int(orig_ws)} }})")
        if _wait_for(restored, 1.0):
            break
    return ok


def workspace_info() -> dict:
    """What the workspace picker shows: {"workspaces": {id: {"windows": n, "monitor": name, "shown": bool}},
    "real": [monitor names], "virtual": [monitor names]}. Raises CastError without hyprctl."""
    mons = _monitors(True)
    shown = {m["activeWorkspace"]["id"] for m in mons if m.get("activeWorkspace")}
    out = {}
    for w in json.loads(_hypr("workspaces", "-j")):
        if w["id"] > 0:
            out[w["id"]] = {"windows": w.get("windows", 0), "monitor": w.get("monitor", ""), "shown": w["id"] in shown}
    return {"workspaces": out, "real": [m["name"] for m in mons if not _is_virtual(m["name"])],
            "virtual": [m["name"] for m in mons if _is_virtual(m["name"])]}


def set_workspace(call, ws: int) -> int:
    """Lock the (virtual) cast monitor to workspace `ws` while it is running: that workspace, with its windows,
    now lives on the panel, and the one it held before goes back to your real monitor. Raises CastError."""
    ws = int(ws)
    if not 1 <= ws <= 99:
        raise CastError("workspace must be 1-99")
    state = load_state()
    if not state or not state.get("created") or not state.get("output"):
        raise CastError("not casting a virtual monitor (a mirrored real output has no workspace of its own)")
    if ws == state.get("ws"):
        return ws
    _check_workspace_free(ws)
    output = state["output"]
    focused = next((m for m in _monitors() if m.get("focused") and m["name"] != output), None) \
        or next((m for m in _monitors() if not _is_virtual(m["name"])), None)
    orig = focused["name"] if focused else None
    orig_ws = focused["activeWorkspace"]["id"] if focused else None
    cursor = _cursor()
    _bind_workspaces(output, ws)
    if not _park_on_workspace(output, ws, orig, orig_ws):
        raise CastError(f"Hyprland would not put workspace {ws} on the panel")
    _move_cursor(*cursor)
    state["ws"] = ws
    _save_state(state)
    return ws


def out_of_reach_position(monitors: list[dict]) -> str:
    """A layout position that touches no other monitor (diagonally past the bottom-right corner, with a gap), so
    the mouse can never wander onto the virtual monitor: a stray move there would hand it your focus, and your
    next workspace shortcut would then be applied to the pump instead of your screen."""
    real = [m for m in monitors if not m["name"].startswith(("HEADLESS-", "WL-", "X11-"))] or monitors
    x = max(int(m["x"] + m["width"] / (m.get("scale") or 1)) for m in real) + 200
    y = max(int(m["y"] + m["height"] / (m.get("scale") or 1)) for m in real) + 200
    return f"{x}x{y}"


def reach_position(monitors: list[dict]) -> str:
    """Right next to the rightmost real screen (top aligned), so the mouse can cross onto the virtual monitor."""
    real = [m for m in monitors if not _is_virtual(m["name"])] or monitors
    edge = max(real, key=lambda m: m["x"] + m["width"] / (m.get("scale") or 1))
    return f"{int(edge['x'] + edge['width'] / (edge.get('scale') or 1))}x{int(edge['y'])}"


def set_mouse_reach(reachable: bool) -> bool:
    """Live: let the mouse cross onto the virtual monitor (reachable) or take it out of reach. Only moves the
    monitor in the layout, the workspace and its windows stay where they are. If the cursor (or focus) is on the
    virtual monitor when it becomes unreachable, they are handed back to a real screen first."""
    state = load_state()
    if not state or not state.get("created") or not state.get("output"):
        raise CastError("not casting a virtual monitor (a mirrored real output is where it is)")
    output = state["output"]
    mons = _monitors()
    me = next((m for m in mons if m["name"] == output), None)
    if me is None:
        raise CastError(f"{output} is gone")
    others = [m for m in mons if m["name"] != output]
    mode = state.get("mode") or f'{me["width"]}x{me["height"]}'
    scale = state.get("scale") or me.get("scale") or 1
    if not reachable:
        real = next((m for m in others if m.get("focused")), None) or (others[0] if others else None)
        cx, cy = _cursor()
        on_me = me["x"] <= cx < me["x"] + me["width"] / (me.get("scale") or 1) and \
            me["y"] <= cy < me["y"] + me["height"] / (me.get("scale") or 1)
        if real and (on_me or me.get("focused")):
            _dispatch(f'hl.dsp.focus({{ monitor = "{real["name"]}" }})')
            _wait_for(lambda: (_focused() or {}).get("name") == real["name"], 1.0)
            _move_cursor(int(real["x"] + real["width"] / (real.get("scale") or 1) / 2),
                         int(real["y"] + real["height"] / (real.get("scale") or 1) / 2))
    position = reach_position(others) if reachable else out_of_reach_position(others)
    _hypr("eval", f'hl.monitor({{ output = "{output}", mode = "{mode}@60", position = "{position}", scale = {scale} }})')
    want = tuple(int(v) for v in position.split("x"))
    if not _wait_for(lambda: any(m["name"] == output and (m["x"], m["y"]) == want for m in _monitors()), 1.5):
        raise CastError("Hyprland did not move the virtual monitor")
    state["reachable"] = bool(reachable)
    _save_state(state)
    return bool(reachable)


def start(call, output: str | None = None, zoom: float = DEFAULT_ZOOM, ws: int = DEFAULT_WS,
          reachable: bool = False) -> str:
    """Start casting. Without `output`, creates a virtual Hyprland monitor on workspace `ws` whose UI is `zoom`
    times the size of the panel's pixels. It sits out of the mouse's reach unless `reachable` (then it is placed
    right of your screens so windows can be dragged onto it). Returns the output name. Raises CastError."""
    if not 1.0 <= float(zoom) <= 3.0:
        raise CastError("zoom must be between 1 and 3")
    if not 1 <= int(ws) <= 99:
        raise CastError("workspace must be 1-99")
    if load_state():
        stop(call)  # a previous cast (maybe crashed): start from a clean slate
        _settle()   # let the compositor finish after removing its output, or the focus steps below race it
    if not output:
        _check_workspace_free(int(ws))

    state = {"output": output, "created": False, "ws": int(ws), "cursor": None}
    if output:
        try:
            if output not in [m["name"] for m in _monitors()]:
                raise CastError(f"no such output: {output}")
        except CastError as e:
            if "no such output" in str(e):
                raise  # (any other hyprctl failure: not Hyprland, trust the name and let the daemon check it)
    else:
        _bind_workspaces(None, int(ws))   # before the output exists, so it cannot grab one of yours
        before = {m["name"] for m in _monitors(True)}
        focused = next((m for m in _monitors() if m.get("focused")), None)
        orig = focused["name"] if focused else None
        orig_ws = focused["activeWorkspace"]["id"] if focused else None
        state["cursor"] = list(_cursor())
        _hypr("output", "create", "headless")
        new = set()
        for _ in range(20):
            time.sleep(0.1)
            new = {m["name"] for m in _monitors(True)} - before
            if new:
                break
        if not new:
            raise CastError("could not create a virtual output")
        output = state["output"] = sorted(new)[0]
        state["created"] = True
        _save_state(state)  # from here on `stop` can clean up
        try:
            panel = PANEL_SIZE
            try:
                if call({"action": "get_status"}).get("status", {}).get("mode") == "vertical":
                    panel = PANEL_SIZE[::-1]
            except OSError:
                pass
            mode, scale = virtual_mode(panel, float(zoom))
            others = [m for m in _monitors() if m["name"] != output]
            position = reach_position(others) if reachable else out_of_reach_position(others)
            state.update(mode=mode, scale=scale, reachable=bool(reachable))
            _hypr("eval", f'hl.monitor({{ output = "{output}", mode = "{mode}@60", position = "{position}", scale = {scale} }})')
            for _ in range(30):  # wait until the new output really has the requested mode
                time.sleep(0.1)
                if any(m["name"] == output and f'{m["width"]}x{m["height"]}' == mode for m in _monitors()):
                    break
            _settle()
            _bind_workspaces(output, int(ws))
            if not _park_on_workspace(output, int(ws), orig, orig_ws):
                raise CastError(f"Hyprland would not put workspace {int(ws)} on the panel")
            _move_cursor(*state["cursor"])
        except CastError:
            _teardown(state)
            raise
    _save_state(state)
    try:
        resp = call({"action": "set_cast", "source": output})
    except OSError as e:
        _teardown(state)
        raise CastError(f"cannot reach lt360d: {e}")
    if not resp.get("ok"):
        _teardown(state)
        raise CastError(resp.get("error", "lt360d refused the cast"))
    return output


def stop(call) -> bool:
    """Stop casting: the daemon returns to the user's media, the virtual output (if we made one) goes away."""
    state = load_state()
    try:
        call({"action": "stop_cast"})
    except OSError:
        pass  # daemon not running: nothing to stop on its side
    _teardown(state)
    return state is not None


def status(call) -> dict:
    state = load_state()
    info = {"active": False, "output": None, "created": False, "fps": 0.0, "error": "", "ws": (state or {}).get("ws"),
            "reachable": (state or {}).get("reachable")}
    try:
        resp = call({"action": "get_status"})
    except OSError:
        return info
    st = resp.get("status", {})
    if st.get("cast"):
        info.update(active=True, output=st["cast"], created=bool((state or {}).get("created")),
                    fps=st.get("stream_fps", 0.0), error=st.get("cast_error", ""))
    return info


def run(command: list[str], ws: int | None = None):
    """Launch a program on the cast workspace without switching to it."""
    state = load_state()
    if not state:
        raise CastError("not casting")
    ws = ws or state.get("ws", DEFAULT_WS)
    _dispatch(f'hl.dsp.exec_cmd({_lua_string(shlex.join(command))}, {{workspace = "{int(ws)} silent"}})')
