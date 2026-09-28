# deepcool-lt360

Linux driver, daemon, CLI and native GUI for the DeepCool LT360 VISION AIO cooler's
USB display (VID `3633`, PID `002e`), reverse-engineered in [`PROTOCOL.md`](PROTOCOL.md).
No official Linux support exists for this panel — this project drives it entirely
over `libusb`, no kernel module or vendor tooling required.

![screenshot placeholder](docs/screenshot-gui.png)
*(screenshot coming soon — run `lt360-gui` to see it live)*

## Features

- Stream any static image or looping GIF to the pump's 480×854 panel.
- Horizontal/vertical orientation and 180° mirror flip, brightness, °C/°F.
- Live CPU/GPU telemetry overlay (temp, load, clock, time) in three built-in
  themes, composited on top of your media in real time at <2% CPU overhead.
- A background daemon (`lt360d`) that reconnects automatically if the device
  is unplugged, plus a scriptable CLI (`lt360ctl`) and a native dark-themed
  GUI (`lt360-gui`) with a live mirror of what's on the pump screen.

## Components

| File | Purpose |
|---|---|
| `src/lt360d.py` | Background daemon. Streams media to the panel, runs the sensor poll + overlay compositor, and listens on a Unix socket (`/tmp/lt360.sock`) for commands. |
| `src/lt360ctl.py` | Scriptable CLI to control the running daemon. |
| `src/lt360_gui.py` | Native PyQt6 desktop app — live preview, media picker, hardware & overlay controls, service management. |
| `src/lt360_sensors.py` | CPU (`psutil`/`k10temp`/`zenpower`/`coretemp`) and GPU (`amdgpu` hwmon or `nvidia-smi`) telemetry reader. |
| `src/lt360_overlay.py` | Themed overlay compositor (Pillow + the bundled DeepCool fonts). |
| `src/lt360_common.py`, `src/lt360_media.py` | Wire protocol encoding and GIF/image pre-rendering. |
| `default_config.json` | Media/brightness/mode/overlay loaded by the daemon on startup. |
| `systemd/deepcool-lt360.service` | User service unit. |
| `udev/99-deepcool-lt360.rules` | Lets the daemon open the device without root. |
| `desktop/` | `.desktop` launcher entry and app icon. |
| `packaging/PKGBUILD` | Arch Linux / AUR package. |

## Hardware compatibility

Tested against the **DeepCool LT360 VISION** (`lsusb` name `DC LT360 VISION`,
VID:PID `3633:002e`). The device exposes one vendor-specific USB interface
with four bulk endpoints — no HID interface, no kernel driver binds to it, so
it must be driven via `libusb`/`pyusb`, which is what this project does. Other
DeepCool "VISION" panels sharing this protocol family may work but are
untested; see `PROTOCOL.md` for the full reverse-engineering notes if you want
to check your own device against them.

## Install

### Arch Linux / AUR

```sh
git clone https://github.com/hereisren/deepcool-lt360-driver.git
cd deepcool-lt360-driver/packaging
makepkg -si
```

This installs system-wide (`/usr`), including the udev rule, systemd user
unit, and desktop launcher. Enable the service with:

```sh
systemctl --user enable --now deepcool-lt360.service
```

### Any other distro

```sh
git clone https://github.com/hereisren/deepcool-lt360-driver.git
cd deepcool-lt360-driver
./install.sh
```

This creates a venv under `~/.local/share/deepcool-lt360`, installs the
daemon/CLI/GUI into it, installs the udev rule (asks for `sudo` once), copies
the systemd user unit and enables it, and installs a desktop launcher +
icon so **LT360 VISION — For Renmin** shows up in your app menu. Safe to re-run.

## Usage

### GUI

```sh
lt360-gui
```

or launch **LT360 VISION — For Renmin** from your app menu / rofi / wofi. The GUI
shows a live mirror of the pump screen, lets you pick media, adjust
brightness/orientation/mirror/units, configure the telemetry overlay, and
start/stop/restart the daemon service.

### CLI

```sh
lt360ctl media ~/Pictures/loop.gif
lt360ctl brightness 75
lt360ctl mode horizontal
lt360ctl mirror on
lt360ctl celsius on

# telemetry overlay
lt360ctl overlay on --theme codezero --primary cpu_temp --secondary gpu_temp,cpu_load,time
lt360ctl overlay off

lt360ctl preview ./snapshot.jpg   # save the exact frame currently on the panel
lt360ctl status
```

Overlay themes: `boundary` (red), `codezero` (cyan/blue), `pixelworld`
(purple — great for RGB builds), `custom` (your own `customize.json`, below).
Metrics: `cpu_temp`, `gpu_temp`, `cpu_load`, `gpu_load`, `time`, `off`.

## Custom Overlays & Telemetry (`customize.json`)

`~/.config/deepcool-lt360/customize.json` lets you design your own overlay and
add your own telemetry without touching the driver's code. It is created from a
default on first run, and the daemon **hot-reloads it within a second of every
save**. A typo never blanks the screen: the error is logged
(`journalctl --user -u deepcool-lt360 -f`, shown in the GUI, and in
`lt360ctl status` under `custom.error`) and the last valid layout stays up.

