#!/usr/bin/env python3
"""First hardware bring-up probe for the DeepCool LT360 VISION (3633:002e).

Implements the reference encoder from PROTOCOL.md §24: sends [05 01] stream-start
and [04 mode brightness unit] settings on EP 0x04, then streams a rendered test
card as baseline JPEG frames (480x854, rotated per §23) on EP 0x02.

Usage:
    .venv/bin/python test_probe.py --duration 10 --brightness 60
"""
import argparse
import io
import sys
import time

import psutil
import usb.core
import usb.util
from PIL import Image, ImageDraw, ImageFont

VID, PID = 0x3633, 0x002E
EP_IMAGE = 0x02
EP_CMD = 0x04
INTERFACE = 0

HORIZONTAL, VERTICAL = 0, 1
W, H = 480, 854  # native panel: portrait, always what's sent to the device

FONT_DIR = "assets/fonts"


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


def prepare_frame(img: Image.Image, display_model: int = HORIZONTAL, is_mirror: bool = False,
                   quality: int = 95) -> bytes:
    size = (854, 480) if display_model == HORIZONTAL else (480, 854)
    img = img.convert("RGB").resize(size)
    cw = rotation_deg(display_model, is_mirror)
    if cw:
        img = img.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                              270: Image.Transpose.ROTATE_90}[cw])
    assert img.size == (W, H)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, subsampling=2, progressive=False, optimize=False)
    return buf.getvalue()


def image_packets(data: bytes):
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


def load_font(name_substr, size):
    import glob
    matches = glob.glob(f"{FONT_DIR}/*{name_substr}*")
    if matches:
        try:
            return ImageFont.truetype(matches[0], size)
        except Exception:
            pass
    return ImageFont.load_default()


def cpu_temp_c():
    try:
        temps = psutil.sensors_temperatures()
        for key in ("k10temp", "coretemp", "cpu_thermal", "zenpower"):
            if key in temps and temps[key]:
                return temps[key][0].current
        for entries in temps.values():
            if entries:
                return entries[0].current
    except Exception:
        pass
    return None


def render_test_card(size, frame_no, elapsed, mode):
    img = Image.new("RGB", size, (10, 10, 20))
    draw = ImageDraw.Draw(img)
    w, h = size
    border = 12
    colors = {"top": (255, 60, 60), "bottom": (60, 120, 255), "left": (60, 220, 90), "right": (240, 200, 40)}
    draw.rectangle([0, 0, w - 1, border - 1], fill=colors["top"])
    draw.rectangle([0, h - border, w - 1, h - 1], fill=colors["bottom"])
    draw.rectangle([0, 0, border - 1, h - 1], fill=colors["left"])
    draw.rectangle([w - border, 0, w - 1, h - 1], fill=colors["right"])

    label_font = load_font("JZFSSans-Regular", 22)
    big_font = load_font("JZFSSans-SemiBold", 28)
    num_font = load_font("Pixel-numsymbol", 40)

    draw.text((w // 2, border + 4), "TOP", font=label_font, fill="white", anchor="ma")
    draw.text((w // 2, h - border - 4), "BOTTOM", font=label_font, fill="white", anchor="mb")
    left_txt = Image.new("RGBA", (150, 30), (0, 0, 0, 0))
    ld = ImageDraw.Draw(left_txt)
    ld.text((0, 0), "LEFT", font=label_font, fill="white")
    left_txt = left_txt.rotate(90, expand=True)
    img.paste(left_txt, (border + 2, h // 2 - left_txt.height // 2), left_txt)
    right_txt = Image.new("RGBA", (150, 30), (0, 0, 0, 0))
    rd = ImageDraw.Draw(right_txt)
    rd.text((0, 0), "RIGHT", font=label_font, fill="white")
    right_txt = right_txt.rotate(-90, expand=True)
    img.paste(right_txt, (w - border - 2 - right_txt.width, h // 2 - right_txt.height // 2), right_txt)

    cy = h // 2
    draw.text((w // 2, cy - 60), "LT360 VISION — ARCH LINUX", font=big_font, fill="white", anchor="mm")
    temp = cpu_temp_c()
    temp_str = f"{temp:.1f}C" if temp is not None else "N/A"
    draw.text((w // 2, cy - 10), f"CPU {temp_str}  {psutil.cpu_percent():.0f}%",
              font=num_font, fill=(255, 230, 120), anchor="mm")
    draw.text((w // 2, cy + 40), f"frame {frame_no}  t={elapsed:.1f}s  mode={mode}",
              font=label_font, fill=(180, 180, 200), anchor="mm")
    return img


def open_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print(f"Device {VID:04x}:{PID:04x} not found", file=sys.stderr)
        sys.exit(1)
    if dev.is_kernel_driver_active(INTERFACE):
        dev.detach_kernel_driver(INTERFACE)
    usb.util.claim_interface(dev, INTERFACE)
    return dev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=10)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--brightness", type=int, default=50)
    ap.add_argument("--mode", choices=["horizontal", "vertical"], default="horizontal")
    ap.add_argument("--mirror", action="store_true")
    args = ap.parse_args()

    display_model = HORIZONTAL if args.mode == "horizontal" else VERTICAL
    canvas_size = (854, 480) if display_model == HORIZONTAL else (480, 854)

    dev = open_device()
    print(f"Opened {VID:04x}:{PID:04x}, claimed interface {INTERFACE}")

    try:
        n = dev.write(EP_CMD, cmd_stream_start(), timeout=1000)
        print(f"cmd_stream_start: wrote {n} bytes")
        n = dev.write(EP_CMD, cmd_settings(args.brightness, celsius=True, display_model=display_model), timeout=1000)
        print(f"cmd_settings(brightness={args.brightness}): wrote {n} bytes")

        period = 1.0 / args.fps
        start = time.monotonic()
        frame_no = 0
        frame_times = []
        pkt_count_total = 0
        errors = 0

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= args.duration:
                break
            t0 = time.monotonic()

            img = render_test_card(canvas_size, frame_no, elapsed, args.mode)
            jpg = prepare_frame(img, display_model=display_model, is_mirror=args.mirror)

            pkt_count = 0
            try:
                for pkt in image_packets(jpg):
                    dev.write(EP_IMAGE, pkt, timeout=5000)
                    pkt_count += 1
            except usb.core.USBError as e:
                errors += 1
                print(f"frame {frame_no}: USB error: {e}", file=sys.stderr)

            dt_ms = (time.monotonic() - t0) * 1000
            frame_times.append(dt_ms)
            pkt_count_total += pkt_count
            frame_no += 1

            if frame_no % max(1, args.fps) == 0:
                print(f"frame {frame_no}: {dt_ms:.1f} ms, {pkt_count} pkts, jpeg {len(jpg)} B")

            sleep_left = period - (time.monotonic() - t0)
            if sleep_left > 0:
                time.sleep(sleep_left)

        avg = sum(frame_times) / len(frame_times) if frame_times else 0
        print("\n--- summary ---")
        print(f"frames sent: {frame_no}")
        print(f"packets sent: {pkt_count_total}")
        print(f"avg frame time: {avg:.1f} ms ({1000/avg:.1f} fps actual)" if avg else "no frames")
        print(f"errors: {errors}")

    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        try:
            usb.util.release_interface(dev, INTERFACE)
            usb.util.dispose_resources(dev)
            print("Released interface, disposed resources")
        except Exception as e:
            print(f"cleanup error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
