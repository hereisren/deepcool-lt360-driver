# Changelog

## v1.0.2 - 2026-10-04

### Fixed
- Casting failed with "failed to create display: No such file or directory" when the daemon had started at login
  before the compositor exported `WAYLAND_DISPLAY` to the systemd user session. The daemon now finds the Wayland
  socket in `$XDG_RUNTIME_DIR` itself when launching the capture.

## v1.0.1 - 2026-10-04

### Fixed
- Launching the app again (from the launcher, or `lt360-gui --tray` at login) started a second instance and added a
  second tray icon. There is now one instance per user: a second launch raises the running window and exits
  (`--tray`/`--minimized` launches leave it as it is). A stale lock from a crash is cleaned up automatically.

### Changed
- README screenshot updated to the v1.0 GUI.

## v1.0.0 - 2026-10-04

### Added
- **Second monitor (live screen cast).** `lt360ctl cast start|stop|status|run|ws|mouse` and the GUI **Cast** page
  turn the pump into a live second monitor. The daemon reads raw `wf-recorder` frames and always sends the newest
  one, for about 50-70 ms latency at up to 30 fps. Nothing is persisted; a daemon restart returns to your media.
- Hyprland virtual output (854x480, optional 1-3x zoom) with a workspace picker that locks the panel to an empty
  workspace, workspace rules that keep your own workspaces on your real monitor, and a live "mouse can enter it"
  switch that hands the cursor back to a real screen when turned off.
- `--output NAME` mirrors an existing output (any wlroots compositor).
- New modules `lt360_cast.py` (compositor side) and `lt360_fx.py` (animation toolkit).
- `CHANGELOG.md`.

- Since v0.3.0: GPU telemetry picks the discrete card on iGPU + dGPU systems, a package-ready systemd unit, data
  discovery for every install layout, a cross-distro `install.sh`, and wider CPU/GPU sensor coverage.

### Changed
- **GUI redesign.** Four pages (Display, HUD, Cast, System; `Ctrl+1..4`), a flat graphite theme with a single blue
  accent, and animation only where it carries meaning. Idle CPU dropped from about 7% to about 2% (0.4% with the
  preview off).
- Stopping or switching a cast takes about 0.15 s (was about 1 s plus a media re-bake of up to several seconds).
  Starting takes about 0.6 s (was 1.65 s). Fixed sleeps were replaced with polling of the compositor state.
- Changing the cast source or zoom while casting restarts it with the new settings.
- `wf-recorder` is an optional dependency of the package.

### Fixed
- GUI crash when hovering a workspace tile (a `TypeError` in an event handler aborted the whole process). A crash
  guard now logs such errors to `~/.local/state/lt360/gui-errors.log`.
- Crash on close from a background job emitting to a deleted signal object.
- The mouse-reach toggle only applied at cast start; it is now live and follows `lt360ctl cast mouse`.
- Daemon leaked a zombie `wf-recorder` and two file descriptors per cast.
- A duplicate `showEvent` in the GUI silently disabled the start-up fade.
- **CPU temperature showing about 17 C on Ryzen.** Without `k10temp`/`zenpower` the daemon fell back to the
  board-level `acpitz` zone. On AMD that zone is no longer used (the readout is `N/A`), a warning explains how to
  load `k10temp`, and `install.sh` flags a `k10temp` blacklist such as the one `zenpower3-dkms` installs.

## v0.3.0

HUD editor, `ring`/`sparkline`/`image` widgets, media framing and speed, hardware deck, system tray and Waybar
module.

## v0.2.0

Earlier release; see the git history.