```sh
lt360ctl customize --edit                       # print path, switch to the "custom" theme, open $EDITOR
lt360ctl customize --list                       # bundled presets
lt360ctl customize --preset renmin_cyberpunk    # copy a preset in (old file kept as customize.json.bak)
lt360ctl customize --reset                      # back to the default layout
```

In the GUI pick the **CUSTOM HUD** card, then **Open customize.json** or
**Load Preset…**. Presets live in `examples/overlays/` (`renmin_cyberpunk`,
`minimal_pills`, `now_playing_hud`, `full_telemetry_grid`); copy-paste widgets
from them into your own file.

### Layout

```json
{
  "custom_sensors": { "...": "..." },
  "elements": [ { "type": "text", "...": "..." } ],
  "elements_vertical": [ "optional: used instead of elements when the panel is in vertical mode" ]
}
```

The canvas is **854×480** (horizontal) or **480×854** (vertical); `x`/`y` are
top-left pixel coordinates. Colors are `#RRGGBB` or `#RRGGBBAA` (AA = alpha, so
`#000000aa` is translucent black). Elements draw in list order. Keys starting
with `_` (e.g. `"_comment"`) are ignored, as are whole lines starting with `//`.

| type | keys |
|------|------|
| `rect` / `box` | `x y w h`, `fill`, `outline`, `radius`, `width` |
| `text` | `x y`, `text`, `font` (`pixel`, `sans`, `semibold`, `light`, `thin` or a `.ttf` path), `size`, `color`, `align` (`left`/`center`/`right`), `shadow`, `max_width` (truncates with …) |
| `bar` | `x y w h`, `source`, `min`, `max`, `fill`, `bg`, `outline`, `radius` |
| `line` | `x1 y1 x2 y2`, `color`, `width` |

Text variables (`"CPU {cpu_temp}°{temp_unit} | {my_sensor}"`): `cpu_temp`,
`gpu_temp` (in your °C/°F setting), `cpu_load`, `gpu_load`, `cpu_freq` (MHz),
`cpu_freq_ghz`, `gpu_power` (W), `gpu_clock`, `ram_percent`, `ram_used`,
`ram_total` (GB), `time`, `date`, `temp_unit`, plus every custom sensor.
Unavailable readings show `N/A`. A bar's `source` is any of the numeric ones
above or a custom sensor (temperatures are in °C for bars).

### Custom sensors

```json
"custom_sensors": {
  "nvme_temp": { "type": "sysfs", "path": "/sys/class/hwmon/hwmon1/temp1_input",
                 "scale": 0.001, "unit": "°C" },
  "now_playing": { "type": "command",
                   "command": "playerctl metadata --format '{{artist}} - {{title}}'",
                   "interval_sec": 2.0, "timeout_sec": 1.5, "fallback": "Nothing playing" },
  "vram": { "type": "command", "command": "cat /sys/class/drm/card1/device/mem_info_vram_used",
            "scale": 9.5367e-7, "decimals": 1, "unit": "MB", "interval_sec": 2 }
}
```

- `sysfs` reads the first number in any `/sys` or `/proc` file, multiplied by `scale` (default 1).
- `command` runs through the shell every `interval_sec` (minimum 0.5) in its own
  thread, with a `timeout_sec` (max 10). The **first line** of stdout is the value;
  a non-zero exit, timeout or empty output shows `fallback` (default empty).
  Commands only run while the custom theme is on screen.
- With `scale` the output is treated as a number (`decimals` sets the precision);
  otherwise it is shown as text. `unit` is appended (`"GB"` gets a space, `"°C"` doesn't).
- Use it as `{name}` (with unit) or `{name_value}` (without), and as a bar `source`.
- Names must be identifiers and can't reuse a built-in name.

The commands run as you, from a file only you can edit; only paste layouts you trust.

## Protocol overview

The panel is driven over two bulk endpoints on interface 0:

- `EP 0x04` (OUT) — short (48-byte) command packets: start streaming, and a
  settings packet (orientation, brightness, °C/°F).
- `EP 0x02` (OUT) — image data: each JPEG frame is chunked into 512-byte
  packets (`Start` header with length/checksum, `trans####` chunks,
  `DCLdfinish` trailer).

Frames are always encoded at the panel's native 480×854 portrait resolution;
horizontal/vertical "mode" and the mirror flag just control how far the
source image is rotated before encoding (`lt360_common.rotation_deg`). See
`PROTOCOL.md` for the full reverse-engineering log.

## Uninstall

```sh
systemctl --user disable --now deepcool-lt360.service
rm -rf ~/.local/share/deepcool-lt360 ~/.config/deepcool-lt360
rm -f ~/.local/bin/lt360d ~/.local/bin/lt360ctl ~/.local/bin/lt360-gui
rm -f ~/.local/share/applications/deepcool-lt360.desktop
rm -f ~/.local/share/icons/hicolor/scalable/apps/deepcool-lt360.svg
sudo rm -f /etc/udev/rules.d/99-deepcool-lt360.rules
```
