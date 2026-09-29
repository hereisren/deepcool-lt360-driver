"""User-editable declarative overlay + telemetry (~/.config/deepcool-lt360/customize.json).

The file has two optional blocks:

  custom_sensors  named telemetry sources ("sysfs" file reads, "command" shell polls)
  elements        widgets (rect/box, text, bar, line, ring, sparkline, image) drawn in order on the canvas

`CustomManager` owns the file: it seeds a default on first run, watches its mtime
(`poll()`, throttled to 1 Hz) and, on a syntax/shape error, keeps the last valid layout
so a typo never blanks the screen. It also keeps a 60-sample rolling history of the numeric
sensors for sparklines. Rendering is a pure function of (layout, sensor data, history).

The helpers at the bottom (element_bbox, move_element, format_layout) back the GUI's visual HUD editor.
"""
import collections
import json
import logging
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("lt360d.custom")

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "deepcool-lt360")
CUSTOM_PATH = os.path.join(CONFIG_DIR, "customize.json")
CHECK_INTERVAL = 0.9  # just under 1 s so loop jitter never skips a whole tick
MIN_SENSOR_INTERVAL = 0.5
MAX_COMMAND_TIMEOUT = 3.0
MAX_SYSFS_READ = 4096  # bytes; sysfs values are tiny, and a mistyped /dev path can't eat RAM
MAX_TEXT_LEN = 200
HISTORY_SAMPLES = 60          # sparkline window: one sample per second
HISTORY_KEYS = ("cpu_temp", "gpu_temp", "cpu_load", "gpu_load", "gpu_power", "ram_percent")
SUPERSAMPLE = 4               # rings/sparklines are drawn 4x and downsampled for smooth edges
MAX_IMAGE_CACHE = 32

BUILTIN_KEYS = {
    "cpu_temp", "gpu_temp", "cpu_load", "gpu_load", "cpu_freq", "cpu_freq_ghz", "gpu_power", "gpu_wattage",
    "gpu_power_str", "gpu_wattage_str", "gpu_clock", "ram_percent", "ram_used", "ram_total", "time", "date",
    "temp_unit",
}
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

DEFAULT_LAYOUT = {
    "_comment": [
        "LT360 custom overlay. Edit and save: the daemon reloads this file within a second.",
        "A JSON typo is logged and the last valid layout stays on screen.",
        "Canvas is 854x480 (horizontal) or 480x854 (vertical, use 'elements_vertical').",
        "Element types: rect, text, bar, line, ring (arc gauge), sparkline (60 s graph), image (PNG sticker).",
        "Tip: lt360-gui > CUSTOM HUD > 'Edit HUD Layout' lets you drag elements on the live preview.",
        "Text variables: {cpu_temp} {gpu_temp} {cpu_load} {gpu_load} {cpu_freq} {cpu_freq_ghz} {gpu_power}",
        "  {gpu_wattage} {gpu_power_str} {gpu_clock} {ram_percent} {ram_used} {ram_total} {time} {date}",
        "  {temp_unit} + your custom_sensors. GPU values come from the discrete card on iGPU + dGPU systems.",
        "More examples: examples/overlays/*.json  (or: lt360ctl customize --list)",
    ],
    "custom_sensors": {
        "_comment": "Example (remove the underscore to enable): a sysfs source scaled from millidegrees.",
        "_nvme_temp": {"type": "sysfs", "path": "/sys/class/nvme/nvme0/hwmon1/temp1_input",
                       "scale": 0.001, "unit": "°C"},
    },
    "elements": [
        {"type": "rect", "x": 0, "y": 392, "w": 854, "h": 88, "fill": "#0c0614cc"},
        {"type": "line", "x1": 0, "y1": 392, "x2": 854, "y2": 392, "color": "#a855f7", "width": 3},
        {"type": "text", "x": 28, "y": 404, "text": "CPU {cpu_temp}°{temp_unit}", "font": "semibold",
         "size": 40, "color": "#ffffff", "shadow": "#000000aa"},
        {"type": "bar", "x": 28, "y": 458, "w": 250, "h": 8, "source": "cpu_load", "min": 0, "max": 100,
         "fill": "#a855f7", "bg": "#1a1625cc", "radius": 4},
        {"type": "text", "x": 330, "y": 404, "text": "GPU {gpu_temp}°{temp_unit}", "font": "semibold",
         "size": 40, "color": "#facc15", "shadow": "#000000aa"},
        {"type": "bar", "x": 330, "y": 458, "w": 250, "h": 8, "source": "gpu_load", "min": 0, "max": 100,
         "fill": "#facc15", "bg": "#1a1625cc", "radius": 4},
        {"type": "text", "x": 826, "y": 404, "text": "{time}", "font": "pixel", "size": 40,
         "color": "#ffffff", "align": "right", "shadow": "#000000aa"},
    ],
}


# ---------------------------------------------------------------- files / presets

def _find_presets_dir() -> str:
    from lt360_common import REPO_ROOT, find_data_dir
    return find_data_dir("examples", "overlays") or os.path.join(REPO_ROOT, "examples", "overlays")


PRESETS_DIR = _find_presets_dir()


