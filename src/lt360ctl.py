#!/usr/bin/env python3
"""lt360ctl - control CLI for the lt360d daemon.

Examples:
    lt360ctl media /path/to/image.gif
    lt360ctl brightness 75
    lt360ctl mode horizontal
    lt360ctl mirror on
    lt360ctl engine full
    lt360ctl customize --preset renmin_cyberpunk
    lt360ctl status
"""
import argparse
import json
import os
import socket
import sys

DEFAULT_SOCKET_PATH = "/tmp/lt360.sock"


def send(sock_path: str, request: dict) -> dict:
    if not os.path.exists(sock_path):
        print(f"error: daemon socket not found at {sock_path} (is lt360d running?)", file=sys.stderr)
        sys.exit(1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.sendall((json.dumps(request) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode())


def bool_arg(value: str) -> bool:
    if value.lower() in ("on", "true", "1", "yes"):
        return True
    if value.lower() in ("off", "false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"expected on/off, got {value!r}")


def customize(args):
    import shlex
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
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        cmd = shlex.split(editor) + [C.CUSTOM_PATH] if editor else ["xdg-open", C.CUSTOM_PATH]
        try:
            subprocess.call(cmd)
        except OSError as e:
            print(f"error: cannot launch {cmd[0]}: {e}", file=sys.stderr)
            sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Control the DeepCool LT360 VISION daemon")
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
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

    sub.add_parser("status", help="show current daemon state")

    args = ap.parse_args()

    if args.command == "customize":
        customize(args)
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
