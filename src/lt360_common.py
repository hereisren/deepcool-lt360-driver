"""Shared protocol constants and encoding helpers for the DeepCool LT360 VISION.

Reference: PROTOCOL.md §14, §15, §20, §23, §24 (confirmed against hardware in Phase 4).
"""
import io
import os
import site
import sys

from PIL import Image

__version__ = "0.3.0"

VID, PID = 0x3633, 0x002E
EP_IMAGE = 0x02
EP_CMD = 0x04
INTERFACE = 0

HORIZONTAL, VERTICAL = 0, 1
W, H = 480, 854  # native panel: portrait, always what's sent to the device

MODE_NAMES = {"horizontal": HORIZONTAL, "vertical": VERTICAL}

# Engine modes. "performance" pre-bakes USB packets in RAM (near-zero CPU, larger JPEGs, capped video
# length). "full" streams video of any length through ffmpeg and encodes smaller JPEGs (fewer packets).
ENGINE_MODES = ("performance", "full")
DEFAULT_ENGINE_MODE = "full"
ENGINE_PROFILES = {
    "performance": {"quality": 90, "optimize": False},
    "full": {"quality": 78, "optimize": True},
}


# ---------------------------------------------------------------- bundled data (fonts, presets, icon, defaults)

APP_DATA_NAME = "deepcool-lt360"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def share_dirs() -> list[str]:
    """Candidate <prefix>/share directories, most specific first: this interpreter's prefix (venv or
    system), the pip --user base (~/.local), $XDG_DATA_HOME, /usr/local, /usr, then $XDG_DATA_DIRS."""
    dirs = [os.path.join(sys.prefix, "share")]
    try:
        dirs.append(os.path.join(site.getuserbase(), "share"))
    except Exception:
        pass
    dirs.append(os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"))
    dirs += ["/usr/local/share", "/usr/share"]
    dirs += [d for d in os.environ.get("XDG_DATA_DIRS", "").split(":") if d]
    out = []
    for d in dirs:
        d = os.path.abspath(d)
        if d not in out:
            out.append(d)
    return out


def data_roots() -> list[str]:
    """Where assets/ and examples/ may live: the source checkout, then <share>/deepcool-lt360 for each
    share_dirs() entry (covers venv/pip wheel installs and /usr or /usr/local system packages)."""
    return [REPO_ROOT] + [os.path.join(s, APP_DATA_NAME) for s in share_dirs()]


def find_data_dir(*rel: str) -> str | None:
    """First existing <root>/<rel...> directory, e.g. find_data_dir("assets", "fonts")."""
    for root in data_roots():
        p = os.path.join(root, *rel)
        if os.path.isdir(p):
            return p
    return None


def find_data_file(*rel: str) -> str | None:
    for root in data_roots():
        p = os.path.join(root, *rel)
        if os.path.isfile(p):
            return p
    return None


def find_icon_file(name: str = "deepcool-lt360.svg") -> str | None:
    """The app icon: the checkout's desktop/ dir, else the hicolor theme under any share dir."""
    for p in [os.path.join(REPO_ROOT, "desktop", name)] + \
             [os.path.join(s, "icons", "hicolor", "scalable", "apps", name) for s in share_dirs()]:
        if os.path.isfile(p):
            return p
    return None


def checksum16(b: bytes) -> int:
    return sum(b) & 0xFFFF


def general(payload: bytes) -> bytes:
    assert len(payload) <= 42
    body = b"\xAA\x2E" + payload.ljust(42, b"\0")
    return body + checksum16(body).to_bytes(2, "little")


def cmd_stream_start() -> bytes:
    return general(bytes([0x05, 0x01]))


def cmd_settings(brightness: int, celsius: bool = True, display_model: int = 0) -> bytes:
    assert 0 <= brightness <= 100 and display_model in (0, 1)
    return general(bytes([0x04, display_model, brightness, 0 if celsius else 1]))


def rotation_deg(display_model: int, is_mirror: bool) -> int:
    if display_model == VERTICAL:
        return 0 if is_mirror else 180
    return 90 if is_mirror else 270


def canvas_size(display_model: int) -> tuple[int, int]:
    return (854, 480) if display_model == HORIZONTAL else (480, 854)


def cover_resize(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale to cover (target_w, target_h), then center-crop the overflow."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    img = img.resize((new_w, new_h))
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


# Media framing (config.json "framing"): how a source frame is fitted onto the canvas.
FIT_MODES = ("cover", "contain")
ZOOM_RANGE = (1.0, 2.5)
PAN_RANGE = (-1.0, 1.0)      # -1 = left/top edge, 0 = centered, +1 = right/bottom edge
SPEED_RANGE = (0.25, 4.0)
DEFAULT_FRAMING = {"fit": "cover", "zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0, "speed": 1.0}


def normalize_framing(value: dict | None) -> dict:
    """Validated copy of a (possibly partial) framing dict; out-of-range numbers are clamped."""
    out = dict(DEFAULT_FRAMING)
    if not isinstance(value, dict):
        return out
    if value.get("fit") in FIT_MODES:
        out["fit"] = value["fit"]
    for key, (lo, hi) in (("zoom", ZOOM_RANGE), ("pan_x", PAN_RANGE), ("pan_y", PAN_RANGE), ("speed", SPEED_RANGE)):
        try:
            out[key] = round(min(hi, max(lo, float(value.get(key, out[key])))), 3)
        except (TypeError, ValueError):
            pass
    return out


def framing_geometry(src_w: int, src_h: int, target_w: int, target_h: int, framing: dict | None = None):
    """(scale, offset_x, offset_y): where the scaled source lands on the target canvas.

    One formula serves both fits: the offset is (target - scaled) * (1 + pan) / 2, so pan -1 aligns
    the source's left/top edge with the canvas and +1 its right/bottom edge -- a crop offset when the
    source overflows (cover / zoomed), a letterbox position when it is smaller (contain).
    """
    f = framing or DEFAULT_FRAMING
    fit = max if f.get("fit", "cover") == "cover" else min
    s = fit(target_w / src_w, target_h / src_h) * float(f.get("zoom", 1.0))
    nw, nh = src_w * s, src_h * s
    return s, (target_w - nw) * (1 + float(f.get("pan_x", 0.0))) / 2, (target_h - nh) * (1 + float(f.get("pan_y", 0.0))) / 2


def frame_image(img: Image.Image, target_w: int, target_h: int, framing: dict | None = None) -> Image.Image:
    """Fit an RGB image onto a black target_w x target_h canvas using `framing` (cover/contain, zoom, pan).
    Only the visible part of the source is resampled, so zooming in never costs a full-size upscale.
    """
    if framing is None or framing == DEFAULT_FRAMING or \
            all(framing.get(k) == DEFAULT_FRAMING[k] for k in ("fit", "zoom", "pan_x", "pan_y")):
        return cover_resize(img, target_w, target_h)
    src_w, src_h = img.size
    s, ox, oy = framing_geometry(src_w, src_h, target_w, target_h, framing)
    # visible destination rect, clipped to the canvas
    dx0, dy0 = max(0, round(ox)), max(0, round(oy))
    dx1, dy1 = min(target_w, round(ox + src_w * s)), min(target_h, round(oy + src_h * s))
    canvas = Image.new("RGB", (target_w, target_h))
    if dx1 <= dx0 or dy1 <= dy0:
        return canvas
    box = (max(0.0, (dx0 - ox) / s), max(0.0, (dy0 - oy) / s),
           min(float(src_w), (dx1 - ox) / s), min(float(src_h), (dy1 - oy) / s))  # rounding can overshoot
    canvas.paste(img.resize((dx1 - dx0, dy1 - dy0), box=box), (dx0, dy0))
    return canvas


def render_canvas(img: Image.Image, display_model: int = HORIZONTAL, framing: dict | None = None) -> Image.Image:
    """Fit a frame onto the target canvas (cover-crop by default), pre-rotation. This is the
    orientation the overlay compositor draws on top of.
    """
    target_w, target_h = canvas_size(display_model)
    return frame_image(img.convert("RGB"), target_w, target_h, framing)


def rotate_and_encode(img: Image.Image, display_model: int = HORIZONTAL, is_mirror: bool = False,
                       quality: int = 95, optimize: bool = False) -> bytes:
    """Rotate a canvas-sized frame to panel-native orientation and JPEG-encode it."""
    cw = rotation_deg(display_model, is_mirror)
    if cw:
        img = img.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                              270: Image.Transpose.ROTATE_90}[cw])
    assert img.size == (W, H)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, subsampling=2, progressive=False, optimize=optimize)
    return buf.getvalue()


def render_to_jpeg(img: Image.Image, display_model: int = HORIZONTAL, is_mirror: bool = False,
                    quality: int = 95) -> bytes:
    """Cover-resize/crop a frame to the target canvas, rotate, and JPEG-encode for sendImageData."""
    canvas = render_canvas(img, display_model)
    return rotate_and_encode(canvas, display_model, is_mirror, quality)


def image_packets(data: bytes):
    """512-byte EP 0x02 packets built by L136.node sendImageData."""
    C = 505
    cnt = -(-len(data) // C)
    yield (b"Start\x01" + len(data).to_bytes(4, "little") + checksum16(data).to_bytes(2, "little")
           + cnt.to_bytes(2, "little") + b"\0" * 498)
    for i in range(len(data) // C + 1):
        chunk = data[i * C:(i + 1) * C]
        yield b"trans" + (i + 1).to_bytes(2, "little") + chunk.ljust(C, b"\0")
        if len(chunk) < C:
            break
    yield b"DCLdfinish".ljust(512, b"\0")


def jpeg_packets(data: bytes) -> list[bytes]:
    """Pre-bake the full 512-byte packet list for a JPEG so the stream loop is pure USB writes."""
    return list(image_packets(data))
