#!/usr/bin/env python3
"""lt360-gui - "LT360 VISION — For Renmin": native desktop GUI for the LT360 display daemon.

Talks to lt360d over the same Unix-socket protocol as lt360ctl. **No socket or
subprocess I/O ever happens on the Qt UI thread**: all of it lives in IpcWorker
(a QThread) and in short-lived thread-pool jobs; the UI only reacts to signals.
(The HUD editor writes customize.json -- a small local file -- directly.)

Launch flags: --tray (start hidden in the system tray; closing keeps it there), --minimized.
"""
import argparse
import base64
import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time

from PyQt6.QtCore import QObject, QRectF, QRunnable, Qt, QThread, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QActionGroup, QColor, QFontDatabase, QIcon, QImage, QPainter, QPen, QPixmap, QTransform
from PyQt6.QtWidgets import (
    QApplication, QColorDialog, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QPushButton, QScrollArea, QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget,
)

import lt360_custom as C
from lt360_common import __version__, find_data_dir, find_icon_file
from lt360_ipc import default_socket_path
from lt360_overlay import normalize_readout
import lt360_widgets as W

DEFAULT_SOCKET_PATH = default_socket_path()
SERVICE_NAME = "deepcool-lt360.service"
APP_TITLE = "LT360 VISION — For Renmin"
GUI_PREFS_PATH = os.path.join(C.CONFIG_DIR, "gui.json")   # GUI-only settings (close-to-tray)
MEDIA_EXTS = {".gif", ".mp4", ".webm", ".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
MEDIA_FILTER = "Media (*.gif *.mp4 *.webm *.png *.jpg *.jpeg *.webp);;All files (*)"

PREVIEW_INTERVAL = 0.10   # matches the daemon's 10 fps minimum send cadence
STATUS_INTERVAL = 0.7
STATUS_INTERVAL_HIDDEN = 3.0
TOUCH_HOLD = 1.5          # seconds a user-touched control ignores polled status (prevents flicker-back)

THEMES = ["boundary", "codezero", "pixelworld", "custom"]
METRICS = ["cpu_temp", "gpu_temp", "cpu_load", "gpu_load", "time", "off"]
ENGINE_NOTES = {
    "performance": "Pre-baked USB buffers in RAM: under 0.5% CPU, best for GIFs and short loops. "
                   "Videos are capped at 30 s.",
    "full": "Streams MP4/WebM of any length through ffmpeg and sends smaller, de-duplicated frames "
            "(far fewer USB packets, more CPU).",
}
ENGINE_SHORT = {"performance": "PERF", "full": "FULL"}
METRIC_LABELS = {
    "cpu_temp": "CPU Temp", "gpu_temp": "GPU Temp", "cpu_load": "CPU Load",
    "gpu_load": "GPU Load", "time": "Time", "off": "Off",
}
THEME_LABELS = {"boundary": "Boundary", "codezero": "Code Zero", "pixelworld": "Pixel World", "custom": "Custom HUD"}
SPEEDS = [("0.5x", 0.5), ("1.0x", 1.0), ("1.5x", 1.5), ("2.0x", 2.0)]
TRAY_BRIGHTNESS = (10, 25, 50, 75, 100)
SLOW_ACTIONS = ("set_media", "set_engine_mode", "set_framing", "set_mode", "set_mirror", "set_overlay")  # may re-bake


# ---------------------------------------------------------------------------
# IPC (background thread only)
# ---------------------------------------------------------------------------

class DaemonClient:
    """Blocking JSON-over-Unix-socket client. Only ever called from IpcWorker."""

    def __init__(self, sock_path: str = DEFAULT_SOCKET_PATH):
        self.sock_path = sock_path

    def call(self, request: dict, timeout: float = 3.0) -> dict:
        if not os.path.exists(self.sock_path):
            return {"ok": False, "error": "daemon socket not found (is lt360d running?)"}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(self.sock_path)
                s.sendall((json.dumps(request) + "\n").encode())
                data = b""
                while not data.endswith(b"\n"):
                    chunk = s.recv(262144)
                    if not chunk:
                        break
                    data += chunk
            return json.loads(data.decode())
        except (OSError, ValueError) as e:
            return {"ok": False, "error": str(e) or type(e).__name__}


class IpcWorker(QThread):
    """Owns all daemon traffic: periodic get_status/get_preview polling plus command dispatch.

    Commands are queued by key and *coalesced*: submitting the same key again before
    it is sent replaces (or, with merge=True, merges into) the pending request, so a
    slider drag produces a few trailing writes instead of one per pixel.
    """
    status_ready = pyqtSignal(dict)
    preview_ready = pyqtSignal(QImage)
    connection_changed = pyqtSignal(bool, str)
    command_done = pyqtSignal(str, bool, str)   # key, ok, error

    def __init__(self, sock_path: str = DEFAULT_SOCKET_PATH):
        super().__init__()
        self.client = DaemonClient(sock_path)
        self._cond = threading.Condition()
        self._pending: dict[str, tuple[float, dict]] = {}
        self._stop = False
        self.preview_enabled = False  # plain bool, flipped from the UI thread (atomic); on once the window shows
        self._connected: bool | None = None
        self._last_preview_id = None

    # ---- called from the UI thread; never blocks on I/O ----
    def submit(self, key: str, request: dict, delay: float = 0.0, merge: bool = False):
        with self._cond:
            old = self._pending.get(key)
            if old is not None:
                not_before, prev = old
                if merge and isinstance(prev.get("value"), dict):
                    request = {**request, "value": {**prev["value"], **request["value"]}}
            else:
                not_before = time.monotonic() + delay
            self._pending[key] = (not_before, request)
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stop = True
            self._cond.notify()

    def wake(self):
        with self._cond:
            self._cond.notify()

    # ---- worker thread ----
    def _set_connected(self, ok: bool, msg: str = ""):
        if ok != self._connected:
            self._connected = ok
            self.connection_changed.emit(ok, msg)

    def _poll_status(self):
        resp = self.client.call({"action": "get_status"})
        if resp.get("ok"):
            self._set_connected(True)
            self.status_ready.emit(resp["status"])
        else:
            self._set_connected(False, resp.get("error", "disconnected"))

    def _poll_preview(self):
        resp = self.client.call({"action": "get_preview", "last_id": self._last_preview_id}, timeout=1.5)
        if not resp.get("ok") or resp.get("unchanged"):
            return
        img = QImage.fromData(base64.b64decode(resp["jpeg_base64"]), "JPEG")
        rotation = int(resp.get("rotation", 0))
        if rotation and not img.isNull():   # undo the panel-native rotation so the stage shows the viewing orientation
            img = img.transformed(QTransform().rotate(-rotation))
        if not img.isNull():
            self._last_preview_id = resp.get("id")
            self.preview_ready.emit(img)

    def run(self):
        next_status = next_preview = 0.0
        while True:
            now = time.monotonic()
            due = []
            with self._cond:
                if self._stop:
                    return
                for key, (t, req) in list(self._pending.items()):
                    if t <= now:
                        due.append((key, req))
                        del self._pending[key]
            for key, req in due:
                timeout = 90.0 if req.get("action") in SLOW_ACTIONS else 10.0   # decode / re-bake can be slow
                resp = self.client.call(req, timeout=timeout)
                if resp.get("ok"):
                    self._set_connected(True)
                    if "status" in resp:
                        self.status_ready.emit(resp["status"])
                    self.command_done.emit(key, True, "")
                else:
                    self.command_done.emit(key, False, resp.get("error", "unknown error"))
                self._last_preview_id = None   # force one fresh frame after any change
                next_preview = 0.0

            now = time.monotonic()
            if now >= next_status:
                self._poll_status()
                next_status = time.monotonic() + (STATUS_INTERVAL if self.preview_enabled else STATUS_INTERVAL_HIDDEN)
            if self.preview_enabled and now >= next_preview and self._connected:
                self._poll_preview()
                next_preview = time.monotonic() + PREVIEW_INTERVAL

            wake_at = next_status
            if self.preview_enabled:
                wake_at = min(wake_at, next_preview)
            with self._cond:
                if self._stop:
                    return
                if self._pending:
                    wake_at = min(wake_at, min(t for t, _ in self._pending.values()))
                self._cond.wait(max(0.0, wake_at - time.monotonic()))


# ---------------------------------------------------------------------------
# Small background jobs (systemctl, thumbnails)
# ---------------------------------------------------------------------------

class _Emitter(QObject):
    done = pyqtSignal(object)


class _Job(QRunnable):
    def __init__(self, fn, emitter: _Emitter):
        super().__init__()
        self.fn, self.emitter = fn, emitter

    def run(self):
        try:
            result = self.fn()
        except Exception as e:  # noqa: BLE001 - surfaced to the callback as data
            result = e
        self.emitter.done.emit(result)


def run_async(parent: QObject, fn, callback):
    em = _Emitter(parent)
    em.done.connect(callback)
    em.done.connect(em.deleteLater)
    QThreadPool.globalInstance().start(_Job(fn, em))


def _systemctl(*args) -> tuple[bool, str]:
    try:
        r = subprocess.run(["systemctl", "--user", *args, SERVICE_NAME], capture_output=True, text=True, timeout=15)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


SERVICE_STATES = {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "refreshing"}


def _service_state() -> tuple[str, bool]:
    _, active = _systemctl("is-active")
    ok, _ = _systemctl("is-enabled")
    state = (active.split() or ["unknown"])[0]
    return (state if state in SERVICE_STATES else "n/a"), ok   # e.g. no user bus: an error text, not a state


def _make_thumbnail(path: str) -> QImage:
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-frames:v", "1", "-vf", "scale=272:-2",
                            "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, timeout=20)
        return QImage.fromData(r.stdout, "PNG")
    from PyQt6.QtGui import QImageReader
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    return reader.read()


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def card(title: str, right: QWidget | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 14, 18, 18)
    lay.setSpacing(12)
    head = QHBoxLayout()
    lbl = QLabel(title)
    lbl.setObjectName("cardTitle")
    head.addWidget(lbl)
    head.addStretch()
    if right is not None:
        head.addWidget(right)
    lay.addLayout(head)
    return frame, lay


def field_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("fieldLabel")
    return lbl


def value_badge(text: str, width: int = 62) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("valueBadge")
    lbl.setFixedWidth(width)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


def _repolish(w: QWidget):
    w.style().unpolish(w)
    w.style().polish(w)


def load_prefs() -> dict:
    try:
        with open(GUI_PREFS_PATH) as f:
            prefs = json.load(f)
        return prefs if isinstance(prefs, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict):
    try:
        C.write_layout(GUI_PREFS_PATH, json.dumps(prefs, indent=2) + "\n")   # atomic small-file write
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Visual HUD editor (custom theme): inspector bar + controller
# ---------------------------------------------------------------------------

def _hex(c: QColor) -> str:
    return c.name(QColor.NameFormat.HexRgb) + (f"{c.alpha():02x}" if c.alpha() < 255 else "")


def _qcolor(value) -> QColor:
    try:
        r, g, b, a = C.parse_color(value, (255, 255, 255, 255))
        return QColor(r, g, b, a)
    except ValueError:
        return QColor("white")


# per element type: the two "size" fields the inspector exposes -> (label, key, default)
SIZE_FIELDS = {
    "text": [("SIZE", "size", 32)],
    "ring": [("RADIUS", "radius", 60), ("THICK", "thickness", 8)],
    "line": [("WIDTH", "width", 2)],
    "rect": [("W", "w", 100), ("H", "h", 40)],
    "box": [("W", "w", 100), ("H", "h", 40)],
    "bar": [("W", "w", 200), ("H", "h", 8)],
    "sparkline": [("W", "w", 300), ("H", "h", 100)],
    "image": [("W", "w", 64), ("H", "h", 64)],
}


class HudInspector(QFrame):
    """Inline property bar for the selected customize.json element."""
    patched = pyqtSignal(dict)       # element keys to set
    moved_to = pyqtSignal(int, int)  # new anchor x, y
    color_clicked = pyqtSignal()
    done = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("inspector")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.tag = W.Chip("CLICK AN ELEMENT", W.MUTED, dot=False)
        row.addWidget(self.tag)
        row.addSpacing(4)
        self.x_spin, self.y_spin = self._spin(-1000, 2000), self._spin(-1000, 2000)
        self.x_spin.valueChanged.connect(self._on_xy)
        self.y_spin.valueChanged.connect(self._on_xy)
        self.size_labels, self.size_spins = [], []
        widgets = [(field_label("X"), self.x_spin), (field_label("Y"), self.y_spin)]
        for _ in range(2):
            lbl, spin = field_label(""), self._spin(1, 2000)
            spin.valueChanged.connect(self._on_size)
            self.size_labels.append(lbl)
            self.size_spins.append(spin)
            widgets.append((lbl, spin))
        for lbl, spin in widgets:
            row.addWidget(lbl)
            row.addWidget(spin)
        row.addStretch()
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.text_label = field_label("TEXT")
        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText("e.g. CPU {cpu_temp}°{temp_unit}")
        self.text_edit.setToolTip(
            "Variables: {cpu_temp} {gpu_temp} {cpu_load} {gpu_load} {gpu_power} {gpu_wattage} {gpu_power_str}\n"
            "{gpu_clock} {cpu_freq} {cpu_freq_ghz} {ram_percent} {ram_used} {ram_total} {time} {date} {temp_unit}\n"
            "plus your custom_sensors. {gpu_power_str} prints e.g. 85W.")
        self.text_edit.textEdited.connect(lambda t: self.patched.emit({"text": t}))
        self.color_btn = QPushButton("Color")
        self.color_btn.setObjectName("swatch")
        self.color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.color_btn.clicked.connect(self.color_clicked)
        self.note = QLabel("")
        self.note.setObjectName("fieldLabel")
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.setCursor(Qt.CursorShape.PointingHandCursor)
        done.clicked.connect(self.done)
        row2.addWidget(self.text_label)
        row2.addWidget(self.text_edit, 1)
        row2.addStretch(0)   # takes the room when the element has no text field
        row2.addWidget(self.color_btn)
        row2.addWidget(self.note)
        row2.addWidget(done)
        lay.addLayout(row2)
        self._kind = None
        self.show_element(None, -1)

    @staticmethod
    def _spin(lo: int, hi: int) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setFixedWidth(66)
        s.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return s

    def _set_spin(self, spin: QSpinBox, value):
        spin.blockSignals(True)
        spin.setValue(int(round(float(value))))
        spin.blockSignals(False)

    def show_element(self, el: dict | None, index: int):
        """Populate the fields for `el` (None: nothing selected) without emitting edits."""
        editable = el is not None
        self._kind = el.get("type") if editable else None
        for w in (self.x_spin, self.y_spin, self.color_btn, self.text_edit):
            w.setEnabled(editable)
        if not editable:
            self.tag.set("CLICK AN ELEMENT", W.MUTED)
            for lbl, spin in zip(self.size_labels, self.size_spins):
                lbl.hide()
                spin.hide()
            self.text_label.hide()
            self.text_edit.hide()
            self._paint_swatch(None)
            return
        self.tag.set(f"{str(self._kind).upper()} #{index + 1}", W.CYAN)
        ax, ay = ("x1", "y1") if self._kind == "line" else ("x", "y")
        self._set_spin(self.x_spin, el.get(ax, 0))
        self._set_spin(self.y_spin, el.get(ay, 0))
        fields = SIZE_FIELDS.get(self._kind, [])
        for i, (lbl, spin) in enumerate(zip(self.size_labels, self.size_spins)):
            if i < len(fields):
                name, key, default = fields[i]
                value = el.get(key)
                if value is None and key == "radius":
                    value = min(float(el.get("w", 2 * default)), float(el.get("h", 2 * default))) / 2
                lbl.setText(name)
                self._set_spin(spin, value if value is not None else default)
                lbl.show()
                spin.show()
            else:
                lbl.hide()
                spin.hide()
        color_key = C.EDITABLE_COLOR_KEY.get(self._kind)
        self.color_btn.setVisible(color_key is not None)
        self._paint_swatch(el.get(color_key) if color_key else None)
        has_text = self._kind in ("text", "ring")
        self.text_label.setVisible(has_text)
        self.text_edit.setVisible(has_text)
        if has_text and self.text_edit.text() != str(el.get("text", "")):
            self.text_edit.setText(str(el.get("text", "")))

    def _paint_swatch(self, value):
        c = _qcolor(value) if value is not None else QColor(W.BORDER)
        fg = "#000000" if c.lightness() > 150 else "#ffffff"
        self.color_btn.setStyleSheet(f"QPushButton#swatch {{ background: {c.name()}; color: {fg}; "
                                     f"border: 1px solid {W.VIOLET_HI}; }}")

    def _on_xy(self, *_):
        self.moved_to.emit(self.x_spin.value(), self.y_spin.value())

    def _on_size(self, *_):
        fields = SIZE_FIELDS.get(self._kind, [])
        self.patched.emit({key: spin.value() for (_, key, _), spin in zip(fields, self.size_spins)})


class HudEditor(QObject):
    """Owns the in-memory customize.json while "Edit HUD Layout" is on: hit boxes for the stage,
    drag / nudge / inspector edits, and throttled atomic saves the daemon hot-reloads (<= 1 s).
    Edits made to the file outside the GUI win: they are reloaded instead of being overwritten.
    """
    status = pyqtSignal(str)

    SAVE_DELAY_MS = 300

    def __init__(self, window: QMainWindow, stage: W.PumpStage, inspector: HudInspector):
        super().__init__(window)
        self.window, self.stage, self.inspector = window, stage, inspector
        self.layout: dict | None = None
        self.key = "elements"
        self.selected = -1
        self.sensors: dict = {}
        self.celsius = True
        self._origin: dict | None = None   # element as it was when the drag started
        self._stamp = None
        self._backed_up = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save)

        stage.element_pressed.connect(self._on_pressed)
        stage.element_dragged.connect(self._on_dragged)
        stage.drag_finished.connect(self._flush)
        stage.nudged.connect(self._on_nudged)
        inspector.moved_to.connect(self._on_moved_to)
        inspector.patched.connect(self._on_patched)
        inspector.color_clicked.connect(self._pick_color)

    # ---- lifecycle ----
    @staticmethod
    def _file_stamp():
        try:
            st = os.stat(C.CUSTOM_PATH)
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def start(self) -> bool:
        try:
            C.seed_default()
            self.layout = C.load_layout()
        except (OSError, ValueError) as e:
            self.status.emit(f"Can't edit: customize.json is invalid ({e}). Fix it in a text editor first.")
            self.layout = None
            return False
        self._stamp = self._file_stamp()
        self._backed_up = False
        self.selected = -1
        self.refresh()
        self.status.emit("Edit mode: click an element on the preview, drag it, or use the inspector.")
        return True

    def stop(self):
        self._flush()
        self.layout, self.selected = None, -1
        self.stage.set_edit_boxes([], -1)

    def active(self) -> bool:
        return self.layout is not None

    # ---- model ----
    def elements(self) -> list:
        if self.layout is None:
            return []
        w, h = self.stage.canvas_size()
        key = "elements_vertical" if h > w and self.layout.get("elements_vertical") else "elements"
        if key != self.key:
            self.key, self.selected = key, -1
        return self.layout.setdefault(key, [])

    def update_data(self, sensors: dict, celsius: bool):
        self.sensors, self.celsius = sensors, celsius
        if self.layout is None:
            return
        if not self._save_timer.isActive() and self._origin is None and self._file_stamp() != self._stamp:
            self._reload("customize.json changed on disk — reloaded")
            return
        self.refresh()

    def _reload(self, msg: str):
        try:
            self.layout = C.load_layout()
        except (OSError, ValueError) as e:
            self.status.emit(f"customize.json changed on disk but is invalid, editor paused: {e}")
            self._stamp = self._file_stamp()
            return
        self._stamp = self._file_stamp()
        self.selected = -1
        self.refresh()
        self.status.emit(msg)

    def refresh(self):
        els = self.elements()
        boxes = []
        for i, el in enumerate(els):
            bb = C.element_bbox(el, self.sensors, self.celsius)
            if bb is not None:
                x0, y0, x1, y1 = bb
                boxes.append((i, QRectF(x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0)),
                              f"{str(el.get('type', '?')).upper()} #{i + 1}"))
        if self.selected >= len(els):
            self.selected = -1
        self.stage.set_edit_boxes(boxes, self.selected)
        self.inspector.show_element(els[self.selected] if self.selected >= 0 else None, self.selected)

    def _current(self) -> dict | None:
        els = self.elements()
        return els[self.selected] if 0 <= self.selected < len(els) else None

    # ---- input ----
    def _on_pressed(self, idx: int):
        self.selected = idx
        el = self._current()
        self._origin = copy.deepcopy(el) if el is not None else None
        self.refresh()

    def _on_dragged(self, dx: float, dy: float):
        el = self._current()
        if el is None or self._origin is None:
            return
        moved = copy.deepcopy(self._origin)
        C.move_element(moved, dx, dy)
        el.update(moved)
        self.refresh()
        self._schedule_save(throttle=True)

    def _on_nudged(self, dx: int, dy: int):
        el = self._current()
        if el is not None:
            C.move_element(el, dx, dy)
            self.refresh()
            self._schedule_save()

    def _on_moved_to(self, x: int, y: int):
        el = self._current()
        if el is None:
            return
        ax, ay = ("x1", "y1") if el.get("type") == "line" else ("x", "y")
        C.move_element(el, x - float(el.get(ax, 0)), y - float(el.get(ay, 0)))
        self.refresh()
        self._schedule_save()

    def _on_patched(self, patch: dict):
        el = self._current()
        if el is None:
            return
        el.update(patch)
        if el.get("type") == "ring" and "radius" in patch:
            el.pop("w", None)
            el.pop("h", None)
        self.refresh()
        self._schedule_save()

    def _pick_color(self):
        el = self._current()
        key = C.EDITABLE_COLOR_KEY.get(el.get("type")) if el else None
        if key is None:
            return
        c = QColorDialog.getColor(_qcolor(el.get(key)), self.window, f"{el.get('type', '').title()} color",
                                  QColorDialog.ColorDialogOption.ShowAlphaChannel |
                                  QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if c.isValid():
            self._on_patched({key: _hex(c)})

    # ---- persistence ----
    def _schedule_save(self, throttle: bool = False):
        """throttle=True (dragging): save at most every SAVE_DELAY_MS so the panel follows the drag;
        otherwise debounce so typing a number writes once."""
        if throttle and self._save_timer.isActive():
            return
        self._save_timer.start(self.SAVE_DELAY_MS)

    def _flush(self):
        self._origin = None
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save()

    def _save(self):
        if self.layout is None:
            return
        if self._file_stamp() != self._stamp:
            self._reload("customize.json was edited elsewhere — reloaded it instead of overwriting")
            return
        try:
            C.save_layout(self.layout, backup=not self._backed_up)
        except (OSError, ValueError) as e:
            self.status.emit(f"Save failed: {e}")
            return
        if not self._backed_up:
            self.status.emit("Saved to customize.json (the version before this edit session is customize.json.bak)")
        self._backed_up = True
        self._stamp = self._file_stamp()
        self.inspector.note.setText("SAVED ✓")


class MainWindow(QMainWindow):
    def __init__(self, tray_session: bool = False):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 900)
        self.setMinimumSize(1100, 720)
        self.setAcceptDrops(True)

        self._touched: dict[str, float] = {}
        self._usb = False
        self._connected = False
        self._recents: list[str] = []
        self._active_media: str | None = None
        self._cards: dict[str, W.MediaCard] = {}
        self._thumbs: dict[str, QImage] = {}
        self._thumb_pending: set[str] = set()
        self._celsius = True
        self._sensors: dict = {}
        self._overlay_cfg: dict = {}
        self._custom_error: str | None = None
        self._service_busy = False
        self._service_running = False
        self._metric_values = ["cpu_temp", "gpu_temp", "cpu_load", "time"]   # primary + 3 slots, last applied
        self._prefs = load_prefs()
        self._close_to_tray = bool(self._prefs.get("close_to_tray", False)) or tray_session
        self._quitting = False
        self._tray_hint_shown = False
        self._status: dict = {}

        self.worker = IpcWorker()
        self.worker.status_ready.connect(self.on_status)
        self.worker.preview_ready.connect(self.on_preview)
        self.worker.connection_changed.connect(self.on_connection)
        self.worker.command_done.connect(self.on_command_done)

        self._build_ui()
        self.editor = HudEditor(self, self.stage, self.inspector)
        self.editor.status.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.inspector.done.connect(lambda: self._set_edit_mode(False))
        self.tray = self._build_tray()
        self.worker.start()

        self._svc_timer = QTimer(self)
        self._svc_timer.timeout.connect(self.refresh_service)
        self._svc_timer.start(4000)
        self.refresh_service()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 14, 24, 14)
        root.setSpacing(10)
        root.addWidget(W.Banner(self._cjk_family, __version__))

        body = QHBoxLayout()
        body.setSpacing(20)
        root.addLayout(body, 1)
        body.addLayout(self._build_stage_column(), 11)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("scrollInner")
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(14)
        col.addWidget(self._build_engine_card())
        col.addWidget(self._build_media_card())
        col.addWidget(self._build_theme_card())
        col.addStretch()
        scroll.setWidget(inner)
        scroll.setMinimumWidth(510)   # media grid + framing + theme cards never get squeezed/clipped
        body.addWidget(scroll, 10)

    _cjk_family: str | None = None   # set by main() after font registration

    def _build_stage_column(self) -> QVBoxLayout:
        """Left column, never scrolls: status pills, the pump preview, the HUD inspector (edit mode
        only) and the Hardware Deck, so every hardware/service control is always on screen."""
        col = QVBoxLayout()
        col.setSpacing(10)

        pills = QHBoxLayout()
        self.pill_daemon = W.Chip("DAEMON ...", W.MUTED)   # ASCII dots: JZFS Sans draws U+2026 as one dot
        self.pill_usb = W.Chip("USB ...", W.MUTED)
        self.pill_fps = W.Chip("-- FPS", W.CYAN, dot=False)
        self.pill_pkt = W.Chip("-- PKT", W.CYAN, dot=False)
        for c in (self.pill_daemon, self.pill_usb, self.pill_fps, self.pill_pkt):
            pills.addWidget(c)
        pills.addStretch()
        col.addLayout(pills)

        self.stage = W.PumpStage()
        col.addWidget(self.stage, 1)
        self.edit_toggle = W.PillToggle("Edit HUD Layout")
        self.edit_toggle.setToolTip("Drag customize.json elements on the live preview (CUSTOM HUD theme)")
        self.edit_toggle.toggled.connect(self._set_edit_mode)
        self.stage.set_corner_widget(self.edit_toggle)
        self.edit_toggle.hide()

        self.inspector = HudInspector()
        self.inspector.hide()
        col.addWidget(self.inspector)
        col.addWidget(self._build_hardware_deck())
        return col

    def _build_hardware_deck(self) -> QFrame:
        self.svc_chip = W.Chip("SERVICE ...", W.MUTED)
        frame, lay = card("HARDWARE DECK", self.svc_chip)
        lay.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.deck_overlay = W.PillToggle("Overlay")
        self.deck_overlay.toggled.connect(self._on_overlay_toggled)
        self.mode_seg = W.SegmentedControl([("Horizontal", "horizontal"), ("Vertical", "vertical")])
        self.mode_seg.setMinimumWidth(180)
        self.mode_seg.changed.connect(self._on_mode_changed)
        self.flip_toggle = W.PillToggle("180° Flip")
        self.flip_toggle.toggled.connect(self._on_mirror_toggled)
        row.addWidget(self.deck_overlay)
        row.addWidget(self.mode_seg, 1)
        row.addWidget(self.flip_toggle)
        lay.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(field_label("BRIGHTNESS"))
        self.brightness = W.NeonSlider()
        self.brightness.setRange(0, 100)
        self.brightness.setValue(50)
        self.brightness.valueChanged.connect(self._on_brightness_changed)
        self.brightness_value = value_badge("50%")
        self.unit_seg = W.SegmentedControl([("°C", True), ("°F", False)])
        self.unit_seg.setFixedWidth(104)
        self.unit_seg.changed.connect(self._on_celsius_changed)
        row.addWidget(self.brightness, 1)
        row.addWidget(self.brightness_value)
        row.addSpacing(4)
        row.addWidget(self.unit_seg)
        lay.addLayout(row)

        rule = QFrame()
        rule.setObjectName("deckRule")
        rule.setFixedHeight(1)
        lay.addWidget(rule)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(field_label("DAEMON"))
        self.svc_restart = QPushButton("Restart")
        self.svc_restart.setObjectName("primary")
        self.svc_restart.clicked.connect(lambda: self._service_call("restart"))
        self.svc_power = QPushButton("Stop")
        self.svc_power.setObjectName("danger")
        self.svc_power.clicked.connect(lambda: self._service_call("stop" if self._service_running else "start"))
        for b in (self.svc_restart, self.svc_power):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumWidth(92)
            row.addWidget(b)
        row.addStretch()
        self.autostart_toggle = W.PillToggle("Autostart")
        self.autostart_toggle.setToolTip("Start the LT360 daemon on login (systemctl --user enable)")
        self.autostart_toggle.toggled.connect(self._on_autostart_toggled)
        row.addWidget(self.autostart_toggle)
        lay.addLayout(row)
        return frame

    def _build_engine_card(self) -> QFrame:
        frame, lay = card("ENGINE MODE")
        self.engine_seg = W.SegmentedControl([
            ("⚡ PERFORMANCE · Low CPU", "performance"),
            ("🎬 FULL · Unlimited Video", "full"),
        ])
        self.engine_seg.setMinimumHeight(44)
        self.engine_seg.changed.connect(self._on_engine_changed)
        lay.addWidget(self.engine_seg)
        self.engine_note = QLabel(ENGINE_NOTES["full"])
        self.engine_note.setObjectName("fieldLabel")
        self.engine_note.setWordWrap(True)
        lay.addWidget(self.engine_note)
        return frame

    def _build_media_card(self) -> QFrame:
        frame, lay = card("MEDIA")
        self.gallery = QGridLayout()
        self.gallery.setSpacing(10)
        self.gallery.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lay.addLayout(self.gallery)
        self._rebuild_gallery()

        # framing: how the media is fitted onto the 854x480 / 480x854 canvas, plus playback speed
        head = QHBoxLayout()
        head.addWidget(field_label("FRAMING & SPEED"))
        head.addStretch()
        reset = QPushButton("Reset")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setToolTip("Cover · 1.00x zoom · centered · 1.0x speed")
        reset.clicked.connect(self._reset_framing)
        head.addWidget(reset)
        lay.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.fit_seg = W.SegmentedControl([("Cover", "cover"), ("Contain", "contain")])
        self.fit_seg.setFixedWidth(170)
        self.fit_seg.setToolTip("Cover fills the screen and crops the overflow; Contain shows the whole frame")
        self.fit_seg.changed.connect(lambda v: self._queue_framing({"fit": v}, delay=0.05))
        self.speed_seg = W.SegmentedControl(SPEEDS)
        self.speed_seg.set_value(1.0)
        self.speed_seg.setToolTip("Playback speed for GIFs and videos")
        self.speed_seg.changed.connect(lambda v: self._queue_framing({"speed": v}, delay=0.05))
        row.addWidget(self.fit_seg)
        row.addWidget(self.speed_seg, 1)
        lay.addLayout(row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self.zoom_slider = W.NeonSlider()
        self.zoom_slider.setRange(100, 250)
        self.zoom_slider.setValue(100)
        self.pan_x_slider, self.pan_y_slider = W.NeonSlider(bipolar=True), W.NeonSlider(bipolar=True)
        self.framing_badges = {}
        for r, (name, key, slider) in enumerate((("ZOOM", "zoom", self.zoom_slider), ("PAN X", "pan_x", self.pan_x_slider),
                                                 ("PAN Y", "pan_y", self.pan_y_slider))):
            if key != "zoom":
                slider.setRange(-100, 100)
                slider.setValue(0)
            badge = value_badge("", 70)
            self.framing_badges[key] = badge
            slider.valueChanged.connect(lambda v, k=key: self._on_framing_slider(k, v))
            grid.addWidget(field_label(name), r, 0)
            grid.addWidget(slider, r, 1)
            grid.addWidget(badge, r, 2)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self._set_framing_badges()
        return frame

    def _build_theme_card(self) -> QFrame:
        self.theme_overlay_toggle = W.PillToggle("Overlay")
        self.theme_overlay_toggle.toggled.connect(self._on_overlay_toggled)
        frame, lay = card("OVERLAY THEME", self.theme_overlay_toggle)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.theme_cards: dict[str, W.ThemeCard] = {}
        for key in THEMES:
            tc = W.ThemeCard(key)
            tc.clicked.connect(self._on_theme_clicked)
            self.theme_cards[key] = tc
            row.addWidget(tc)
        lay.addLayout(row)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        open_btn = QPushButton("Open customize.json")
        open_btn.clicked.connect(self._open_customize)
        # ASCII "..." on purpose: the bundled JZFS Sans draws U+2026 as a single dot ("Load Preset.")
        self.preset_btn = QPushButton("Load Preset...")
        self.preset_btn.setObjectName("menuButton")   # QSS reserves room for the menu arrow
        self.preset_btn.setMinimumWidth(self.preset_btn.fontMetrics().horizontalAdvance("Load Preset...") + 76)
        self.preset_menu = QMenu(self.preset_btn)
        self.preset_menu.aboutToShow.connect(lambda: self._fill_preset_menu(self.preset_menu))
        self.preset_btn.setMenu(self.preset_menu)
        for b in (open_btn, self.preset_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.custom_status = QLabel("")
        self.custom_status.setWordWrap(True)
        actions.addWidget(open_btn)
        actions.addWidget(self.preset_btn)
        actions.addStretch()
        lay.addLayout(actions)
        lay.addWidget(self.custom_status)

        lay.addWidget(field_label("LIVE TELEMETRY"))
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tiles = {}
        for key, label in (("cpu_temp", "CPU TEMP"), ("gpu_temp", "GPU TEMP"), ("cpu_load", "CPU LOAD"),
                           ("gpu_load", "GPU LOAD"), ("gpu_power", "GPU POWER"), ("time", "TIME")):
            t = W.ReadoutTile(label)
            self.tiles[key] = t
            tiles.addWidget(t)
        lay.addLayout(tiles)

        lay.addWidget(field_label("READOUT MATRIX"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.primary_combo = self._metric_combo()
        self.slot_combos = [self._metric_combo() for _ in range(3)]
        for i, (name, combo) in enumerate([("PRIMARY", self.primary_combo)] +
                                           [(f"SLOT {i + 1}", c) for i, c in enumerate(self.slot_combos)]):
            grid.addWidget(field_label(name), (i // 2) * 2, i % 2)
            grid.addWidget(combo, (i // 2) * 2 + 1, i % 2)
        lay.addLayout(grid)
        return frame

    def _fill_preset_menu(self, menu: QMenu):
        menu.clear()
        presets = C.list_presets()
        for name in presets:
            menu.addAction(name.replace("_", " ").title()).triggered.connect(
                lambda _=False, n=name: self._apply_preset(n))
        if not presets:
            menu.addAction("No presets found").setEnabled(False)

    def _apply_preset(self, name: str):
        def done(result, n=name):
            if isinstance(result, Exception):
                self.custom_status.setText(f"Preset failed: {result}")
                return
            self.custom_status.setText(f"Loaded preset: {n} (previous saved as .bak)")
            self._on_theme_clicked("custom")
        run_async(self, lambda: C.apply_preset(name), done)

    def _open_customize(self):
        def launch():
            C.seed_default()
            C.open_in_text_editor(C.CUSTOM_PATH)
            return None

        def done(result):
            if isinstance(result, Exception):
                self.custom_status.setText(f"Could not open editor: {result}")
                return
            self.custom_status.setText("Opened customize.json — save to hot-reload")
            self._on_theme_clicked("custom")
        run_async(self, launch, done)

    def _metric_combo(self) -> QComboBox:
        c = QComboBox()
        for m in METRICS:
            c.addItem(METRIC_LABELS[m], m)
        c.activated.connect(self._on_metrics_changed)
        return c

    # --------------------------------------------------------------- tray

    def _build_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(_tray_icon(), self)
        tray.setToolTip(APP_TITLE)
        menu = QMenu()
        menu.addAction("Show / Hide Window").triggered.connect(self.toggle_visible)
        menu.addSeparator()

        theme_menu = menu.addMenu("Overlay Theme")
        self._tray_themes = QActionGroup(theme_menu)
        for key in THEMES:
            a = theme_menu.addAction(THEME_LABELS[key])
            a.setCheckable(True)
            a.setData(key)
            self._tray_themes.addAction(a)
            a.triggered.connect(lambda _=False, k=key: self._on_theme_clicked(k))
        theme_menu.addSeparator()
        self._tray_overlay = theme_menu.addAction("Overlay Visible")
        self._tray_overlay.setCheckable(True)
        self._tray_overlay.triggered.connect(self._on_overlay_toggled)

        presets = menu.addMenu("HUD Presets")
        presets.aboutToShow.connect(lambda: self._fill_preset_menu(presets))

        engine_menu = menu.addMenu("Engine Mode")
        self._tray_engine = QActionGroup(engine_menu)
        for mode, label in (("performance", "⚡ Performance (Low CPU)"), ("full", "🎬 Full (Unlimited Video)")):
            a = engine_menu.addAction(label)
            a.setCheckable(True)
            a.setData(mode)
            self._tray_engine.addAction(a)
            a.triggered.connect(lambda _=False, m=mode: self._on_engine_changed(m))

        bright_menu = menu.addMenu("Brightness")
        self._tray_bright = QActionGroup(bright_menu)
        for v in TRAY_BRIGHTNESS:
            a = bright_menu.addAction(f"{v}%")
            a.setCheckable(True)
            a.setData(v)
            self._tray_bright.addAction(a)
            a.triggered.connect(lambda _=False, b=v: self.brightness.setValue(b))

        menu.addSeparator()
        self._tray_close = menu.addAction("Close to Tray")
        self._tray_close.setCheckable(True)
        self._tray_close.setChecked(self._close_to_tray)
        self._tray_close.toggled.connect(self._on_close_to_tray)
        menu.addAction("Quit").triggered.connect(self.quit)

        self._tray_menu = menu   # QSystemTrayIcon does not take ownership
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        return tray

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.toggle_visible()

    def toggle_visible(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _on_close_to_tray(self, on: bool):
        self._close_to_tray = on
        self._prefs["close_to_tray"] = on
        save_prefs(self._prefs)

    def quit(self):
        self._quitting = True
        self.close()

    def _update_tray(self, st: dict):
        if self.tray is None:
            return
        sensors = st.get("sensors", {})
        parts = [f"{name} {W.format_metric(key, sensors, self._celsius)}" for name, key in
                 (("CPU", "cpu_temp"), ("GPU", "gpu_temp")) if sensors.get(key) is not None]
        link = "USB linked" if self._usb else "USB offline"
        self.tray.setToolTip(f"{APP_TITLE}\n{'  ·  '.join(parts + [link])}\nBrightness {st.get('brightness', '?')}%")
        overlay = st.get("overlay", {})
        for a in self._tray_themes.actions():
            a.setChecked(a.data() == overlay.get("theme"))
        self._tray_overlay.setChecked(bool(overlay.get("enabled")))
        for a in self._tray_engine.actions():
            a.setChecked(a.data() == st.get("engine_mode"))
        for a in self._tray_bright.actions():
            a.setChecked(a.data() == st.get("brightness"))

    # ------------------------------------------------------------ gallery

    def _rebuild_gallery(self):
        while self.gallery.count():
            w = self.gallery.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._cards.clear()
        cols = 3
        for i, path in enumerate(self._recents):
            mc = W.MediaCard(path, self._thumbs.get(path))
            mc.set_active(path == self._active_media)
            mc.activated.connect(self.apply_media)
            mc.removed.connect(self._remove_recent)
            self._cards[path] = mc
            self.gallery.addWidget(mc, i // cols, i % cols)
            self._request_thumb(path)
        tile = W.DropTile()
        tile.clicked.connect(self.browse_media)
        n = len(self._recents)
        self.gallery.addWidget(tile, n // cols, n % cols)

    def _request_thumb(self, path: str):
        if path in self._thumbs or path in self._thumb_pending or not os.path.isfile(path):
            return
        self._thumb_pending.add(path)

        def done(img, p=path):
            self._thumb_pending.discard(p)
            if isinstance(img, QImage) and not img.isNull():
                self._thumbs[p] = img
                if p in self._cards:
                    self._cards[p].set_thumb(img)
        run_async(self, lambda p=path: _make_thumbnail(p), done)

    def browse_media(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose media", os.path.expanduser("~"), MEDIA_FILTER,
            options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self.apply_media(path)

    def apply_media(self, path: str):
        path = os.path.abspath(path)
        self.stage.set_busy(True)
        self.worker.submit("media", {"action": "set_media", "path": path})

    def _remove_recent(self, path: str):
        self._recents = [p for p in self._recents if p != path]
        self._touch("recents")
        self._rebuild_gallery()
        self.worker.submit("rm:" + path, {"action": "remove_recent", "path": path})

    # ----------------------------------------------------------- handlers

    def _touch(self, key: str):
        self._touched[key] = time.monotonic()

    def _fresh(self, key: str) -> bool:
        """True if polled status may overwrite this control (user hasn't just touched it)."""
        return time.monotonic() - self._touched.get(key, 0.0) > TOUCH_HOLD

    def _on_brightness_changed(self, value: int):
        self.brightness_value.setText(f"{value}%")     # instant, UI-thread only
        self._touch("brightness")
        self.worker.submit("brightness", {"action": "set_brightness", "value": int(value)}, delay=0.06)

    def _on_mode_changed(self, mode: str):
        self._touch("mode")
        self.mode_seg.set_value(mode)
        self.stage.set_vertical(mode == "vertical")
        if self.editor.active():
            self.editor.refresh()   # vertical mode may edit elements_vertical instead
        self.worker.submit("mode", {"action": "set_mode", "value": mode})

    # framing: sliders are integers (zoom x100, pan in %), the daemon wants floats
    def _framing_from_controls(self) -> dict:
        return {"zoom": self.zoom_slider.value() / 100, "pan_x": self.pan_x_slider.value() / 100,
                "pan_y": self.pan_y_slider.value() / 100}

    def _set_framing_badges(self):
        f = self._framing_from_controls()
        self.framing_badges["zoom"].setText(f"{f['zoom']:.2f}x")
        self.framing_badges["pan_x"].setText(f"{self.pan_x_slider.value():+d}%")
        self.framing_badges["pan_y"].setText(f"{self.pan_y_slider.value():+d}%")

    def _on_framing_slider(self, key: str, _value: int):
        self._set_framing_badges()
        self._queue_framing({key: self._framing_from_controls()[key]}, delay=0.3)   # re-bakes: coalesce drags

    def _queue_framing(self, patch: dict, delay: float):
        self._touch("framing")
        self.worker.submit("framing", {"action": "set_framing", "value": patch}, delay=delay, merge=True)

    def _reset_framing(self):
        for slider, v in ((self.zoom_slider, 100), (self.pan_x_slider, 0), (self.pan_y_slider, 0)):
            slider.blockSignals(True)
            slider.setValue(v)
            slider.blockSignals(False)
        self.fit_seg.set_value("cover")
        self.speed_seg.set_value(1.0)
        self._set_framing_badges()
        self._touch("framing")
        self.worker.submit("framing", {"action": "set_framing", "value": {}, "reset": True})

    def _apply_framing_status(self, framing: dict):
        if any(s.isSliderDown() for s in (self.zoom_slider, self.pan_x_slider, self.pan_y_slider)):
            return
        for slider, v in ((self.zoom_slider, round(float(framing.get("zoom", 1.0)) * 100)),
                          (self.pan_x_slider, round(float(framing.get("pan_x", 0.0)) * 100)),
                          (self.pan_y_slider, round(float(framing.get("pan_y", 0.0)) * 100))):
            if slider.value() != v:
                slider.blockSignals(True)
                slider.setValue(v)
                slider.blockSignals(False)
        self.fit_seg.set_value(framing.get("fit", "cover"))
        self.speed_seg.set_value(float(framing.get("speed", 1.0)))
        self._set_framing_badges()

    def _on_engine_changed(self, mode: str):
        self._touch("engine")
        self._touched["engine"] += 60.0   # rebuilding the reel can take a while: hold off polled status until done
        self.engine_seg.set_value(mode)
        self.engine_note.setText(ENGINE_NOTES.get(mode, ""))
        self.stage.set_busy(True)
        self.worker.submit("engine", {"action": "set_engine_mode", "value": mode})

    def _on_mirror_toggled(self, on: bool):
        self._touch("mirror")
        self.worker.submit("mirror", {"action": "set_mirror", "value": bool(on)})

    def _on_celsius_changed(self, celsius: bool):
        self._touch("celsius")
        self._celsius = celsius
        self._refresh_theme_cards()
        self.worker.submit("celsius", {"action": "set_celsius", "value": bool(celsius)})

    def _queue_overlay(self, patch: dict):
        self._touch("overlay")
        self._overlay_cfg.update(patch)
        self._refresh_theme_cards()
        self.worker.submit("overlay", {"action": "set_overlay", "value": patch}, delay=0.05, merge=True)

    def _on_overlay_toggled(self, on: bool):
        self.deck_overlay.set_on(on)
        self.theme_overlay_toggle.set_on(on)
        self._queue_overlay({"enabled": on})

    def _on_theme_clicked(self, key: str):
        self._queue_overlay({"theme": key, "enabled": True})   # picking a theme implies "show it"
        self.deck_overlay.set_on(True)
        self.theme_overlay_toggle.set_on(True)
        self._sync_edit_toggle()

    def _metric_combos(self) -> list[QComboBox]:
        return [self.primary_combo] + self.slot_combos

    def _on_metrics_changed(self, *_):
        """A metric picked in one slot that is already shown in another swaps the two, so the readout
        never repeats a metric (the daemon normalizes the same way for older configs / the CLI)."""
        combos = self._metric_combos()
        new = [c.currentData() for c in combos]
        for i, (n, o) in enumerate(zip(new, self._metric_values)):
            if n != o and n != "off":
                for j, v in enumerate(new):
                    if j != i and v == n:
                        new[j] = o
        primary, secondary = normalize_readout(new[0], new[1:])
        self._metric_values = [primary] + secondary
        for combo, v in zip(combos, self._metric_values):
            combo.setCurrentIndex(METRICS.index(v))
        self._queue_overlay({"primary": primary, "secondary": secondary})

    # ------------------------------------------------------------ HUD editor

    def _custom_active(self) -> bool:
        return self._overlay_cfg.get("theme") == "custom"

    def _sync_edit_toggle(self):
        custom = self._custom_active()
        self.edit_toggle.setVisible(custom)
        if not custom and self.editor.active():
            self._set_edit_mode(False)

    def _set_edit_mode(self, on: bool):
        if on and (not self._custom_active() or not self.editor.start()):
            on = False
        if not on and self.editor.active():
            self.editor.stop()
        self.edit_toggle.set_on(on)
        self.stage.set_edit_mode(on)
        self.inspector.setVisible(on)
        if on:
            if not self._overlay_cfg.get("enabled"):
                self._on_overlay_toggled(True)   # the HUD must be on screen to be edited
            self.inspector.note.setText("")
            self.stage.setFocus()

    # --------------------------------------------------------------- service

    def _on_autostart_toggled(self, on: bool):
        self._service_call("enable" if on else "disable")

    def _service_call(self, verb: str):
        self.statusBar().showMessage(f"service {verb}...", 4000)

        def done(result, v=verb):
            ok, out = result if isinstance(result, tuple) else (False, str(result))
            self.statusBar().showMessage(f"service {v}: {'ok' if ok else 'failed'}" +
                                         (f" — {out[:160]}" if out and not ok else ""), 8000)
            self.refresh_service()
        run_async(self, lambda: _systemctl(verb), done)

    def refresh_service(self):
        if not self.isVisible() or self.isMinimized() or self._service_busy:
            return
        self._service_busy = True

        def done(result):
            self._service_busy = False
            if not isinstance(result, tuple):
                return
            active, enabled = result
            running = active == "active"
            self._service_running = running
            self.svc_chip.set("SERVICE RUNNING" if running else f"SERVICE {active.upper()}", W.OK if running else W.BAD)
            self.autostart_toggle.set_on(enabled)
            self.svc_power.setText("Stop" if running else "Start")
            self.svc_power.setObjectName("danger" if running else "")
            _repolish(self.svc_power)
        run_async(self, _service_state, done)

    # ------------------------------------------------------- worker signals

    def on_connection(self, ok: bool, msg: str):
        self._connected = ok
        if ok:
            self.pill_daemon.set("DAEMON ONLINE", W.OK)
        else:
            self.pill_daemon.set("DAEMON OFFLINE", W.BAD)
            self.pill_usb.set("USB —", W.MUTED)
            self.pill_fps.set("-- FPS")
            self.setToolTip(msg)

    def on_preview(self, img: QImage):
        self.stage.set_vertical(img.height() > img.width())   # follow the frame actually on the panel
        self.stage.set_image(img)

    def on_command_done(self, key: str, ok: bool, err: str):
        if key == "media":
            self.stage.set_busy(False)
        if key == "engine":
            self.stage.set_busy(False)
            self._touch("engine")   # release the long hold set in _on_engine_changed
        if not ok:
            self.statusBar().showMessage(f"{key}: {err}", 6000)

    def on_status(self, st: dict):
        if "usb_connected" in st:
            self._usb = bool(st["usb_connected"])
        self.pill_usb.set("USB LINKED" if self._usb else "USB OFFLINE", W.OK if self._usb else W.BAD)
        if self._connected:
            self.pill_fps.set(f"{st.get('stream_fps', 0):.0f} FPS")
            rate = int(st.get("packets_per_min", 0))
            eng = ENGINE_SHORT.get(st.get("engine_mode", ""), "")
            self.pill_pkt.set(f"{rate / 1000:.1f}k PKT/MIN • {eng}" if rate >= 10000 else f"{rate} PKT/MIN • {eng}")

        if self._fresh("engine") and st.get("engine_mode") in ENGINE_NOTES:
            self.engine_seg.set_value(st["engine_mode"])
            self.engine_note.setText(ENGINE_NOTES[st["engine_mode"]])

        if self._fresh("brightness") and not self.brightness.isSliderDown():
            b = int(st.get("brightness", 50))
            if b != self.brightness.value():
                self.brightness.blockSignals(True)
                self.brightness.setValue(b)
                self.brightness.blockSignals(False)
            self.brightness_value.setText(f"{b}%")

        if self._fresh("mode"):
            mode = st.get("mode", "horizontal")
            self.mode_seg.set_value(mode)
            self.stage.set_vertical(mode == "vertical")
        if self._fresh("mirror"):
            self.flip_toggle.set_on(bool(st.get("mirror")))
        if self._fresh("celsius"):
            self._celsius = bool(st.get("celsius", True))
            self.unit_seg.set_value(self._celsius)
        if self._fresh("framing") and isinstance(st.get("framing"), dict):
            self._apply_framing_status(st["framing"])

        overlay = st.get("overlay", {})
        if self._fresh("overlay"):
            self._overlay_cfg = dict(overlay)
            on = bool(overlay.get("enabled"))
            self.deck_overlay.set_on(on)
            self.theme_overlay_toggle.set_on(on)
            primary, secondary = normalize_readout(overlay.get("primary"), overlay.get("secondary"))
            if not any(c.view().isVisible() for c in self._metric_combos()):
                self._metric_values = [primary] + secondary
                for combo, val in zip(self._metric_combos(), self._metric_values):
                    combo.setCurrentIndex(METRICS.index(val))
            self._sync_edit_toggle()

        self._sensors = st.get("sensors", {})
        self._custom_error = (st.get("custom") or {}).get("error")
        for key, tile in self.tiles.items():
            tile.set_value(W.format_metric(key, self._sensors, self._celsius) if key != "time"
                           else str(self._sensors.get("time", "--:--:--")))
        self._refresh_theme_cards()
        if self.editor.active():
            self.editor.update_data(self._sensors, self._celsius)
        self._update_tray(st)

        recents = st.get("recent_media", [])
        media = st.get("media")
        if self._fresh("recents") and (recents != self._recents):
            self._recents = list(recents)
            self._active_media = media
            self._rebuild_gallery()
        else:
            self._active_media = media
            for p, c in self._cards.items():
                c.set_active(p == media)

    def _refresh_theme_cards(self):
        cfg = self._overlay_cfg
        err = self._custom_error
        if err:
            self.custom_status.setText(f"customize.json error (last valid layout kept): {err}")
        elif self.custom_status.text().startswith("customize.json error"):
            self.custom_status.setText("")
        primary, secondary = normalize_readout(cfg.get("primary", "cpu_temp"), cfg.get("secondary", []))
        for key, tc in self.theme_cards.items():
            tc.set_state(cfg.get("theme") == key, primary, secondary, self._sensors, self._celsius)

    # ------------------------------------------- visibility / drag-and-drop

    def _update_preview_activity(self):
        self.worker.preview_enabled = self.isVisible() and not self.isMinimized()
        self.worker.wake()

    def showEvent(self, e):
        super().showEvent(e)
        self._update_preview_activity()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._update_preview_activity()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type().name == "WindowStateChange":
            self._update_preview_activity()

    def closeEvent(self, e):
        if not self._quitting and self._close_to_tray and self.tray is not None:
            e.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(APP_TITLE, "Still running in the tray. Right-click the icon for quick "
                                      "controls, or Quit.", _tray_icon(), 4000)
            return
        if self.editor.active():
            self.editor.stop()   # flush a pending HUD save
        self.worker.stop()
        self.worker.wait(3000)
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(e)
        QApplication.instance().quit()

    @staticmethod
    def _dropped_media(mime) -> str | None:
        for url in mime.urls():
            p = url.toLocalFile()
            if p and os.path.splitext(p)[1].lower() in MEDIA_EXTS:
                return p
        return None

    def dragEnterEvent(self, e):
        if self._dropped_media(e.mimeData()):
            e.acceptProposedAction()
            self.stage.set_drop_active(True)

    def dragLeaveEvent(self, e):
        self.stage.set_drop_active(False)

    def dropEvent(self, e):
        self.stage.set_drop_active(False)
        path = self._dropped_media(e.mimeData())
        if path:
            e.acceptProposedAction()
            self.apply_media(path)


# ---------------------------------------------------------------------------

def _load_fonts() -> str | None:
    """Register the bundled DeepCool fonts; returns a CJK-capable family for the 人民 badge."""
    fonts = find_data_dir("assets", "fonts")
    cjk = None
    if fonts:
        for fn, tag in (("JZFSSans-Regular-ad9b52af.otf", "sans"), ("JZFSSans-SemiBold-c3a8a050.otf", "sans"),
                        ("JZFSSans-Light-ea2a1e77.otf", "sans"), ("Pixel-numsymbol VF-66a3e782.ttf", "pixel"),
                        ("SourceHanSansCN-Medium-4f554f68.ttf", "cjk")):
            path = os.path.join(fonts, fn)
            if not os.path.isfile(path):
                continue
            fid = QFontDatabase.addApplicationFont(path)
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if not fams:
                continue
            if tag == "sans":
                W.SANS = fams[0]
            elif tag == "pixel":
                W.PIXEL = fams[0]
            else:
                cjk = fams[0]
    return cjk


def build_qss(arrow_path: str) -> str:
    return f"""
* {{ color: {W.TEXT}; font-family: "{W.SANS}", "Inter", "Noto Sans", sans-serif; font-size: 13px; }}
QMainWindow, #root {{ background: {W.BG}; }}
#scrollInner {{ background: transparent; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
#card {{ background: {W.CARD}; border: 1px solid {W.BORDER}; border-radius: 14px; }}
#cardTitle {{ color: {W.VIOLET_HI}; font-size: 11px; font-weight: 700; letter-spacing: 3px; background: transparent; }}
#fieldLabel {{ color: {W.MUTED}; font-size: 10px; font-weight: 600; letter-spacing: 2px; background: transparent; }}
#bigValue {{ color: {W.CYAN}; font-size: 17px; font-weight: 700; background: transparent; }}
QLabel {{ background: transparent; }}
QPushButton {{
    background: #0b0b12; border: 1px solid {W.BORDER}; border-radius: 17px;
    padding: 7px 18px; font-weight: 600; letter-spacing: 1px; min-height: 18px;
}}
QPushButton:hover {{ border-color: {W.VIOLET_HI}; background: #1a1230; }}
QPushButton:pressed {{ background: #241845; }}
QPushButton#primary {{ background: {W.VIOLET}; border-color: {W.VIOLET_HI}; color: white; }}
QPushButton#primary:hover {{ background: {W.VIOLET_HI}; }}
QPushButton#danger {{ border-color: #6b2a35; color: {W.BAD}; }}
QPushButton#danger:hover {{ background: #3a1219; border-color: {W.BAD}; }}
QPushButton#menuButton {{ padding-right: 34px; }}
QPushButton::menu-indicator {{
    image: url({arrow_path}); subcontrol-origin: padding; subcontrol-position: right center;
    right: 14px; width: 10px; height: 10px;
}}
QPushButton#swatch {{ border-radius: 12px; padding: 5px 14px; }}
#deckRule {{ background: {W.BORDER}; border: none; }}
#inspector {{ background: {W.CARD_HI}; border: 1px solid {W.CYAN}; border-radius: 14px; }}
#valueBadge {{
    background: rgba(34, 211, 238, 0.09); border: 1px solid rgba(34, 211, 238, 0.45); border-radius: 12px;
    color: {W.CYAN}; font-size: 12px; font-weight: 700; padding: 3px 0px; letter-spacing: 1px;
}}
QSpinBox, QLineEdit {{
    background: #0b0b12; border: 1px solid {W.BORDER}; border-radius: 8px; padding: 5px 6px;
    selection-background-color: {W.VIOLET}; selection-color: white;
}}
QSpinBox:focus, QLineEdit:focus {{ border-color: {W.CYAN}; }}
QSpinBox:disabled, QLineEdit:disabled {{ color: {W.DIM}; }}
QComboBox {{
    background: #0b0b12; border: 1px solid {W.BORDER}; border-radius: 10px; padding: 6px 12px; min-height: 20px;
}}
QComboBox:hover, QComboBox:focus {{ border-color: {W.VIOLET}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({arrow_path}); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{
    background: {W.CARD}; border: 1px solid {W.VIOLET}; selection-background-color: {W.VIOLET};
    selection-color: white; outline: none; padding: 4px;
}}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2a2144; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {W.VIOLET}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{ background: transparent; color: {W.MUTED}; }}
QToolTip {{ background: {W.CARD}; color: {W.TEXT}; border: 1px solid {W.VIOLET}; padding: 4px; }}
QMenu {{ background: #111119; color: #ffffff; border: 1px solid {W.VIOLET}; padding: 4px; }}
QMenu::item {{ background: transparent; color: #ffffff; padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: #a855f7; color: #ffffff; }}
QMenu::item:disabled {{ color: {W.MUTED}; }}
QMenu::separator {{ height: 1px; background: {W.BORDER}; margin: 4px 8px; }}
QMessageBox, QDialog, QFileDialog {{ background: #08080c; color: #ffffff; }}
QDialog QLabel {{ color: #ffffff; }}
QDialog QLineEdit {{ background: #111119; color: #ffffff; border: 1px solid {W.BORDER}; border-radius: 8px; padding: 5px 8px; selection-background-color: #a855f7; }}
QListView, QTreeView {{
    background: #08080c; color: #ffffff; border: 1px solid {W.BORDER}; outline: none;
    alternate-background-color: #0d0d14; selection-background-color: #a855f7; selection-color: #ffffff;
}}
QListView::item:hover, QTreeView::item:hover {{ background: #1a1230; }}
QListView::item:selected, QTreeView::item:selected {{ background: #a855f7; color: #ffffff; }}
QHeaderView::section {{ background: #111119; color: #ffffff; border: none; border-right: 1px solid {W.BORDER}; padding: 4px 8px; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2a2144; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {W.VIOLET}; }}
"""


def _write_arrow() -> str:
    cache = os.path.join(os.path.expanduser("~"), ".cache", "deepcool-lt360")
    path = os.path.join(cache, "arrow.svg")
    try:
        os.makedirs(cache, exist_ok=True)
        with open(path, "w") as f:
            f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path d="M1 3l4 4 4-4" fill="none" '
                    f'stroke="{W.VIOLET_HI}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    except OSError:
        pass
    return path


def _find_icon() -> QIcon | None:
    path = find_icon_file()
    return QIcon(path) if path else None


def _tray_icon() -> QIcon:
    """The app icon, or a painted violet/cyan ring when it is not installed."""
    icon = _find_icon()
    if icon is not None and not icon.isNull():
        return icon
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(W.VIOLET), 9, cap=Qt.PenCapStyle.RoundCap))
    p.drawArc(QRectF(8, 8, 48, 48), -135 * 16, -270 * 16)
    p.setPen(QPen(QColor(W.CYAN), 9, cap=Qt.PenCapStyle.RoundCap))
    p.drawArc(QRectF(8, 8, 48, 48), -135 * 16, -150 * 16)
    p.end()
    return QIcon(pm)


def main():
    ap = argparse.ArgumentParser(prog="lt360-gui", description=f"{APP_TITLE} — desktop control for lt360d")
    ap.add_argument("--tray", action="store_true",
                    help="start hidden in the system tray; closing the window keeps it running there")
    ap.add_argument("--minimized", action="store_true",
                    help="start minimized (hidden in the tray when a tray is available)")
    ap.add_argument("--version", action="version", version=f"lt360-gui {__version__}")
    args, qt_args = ap.parse_known_args()

    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("deepcool-lt360")
    MainWindow._cjk_family = _load_fonts()
    app.setStyleSheet(build_qss(_write_arrow()))
    icon = _find_icon()
    if icon:
        app.setWindowIcon(icon)
    win = MainWindow(tray_session=args.tray)
    if win.tray is not None:
        app.setQuitOnLastWindowClosed(False)   # hidden-to-tray must not end the app; MainWindow.quit() does
    if (args.tray or args.minimized) and win.tray is not None:
        pass   # stay hidden; the tray icon brings the window back
    elif args.minimized or args.tray:
        win.showMinimized()
    else:
        win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
