#!/usr/bin/env python3
"""lt360d - background daemon for the DeepCool LT360 VISION AIO screen.

Continuously streams a static image or looping GIF to the panel over USB and
listens on a local Unix domain socket for runtime control commands. See
lt360ctl.py for the companion CLI and PROTOCOL.md for the wire protocol.
"""
import argparse
import base64
import collections
import json
import logging
import os
import socketserver
import sys
import threading
import time

import usb.core
import usb.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path

from PIL import Image

from lt360_common import (
    DEFAULT_ENGINE_MODE, ENGINE_MODES, EP_CMD, EP_IMAGE, FIT_MODES, INTERFACE, MODE_NAMES, PID, VID,
    __version__, canvas_size, cmd_settings, cmd_stream_start, find_data_file, jpeg_packets, normalize_framing,
    rotate_and_encode, rotation_deg,
)
from lt360_ipc import default_socket_path
from lt360_custom import CustomManager, signature as custom_signature
from lt360_media import PERF_MAX_FRAMES, Reel, geometry_key, load_reel
from lt360_overlay import OverlayRenderer, THEMES, default_overlay_config, format_metric, normalize_readout
from lt360_sensors import SensorReader

log = logging.getLogger("lt360d")

DEFAULT_SOCKET_PATH = default_socket_path()


USER_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "deepcool-lt360", "config.json")


def _find_seed_config() -> str | None:
    """Read-only defaults used only when the per-user config does not exist yet:
    the repo checkout (dev) or the system package data dir (AUR install).
    """
    return find_data_file("default_config.json")


DEFAULT_CONFIG_PATH = USER_CONFIG_PATH
RECONNECT_BACKOFF = (0.5, 1, 2, 5)
SENSOR_INTERVAL = 1.0
METRIC_CHOICES = {"cpu_temp", "gpu_temp", "cpu_load", "gpu_load", "time", "off"}
MAX_RECENTS = 12
MEDIA_RETRY_SECONDS = 5.0
# A frame that hasn't changed is never re-sent, except once per HEARTBEAT to keep the panel showing it.
HEARTBEAT = 1.0
PACKET_RATE_WINDOW = 10.0


def resolve_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