def list_presets() -> list[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(PRESETS_DIR) if f.endswith(".json"))
    except OSError:
        return []


def _strip_line_comments(text: str) -> str:
    """Allow whole-line `// comments` in the user's file (never touches strings)."""
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("//"))


def parse_layout(text: str) -> dict:
    """Parse + shape-check; raises ValueError with a human-readable message."""
    try:
        layout = json.loads(_strip_line_comments(text))
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON syntax error at line {e.lineno} col {e.colno}: {e.msg}") from None
    if not isinstance(layout, dict):
        raise ValueError("top level must be a JSON object")
    for key in ("elements", "elements_vertical"):
        els = layout.get(key, [])
        if not isinstance(els, list) or not all(isinstance(e, dict) for e in els):
            raise ValueError(f"'{key}' must be a list of objects")
    sensors = layout.get("custom_sensors", {})
    if not isinstance(sensors, dict):
        raise ValueError("'custom_sensors' must be an object")
    return layout


def write_layout(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)  # atomic: the watcher never sees a half-written file


def _backup(path: str):
    if os.path.isfile(path):
        shutil.copy2(path, path + ".bak")


_TERMINAL_EDITORS = {"nano", "vim", "vi", "nvim", "emacs", "micro", "helix", "hx", "joe", "ed", "pico", "kak", "ne"}
_GUI_EDITORS = ["kate", "gedit", "gnome-text-editor", "kwrite", "mousepad", "xed"]
# (binary, args-before-command) for wrapping a terminal editor when we have no tty
_TERMINALS = [("alacritty", ["-e"]), ("kitty", []), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
              ("xfce4-terminal", ["-x"]), ("foot", []), ("wezterm", ["start", "--"]), ("xterm", ["-e"])]


def open_in_text_editor(filepath: str, wait: bool = False):
    """Open filepath in a text editor. Returns the command used.

    Order: $VISUAL/$EDITOR (GUI editors directly) -> common GUI editors -> $EDITOR
    terminal editor (run inline on a tty, else wrapped in a terminal emulator) -> xdg-open.
    """
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    env = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    env_cmd = shlex.split(env) if env else []
    env_is_tui = bool(env_cmd) and os.path.basename(env_cmd[0]) in _TERMINAL_EDITORS

    def run(cmd, **kw):
        proc = subprocess.Popen(cmd, **kw)
        if wait:
            proc.wait()
        return cmd

    if env_cmd and not env_is_tui and shutil.which(env_cmd[0]):
        return run(env_cmd + [filepath], **quiet)
    for name in _GUI_EDITORS:
        exe = shutil.which(name)
        if exe:
            return run([exe, filepath], **quiet)
    if env_cmd and env_is_tui and shutil.which(env_cmd[0]):
        if sys.stdin.isatty():
            subprocess.Popen(env_cmd + [filepath]).wait()
            return env_cmd
        for term, pre in _TERMINALS:
            if shutil.which(term):
                return run([term] + pre + env_cmd + [filepath], **quiet)
    return run(["xdg-open", filepath], **quiet)


def seed_default(path: str = CUSTOM_PATH) -> bool:
    """Write the default template if the file does not exist yet."""
    if os.path.exists(path):
        return False
    write_layout(path, json.dumps(DEFAULT_LAYOUT, indent=2, ensure_ascii=False) + "\n")
    return True


def reset_default(path: str = CUSTOM_PATH):
    _backup(path)
    write_layout(path, json.dumps(DEFAULT_LAYOUT, indent=2, ensure_ascii=False) + "\n")


def apply_preset(name: str, path: str = CUSTOM_PATH):
    """Copy examples/overlays/<name>.json over customize.json (previous file kept as .bak)."""
    name = os.path.basename(name)
    name = name[:-5] if name.endswith(".json") else name
    src = os.path.join(PRESETS_DIR, name + ".json")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"unknown preset '{name}' (available: {', '.join(list_presets()) or 'none'})")
    with open(src, encoding="utf-8") as f:
        text = f.read()
    parse_layout(text)  # never install a broken preset
    _backup(path)
    write_layout(path, text)


# ---------------------------------------------------------------- custom sensors

def _fmt_number(v: float, decimals) -> str:
    if decimals is not None:
        return f"{v:.{int(decimals)}f}"
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}".rstrip("0").rstrip(".")


def _with_unit(text: str, unit: str) -> str:
    if not unit:
        return text
    return f"{text} {unit}" if unit[0].isalpha() else f"{text}{unit}"


def _to_reading(raw: str, sdef: dict) -> tuple[str, float | None]:
    """(display text without unit, numeric value or None)."""
    m = _NUM_RE.search(raw)
    scale = sdef.get("scale")
    if m and (scale is not None or sdef["type"] == "sysfs"):
        v = float(m.group()) * float(scale if scale is not None else 1)
        return _fmt_number(v, sdef.get("decimals")), v
    text = raw.strip().split("\n")[0][:MAX_TEXT_LEN]
    try:
        return text, float(text)
    except ValueError:
        return text, None


