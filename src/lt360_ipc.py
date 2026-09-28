"""Where the daemon's control socket lives (shared by lt360d, lt360ctl and lt360-gui)."""
import os


def default_socket_path() -> str:
    """Per-user socket: $XDG_RUNTIME_DIR (0700, tmpfs) when available, else a uid-scoped /tmp name."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "lt360.sock")
    return f"/tmp/lt360-{os.getuid()}.sock"