class State:
    """All mutable daemon state.

    `lock` is only ever held for a few field reads/writes -- never across JPEG
    encoding, media decoding, disk writes or USB transfers. Heavy work happens
    on a snapshot outside the lock and the result is swapped in atomically.
    `reload_lock` serialises media (re)loads against each other instead.
    """

    MANAGED_KEYS = {"media", "brightness", "mode", "mirror", "celsius", "quality", "overlay", "recent_media",
                    "engine_mode", "framing"}

    def __init__(self, config: dict, config_path: str | None = None):
        self.lock = threading.RLock()
        self.reload_lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.config_path = config_path
        self.extra = {k: v for k, v in config.items() if k not in self.MANAGED_KEYS}

        self.brightness = int(config.get("brightness", 50))
        self.celsius = bool(config.get("celsius", True))
        self.display_model = MODE_NAMES.get(config.get("mode", "horizontal"), 0)
        self.is_mirror = bool(config.get("mirror", False))
        self.quality = int(config.get("quality", 95))  # legacy: JPEG settings now come from the engine profile
        engine = config.get("engine_mode", DEFAULT_ENGINE_MODE)
        self.engine_mode = engine if engine in ENGINE_MODES else DEFAULT_ENGINE_MODE
        self.perf_max_frames = int(config.get("perf_max_frames", PERF_MAX_FRAMES))  # unmanaged key, hand-editable
        self.framing = normalize_framing(config.get("framing"))  # fit/zoom/pan_x/pan_y/speed
        media = config.get("media")
        self.media_path = resolve_path(media) if media else None
        self.recent_media = [resolve_path(p) for p in config.get("recent_media", []) if p][:MAX_RECENTS]
        self.reel: Reel | None = None
        self.frame_index = 0
        self.settings_dirty = True  # forces a cmd_settings resend on next loop tick / reconnect

        self.overlay = default_overlay_config()
        self.overlay.update(config.get("overlay", {}))
        self._normalize_readout()
        self.sensor_data: dict = {}
        self.overlay_layer = None  # RGBA layer, rebuilt off-lock by sensor_loop
        self.custom = CustomManager()  # ~/.config/deepcool-lt360/customize.json, hot-reloaded by sensor_loop
        self.overlay_generation = 0  # bumped whenever the rendered overlay pixels change
        self.overlay_wake = threading.Event()  # nudges sensor_loop after a config change
        self.frame_wake = threading.Event()  # nudges frame_loop out of its sleep when something changed

        # Written by frame_loop, read lock-free by get_preview (single reference assignment is atomic).
        self.last_preview: tuple[bytes, int] | None = None  # (jpeg, clockwise rotation applied to the canvas)
        self.preview_id = 0
        self.frames_sent = 0
        self.packets_sent = 0
        self.stream_fps = 0.0
        self._pkt_log: collections.deque = collections.deque()  # (monotonic time, packets) per USB frame
        self._started = time.monotonic()
        self._next_media_retry = 0.0

    def _normalize_readout(self):
        """No metric twice in the readout bar (fixes configs saved as e.g. TIME / CPU / CPU)."""
        self.overlay["primary"], self.overlay["secondary"] = normalize_readout(
            self.overlay.get("primary"), self.overlay.get("secondary"))

    # ---------- persistence ----------

    def snapshot_config(self) -> dict:
        with self.lock:
            cfg = dict(self.extra)
            cfg.update({
                "media": self.media_path,
                "brightness": self.brightness,
                "mode": "horizontal" if self.display_model == 0 else "vertical",
                "mirror": self.is_mirror,
                "celsius": self.celsius,
                "quality": self.quality,
                "engine_mode": self.engine_mode,
                "framing": dict(self.framing),
                "overlay": dict(self.overlay),
                "recent_media": list(self.recent_media),
            })
        return cfg

    def record_packets(self, n: int):
        now = time.monotonic()
        with self.lock:
            self.packets_sent += n
            self._pkt_log.append((now, n))
            while self._pkt_log and now - self._pkt_log[0][0] > PACKET_RATE_WINDOW:
                self._pkt_log.popleft()

    def packets_per_min(self) -> int:
        """Rolling USB packet rate over the last PACKET_RATE_WINDOW seconds."""
        now = time.monotonic()
        with self.lock:
            total = sum(n for t, n in self._pkt_log if now - t <= PACKET_RATE_WINDOW)
        window = min(PACKET_RATE_WINDOW, max(now - self._started, 1.0))
        return round(total / window * 60)

    def save(self):
        """Atomically persist the full state (write .tmp, fsync, os.replace)."""
        if not self.config_path:
            return
        try:
            with self.save_lock:
                cfg = self.snapshot_config()
                os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
                tmp = self.config_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(cfg, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.config_path)
        except OSError as e:
            log.error("failed to save config to %s: %s", self.config_path, e)

    # ---------- media / display reconfiguration (all heavy work off-lock) ----------

    def _params(self):
        with self.lock:
            return (self.media_path, self.display_model, self.is_mirror, self.engine_mode,
                    bool(self.overlay["enabled"]), dict(self.framing))

    def set_media(self, path: str, persist: bool = True):
        resolved = resolve_path(path)
        if not os.path.isfile(resolved):  # also rejects FIFOs and device nodes such as /dev/zero
            raise FileNotFoundError(f"no such file (or not a regular file): {resolved}")
        with self.reload_lock:
            _, dm, mirror, engine, overlay_on, framing = self._params()
            reel = load_reel(resolved, dm, mirror, engine, with_canvas=overlay_on,
                             max_frames=self.perf_max_frames, framing=framing)  # off-lock
            with self.lock:
                self.reel = reel  # atomic swap
                self.media_path = resolved
                self.frame_index = 0
                self.recent_media = [resolved] + [p for p in self.recent_media if p != resolved]
                self.recent_media = self.recent_media[:MAX_RECENTS]
        self.frame_wake.set()
        if persist:
            self.save()

    def reconfigure(self, display_model: int | None = None, is_mirror: bool | None = None,
                    overlay_patch: dict | None = None, engine_mode: str | None = None,
                    framing_patch: dict | None = None):
        """Change orientation/mirror/overlay-enabled/engine mode/framing: re-bake the reel off-lock, then
        swap the reel and the new settings in together so a frame is never rendered mismatched.
        Playback speed alone never re-bakes a pre-rendered reel (it only scales frame delays), but a
        streamed video restarts ffmpeg with the new speed.
        """
        with self.reload_lock:
            path, dm, mirror, engine, overlay_on, framing = self._params()
            new_dm = dm if display_model is None else display_model
            new_mirror = mirror if is_mirror is None else is_mirror
            new_engine = engine if engine_mode is None else engine_mode
            new_overlay_on = overlay_on if not overlay_patch else bool(overlay_patch.get("enabled", overlay_on))
            new_framing = normalize_framing({**framing, **(framing_patch or {})})
            with self.lock:
                streaming = bool(self.reel is not None and self.reel.streaming)
            rebake = (new_dm, new_mirror, new_overlay_on, new_engine, geometry_key(new_framing)) != \
                (dm, mirror, overlay_on, engine, geometry_key(framing))
            if streaming and new_framing["speed"] != framing["speed"]:
                rebake = True
            reel = None
            if path and rebake:
                reel = load_reel(path, new_dm, new_mirror, new_engine, with_canvas=new_overlay_on,
                                 max_frames=self.perf_max_frames, framing=new_framing)  # off-lock
            with self.lock:
                self.display_model, self.is_mirror, self.engine_mode = new_dm, new_mirror, new_engine
                self.framing = new_framing
                if overlay_patch:
                    self.overlay.update(overlay_patch)
                    self._normalize_readout()
                if reel is not None:
                    self.reel = reel
                    self.frame_index = 0
                self.settings_dirty = True
        self.overlay_wake.set()
        self.frame_wake.set()
        self.save()

    def retry_pending_media(self):
        """Called from the idle frame loop: if a saved media path could not be loaded at
        startup (drive not mounted yet, etc.), keep trying instead of staying blank.
        """
        now = time.monotonic()
        if self.reel is not None or not self.media_path or now < self._next_media_retry:
            return
        self._next_media_retry = now + MEDIA_RETRY_SECONDS
        try:
            self.set_media(self.media_path, persist=False)
            log.info("loaded pending media %s", self.media_path)
        except Exception as e:
            log.warning("media %s still unavailable: %s", self.media_path, e)

    def status(self) -> dict:
        pkts_per_min = self.packets_per_min()  # takes the lock itself
        with self.lock:
            streaming = bool(self.reel and self.reel.streaming)
            return {
                "version": __version__,
                "engine_mode": self.engine_mode,
                "framing": dict(self.framing),
                "packets_per_min": pkts_per_min,
                "media": self.media_path,
                "recent_media": list(self.recent_media),
                "streaming": streaming,
                "frames": len(self.reel) if self.reel and not streaming else 0,
                "brightness": self.brightness,
                "mode": "horizontal" if self.display_model == 0 else "vertical",
                "mirror": self.is_mirror,
                "celsius": self.celsius,
                "overlay": dict(self.overlay),
                "sensors": dict(self.sensor_data),
                "custom": {"path": self.custom.path, "error": self.custom.error},
                "stream_fps": round(self.stream_fps, 1),
                "frames_sent": self.frames_sent,
                "packets_sent": self.packets_sent,
            }


