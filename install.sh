#!/usr/bin/env bash
# Installs the deepcool-lt360 daemon/CLI into a venv, plus its udev rule and
# systemd user service. Safe to re-run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$HOME/.local/share/deepcool-lt360"
VENV="$PREFIX/venv"
CONFIG_DIR="$HOME/.config/deepcool-lt360"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UDEV_RULE_SRC="$REPO_DIR/udev/99-deepcool-lt360.rules"
UDEV_RULE_DST="/etc/udev/rules.d/99-deepcool-lt360.rules"
UDEV_RULE_STALE="/etc/udev/rules.d/70-deepcool-lt360.rules"   # left behind by an earlier dev build
SERVICE_DST="$SYSTEMD_USER_DIR/deepcool-lt360.service"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

# ---------------------------------------------------------------- system dependencies
# Package names per package manager, as "required|optional" lists:
#   python3 >= 3.10 with venv/ensurepip and libusb-1.0 are required; ffmpeg (video media), gcc (only if pip
#   must build psutil from source) and xcb-cursor (PyQt6's X11 plugin; not needed on Wayland) are optional.
PM=""
for pm in pacman apt-get dnf zypper; do
    if command -v "$pm" >/dev/null 2>&1; then PM="$pm"; break; fi
done
case "$PM" in
    pacman)  PM_INSTALL="sudo pacman -S --needed"
             PKG_PY="python"; PKG_VENV="python"; PKG_USB="libusb"; PKG_FFMPEG="ffmpeg"; PKG_GCC="gcc"
             PKG_XCB="xcb-util-cursor" ;;
    apt-get) PM_INSTALL="sudo apt-get install -y"
             PKG_PY="python3"; PKG_VENV="python3-venv"; PKG_USB="libusb-1.0-0"; PKG_FFMPEG="ffmpeg"
             PKG_GCC="gcc python3-dev"; PKG_XCB="libxcb-cursor0" ;;
    dnf)     PM_INSTALL="sudo dnf install -y"
             PKG_PY="python3"; PKG_VENV="python3"; PKG_USB="libusb1"; PKG_FFMPEG="ffmpeg-free"
             PKG_GCC="gcc python3-devel"; PKG_XCB="xcb-util-cursor" ;;
    zypper)  PM_INSTALL="sudo zypper install -y"
             PKG_PY="python3"; PKG_VENV="python3"; PKG_USB="libusb-1_0-0"; PKG_FFMPEG="ffmpeg"
             PKG_GCC="gcc python3-devel"; PKG_XCB="libxcb-cursor0" ;;
    *)       PM_INSTALL=""
             PKG_PY="python3 (>= 3.10)"; PKG_VENV="python3-venv"; PKG_USB="libusb-1.0"; PKG_FFMPEG="ffmpeg"
             PKG_GCC="gcc"; PKG_XCB="libxcb-cursor" ;;
esac

has_py_lib() { python3 -c "import ctypes.util, sys; sys.exit(ctypes.util.find_library('$1') is None)" 2>/dev/null; }

echo "==> Checking system dependencies (${PM:-unknown package manager})"
REQUIRED=()
OPTIONAL=()
if ! command -v python3 >/dev/null 2>&1; then
    REQUIRED+=($PKG_PY)
elif ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "    ERROR: python3 is $(python3 -V 2>&1 | cut -d' ' -f2); lt360 needs Python 3.10 or newer." >&2
    exit 1
else
    python3 -c 'import venv, ensurepip' 2>/dev/null || REQUIRED+=($PKG_VENV)
    has_py_lib usb-1.0 || REQUIRED+=($PKG_USB)
    has_py_lib xcb-cursor || OPTIONAL+=($PKG_XCB)
fi
command -v ffmpeg >/dev/null 2>&1 || OPTIONAL+=($PKG_FFMPEG)
command -v gcc >/dev/null 2>&1 || OPTIONAL+=($PKG_GCC)

