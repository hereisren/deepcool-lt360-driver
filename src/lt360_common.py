"""Shared protocol constants and encoding helpers for the DeepCool LT360 VISION.

Reference: PROTOCOL.md §14, §15, §20, §23, §24 (confirmed against hardware in Phase 4).
"""
import io

from PIL import Image

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


def render_canvas(img: Image.Image, display_model: int = HORIZONTAL) -> Image.Image:
    """Cover-resize/crop a frame to the target canvas, pre-rotation. This is the
    orientation the overlay compositor draws on top of.
    """
    target_w, target_h = canvas_size(display_model)
    return cover_resize(img.convert("RGB"), target_w, target_h)


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