class Device:
    """Owns the USB handle. Reconnects transparently on USBError."""

    def __init__(self):
        self.dev = None
        self.write_lock = threading.Lock()

    def _open(self):
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise usb.core.USBError(f"device {VID:04x}:{PID:04x} not found")
        if dev.is_kernel_driver_active(INTERFACE):
            dev.detach_kernel_driver(INTERFACE)
        usb.util.claim_interface(dev, INTERFACE)
        self.dev = dev

    def _close(self):
        if self.dev is not None:
            try:
                usb.util.release_interface(self.dev, INTERFACE)
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None

    def connect_blocking(self, stop_event: threading.Event):
        """Retry until connected or stop_event is set."""
        attempt = 0
        while not stop_event.is_set():
            try:
                self._open()
                log.info("connected to %04x:%04x", VID, PID)
                return True
            except usb.core.USBError as e:
                delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                log.warning("USB connect failed (%s), retrying in %.1fs", e, delay)
                attempt += 1
                stop_event.wait(delay)
        return False

    def write_cmd(self, data: bytes):
        with self.write_lock:
            self.dev.write(EP_CMD, data, timeout=1000)

    def write_packets(self, packets: list[bytes]):
        with self.write_lock:
            # One bulk transfer per frame: every packet is exactly 512 B (the endpoint's max packet
            # size), so the device sees the identical packet sequence with ~50x fewer syscalls.
            self.dev.write(EP_IMAGE, b"".join(packets), timeout=5000)

    def disconnect(self):
        self._close()


