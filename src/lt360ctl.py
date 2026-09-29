#!/usr/bin/env python3
"""lt360ctl - control CLI for the lt360d daemon.

Examples:
    lt360ctl media /path/to/image.gif
    lt360ctl brightness 75
    lt360ctl mode horizontal
    lt360ctl mirror on
    lt360ctl engine full
    lt360ctl framing --fit cover --zoom 1.4 --pan-x -30 --speed 1.5
    lt360ctl customize --preset renmin_cyberpunk
    lt360ctl status
    lt360ctl status --waybar      # one-line JSON for a Waybar "custom" module
"""
import argparse
import json
import os
import socket
import sys

from lt360_common import __version__
from lt360_ipc import default_socket_path

DEFAULT_SOCKET_PATH = default_socket_path()
HOT_C, WARM_C = 85.0, 70.0   # Waybar "class" thresholds (hottest of CPU/GPU, in °C)


def call(sock_path: str, request: dict, timeout: float | None = None) -> dict:
    """One request/response over the daemon socket; raises OSError/ValueError on failure."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        if timeout is not None:
            s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall((json.dumps(request) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode())


def send(sock_path: str, request: dict) -> dict:
    if not os.path.exists(sock_path):
        print(f"error: daemon socket not found at {sock_path} (is lt360d running?)", file=sys.stderr)
        sys.exit(1)
    return call(sock_path, request)


def waybar_status(sock_path: str) -> dict:
    """Waybar custom-module payload. Never raises: an unreachable daemon is just the "offline" class."""
    try:
        resp = call(sock_path, {"action": "get_status"}, timeout=2.0) if os.path.exists(sock_path) \
            else {"ok": False, "error": "daemon not running"}
    except (OSError, ValueError) as e:
        resp = {"ok": False, "error": str(e) or type(e).__name__}
    if not resp.get("ok"):
        return {"text": "LT360 off", "alt": "offline", "class": "offline", "percentage": 0,
                "tooltip": f"LT360 VISION: daemon offline\n{resp.get('error', '')}".strip()}

    st = resp.get("status", {})
    sensors = st.get("sensors", {})
    celsius = bool(st.get("celsius", True))
    unit = "C" if celsius else "F"

    def temp(key):
        v = sensors.get(key)
        return None if v is None else (v if celsius else v * 9 / 5 + 32)

    def fmt(v, suffix=""):
        return "N/A" if v is None else f"{v:.0f}{suffix}"

    cpu, gpu = temp("cpu_temp"), temp("gpu_temp")
    parts = [f"{fmt(t)}°" for t in (cpu, gpu) if t is not None]
    text = " · ".join(parts) if parts else "LT360"
    hottest_c = max([v for v in (sensors.get("cpu_temp"), sensors.get("gpu_temp")) if v is not None], default=None)
    usb = st.get("usb_connected", True)
    if not usb:
        cls = "disconnected"
    elif hottest_c is not None and hottest_c >= HOT_C:
        cls = "hot"
    elif hottest_c is not None and hottest_c >= WARM_C:
        cls = "warm"
    else:
        cls = "normal"
    overlay = st.get("overlay", {})
    media = os.path.basename(st.get("media") or "") or "none"
    tooltip = "\n".join([
        f"LT360 VISION {st.get('version', '')}".rstrip(),
        f"CPU  {fmt(cpu, '°' + unit)}  ·  {fmt(sensors.get('cpu_load'), '%')} load",
        f"GPU  {fmt(gpu, '°' + unit)}  ·  {fmt(sensors.get('gpu_load'), '%')} load",
        f"USB  {'linked' if usb else 'OFFLINE'}  ·  {st.get('stream_fps', 0):g} fps  ·  {st.get('engine_mode', '?')} engine",
        f"Brightness {st.get('brightness', '?')}%  ·  overlay {overlay.get('theme', '?') if overlay.get('enabled') else 'off'}",
        f"Media  {media}",
    ])
    pct = 0 if hottest_c is None else max(0, min(100, round(hottest_c)))
    return {"text": text, "alt": cls, "class": cls, "percentage": pct, "tooltip": tooltip}


def bool_arg(value: str) -> bool:
    if value.lower() in ("on", "true", "1", "yes"):
        return True
    if value.lower() in ("off", "false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"expected on/off, got {value!r}")


def customize(args):
    import subprocess

    import lt360_custom as C

    if args.list:
        print("\n".join(C.list_presets()) or "no presets found in " + C.PRESETS_DIR)
        return
    try:
        if args.preset:
            C.apply_preset(args.preset)
            print(f"applied preset '{args.preset}' (previous file saved as customize.json.bak)")
        elif args.reset:
            C.reset_default()
            print("restored default customize.json (previous file saved as customize.json.bak)")
        else:
            C.seed_default()
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(C.CUSTOM_PATH)

    if os.path.exists(args.socket):  # switching the theme needs the daemon; the file work above does not
        response = send(args.socket, {"action": "set_overlay", "value": {"theme": "custom", "enabled": True}})
        if not response.get("ok"):
            print(f"warning: could not activate the custom theme: {response.get('error')}", file=sys.stderr)
        else:
            err = response.get("status", {}).get("custom", {}).get("error")
            print("custom overlay theme active (edits hot-reload within ~1 s)")
            if err:
                print(f"warning: customize.json currently invalid, last good layout stays: {err}", file=sys.stderr)
    else:
        print(f"warning: daemon socket not found at {args.socket}; theme not switched", file=sys.stderr)

    if args.edit:
        try:
            C.open_in_text_editor(C.CUSTOM_PATH, wait=True)
        except OSError as e:
            print(f"error: cannot launch editor: {e}", file=sys.stderr)
            sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Control the DeepCool LT360 VISION daemon")
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
    ap.add_argument("--version", action="version", version=f"lt360ctl {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("media", help="set the image/GIF the panel displays")
    p.add_argument("path")

    p = sub.add_parser("brightness", help="set panel brightness 0-100")
    p.add_argument("value", type=int)

    p = sub.add_parser("mode", help="set panel orientation")
    p.add_argument("value", choices=["horizontal", "vertical"])

    p = sub.add_parser("mirror", help="enable/disable mirrored rotation")
    p.add_argument("value", type=bool_arg)

    p = sub.add_parser("engine", help="performance = low CPU (pre-baked, 30s video cap); "
                                      "full = low USB packets + unlimited video streaming")
    p.add_argument("value", choices=["performance", "full"])

    p = sub.add_parser("framing", help="media framing: fit, zoom, pan and playback speed")
    p.add_argument("--fit", choices=["cover", "contain"], help="cover = fill and crop, contain = letterbox")
    p.add_argument("--zoom", type=float, metavar="X", help="1.0 - 2.5")
    p.add_argument("--pan-x", type=float, metavar="PCT", help="-100 (left edge) .. 0 (center) .. 100 (right edge)")
    p.add_argument("--pan-y", type=float, metavar="PCT", help="-100 (top edge) .. 0 (center) .. 100 (bottom edge)")
    p.add_argument("--speed", type=float, metavar="X", help="playback speed, e.g. 0.5, 1, 1.5, 2")
    p.add_argument("--reset", action="store_true", help="back to cover / 1.0x / centered / 1.0x speed")

    p = sub.add_parser("celsius", help="switch temperature unit")
    p.add_argument("value", type=bool_arg)

    p = sub.add_parser("overlay", help="configure the sensor telemetry overlay")
    p.add_argument("enabled", type=bool_arg)
    p.add_argument("--theme", choices=["boundary", "codezero", "pixelworld", "custom"])
    p.add_argument("--primary", choices=["cpu_temp", "gpu_temp", "cpu_load", "gpu_load", "time", "off"])
    p.add_argument("--secondary", help="comma-separated list, e.g. gpu_temp,cpu_load,time")

    p = sub.add_parser("customize", help="hot-reloading custom overlay file (customize.json)")
    p.add_argument("--edit", action="store_true", help="open the file in $EDITOR")
    p.add_argument("--preset", metavar="NAME", help="copy examples/overlays/NAME.json into customize.json")
    p.add_argument("--reset", action="store_true", help="restore the default customize.json")
    p.add_argument("--list", action="store_true", help="list bundled presets")

    p = sub.add_parser("preview", help="save the last streamed frame as a JPEG")
    p.add_argument("path", nargs="?", default="preview.jpg")

    p = sub.add_parser("status", help="show current daemon state")
    p.add_argument("--waybar", action="store_true",
                   help='print one line of Waybar JSON ({"text", "tooltip", "class", ...}) and always exit 0')

    args = ap.parse_args()

    if args.command == "customize":
        customize(args)
        return
    if args.command == "status" and args.waybar:
        print(json.dumps(waybar_status(args.socket), ensure_ascii=False), flush=True)
        return

    if args.command == "media":
        request = {"action": "set_media", "path": os.path.abspath(args.path)}
    elif args.command == "brightness":
        request = {"action": "set_brightness", "value": args.value}
    elif args.command == "mode":
        request = {"action": "set_mode", "value": args.value}
    elif args.command == "mirror":
        request = {"action": "set_mirror", "value": args.value}
    elif args.command == "engine":
        request = {"action": "set_engine_mode", "value": args.value}
    elif args.command == "framing":
        value = {k: v for k, v in (("fit", args.fit), ("zoom", args.zoom), ("speed", args.speed),
                                   ("pan_x", None if args.pan_x is None else args.pan_x / 100),
                                   ("pan_y", None if args.pan_y is None else args.pan_y / 100)) if v is not None}
        if not value and not args.reset:
            ap.error("framing: give at least one of --fit/--zoom/--pan-x/--pan-y/--speed, or --reset")
        request = {"action": "set_framing", "value": value, "reset": args.reset}
    elif args.command == "celsius":
        request = {"action": "set_celsius", "value": args.value}
    elif args.command == "overlay":
        value = {"enabled": args.enabled}
        if args.theme:
            value["theme"] = args.theme
        if args.primary:
            value["primary"] = args.primary
        if args.secondary is not None:
            value["secondary"] = [m.strip() for m in args.secondary.split(",") if m.strip()]
        request = {"action": "set_overlay", "value": value}
    elif args.command == "preview":
        request = {"action": "get_preview"}
    elif args.command == "status":
        request = {"action": "get_status"}

    response = send(args.socket, request)
    if not response.get("ok"):
        print(f"error: {response.get('error')}", file=sys.stderr)
        sys.exit(1)

    if args.command == "preview":
        import base64
        with open(args.path, "wb") as f:
            f.write(base64.b64decode(response["jpeg_base64"]))
        print(f"saved preview to {args.path}")
        return

    status = response.get("status", {})
    if args.command == "status":
        print(f"engine: {status.get('engine_mode')}   pkts/min: {status.get('packets_per_min')}   "
              f"fps: {status.get('stream_fps')}")
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
