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
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

echo "==> Creating venv at $VENV"
mkdir -p "$PREFIX"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install "$REPO_DIR[gui]"

echo "==> Writing config to $CONFIG_DIR/config.json"
mkdir -p "$CONFIG_DIR"
# Never overwrite: this file is the daemon's persisted state (media, brightness, ...).
if [ ! -e "$CONFIG_DIR/config.json" ]; then
    cp "$REPO_DIR/default_config.json" "$CONFIG_DIR/config.json"
else
    echo "    (existing config.json kept - your saved media/settings are untouched)"
fi

echo "==> Installing udev rule (requires sudo)"
if [ -f "$UDEV_RULE_DST" ] && cmp -s "$UDEV_RULE_SRC" "$UDEV_RULE_DST"; then
    echo "    (already installed)"
else
    sudo cp "$UDEV_RULE_SRC" "$UDEV_RULE_DST"
    sudo udevadm control --reload-rules
    sudo udevadm trigger
fi

echo "==> Enabling k10temp (AMD CPU temperature) now and on every boot (requires sudo)"
K10_CONF="/etc/modules-load.d/k10temp.conf"
if grep -qi AuthenticAMD /proc/cpuinfo; then
    if lsmod | grep -q '^k10temp'; then
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
    fish -c "fish_add_path -U $HOME/.local/bin" || echo "    (could not update fish PATH; add ~/.local/bin manually)"
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
mkdir -p "$SYSTEMD_USER_DIR"
cp "$REPO_DIR/systemd/deepcool-lt360.service" "$SYSTEMD_USER_DIR/deepcool-lt360.service"
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
echo "    Logs:   journalctl --user -u deepcool-lt360 -f"