def _overlay_key(state: State, custom: CustomManager) -> tuple | None:
    """Everything that changes what the overlay layer looks like, incl. the formatted numbers."""
    with state.lock:
        if not state.overlay["enabled"]:
            return None
        cfg, data, celsius, dm = dict(state.overlay), dict(state.sensor_data), state.celsius, state.display_model
    if cfg.get("theme") == "custom":
        sig = custom_signature(custom.elements_for(canvas_size(dm)), data, celsius, custom.history)
        return (dm, celsius, "custom", custom.version, sig)
    metrics = [cfg.get("primary")] + list(cfg.get("secondary", []))
    text = tuple(format_metric(m, data, celsius) for m in metrics)
    return (dm, celsius, json.dumps(cfg, sort_keys=True), text)


def sensor_loop(state: State, stop_event: threading.Event):
    """Polls sensors once a second (or immediately after an overlay/unit/mode change) and
    re-renders the overlay layer -- off-lock -- only when its pixels would actually differ.
    """
    reader = SensorReader()
    renderer = OverlayRenderer()
    custom = state.custom
    last_key = None
    while not stop_event.is_set():
        state.overlay_wake.clear()
        try:
            custom.poll()  # 1 Hz mtime check; a reload bumps custom.version, which changes the overlay key
            with state.lock:
                using_custom = bool(state.overlay["enabled"]) and state.overlay.get("theme") == "custom"
            custom.set_active(using_custom)
            data = reader.read()
            if using_custom:
                data["custom"] = custom.sensor_values()
            custom.history.record(data)  # always, so sparklines have a full minute as soon as they appear
            with state.lock:
                state.sensor_data = data
            key = _overlay_key(state, custom)
            if key is None:
                last_key = None
            elif key != last_key:
                with state.lock:
                    cfg, celsius, dm = dict(state.overlay), state.celsius, state.display_model
                layer = renderer.render_layer(canvas_size(dm), data, cfg, celsius, custom)
                with state.lock:
                    state.overlay_layer = layer
                    state.overlay_generation += 1
                state.frame_wake.set()
                last_key = key
        except Exception as e:
            log.warning("sensor read failed: %s", e)
        state.overlay_wake.wait(SENSOR_INTERVAL)


class OverlayFrameCache:
    """Encoded (jpeg, packets) per frame index for one (reel, overlay generation).
    A still image + overlay therefore costs one JPEG encode per changed sensor readout.
    """

    def __init__(self):
        self.key = None
        self.reel = None
        self.frames: dict[int, tuple[bytes, list[bytes]]] = {}

    def get(self, reel: Reel, idx: int, generation: int, build):
        if self.reel is not reel or self.key != generation:
            self.reel, self.key, self.frames = reel, generation, {}
        hit = self.frames.get(idx)
        if hit is None:
            hit = self.frames[idx] = build()
        return hit


def _compose_and_encode(reel, canvas, layer):
    composed = OverlayRenderer.composite(canvas, layer) if layer is not None and layer.size == canvas.size else canvas
    jpg = rotate_and_encode(composed, reel.display_model, reel.is_mirror, reel.quality, reel.optimize)
    return jpg, jpeg_packets(jpg)


class _CardReel:
    """Just enough of a Reel for _compose_and_encode: the built-in card shown when no media is set."""
    optimize = False

    def __init__(self, display_model: int, is_mirror: bool, quality: int):
        self.display_model, self.is_mirror, self.quality = display_model, is_mirror, quality