def _read_sysfs(sdef: dict) -> tuple[str, float | None] | None:
    try:
        with open(os.path.expanduser(sdef["path"])) as f:
            return _to_reading(f.read(MAX_SYSFS_READ), sdef)
    except (OSError, KeyError, ValueError):
        return None


def _run_command(sdef: dict) -> tuple[str, float | None] | None:
    try:
        timeout = min(max(float(sdef.get("timeout_sec", 2.0)), 0.1), MAX_COMMAND_TIMEOUT)
    except (TypeError, ValueError):
        timeout = 2.0
    try:
        # Own session/process group and no stdin: a command can't wait on a terminal, and on timeout the
        # whole group (shell + anything it started) is killed, so no child is left holding stdout open.
        proc = subprocess.Popen(sdef["command"], shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True)
    except (subprocess.SubprocessError, OSError, KeyError, TypeError):
        return None
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=1.0)  # reap the shell; the pipe closes once the group is dead
        except (subprocess.SubprocessError, OSError):
            proc.kill()
        return None
    except (subprocess.SubprocessError, OSError):
        proc.kill()
        return None
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        return None
    return _to_reading(lines[0], sdef)


class _CommandWorker(threading.Thread):
    def __init__(self, name: str, sdef: dict):
        super().__init__(daemon=True, name=f"custom-sensor-{name}")
        self.sdef = sdef
        try:
            self.interval = max(MIN_SENSOR_INTERVAL, float(sdef.get("interval_sec", 2.0)))
        except (TypeError, ValueError):
            self.interval = 2.0
        self.stop_event = threading.Event()
        self.result: tuple[str, float | None] | None = None

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.result = _run_command(self.sdef)  # single reference assignment: read lock-free
            except Exception:
                log.exception("custom sensor '%s' failed", self.name)
                self.result = None
            self.stop_event.wait(self.interval)


class CustomSensors:
    """sysfs sources are read inline on each snapshot (1 Hz); command sources run in their own
    thread at their own interval so a slow script can never stall the sensor loop.
    """

    def __init__(self):
        self._defs: dict[str, dict] = {}
        self._workers: dict[str, _CommandWorker] = {}

    def configure(self, defs: dict, active: bool):
        good = {}
        for name, sdef in defs.items():
            if name.startswith("_"):
                continue  # "_comment" and disabled examples
            if not _NAME_RE.match(name) or name in BUILTIN_KEYS or not isinstance(sdef, dict):
                log.warning("custom_sensors: ignoring '%s' (bad name, built-in name or not an object)", name)
                continue
            if sdef.get("type") not in ("sysfs", "command"):
                log.warning("custom_sensors: '%s' needs type 'sysfs' or 'command'", name)
                continue
            good[name] = sdef
        self._defs = good
        wanted = {n: d for n, d in good.items() if d["type"] == "command"} if active else {}
        for name in list(self._workers):
            if name not in wanted or self._workers[name].sdef != wanted[name]:
                self._workers.pop(name).stop_event.set()
        for name, sdef in wanted.items():
            if name not in self._workers:
                w = self._workers[name] = _CommandWorker(name, sdef)
                w.start()

    def snapshot(self, active: bool) -> dict:
        """{name: {"text": str, "value": float|None}}; empty while the custom theme is inactive."""
        out = {}
        if not active:
            return out
        for name, sdef in self._defs.items():
            res = _read_sysfs(sdef) if sdef["type"] == "sysfs" else (
                self._workers[name].result if name in self._workers else None)
            if res is None:
                out[name] = {"text": str(sdef.get("fallback", "")), "value": None}
            else:
                out[name] = {"text": res[0], "unit": sdef.get("unit", ""), "value": res[1]}
        return out

    def stop(self):
        self.configure({}, False)


# ---------------------------------------------------------------- telemetry history (sparklines)

class TelemetryHistory:
    """Rolling HISTORY_SAMPLES-long buffers (one sample per second) of every numeric sensor: the
    built-ins in HISTORY_KEYS plus numeric custom sensors. A missing reading is stored as None so all
    series share one time axis and a gap shows as a break in the line.
    """

    def __init__(self, samples: int = HISTORY_SAMPLES, min_interval: float = 0.9):
        self.samples = samples
        self.min_interval = min_interval
        self.version = 0            # bumped per recorded sample; part of the overlay signature
        self._buf: dict[str, collections.deque] = {}
        self._last = -1e9
        self._lock = threading.Lock()

    def record(self, data: dict, now: float | None = None) -> bool:
        """Add one sample; extra calls within `min_interval` (e.g. overlay-change wake-ups) are ignored."""
        now = time.monotonic() if now is None else now
        if now - self._last < self.min_interval:
            return False
        self._last = now
        values = {k: data.get(k) for k in HISTORY_KEYS}
        for name, c in (data.get("custom") or {}).items():
            values[name] = c.get("value") if isinstance(c, dict) else None
        with self._lock:
            for key in set(values) | set(self._buf):
                v = values.get(key)
                buf = self._buf.get(key)
                if buf is None:
                    buf = self._buf[key] = collections.deque(maxlen=self.samples)
                buf.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
            for key in [k for k, b in self._buf.items() if k not in HISTORY_KEYS and not any(x is not None for x in b)]:
                del self._buf[key]  # a custom sensor that is gone (or never numeric)
            self.version += 1
        return True

    def series(self, key: str) -> list[float | None]:
        with self._lock:
            return list(self._buf.get(key, ()))


