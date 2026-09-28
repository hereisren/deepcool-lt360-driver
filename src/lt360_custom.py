"""User-editable declarative overlay + telemetry (~/.config/deepcool-lt360/customize.json).

The file has two optional blocks:

  custom_sensors  named telemetry sources ("sysfs" file reads, "command" shell polls)
  elements        widgets (rect/box, text, bar, line) drawn in order on the canvas

`CustomManager` owns the file: it seeds a default on first run, watches its mtime
(`poll()`, throttled to 1 Hz) and, on a syntax/shape error, keeps the last valid layout
so a typo never blanks the screen. Rendering is a pure function of (layout, sensor data).
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("lt360d.custom")

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "deepcool-lt360")
CUSTOM_PATH = os.path.join(CONFIG_DIR, "customize.json")
CHECK_INTERVAL = 0.9  # just under 1 s so loop jitter never skips a whole tick
MIN_SENSOR_INTERVAL = 0.5
MAX_COMMAND_TIMEOUT = 10.0
MAX_TEXT_LEN = 200

BUILTIN_KEYS = {
    "cpu_temp", "gpu_temp", "cpu_load", "gpu_load", "cpu_freq", "cpu_freq_ghz", "gpu_power", "gpu_clock",
    "ram_percent", "ram_used", "ram_total", "time", "date", "temp_unit",
}
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

DEFAULT_LAYOUT = {
    "_comment": [
        "LT360 custom overlay. Edit and save: the daemon reloads this file within a second.",
        "A JSON typo is logged and the last valid layout stays on screen.",
        "Canvas is 854x480 (horizontal) or 480x854 (vertical, use 'elements_vertical').",
        "Text variables: {cpu_temp} {gpu_temp} {cpu_load} {gpu_load} {cpu_freq} {cpu_freq_ghz} {gpu_power}",
        "  {gpu_clock} {ram_percent} {ram_used} {ram_total} {time} {date} {temp_unit} + your custom_sensors.",
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
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "examples", "overlays"),
        os.path.join(sys.prefix, "share", "deepcool-lt360", "examples", "overlays"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    return os.path.abspath(candidates[0])


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
            return _to_reading(f.read(), sdef)
    except (OSError, KeyError, ValueError):
        return None


def _run_command(sdef: dict) -> tuple[str, float | None] | None:
    timeout = min(float(sdef.get("timeout_sec", 2.0)), MAX_COMMAND_TIMEOUT)
    try:
        out = subprocess.run(sdef["command"], shell=True, capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError, KeyError):
        return None
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if out.returncode != 0 or not lines:
        return None
    return _to_reading(lines[0], sdef)


class _CommandWorker(threading.Thread):
    def __init__(self, name: str, sdef: dict):
        super().__init__(daemon=True, name=f"custom-sensor-{name}")
        self.sdef = sdef
        self.interval = max(MIN_SENSOR_INTERVAL, float(sdef.get("interval_sec", 2.0)))
        self.stop_event = threading.Event()
        self.result: tuple[str, float | None] | None = None

    def run(self):
        while not self.stop_event.is_set():
            self.result = _run_command(self.sdef)  # single reference assignment: read lock-free
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


# ---------------------------------------------------------------- manager (file watcher)

class CustomManager:
    def __init__(self, path: str = CUSTOM_PATH):
        self.path = path
        self.layout: dict = {}
        self.version = 0           # bumped on every successful (re)load
        self.error: str | None = None
        self.sensors = CustomSensors()
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

    ctx = _Ctx(
        cpu_temp=temp("cpu_temp"), gpu_temp=temp("gpu_temp"), temp_unit="C" if celsius else "F",
        cpu_load=num("cpu_load"), gpu_load=num("gpu_load"), cpu_freq=num("cpu_freq"),
        cpu_freq_ghz=("N/A" if data.get("cpu_freq") is None else f"{data['cpu_freq'] / 1000:.1f}"),
        gpu_power=num("gpu_power"), gpu_clock=num("gpu_clock"), ram_percent=num("ram_percent"),
        ram_used=num("ram_used", "{:.1f}"), ram_total=num("ram_total", "{:.1f}"),
        time=str(data.get("time", "--:--:--")), date=str(data.get("date", "")),
    )
    raw = {k: v for k, v in data.items() if isinstance(v, (int, float))}
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


def signature(layout_elements: list[dict], data: dict, celsius: bool) -> tuple:
    """Cheap fingerprint of everything data-dependent on screen; equal => identical pixels."""
    ctx, raw = build_context(data, celsius)
    sig = []
    for el in layout_elements:
        try:
            if el.get("type") == "text":
                sig.append(_text_of(el, ctx))
            elif el.get("type") == "bar":
                sig.append(round(_bar_fraction(el, raw) * _f(el, "w", 0)))
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


_warned: set = set()


def render(size: tuple[int, int], elements: list[dict], data: dict, celsius: bool, version: int = 0) -> Image.Image:
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
            else:
                raise ValueError(f"unknown element type {kind!r}")
        except (ValueError, TypeError) as e:
            if (version, i) not in _warned:  # once per layout version, not once per second
                _warned.add((version, i))
                log.warning("customize.json element #%d (%s) skipped: %s", i + 1, kind, e)
    return overlay