def _fallback_card(display_model: int) -> Image.Image:
    from PIL import ImageDraw
    from lt360_common import canvas_size
    from lt360_overlay import _font
    w, h = canvas_size(display_model)
    img = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(img)
    for y in range(h):  # subtle vertical gradient
        t = y / max(1, h - 1)
        px.line([(0, y), (w, y)], fill=(int(12 + 20 * t), int(6 + 4 * t), int(24 + 30 * t)))
    for i, (text, name, size, colour) in enumerate((
            ("LT360 VISION", "semibold", w // 12, (255, 255, 255)),
            ("// FOR RENMIN", "light", w // 22, (168, 85, 247)),
            ("No media selected", "light", w // 34, (150, 150, 170)))):
        font = _font(name, size)
        box = px.textbbox((0, 0), text, font=font)
        px.text(((w - (box[2] - box[0])) / 2 - box[0], h * 0.30 + i * h * 0.16), text, font=font, fill=colour)
    return img


def frame_loop(device: Device, state: State, stop_event: threading.Event):
    """Send a frame over USB only when something changed: a new media frame is due, the overlay
    pixels changed, or the HEARTBEAT elapsed with nothing new (static image keeps showing).
    """
    cache = OverlayFrameCache()
    cur_reel, cur_idx, frame_start = None, -1, 0.0
    sent_key = None                 # (idx, overlay generation) last sent from a pre-baked reel
    prev_raw, prev_gen = None, None  # last raw frame / overlay generation encoded from a streaming reel
    out = None                      # (jpg, packets) of the most recent frame, re-sent on the heartbeat
    last_send, stream_due = 0.0, 0.0
    fps_window_start, fps_window_frames = time.monotonic(), 0

    def nap(seconds: float):
        state.frame_wake.wait(max(0.0, seconds))

    try:
        while not stop_event.is_set():
            if device.dev is None:
                if not device.connect_blocking(stop_event):
                    return
                with state.lock:
                    state.settings_dirty = True

            try:
                state.frame_wake.clear()  # anything that changes state after this point re-wakes us
                with state.lock:
                    dirty = state.settings_dirty
                    state.settings_dirty = False
                    settings = (state.brightness, state.celsius, state.display_model)
                    reel, idx = state.reel, state.frame_index
                    speed = state.framing["speed"]
                    layer, generation = state.overlay_layer, state.overlay_generation
                    overlay_on = bool(state.overlay["enabled"])

                if dirty:  # USB I/O strictly outside the state lock
                    device.write_cmd(cmd_stream_start())
                    device.write_cmd(cmd_settings(*settings))
                    sent_key, prev_raw, last_send = None, None, 0.0  # (re)show the current frame

                if reel is None or len(reel) == 0:
                    state.retry_pending_media()
                    if not state.media_path:  # nothing chosen yet (fresh install): show the built-in card
                        now = time.monotonic()
                        gen = generation if overlay_on and layer is not None else -1
                        if dirty or gen != prev_gen or out is None or now - last_send >= HEARTBEAT:
                            card = _CardReel(settings[2], state.is_mirror, state.quality)
                            out = _compose_and_encode(card, _fallback_card(settings[2]), layer if gen != -1 else None)
                            device.write_packets(out[1])
                            last_send, prev_gen = now, gen
                            state.last_preview = (out[0], rotation_deg(card.display_model, card.is_mirror))
                            state.preview_id += 1
                    nap(0.5)
                    continue

                now = time.monotonic()
                if reel is not cur_reel:
                    if cur_reel is not None and cur_reel.streaming:
                        cur_reel.close()
                    cur_reel, cur_idx, frame_start, stream_due = reel, idx, now, now
                    sent_key, prev_raw, out, last_send = None, None, None, 0.0
                elif idx != cur_idx:
                    cur_idx, frame_start = idx, now

                active_layer = layer if overlay_on else None
                gen = generation if active_layer is not None else -1

                if reel.streaming:
                    duration = 1.0 / reel.fps
                    raw = reel.read_raw()  # blocks briefly on ffmpeg; loops the video forever
                    fresh = raw != prev_raw or gen != prev_gen
                    if fresh:
                        canvas = Image.frombytes("RGB", reel.size, raw)
                        out = _compose_and_encode(reel, canvas, active_layer)
                        prev_raw, prev_gen = raw, gen
                else:
                    key = (idx, gen)
                    fresh = key != sent_key
                    if fresh:
                        if reel.canvas_frames:
                            canvas, duration = reel.canvas_frame_at(idx)
                            out = cache.get(reel, idx, gen, lambda: _compose_and_encode(reel, canvas, active_layer))
                        else:
                            jpg, packets, duration = reel.frame_at(idx)
                            out = (jpg, packets)
                        sent_key = key
                    else:
                        duration = (reel.canvas_frame_at(idx) if reel.canvas_frames else reel.frame_at(idx))[-1]
                    duration /= speed  # playback speed: pre-baked frames just show for a shorter/longer time

                now = time.monotonic()
                if out is not None and (fresh or now - last_send >= HEARTBEAT):
                    jpg, packets = out
                    device.write_packets(packets)
                    last_send = now
                    state.last_preview = (jpg, rotation_deg(reel.display_model, reel.is_mirror))  # atomic swap; read lock-free
                    state.preview_id += 1
                    state.frames_sent += 1
                    state.record_packets(len(packets))
                    fps_window_frames += 1
                if now - fps_window_start >= 1.0:
                    state.stream_fps = fps_window_frames / (now - fps_window_start)
                    fps_window_start, fps_window_frames = now, 0

                if reel.streaming:
                    stream_due += duration
                    if stream_due < now - 0.25:  # fell behind (slow encode / stall): resync, don't burst
                        stream_due = now + duration
                    # Not nap(): a wake-up (e.g. the 1 Hz overlay bump) must not pull the next video
                    # frame early. State changes are picked up within one frame period anyway.
                    stop_event.wait(max(0.0, stream_due - time.monotonic()))
                    continue

                # Pre-baked reel: sleep until this frame's delay is up or the heartbeat is due,
                # whichever is first, then advance if the frame's delay elapsed.
                nap(min(frame_start + duration, last_send + HEARTBEAT) - time.monotonic())
                now = time.monotonic()
                if now - frame_start >= duration - 0.005:
                    frame_start = now if now - frame_start > 2 * duration else frame_start + duration
                    with state.lock:
                        if state.reel is reel:
                            state.frame_index = (idx + 1) % len(reel)
                            cur_idx = state.frame_index

            except usb.core.USBError as e:
                log.warning("USB error during streaming: %s, reconnecting", e)
                device.disconnect()
                with state.lock:
                    state.settings_dirty = True
                nap(0.5)
            except Exception:
                log.exception("frame loop error")
                nap(0.5)
    finally:
        if cur_reel is not None and cur_reel.streaming:
            cur_reel.close()


class IPCHandler(socketserver.StreamRequestHandler):
    def handle(self):
        state: State = self.server.state
        device: Device = self.server.device
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                response = dispatch(request, state, device)
            except Exception as e:
                response = {"ok": False, "error": str(e)}
            self.wfile.write((json.dumps(response) + "\n").encode())


class IPCServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def dispatch(request: dict, state: State, device: Device) -> dict:
    action = request.get("action")

    if action == "get_preview":  # hot path: no lock, no USB, no encoding
        preview = state.last_preview
        if preview is None:
            return {"ok": False, "error": "no frame streamed yet"}
        jpg, rotation = preview
        pid = state.preview_id
        if request.get("last_id") == pid:
            return {"ok": True, "unchanged": True, "id": pid}
        return {"ok": True, "id": pid, "rotation": rotation, "jpeg_base64": base64.b64encode(jpg).decode("ascii")}

    if action == "get_status":
        status = state.status()
        status["usb_connected"] = device.dev is not None
        return {"ok": True, "status": status}

    if action == "set_media":
        state.set_media(request["path"])
        return {"ok": True, "status": state.status()}

    if action == "remove_recent":
        target = resolve_path(request["path"])
        with state.lock:
            state.recent_media = [p for p in state.recent_media if p != target]
        state.save()
        return {"ok": True, "status": state.status()}

    if action == "set_brightness":
        value = int(request["value"])
        if not 0 <= value <= 100:
            return {"ok": False, "error": "brightness must be 0-100"}
        with state.lock:
            state.brightness = value
            state.settings_dirty = True
        state.frame_wake.set()
        state.save()
        return {"ok": True, "status": state.status()}

    if action == "set_mode":
        mode = request.get("value")
        if mode not in MODE_NAMES:
            return {"ok": False, "error": "mode must be 'horizontal' or 'vertical'"}
        state.reconfigure(display_model=MODE_NAMES[mode])
        return {"ok": True, "status": state.status()}

    if action == "set_mirror":
        state.reconfigure(is_mirror=bool(request["value"]))
        return {"ok": True, "status": state.status()}

    if action == "set_celsius":
        with state.lock:
            state.celsius = bool(request["value"])
            state.settings_dirty = True
        state.overlay_wake.set()
        state.frame_wake.set()
        state.save()
        return {"ok": True, "status": state.status()}

    if action == "set_engine_mode":
        mode = request.get("value")
        if mode not in ENGINE_MODES:
            return {"ok": False, "error": f"engine mode must be one of {list(ENGINE_MODES)}"}
        state.reconfigure(engine_mode=mode)
        return {"ok": True, "status": state.status()}

    if action == "set_framing":
        patch = request.get("value", {})
        if not isinstance(patch, dict):
            return {"ok": False, "error": "framing must be an object"}
        unknown = set(patch) - {"fit", "zoom", "pan_x", "pan_y", "speed"}
        if unknown:
            return {"ok": False, "error": f"unknown framing key(s): {sorted(unknown)}"}
        if "fit" in patch and patch["fit"] not in FIT_MODES:
            return {"ok": False, "error": f"fit must be one of {list(FIT_MODES)}"}
        if request.get("reset"):
            patch = {**normalize_framing(None), **patch}
        state.reconfigure(framing_patch=patch)
        return {"ok": True, "status": state.status()}

    if action == "set_overlay":
        patch = request.get("value", {})
        if "theme" in patch and patch["theme"] not in THEMES:
            return {"ok": False, "error": f"theme must be one of {sorted(THEMES)}"}
        for key in ("primary",):
            if key in patch and patch[key] not in METRIC_CHOICES:
                return {"ok": False, "error": f"{key} must be one of {sorted(METRIC_CHOICES)}"}
        if "secondary" in patch:
            bad = [m for m in patch["secondary"] if m not in METRIC_CHOICES]
            if bad:
                return {"ok": False, "error": f"unknown secondary metric(s): {bad}"}
        state.reconfigure(overlay_patch=patch)
        return {"ok": True, "status": state.status()}

    return {"ok": False, "error": f"unknown action: {action}"}


def load_config(path: str) -> dict:
    """Load the user config; if it doesn't exist yet, seed from the packaged defaults.
    A corrupt file is moved aside rather than crashing the service into a restart loop.
    """
    for candidate in (path, _find_seed_config()):
        if candidate and os.path.isfile(candidate):
            try:
                with open(candidate) as f:
                    return json.load(f)
            except (OSError, ValueError) as e:
                log.error("cannot read config %s: %s", candidate, e)
                if candidate == path:
                    try:
                        os.replace(path, path + ".corrupt")
                    except OSError:
                        pass
    return {}


def main():
    ap = argparse.ArgumentParser(description="DeepCool LT360 VISION display daemon")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config = load_config(args.config)
    state = State(config, config_path=args.config)
    log.info("lt360d %s", __version__)
    log.info("config %s: media=%s brightness=%d mode=%s engine=%s", args.config, state.media_path,
             state.brightness, "horizontal" if state.display_model == 0 else "vertical", state.engine_mode)

    device = Device()
    stop_event = threading.Event()

    if os.path.exists(args.socket):
        os.remove(args.socket)
    old_umask = os.umask(0o177)  # socket is created 0600: no window where other users can connect
    try:
        server = IPCServer(args.socket, IPCHandler)
    finally:
        os.umask(old_umask)
    os.chmod(args.socket, 0o600)  # owner only: the socket can drive the cooler and returns screen previews
    server.state = state
    server.device = device

    ipc_thread = threading.Thread(target=server.serve_forever, daemon=True)
    ipc_thread.start()
    log.info("IPC listening on %s", args.socket)

    sensor_thread = threading.Thread(target=sensor_loop, args=(state, stop_event), daemon=True)
    sensor_thread.start()

    def handle_signal(signum, frame):
        log.info("shutting down")
        stop_event.set()
        state.frame_wake.set()
        server.shutdown()

    import signal
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Restore the saved reel before entering the USB streaming loop (IPC is already up, so
    # a slow decode never makes the socket unresponsive).
    if state.media_path:
        try:
            state.set_media(state.media_path, persist=False)
        except Exception as e:
            log.error("failed to load saved media %s (will keep retrying): %s", state.media_path, e)

    try:
        frame_loop(device, state, stop_event)
    finally:
        device.disconnect()
        server.shutdown()
        if os.path.exists(args.socket):
            os.remove(args.socket)


if __name__ == "__main__":
    main()