# ---------------------------------------------------------------- manager (file watcher)

class CustomManager:
    def __init__(self, path: str = CUSTOM_PATH):
        self.path = path
        self.layout: dict = {}
        self.version = 0           # bumped on every successful (re)load
        self.error: str | None = None
        self.sensors = CustomSensors()
        self.history = TelemetryHistory()
        self._stamp = None
        self._next_check = 0.0
        self._active = False
        try:
            seed_default(path)
        except OSError as e:
            log.error("cannot create %s: %s", path, e)
        self._load(first=True)

    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self, first: bool = False) -> bool:
        stamp = self._stat()
        self._stamp = stamp
        if stamp is None:
            self.error = "file missing"
            return False
        try:
            with open(self.path, encoding="utf-8") as f:
                layout = parse_layout(f.read())
        except (OSError, ValueError) as e:
            self.error = str(e)
            log.error("customize.json not reloaded, keeping last valid layout: %s", e)
            return False
        self.layout, self.error = layout, None
        self.version += 1
        self.sensors.configure(layout.get("custom_sensors", {}), self._active)
        if not first:
            log.info("customize.json reloaded (%d elements)", len(self.elements_for((854, 480))))
        return True

    def poll(self) -> bool:
        """Call freely; stats the file at most once per CHECK_INTERVAL. True if a new layout loaded."""
        now = time.monotonic()
        if now < self._next_check:
            return False
        self._next_check = now + CHECK_INTERVAL
        stamp = self._stat()
        if stamp == self._stamp:
            return False
        return self._load()

    def set_active(self, active: bool):
        """Command sensors only run while the custom theme is on screen."""
        if active != self._active:
            self._active = active
            self.sensors.configure(self.layout.get("custom_sensors", {}), active)

    def sensor_values(self) -> dict:
        return self.sensors.snapshot(self._active)

    def elements_for(self, size: tuple[int, int]) -> list[dict]:
        if size[1] > size[0] and self.layout.get("elements_vertical"):
            return self.layout["elements_vertical"]
        return self.layout.get("elements", [])


# ---------------------------------------------------------------- rendering

class _Ctx(dict):
    def __missing__(self, key):
        return "?"


def build_context(data: dict, celsius: bool) -> tuple[_Ctx, dict]:
    """(format-string variables, numeric values for bars)."""
    def num(key, fmt="{:.0f}"):
        v = data.get(key)
        return "N/A" if v is None else fmt.format(v)

    def temp(key):
        v = data.get(key)
        if v is None:
            return "N/A"
        return f"{(v if celsius else v * 9 / 5 + 32):.0f}"

    watts = num("gpu_power", "{:.0f}W")
    ctx = _Ctx(
        cpu_temp=temp("cpu_temp"), gpu_temp=temp("gpu_temp"), temp_unit="C" if celsius else "F",
        cpu_load=num("cpu_load"), gpu_load=num("gpu_load"), cpu_freq=num("cpu_freq"),
        cpu_freq_ghz=("N/A" if data.get("cpu_freq") is None else f"{data['cpu_freq'] / 1000:.1f}"),
        gpu_power=num("gpu_power"), gpu_wattage=num("gpu_power"),
        gpu_power_str=watts, gpu_wattage_str=watts, gpu_clock=num("gpu_clock"), ram_percent=num("ram_percent"),
        ram_used=num("ram_used", "{:.1f}"), ram_total=num("ram_total", "{:.1f}"),
        time=str(data.get("time", "--:--:--")), date=str(data.get("date", "")),
    )
    raw = {k: v for k, v in data.items() if isinstance(v, (int, float))}
    if raw.get("gpu_power") is not None:
        raw["gpu_wattage"] = raw["gpu_power"]
    for name, c in (data.get("custom") or {}).items():
        ctx[name] = _with_unit(c["text"], c.get("unit", "")) if c.get("value") is not None or c["text"] else ""
        ctx[name + "_value"] = c["text"]
        if c.get("value") is not None:
            raw[name] = c["value"]
    return ctx, raw


def parse_color(value, default=None) -> tuple[int, int, int, int] | None:
    if value is None:
        return default
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        c = [int(x) for x in value]
        return tuple(c + [255] * (4 - len(c)))
    if isinstance(value, str) and value.startswith("#"):
        h = value[1:]
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        if len(h) in (6, 8):
            try:
                v = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
            except ValueError:
                raise ValueError(f"bad color {value!r}") from None
            return tuple(v + [255] * (4 - len(v)))
    raise ValueError(f"bad color {value!r} (use #RRGGBB or #RRGGBBAA)")


_font_cache: dict = {}