if [ ${#REQUIRED[@]} -gt 0 ]; then
    echo "    missing required packages: ${REQUIRED[*]}"
    if [ -n "$PM_INSTALL" ] && [ -t 0 ]; then
        read -r -p "    Install them now with '$PM_INSTALL ${REQUIRED[*]}'? [y/N] " answer
        if [[ "$answer" =~ ^[Yy] ]]; then
            $PM_INSTALL "${REQUIRED[@]}"
        else
            exit 1
        fi
    else
        echo "    Install them with: ${PM_INSTALL:-your package manager} ${REQUIRED[*]}" >&2
        exit 1
    fi
fi
if [ ${#OPTIONAL[@]} -gt 0 ]; then
    echo "    optional, not installed: ${OPTIONAL[*]}"
    echo "      (ffmpeg: MP4/WebM media; gcc: only if pip has to compile psutil; xcb-cursor: lt360-gui on X11)"
    echo "      install with: ${PM_INSTALL:-your package manager} ${OPTIONAL[*]}"
fi

echo "==> Creating venv at $VENV"
mkdir -p "$PREFIX"
python3 -m venv "$VENV"
# Distro venvs (Debian 12, Ubuntu 24.04, ...) seed an old setuptools; pyproject's license = "MIT" needs >= 77.
"$VENV/bin/pip" install --upgrade pip "setuptools>=77" wheel >/dev/null
"$VENV/bin/pip" install "$REPO_DIR[gui]"

echo "==> Writing config to $CONFIG_DIR/config.json"
mkdir -p "$CONFIG_DIR"
# Never overwrite: this file is the daemon's persisted state (media, brightness, ...).
if [ ! -e "$CONFIG_DIR/config.json" ]; then
    cp "$REPO_DIR/default_config.json" "$CONFIG_DIR/config.json"
else
    echo "    (existing config.json kept - your saved media/settings are untouched)"
fi

echo "==> Seeding $CONFIG_DIR/customize.json (custom overlay, hot-reloaded)"
# Never overwritten: this is the user's hand-edited file. Presets: lt360ctl customize --list
"$VENV/bin/python" -c "import lt360_custom as c; print('    created' if c.seed_default() else '    (existing customize.json kept)')"

echo "==> Installing udev rule (requires sudo)"
if [ -f "$UDEV_RULE_DST" ] && cmp -s "$UDEV_RULE_SRC" "$UDEV_RULE_DST" && [ ! -e "$UDEV_RULE_STALE" ]; then
    echo "    (already installed)"
else
    sudo rm -f "$UDEV_RULE_STALE"
    sudo cp "$UDEV_RULE_SRC" "$UDEV_RULE_DST"
    sudo udevadm control --reload-rules
    sudo udevadm trigger
fi

echo "==> Enabling k10temp (AMD CPU temperature) now and on every boot (requires sudo)"
K10_CONF="/etc/modules-load.d/k10temp.conf"
if grep -qi AuthenticAMD /proc/cpuinfo; then
    if grep -q '^k10temp ' /proc/modules; then   # not `lsmod | grep -q`: SIGPIPE + pipefail = false negative
        echo "    (module already loaded)"
    else
        sudo modprobe k10temp || echo "    WARNING: modprobe k10temp failed; CPU temp may show N/A"
    fi
    if [ "$(cat "$K10_CONF" 2>/dev/null)" = "k10temp" ]; then
        echo "    ($K10_CONF already present)"
    else
        echo k10temp | sudo tee "$K10_CONF" >/dev/null
    fi
else
    echo "    (not an AMD CPU, skipping)"
fi

echo "==> Linking CLI/GUI into ~/.local/bin"
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/lt360ctl" "$HOME/.local/bin/lt360ctl"
ln -sf "$VENV/bin/lt360d" "$HOME/.local/bin/lt360d"
ln -sf "$VENV/bin/lt360-gui" "$HOME/.local/bin/lt360-gui"
if command -v fish >/dev/null 2>&1; then
    # universal var, idempotent: persists across shells and reboots
    # fish_add_path returns 1 when the path is already there, so check first
    fish -c 'contains -- $argv[1] $fish_user_paths; or fish_add_path -U $argv[1]' "$HOME/.local/bin" \
        || echo "    (could not update fish PATH; add ~/.local/bin manually)"
fi

echo "==> Installing desktop launcher and icon"
mkdir -p "$APPS_DIR" "$ICON_DIR"
sed "s|^Exec=lt360-gui|Exec=$HOME/.local/bin/lt360-gui|" \
    "$REPO_DIR/desktop/deepcool-lt360.desktop" > "$APPS_DIR/deepcool-lt360.desktop"
cp "$REPO_DIR/desktop/deepcool-lt360.svg" "$ICON_DIR/deepcool-lt360.svg"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

echo "==> Installing systemd user service"
if command -v pacman >/dev/null 2>&1 && pacman -Qq deepcool-lt360-git >/dev/null 2>&1; then
    echo "    WARNING: the deepcool-lt360-git package is also installed. This per-user unit overrides the"
    echo "             package's (it runs the venv). To use the package instead, remove $SERVICE_DST"
    echo "             and ~/.local/share/deepcool-lt360, then: systemctl --user daemon-reload"
elif [ -x /usr/bin/lt360d ]; then
    echo "    WARNING: /usr/bin/lt360d from a system package also exists; this per-user unit overrides it."
fi
mkdir -p "$SYSTEMD_USER_DIR"
# A drop-in from manual debugging would silently override the ExecStart written below.
if [ -d "$SERVICE_DST.d" ]; then
    echo "    removing stale override $SERVICE_DST.d"
    rm -rf "$SERVICE_DST.d"
fi
# The shipped unit runs the packaged /usr/bin/lt360d; this per-user copy runs the venv instead.
sed "s|^ExecStart=.*|ExecStart=$VENV/bin/lt360d --config %h/.config/deepcool-lt360/config.json|" \
    "$REPO_DIR/systemd/deepcool-lt360.service" > "$SERVICE_DST"
systemctl --user daemon-reload
systemctl --user enable --now deepcool-lt360.service
# enable --now does nothing to an already-running service; restart so it runs the freshly
# installed code. State is reloaded from config.json, so nothing is lost.
systemctl --user restart deepcool-lt360.service
systemctl --user is-active --quiet deepcool-lt360.service \
    && echo "    service active, autostart: $(systemctl --user is-enabled deepcool-lt360.service)" \
    || echo "    WARNING: service not active - see: journalctl --user -u deepcool-lt360"

echo "==> Done."
echo "    CLI:    lt360ctl status   (~/.local/bin is on PATH after a new shell)"
echo "    GUI:    lt360-gui         (also in your app launcher as 'LT360 VISION — For Renmin')"
echo "    Custom: lt360ctl customize --edit   (presets: lt360ctl customize --preset renmin_cyberpunk)"
echo "    Logs:   journalctl --user -u deepcool-lt360 -f"
