#!/usr/bin/env python3
"""lt360-gui - "LT360 VISION — For Renmin": native desktop GUI for the LT360 display daemon.

Talks to lt360d over the same Unix-socket protocol as lt360ctl. **No socket or
subprocess I/O ever happens on the Qt UI thread**: all of it lives in IpcWorker
(a QThread) and in short-lived thread-pool jobs; the UI only reacts to signals.
"""
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time

from PyQt6.QtCore import QObject, QRunnable, Qt, QThread, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QIcon, QImage, QTransform
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QMenu, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

import lt360_custom as C
import lt360_widgets as W

DEFAULT_SOCKET_PATH = "/tmp/lt360.sock"
SERVICE_NAME = "deepcool-lt360.service"
APP_TITLE = "LT360 VISION — For Renmin"
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
ENGINE_SHORT = {"performance": "PERFORMANCE", "full": "FULL"}
METRIC_LABELS = {
    "cpu_temp": "CPU Temp", "gpu_temp": "GPU Temp", "cpu_load": "CPU Load",
    "gpu_load": "GPU Load", "time": "Time", "off": "Off",
}


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
        self.preview_enabled = True   # plain bool, flipped from the UI thread (atomic)
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
                timeout = 90.0 if req.get("action") in ("set_media", "set_engine_mode") else 10.0   # decode can be slow
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