def _font(spec: str, size: int) -> ImageFont.FreeTypeFont:
    key = (spec, size)
    if key not in _font_cache:
        from lt360_overlay import ASSETS_FONT_DIR, _FONT_FILES  # lazy: overlay imports this module
        alias = {"sans": "regular"}.get(spec, spec)
        path = os.path.join(ASSETS_FONT_DIR, _FONT_FILES[alias]) if alias in _FONT_FILES \
            else os.path.expanduser(spec)
        try:
            if not os.path.isfile(path):  # regular files only: never open a FIFO or device node
                raise OSError(f"not a regular file: {path}")
            _font_cache[key] = ImageFont.truetype(path, size)
        except OSError:
            log.warning("font %r not found, using sans", spec)
            _font_cache[key] = ImageFont.truetype(os.path.join(ASSETS_FONT_DIR, _FONT_FILES["regular"]), size)
    return _font_cache[key]


def _f(el: dict, key: str, default=None) -> float:
    v = el.get(key, default)
    if v is None:
        raise ValueError(f"missing '{key}'")
    return float(v)


def _text_of(el: dict, ctx: _Ctx) -> str:
    try:
        return str(el.get("text", "")).format_map(ctx)
    except (ValueError, IndexError, KeyError, AttributeError):
        return str(el.get("text", ""))  # stray '{' etc.: show it verbatim instead of failing


def _bar_fraction(el: dict, raw: dict) -> float:
    lo, hi = _f(el, "min", 0), _f(el, "max", 100)
    v = raw.get(el.get("source"))
    if v is None or hi == lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _resolve_path(path: str) -> str:
    """Image paths: ~ expanded; relative paths are relative to ~/.config/deepcool-lt360/."""
    path = os.path.expanduser(str(path))
    return path if os.path.isabs(path) else os.path.join(CONFIG_DIR, path)


def _mtime(path: str):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def signature(layout_elements: list[dict], data: dict, celsius: bool, history: "TelemetryHistory | None" = None) -> tuple:
    """Cheap fingerprint of everything data-dependent on screen; equal => identical pixels."""
    ctx, raw = build_context(data, celsius)
    sig = []
    for el in layout_elements:
        kind = el.get("type")
        try:
            if kind == "text":
                sig.append(_text_of(el, ctx))
            elif kind == "bar":
                sig.append(round(_bar_fraction(el, raw) * _f(el, "w", 0)))
            elif kind == "ring":
                sig.append((round(_bar_fraction(el, raw) * 720), _text_of(el, ctx), _label_of(el, ctx)))
            elif kind == "sparkline":
                sig.append(history.version if history is not None else None)
            elif kind == "image":
                sig.append(_mtime(_resolve_path(el.get("path", ""))))
        except (ValueError, TypeError):
            sig.append(None)
    return tuple(sig)


def _blend(overlay: Image.Image, fn):
    """Draw on a scratch layer, then alpha-composite: translucent shapes blend instead of overwriting."""
    tmp = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    fn(ImageDraw.Draw(tmp))
    overlay.alpha_composite(tmp)


def _draw_box(overlay, el):
    x, y, w, h = _f(el, "x"), _f(el, "y"), _f(el, "w"), _f(el, "h")
    fill, outline = parse_color(el.get("fill")), parse_color(el.get("outline"))
    radius, width = _f(el, "radius", 0), int(_f(el, "width", 2))
    _blend(overlay, lambda d: d.rounded_rectangle(
        [x, y, x + w - 1, y + h - 1], radius=min(radius, w / 2, h / 2), fill=fill, outline=outline,
        width=width if outline else 0))


def _draw_text(overlay, el, ctx):
    text = _text_of(el, ctx)
    font = _font(str(el.get("font", "sans")), int(_f(el, "size", 32)))
    color = parse_color(el.get("color"), (255, 255, 255, 255))
    shadow = parse_color(el.get("shadow"))
    x, y = _f(el, "x"), _f(el, "y")
    align = el.get("align", "left")
    anchor = {"left": "la", "center": "ma", "right": "ra"}.get(align)
    if anchor is None:
        raise ValueError(f"align must be left/center/right, got {align!r}")
    max_w = el.get("max_width")

    def paint(d):
        s = text
        if max_w is not None:
            while len(s) > 1 and d.textlength(s, font=font) > float(max_w):
                s = s[:-2].rstrip() + "…" if s.endswith("…") else s[:-1].rstrip() + "…"
        if shadow:
            d.text((x + 2, y + 2), s, font=font, fill=shadow, anchor=anchor)
        d.text((x, y), s, font=font, fill=color, anchor=anchor)
    _blend(overlay, paint)


def _draw_bar(overlay, el, raw):
    x, y, w, h = _f(el, "x"), _f(el, "y"), _f(el, "w"), _f(el, "h")
    radius = _f(el, "radius", 0)
    bg, fill, outline = parse_color(el.get("bg")), parse_color(el.get("fill"), (168, 85, 247, 255)), \
        parse_color(el.get("outline"))
    fw = w * _bar_fraction(el, raw)

    def paint(d):
        if bg or outline:
            d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=min(radius, w / 2, h / 2), fill=bg,
                                outline=outline, width=1 if outline else 0)
        if fw >= 1:
            d.rounded_rectangle([x, y, x + fw - 1, y + h - 1], radius=min(radius, fw / 2, h / 2), fill=fill)
    _blend(overlay, paint)


