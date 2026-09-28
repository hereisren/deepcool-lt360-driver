"""Media loading for the two engine modes.

* performance: a static image, GIF, or (capped) video is pre-rendered into a loop of JPEG frames
  with their USB packet lists already baked, so the hot loop never touches Pillow or ffmpeg.
* full: images/GIFs are pre-baked with the smaller "full" JPEG profile, while video is *streamed*
  through a long-lived ffmpeg process (VideoStreamReel), so any length plays without a cutoff and
  RAM stays flat.
"""
import fractions
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from PIL import Image, ImageSequence

from lt360_common import (
    DEFAULT_ENGINE_MODE, ENGINE_PROFILES, HORIZONTAL, canvas_size, jpeg_packets, render_canvas,
    rotate_and_encode,
)

log = logging.getLogger("lt360d.media")

# Logical frame delay for stills. A still is only re-sent on the heartbeat, so this just bounds
# how often the frame loop wakes up.
STILL_FRAME_SECONDS = 1.0
DEFAULT_FRAME_SECONDS = 1 / 30

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
# performance mode: whole clip is pre-decoded into RAM, so it is capped. With the overlay on each
# frame is kept as a raw 1.2 MB canvas, i.e. 450 frames ~ 550 MB; lower PERF_MAX_FRAMES if that hurts.
PERF_VIDEO_FPS = 15
PERF_MAX_FRAMES = PERF_VIDEO_FPS * 30  # >= 30 s of video
# full mode: streamed, so only the frame rate is bounded.
STREAM_MAX_FPS = 20


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


@dataclass
class Reel:
    """A pre-rendered, ready-to-loop sequence of frames.

    `frames` holds (jpeg, packets, duration) for overlay-off playback. When the
    overlay is active, `canvas_frames` holds (PIL canvas, duration) instead so
    the daemon can composite a fresh overlay onto each one.
    The display params it was baked for travel with it, so a reel that is
    being swapped out mid-flight is never rendered with mismatched settings.
    """
    frames: list = field(default_factory=list)
    canvas_frames: list = field(default_factory=list)
    path: str = ""
    display_model: int = HORIZONTAL
    is_mirror: bool = False
    quality: int = 90
    optimize: bool = False
    streaming = False

    def frame_at(self, index: int):
        return self.frames[index % len(self.frames)]

    def canvas_frame_at(self, index: int):
        return self.canvas_frames[index % len(self.canvas_frames)]

    def __len__(self):
        return len(self.frames) or len(self.canvas_frames)


def _probe_fps(path: str) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
             "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=10, check=True).stdout
        fps = float(fractions.Fraction(out.strip().splitlines()[0]))
        return fps if fps > 0 else None
    except Exception:
        return None


class VideoStreamReel:
    """Endless video source: one ffmpeg process piping raw RGB24 canvas frames, looping forever with
    `-stream_loop -1`. Used only from the frame-loop thread; `close()` must be called when swapped out.
    """
    streaming = True

    def __init__(self, path: str, display_model: int, is_mirror: bool, quality: int, optimize: bool):
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required for video files but was not found")
        native = _probe_fps(path)
        if native is None and not os.path.isfile(path):
            raise FileNotFoundError(path)
        self.path, self.display_model, self.is_mirror = path, display_model, is_mirror
        self.quality, self.optimize = quality, optimize
        self.fps = min(native or STREAM_MAX_FPS, STREAM_MAX_FPS)
        self.size = canvas_size(display_model)
        self._frame_bytes = self.size[0] * self.size[1] * 3
        self._proc = None

    def __len__(self):
        return 1  # never empty; the frame loop treats len()==0 as "nothing to show"

    def _start(self):
        w, h = self.size
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-stream_loop", "-1", "-i", self.path, "-an",
               "-vf", f"fps={self.fps:g},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        log.info("streaming %s at %g fps (ffmpeg pid %d)", self.path, self.fps, self._proc.pid)

    def read_raw(self) -> bytes:
        """Next canvas frame as raw RGB24 bytes. Restarts ffmpeg once if it exits (e.g. -stream_loop
        unsupported for the container); raises if it still yields nothing.
        """
        for _ in range(2):
            if self._proc is None:
                self._start()
            buf = self._proc.stdout.read(self._frame_bytes)
            if len(buf) == self._frame_bytes:
                return buf
            self.close()
        raise RuntimeError(f"ffmpeg produced no frames for {self.path}")

    def close(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def _video_canvases(path: str, size: tuple[int, int], fps: int, max_seconds: float):
    """Yield canvas-sized RGB frames from a video via ffmpeg (cover-scaled, capped fps/length)."""
    w, h = size
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-t", str(max_seconds),
           "-an", "-vf", f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is required for video files but was not found")
    frame_bytes = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield Image.frombytes("RGB", (w, h), buf)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _source_canvases(path: str, display_model: int, max_frames: int):
    """Yield (canvas, duration_seconds) for every frame of a pre-decoded source."""
    if is_video(path):
        # one frame past the cap, so load_reel can tell the clip was truncated
        for canvas in _video_canvases(path, canvas_size(display_model), PERF_VIDEO_FPS,
                                      (max_frames + 1) / PERF_VIDEO_FPS):
            yield canvas, 1.0 / PERF_VIDEO_FPS
        return
    img = Image.open(path)
    if getattr(img, "is_animated", False):
        for frame in ImageSequence.Iterator(img):
            duration_ms = frame.info.get("duration", 0) or int(DEFAULT_FRAME_SECONDS * 1000)
            yield render_canvas(frame.copy(), display_model), duration_ms / 1000.0
    else:
        yield render_canvas(img, display_model), STILL_FRAME_SECONDS


def load_reel(path: str, display_model: int = HORIZONTAL, is_mirror: bool = False,
              engine_mode: str = DEFAULT_ENGINE_MODE, with_canvas: bool = False,
              max_frames: int = PERF_MAX_FRAMES):
    """Load an image/GIF/video for the given engine mode.

    Full mode + video returns a VideoStreamReel. Otherwise every frame is pre-rendered: with
    `with_canvas` (overlay on) the pre-rotation canvases are kept for the compositor; without it each
    frame is rotated, JPEG-encoded and packetised now (consecutive identical frames are merged).
    """
    profile = ENGINE_PROFILES[engine_mode]
    quality, optimize = profile["quality"], profile["optimize"]
    if engine_mode == "full" and is_video(path):
        return VideoStreamReel(path, display_model, is_mirror, quality, optimize)

    reel = Reel(path=path, display_model=display_model, is_mirror=is_mirror, quality=quality, optimize=optimize)
    for canvas, duration in _source_canvases(path, display_model, max_frames):
        if len(reel) >= max_frames:
            log.warning("%s truncated to %d frames (performance mode); use full mode for the whole video",
                        path, max_frames)
            break
        if with_canvas:
            reel.canvas_frames.append((canvas, duration))
            continue
        jpg = rotate_and_encode(canvas, display_model, is_mirror, quality, optimize)
        if reel.frames and reel.frames[-1][0] == jpg:  # identical to previous frame: hold it longer
            prev_jpg, prev_packets, prev_duration = reel.frames[-1]
            reel.frames[-1] = (prev_jpg, prev_packets, prev_duration + duration)
        else:
            reel.frames.append((jpg, jpeg_packets(jpg), duration))
    if len(reel) == 0:
        raise RuntimeError(f"no frames decoded from {path}")
    log.info("loaded %s: %d frame(s) [%s, canvas cache=%s]", path, len(reel), engine_mode, with_canvas)
    return reel