def _service_state() -> tuple[str, bool]:
    _, active = _systemctl("is-active")
    ok, _ = _systemctl("is-enabled")
    return active.strip() or "unknown", ok


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1240, 860)
        self.setMinimumSize(1020, 700)
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

        self.worker = IpcWorker()
        self.worker.status_ready.connect(self.on_status)
        self.worker.preview_ready.connect(self.on_preview)
        self.worker.connection_changed.connect(self.on_connection)
        self.worker.command_done.connect(self.on_command_done)

        self._build_ui()
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
        root.addWidget(W.Banner(self._cjk_family))

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
        col.addWidget(self._build_hardware_card())
        col.addWidget(self._build_service_card())
        col.addStretch()
        scroll.setWidget(inner)
        body.addWidget(scroll, 10)

    _cjk_family: str | None = None   # set by main() after font registration

    def _build_stage_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(10)

        pills = QHBoxLayout()
        self.pill_daemon = W.Chip("DAEMON …", W.MUTED)
        self.pill_usb = W.Chip("USB …", W.MUTED)
        self.pill_fps = W.Chip("-- FPS", W.CYAN, dot=False)
        self.pill_pkt = W.Chip("-- PKT", W.CYAN, dot=False)
        for c in (self.pill_daemon, self.pill_usb, self.pill_fps, self.pill_pkt):
            pills.addWidget(c)
        pills.addStretch()
        col.addLayout(pills)

        self.stage = W.PumpStage()
        col.addWidget(self.stage, 1)

        # floating quick toggles right under the screen
        quick = QHBoxLayout()
        quick.addStretch()
        self.quick_overlay = W.PillToggle("Overlay")
        self.quick_overlay.toggled.connect(self._on_overlay_toggled)
        self.quick_mode = W.SegmentedControl([("Horizontal", "horizontal"), ("Vertical", "vertical")])
        self.quick_mode.changed.connect(self._on_mode_changed)
        quick.addWidget(self.quick_overlay)
        quick.addWidget(self.quick_mode)
        quick.addStretch()
        col.addLayout(quick)
        return col

    def _build_engine_card(self) -> QFrame:
        frame, lay = card("ENGINE MODE")
        self.engine_seg = W.SegmentedControl([
            ("⚡ PERFORMANCE (Low CPU)", "performance"),
            ("🎬 FULL MODE (Low PKT • Unlimited Video)", "full"),
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
        self.preset_btn = QPushButton("Load Preset…")
        self.preset_menu = QMenu(self.preset_btn)
        self.preset_menu.aboutToShow.connect(self._fill_preset_menu)
        self.preset_btn.setMenu(self.preset_menu)
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
                           ("gpu_load", "GPU LOAD"), ("time", "TIME")):
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

    def _fill_preset_menu(self):
        self.preset_menu.clear()
        presets = C.list_presets()
        for name in presets:
            self.preset_menu.addAction(name.replace("_", " ").title()).triggered.connect(
                lambda _=False, n=name: self._apply_preset(n))
        if not presets:
            self.preset_menu.addAction("No presets found").setEnabled(False)

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
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
            # GUI has no terminal: a terminal $EDITOR would die instantly, so prefer the desktop handler.
            subprocess.Popen(["xdg-open", C.CUSTOM_PATH], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return editor

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

    def _build_hardware_card(self) -> QFrame:
        frame, lay = card("HARDWARE")
        lay.addWidget(field_label("BRIGHTNESS"))
        row = QHBoxLayout()
        self.brightness = W.NeonSlider()
        self.brightness.setRange(0, 100)
        self.brightness.setValue(50)
        self.brightness_value = QLabel("50%")
        self.brightness_value.setObjectName("bigValue")
        self.brightness_value.setMinimumWidth(62)
        self.brightness_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.brightness.valueChanged.connect(self._on_brightness_changed)
        row.addWidget(self.brightness, 1)
        row.addWidget(self.brightness_value)
        lay.addLayout(row)

        lay.addWidget(field_label("ORIENTATION"))
        self.mode_seg = W.SegmentedControl([("Horizontal", "horizontal"), ("Vertical", "vertical")])
        self.mode_seg.changed.connect(self._on_mode_changed)
        lay.addWidget(self.mode_seg)

        opts = QHBoxLayout()
        opts.setSpacing(12)
        self.flip_toggle = W.PillToggle("180° Flip")
        self.flip_toggle.toggled.connect(self._on_mirror_toggled)
        self.unit_seg = W.SegmentedControl([("°C", True), ("°F", False)])
        self.unit_seg.setMinimumWidth(130)
        self.unit_seg.changed.connect(self._on_celsius_changed)
        opts.addWidget(self.flip_toggle)
        opts.addStretch()
        opts.addWidget(field_label("UNIT"))
        opts.addWidget(self.unit_seg)
        lay.addLayout(opts)
        return frame

    def _build_service_card(self) -> QFrame:
        self.svc_chip = W.Chip("SERVICE …", W.MUTED)
        frame, lay = card("SERVICE", self.svc_chip)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.svc_buttons = {}
        for verb, label, obj in (("start", "Start", ""), ("restart", "Restart", "primary"), ("stop", "Stop", "danger")):
            b = QPushButton(label)
            if obj:
                b.setObjectName(obj)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=verb: self._service_action(v))
            self.svc_buttons[verb] = b
            row.addWidget(b)
        row.addStretch()
        self.autostart_toggle = W.PillToggle("Autostart on Boot")
        self.autostart_toggle.toggled.connect(self._on_autostart_toggled)
        row.addWidget(self.autostart_toggle)
        lay.addLayout(row)
        self.svc_note = QLabel("")
        self.svc_note.setObjectName("fieldLabel")
        self.svc_note.setWordWrap(True)
        lay.addWidget(self.svc_note)
        return frame

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
        path, _ = QFileDialog.getOpenFileName(self, "Choose media", os.path.expanduser("~"), MEDIA_FILTER)
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
        self.quick_mode.set_value(mode)
        self.stage.set_vertical(mode == "vertical")
        self.worker.submit("mode", {"action": "set_mode", "value": mode})

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
        self.quick_overlay.set_on(on)
        self.theme_overlay_toggle.set_on(on)
        self._queue_overlay({"enabled": on})

    def _on_theme_clicked(self, key: str):
        self._queue_overlay({"theme": key, "enabled": True})   # picking a theme implies "show it"
        self.quick_overlay.set_on(True)
        self.theme_overlay_toggle.set_on(True)

    def _on_metrics_changed(self, *_):
        self._queue_overlay({
            "primary": self.primary_combo.currentData(),
            "secondary": [c.currentData() for c in self.slot_combos],
        })

    def _on_autostart_toggled(self, on: bool):
        self._service_call("enable" if on else "disable")

    def _service_action(self, verb: str):
        self._service_call(verb)

    def _service_call(self, verb: str):
        self.svc_note.setText(f"{verb}…")

        def done(result, v=verb):
            ok, out = result if isinstance(result, tuple) else (False, str(result))
            self.svc_note.setText(f"{v}: {'ok' if ok else 'failed'}" + (f" — {out[:160]}" if out and not ok else ""))
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
            self.svc_chip.set("RUNNING" if running else active.upper(), W.OK if running else W.BAD)
            self.autostart_toggle.set_on(enabled)
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
            self.quick_mode.set_value(mode)
            self.stage.set_vertical(mode == "vertical")
        if self._fresh("mirror"):
            self.flip_toggle.set_on(bool(st.get("mirror")))
        if self._fresh("celsius"):
            self._celsius = bool(st.get("celsius", True))
            self.unit_seg.set_value(self._celsius)

        overlay = st.get("overlay", {})
        if self._fresh("overlay"):
            self._overlay_cfg = dict(overlay)
            on = bool(overlay.get("enabled"))
            self.quick_overlay.set_on(on)
            self.theme_overlay_toggle.set_on(on)
            for combo, val in [(self.primary_combo, overlay.get("primary"))] + \
                    list(zip(self.slot_combos, list(overlay.get("secondary", [])) + ["off"] * 3)):
                if val in METRICS and not combo.view().isVisible():
                    combo.setCurrentIndex(METRICS.index(val))

        self._sensors = st.get("sensors", {})
        self._custom_error = (st.get("custom") or {}).get("error")
        for key, tile in self.tiles.items():
            tile.set_value(W.format_metric(key, self._sensors, self._celsius) if key != "time"
                           else str(self._sensors.get("time", "--:--:--")))
        self._refresh_theme_cards()

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
        for key, tc in self.theme_cards.items():
            tc.set_state(cfg.get("theme") == key,
                         cfg.get("primary", "cpu_temp"), cfg.get("secondary", []), self._sensors, self._celsius)

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
        self.worker.stop()
        self.worker.wait(3000)
        super().closeEvent(e)

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

def _find_dir(*rel) -> str | None:
    for base in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."), os.path.join(sys.prefix, "share", "deepcool-lt360")):
        p = os.path.join(base, *rel)
        if os.path.isdir(p):
            return p
    return None


def _load_fonts() -> str | None:
    """Register the bundled DeepCool fonts; returns a CJK-capable family for the 人民 badge."""
    fonts = _find_dir("assets", "fonts")
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
QMessageBox, QFileDialog {{ background: {W.CARD}; }}
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
    for path in (
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "desktop", "deepcool-lt360.svg"),
        os.path.join(sys.prefix, "share", "icons", "hicolor", "scalable", "apps", "deepcool-lt360.svg"),
    ):
        if os.path.isfile(path):
            return QIcon(path)
    return None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setDesktopFileName("deepcool-lt360")
    MainWindow._cjk_family = _load_fonts()
    app.setStyleSheet(build_qss(_write_arrow()))
    icon = _find_icon()
    if icon:
        app.setWindowIcon(icon)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
