# L136 Canvas Drawing — Sensor Theme Extraction

Extracted from `/tmp/deepcreative/re/dyn/main.annotated.txt` (annotated V8 Ignition
bytecode disassembly of `index.jsc`/`L136.node`'s bundled renderer/main code).
`/tmp` is cleared on reboot, so this file — plus the RE scripts in `ref/re_scripts/`
and the fonts in `assets/fonts/` — is what survives from that source.

## What was confirmed

**Theme enum.** `Boundary` / `CodeZero` / `PixelWorld` are just the three values of
a small ID→name lookup table, not drawing functions themselves:

```
==== FUNC <anon> @src657547
  0 -> "Boundary"
  1 -> "CodeZero"
  2 -> "PixelWorld"
```
(main.annotated.txt:114207-114235, and repeated at 132473, 146602, 152679+ as the
same object gets rebuilt across closures). A `"BoundaryBlack"` variant also appears
once (line 11205) as a `GetNamedProperty` lookup, suggesting at least a 4th
palette/skin variant of the `Boundary` theme, but no defining site for it was found.

A parallel enum does the same for display orientation: `0 -> "Horizonal"` (sic —
typo preserved in the original), `1 -> "Vertical"` (main.annotated.txt:114190-114206).

**Per-device canvas bootstrap** (`initcanvas`, 4 near-identical closures at
src775076, src886450, src993570, src1084094 — one per supported DeepCool display
model): each creates a `node-canvas-skia` 2D context (`{"antialias": true, "depth":
false}`) sized to that device's panel — `{"monitorWidth": 854, "monitorHeight":
480}` for three of the four (LT360 VISION-class panels), `{"monitorWidth": 1024,
"monitorHeight": 600}` for the fourth (a larger panel, likely a different SKU). This
independently confirms the 854×480 native canvas size already used in
`test_probe.py` and PROTOCOL.md §23/§24.

**Fonts loaded per canvas**, by CSS/canvas family name (confirming the files
archived in `assets/fonts/` are the right ones and how the app refers to them):
- `"JZFSSans"` — body/label text (family name only; weight selection, e.g.
  Regular/SemiBold/Light/Thin, happens via CSS `@font-face` `font-weight`, not
  visible at the canvas API level).
- `"Pixelnumsymbol"` — numeric readouts (temperature, %, RPM), backed by
  `Pixel-numsymbol VF-*.ttf`, a variable font.
- `"AssassinA"` / `"AssassinB"` — appear only in the src886450 `initcanvas` (the
  device with two extra sub-canvases for horizontal+vertical previews), likely an
  alternate numeral skin tied to a different product line (`Assassin-numsymbol-*`
  fonts, also archived).

**Sub-canvas layout objects** in src886450's `initcanvas` show the app maintains
separate 854×480 (`"horizontal"`) and 480×854 (`"vertical"`) offscreen canvases
simultaneously: `{"width": 854, "height": 480, "orientation": "horizontal"}` and
`{"width": 480, "height": 854, "orientation": "vertical"}` (main.annotated.txt
lines ~692 and ~866 relative to that FUNC), each starting as `[0,0,0,0]` (likely a
clear-rect/transform array) before per-theme drawing populates them.

## What could not be recovered

No coordinate, color (hex/rgba), or font-size literals tied specifically to
`Boundary`, `CodeZero`, or `PixelWorld` were located. Two things ruled out as false
leads:
- `processMultiMedia` (the function initially flagged because it references all
  three theme names) is a **message dispatcher/parser** for incoming
  `{element, orientationType, dataType, data}` IPC payloads, not a drawer — it
  routes to per-theme handlers elsewhere but doesn't itself call `fillRect`/
  `fillText`/`drawImage`.
- There is no JSON/config asset or image file anywhere under `/tmp/deepcreative`
  named after the three themes — layout is computed entirely in the closures the
  theme enum feeds into, which weren't identified in this pass. (One legible,
  unrelated example of the app's literal-style — an audio-spectrum visualizer
  config — was found at main.annotated.txt:6524, showing the app *does* sometimes
  keep readable object-literal configs with margins/colors/sizes inline; the
  per-theme sensor layouts likely look similar but their defining closures weren't
  pinned down here.)

Ignition bytecode is register-based and the theme-specific draw code is reached
through several layers of closures/callbacks bound at runtime, so static grepping
for the theme name strings mostly surfaces *consumers* (property lookups) rather
than the *definitions*. Pinning down exact coordinates/colors would need either:
- tracing forward from the `processMultiMedia`/enum call sites to find what
  closure actually receives the routed `element`/`dataType` payload and calls
  canvas draw methods, or
- a runtime capture (e.g. hooking `CanvasRenderingContext2D.prototype.fillText`/
  `fillRect`/`drawImage` while the app is running with each theme selected) rather
  than further static bytecode reading.

## Practical takeaway for the Linux driver

We don't need the official pixel-perfect theme layouts to have a working driver —
`test_probe.py`'s test card already proves the transport end-to-end. For Phase 5's
real themes, the confirmed facts above (854×480 canvas, `JZFSSans` for labels,
`Pixelnumsymbol` for numeric readouts) are enough to build visually-similar custom
themes without needing to replicate DeepCool's exact layout constants.