def _draw_line(overlay, el):
    p = [(_f(el, "x1"), _f(el, "y1")), (_f(el, "x2"), _f(el, "y2"))]
    color, width = parse_color(el.get("color"), (255, 255, 255, 255)), int(_f(el, "width", 2))
    _blend(overlay, lambda d: d.line(p, fill=color, width=width))


def _composite_at(overlay: Image.Image, tile: Image.Image, x: float, y: float):
    """alpha_composite `tile` with its top-left at (x, y); parts outside the canvas are clipped."""
    ix, iy = round(x), round(y)
    if ix < 0 or iy < 0:
        tile = tile.crop((max(0, -ix), max(0, -iy), tile.width, tile.height))
        ix, iy = max(0, ix), max(0, iy)
    if tile.width > 0 and tile.height > 0 and ix < overlay.width and iy < overlay.height:
        overlay.alpha_composite(tile.crop((0, 0, min(tile.width, overlay.width - ix),
                                          min(tile.height, overlay.height - iy))), dest=(ix, iy))


def _downsample(tile: Image.Image, w: int, h: int) -> Image.Image:
    return tile.resize((max(1, w), max(1, h)), Image.Resampling.LANCZOS)


def _label_of(el: dict, ctx: _Ctx) -> str:
    try:
        return str(el.get("label", "")).format_map(ctx)
    except (ValueError, IndexError, KeyError, AttributeError):
        return str(el.get("label", ""))


def _ring_radius(el: dict) -> float:
    if el.get("radius") is not None:
        return _f(el, "radius")
    if el.get("w") is None and el.get("h") is None:
        raise ValueError("ring needs 'radius' (or 'w'/'h')")
    return min(_f(el, "w", el.get("h")), _f(el, "h", el.get("w"))) / 2


def _draw_ring(overlay, el, ctx, raw):
    """Arc gauge. The ring's outer edge fits the (x, y, 2*radius) box; angles run clockwise from 3 o'clock,
    so the default 135 -> 405 is a 270-degree gauge open at the bottom.
    """
    x, y, r = _f(el, "x"), _f(el, "y"), _ring_radius(el)
    if r <= 0:
        raise ValueError("radius must be > 0")
    thick = max(1.0, min(_f(el, "thickness", 8), r))
    a0, a1 = _f(el, "start_angle", 135), _f(el, "end_angle", 405)
    frac = _bar_fraction(el, raw)
    bg = parse_color(el.get("bg"), (26, 22, 37, 204))
    fill = parse_color(el.get("fill"), (168, 85, 247, 255))
    glow = parse_color(el.get("glow"))
    round_cap = el.get("cap", "round") == "round"

    margin = thick * 1.5 if glow else 2.0
    side = int(2 * (r + margin)) + 1
    ss = SUPERSAMPLE
    cx = cy = (margin + r) * ss
    box = [margin * ss, margin * ss, (margin + 2 * r) * ss, (margin + 2 * r) * ss]

    def arc(draw, start, end, color, width):
        if end - start <= 0.01:
            return
        draw.arc(box, start, end, fill=color, width=round(width * ss))
        if round_cap and width > 2:
            mid = (r - width / 2) * ss
            for ang in (start, end):
                px, py = cx + mid * math.cos(math.radians(ang)), cy + mid * math.sin(math.radians(ang))
                h = width * ss / 2
                draw.ellipse([px - h, py - h, px + h, py + h], fill=color)

    tile = Image.new("RGBA", (side * ss, side * ss), (0, 0, 0, 0))
    if bg:
        arc(ImageDraw.Draw(tile), a0, a1, bg, thick)
    value_end = a0 + (a1 - a0) * frac
    if frac > 0:
        if glow:
            halo = Image.new("RGBA", tile.size, (0, 0, 0, 0))
            arc(ImageDraw.Draw(halo), a0, value_end, glow, thick)
            tile.alpha_composite(halo.filter(ImageFilter.GaussianBlur(thick * ss * 0.6)))
        layer = Image.new("RGBA", tile.size, (0, 0, 0, 0))
        arc(ImageDraw.Draw(layer), a0, value_end, fill, thick)
        tile.alpha_composite(layer)
    _composite_at(overlay, _downsample(tile, side, side), x - margin, y - margin)

    text, label = _text_of(el, ctx), _label_of(el, ctx)
    if not text and not label:
        return
    size = int(_f(el, "size", max(10, r * 0.5)))
    label_size = int(_f(el, "label_size", max(9, r * 0.2)))
    ccx, ccy = x + r, y + r
    color = parse_color(el.get("color"), (255, 255, 255, 255))
    label_color = parse_color(el.get("label_color"), fill)
    shadow = parse_color(el.get("shadow"))
    font = _font(str(el.get("font", "semibold")), size)
    label_font = _font(str(el.get("label_font", "light")), label_size)

    def paint(d):
        ty = ccy - (label_size * 0.45 if label else 0)
        if text:
            if shadow:
                d.text((ccx + 2, ty + 2), text, font=font, fill=shadow, anchor="mm")
            d.text((ccx, ty), text, font=font, fill=color, anchor="mm")
        if label:
            d.text((ccx, ty + size * 0.5 + 2), label, font=label_font, fill=label_color, anchor="ma")
    _blend(overlay, paint)


def _draw_sparkline(overlay, el, history):
    """Rolling graph of the last HISTORY_SAMPLES seconds of `source`, newest sample at the right edge."""
    x, y, w, h = _f(el, "x"), _f(el, "y"), _f(el, "w"), _f(el, "h")
    if w < 4 or h < 4:
        raise ValueError("w and h must be >= 4")
    lo, hi = _f(el, "min", 0), _f(el, "max", 100)
    if hi == lo:
        raise ValueError("max must differ from min")
    radius = _f(el, "radius", 8)
    width = max(1.0, _f(el, "width", 2))
    bg = parse_color(el.get("bg"), (10, 10, 18, 170))
    outline = parse_color(el.get("outline"), (38, 32, 56, 255))
    line = parse_color(el.get("line_color"), (34, 211, 238, 255))
    under = parse_color(el.get("fill_color"), (34, 211, 238, 51))
    grid = parse_color(el.get("grid"))
    source = el.get("source")
    if not source:
        raise ValueError("missing 'source'")
    series = history.series(source) if history is not None else []
    n = max(2, HISTORY_SAMPLES)

    ss = SUPERSAMPLE
    W, H = round(w), round(h)
    tile = Image.new("RGBA", (W * ss, H * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    if bg or outline:
        d.rounded_rectangle([0, 0, W * ss - 1, H * ss - 1], radius=min(radius, W / 2, H / 2) * ss, fill=bg,
                            outline=outline, width=ss if outline else 0)
    pad = max(width + 2, min(radius * 0.6, h / 4))
    left, right, top, bottom = pad * ss, (W - pad) * ss, pad * ss, (H - pad) * ss
    if grid:
        for f in (0.25, 0.5, 0.75):
            gy = top + (bottom - top) * f
            d.line([(left, gy), (right, gy)], fill=grid, width=ss)
    step = (right - left) / (n - 1)

    def pt(i, v):  # i: index within the n-sample window
        f = max(0.0, min(1.0, (v - lo) / (hi - lo)))
        return left + i * step, bottom - f * (bottom - top)

    offset = n - len(series)
    segments, cur = [], []
    for i, v in enumerate(series):
        if v is None:
            if cur:
                segments.append(cur)
            cur = []
        else:
            cur.append(pt(offset + i, v))
    if cur:
        segments.append(cur)
    if under:
        fill_layer = Image.new("RGBA", tile.size, (0, 0, 0, 0))
        fd = ImageDraw.Draw(fill_layer)
        for seg in segments:
            if len(seg) > 1:
                fd.polygon(seg + [(seg[-1][0], bottom), (seg[0][0], bottom)], fill=under)
        tile.alpha_composite(fill_layer)
    for seg in segments:
        if len(seg) > 1:
            d.line(seg, fill=line, width=round(width * ss), joint="curve")
    if segments and el.get("dot", True):
        px, py = segments[-1][-1]
        rr = (width + 1.5) * ss
        d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=line)
    _composite_at(overlay, _downsample(tile, W, H), x, y)


_image_cache: "collections.OrderedDict[tuple, Image.Image]" = collections.OrderedDict()


def _sticker(el: dict) -> Image.Image:
    """The element's image resized to w/h (aspect kept if only one is given) with opacity applied.
    Cached by (path, mtime, size, opacity), so replacing the file on disk shows up on the next reload.
    """
    path = _resolve_path(el.get("path") or "")
    if not el.get("path"):
        raise ValueError("missing 'path'")
    if not os.path.isfile(path):  # regular files only (follows symlinks): never open a FIFO or /dev node
        raise ValueError(f"image not found or not a regular file: {path}")
    mtime = _mtime(path)
    if mtime is None:
        raise ValueError(f"image not found: {path}")
    opacity = max(0.0, min(1.0, float(el.get("opacity", 1.0))))
    key = (path, mtime, el.get("w"), el.get("h"), opacity)
    img = _image_cache.get(key)
    if img is not None:
        _image_cache.move_to_end(key)
        return img
    try:
        with Image.open(path) as src:
            src.seek(0)
            img = src.convert("RGBA")
    except OSError as e:
        raise ValueError(f"cannot read image {path}: {e}") from None
    w0, h0 = img.size
    w, h = el.get("w"), el.get("h")
    if w is not None and h is not None:
        size = (round(float(w)), round(float(h)))
    elif w is not None:
        size = (round(float(w)), round(h0 * float(w) / w0))
    elif h is not None:
        size = (round(w0 * float(h) / h0), round(float(h)))
    else:
        size = (w0, h0)
    size = (max(1, min(size[0], 4096)), max(1, min(size[1], 4096)))
    if size != img.size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    if opacity < 1.0:
        img.putalpha(img.getchannel("A").point(lambda a: round(a * opacity)))
    _image_cache[key] = img
    while len(_image_cache) > MAX_IMAGE_CACHE:
        _image_cache.popitem(last=False)
    return img


def _draw_image(overlay, el):
    _composite_at(overlay, _sticker(el), _f(el, "x"), _f(el, "y"))


_warned: set = set()


def render(size: tuple[int, int], elements: list[dict], data: dict, celsius: bool, version: int = 0,
           history: TelemetryHistory | None = None) -> Image.Image:
    """Draw the elements, in order, onto a transparent RGBA layer of `size`."""
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    ctx, raw = build_context(data, celsius)
    for i, el in enumerate(elements):
        kind = el.get("type")
        try:
            if kind in ("rect", "box"):
                _draw_box(overlay, el)
            elif kind == "text":
                _draw_text(overlay, el, ctx)
            elif kind == "bar":
                _draw_bar(overlay, el, raw)
            elif kind == "line":
                _draw_line(overlay, el)
            elif kind == "ring":
                _draw_ring(overlay, el, ctx, raw)
            elif kind == "sparkline":
                _draw_sparkline(overlay, el, history)
            elif kind == "image":
                _draw_image(overlay, el)
            else:
                raise ValueError(f"unknown element type {kind!r}")
        except (ValueError, TypeError) as e:
            if (version, i) not in _warned:  # once per layout version, not once per second
                _warned.add((version, i))
                log.warning("customize.json element #%d (%s) skipped: %s", i + 1, kind, e)
    return overlay


# ---------------------------------------------------------------- visual editor helpers (GUI)

EDITABLE_COLOR_KEY = {"rect": "fill", "box": "fill", "text": "color", "bar": "fill", "line": "color",
                      "ring": "fill", "sparkline": "line_color"}


def element_bbox(el: dict, data: dict | None = None, celsius: bool = True) -> tuple[float, float, float, float] | None:
    """Canvas-space (x0, y0, x1, y1) an element covers, or None if it can't be placed (bad keys)."""
    kind = el.get("type")
    try:
        if kind in ("rect", "box", "bar", "sparkline"):
            x, y = _f(el, "x"), _f(el, "y")
            return x, y, x + _f(el, "w"), y + _f(el, "h")
        if kind == "ring":
            x, y, r = _f(el, "x"), _f(el, "y"), _ring_radius(el)
            return x, y, x + 2 * r, y + 2 * r
        if kind == "line":
            x1, y1, x2, y2 = _f(el, "x1"), _f(el, "y1"), _f(el, "x2"), _f(el, "y2")
            pad = max(3.0, _f(el, "width", 2) / 2 + 2)
            return min(x1, x2) - pad, min(y1, y2) - pad, max(x1, x2) + pad, max(y1, y2) + pad
        if kind == "image":
            x, y = _f(el, "x"), _f(el, "y")
            try:
                img = _sticker(el)
                return x, y, x + img.width, y + img.height
            except (ValueError, OSError):
                return x, y, x + float(el.get("w") or 64), y + float(el.get("h") or 64)
        if kind == "text":
            ctx, _ = build_context(data or {}, celsius)
            text = _text_of(el, ctx) or " "
            font = _font(str(el.get("font", "sans")), int(_f(el, "size", 32)))
            anchor = {"left": "la", "center": "ma", "right": "ra"}.get(el.get("align", "left"), "la")
            x, y = _f(el, "x"), _f(el, "y")
            box = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((x, y), text, font=font, anchor=anchor)
            x0, y0, x1, y1 = box
            if el.get("max_width") is not None and x1 - x0 > float(el["max_width"]):
                mw = float(el["max_width"])
                x0, x1 = {"la": (x, x + mw), "ma": (x - mw / 2, x + mw / 2), "ra": (x - mw, x)}[anchor]
            return x0, min(y, y0), x1, y1
    except (ValueError, TypeError, KeyError):
        return None
    return None


def move_element(el: dict, dx: float, dy: float):
    """Shift an element in place by (dx, dy) canvas pixels; coordinates stay integers."""
    keys = (("x1", "y1"), ("x2", "y2")) if el.get("type") == "line" else (("x", "y"),)
    for kx, ky in keys:
        el[kx] = round(float(el.get(kx, 0)) + dx)
        el[ky] = round(float(el.get(ky, 0)) + dy)


def format_layout(layout: dict) -> str:
    """Serialise a layout the way the bundled presets are written: one element per line."""
    items = list(layout.items())
    lines = ["{"]
    for i, (key, value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        if key in ("elements", "elements_vertical") and isinstance(value, list) and value:
            lines.append(f"  {json.dumps(key)}: [")
            for j, el in enumerate(value):
                lines.append("    " + json.dumps(el, ensure_ascii=False) + ("," if j < len(value) - 1 else ""))
            lines.append("  ]" + comma)
        else:
            body = json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n  ")
            lines.append(f"  {json.dumps(key)}: {body}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def save_layout(layout: dict, path: str = CUSTOM_PATH, backup: bool = False):
    """Validate and atomically write `layout` (the daemon hot-reloads it within a second)."""
    text = format_layout(layout)
    parse_layout(text)
    if backup:
        _backup(path)
    write_layout(path, text)


def load_layout(path: str = CUSTOM_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return parse_layout(f.read())
