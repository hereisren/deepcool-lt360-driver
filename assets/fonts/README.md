# Bundled fonts — provenance and licensing

The fonts in this directory (`JZFSSans-*`, `*-numsymbol*`, `Pixel-numsymbol VF`, `dc-font`,
`SourceHanSansCN-Medium`) were extracted from the official DeepCool desktop application
so the overlay matches the look of the vendor software. They are **not** covered by this
repository's MIT license and remain the property of their respective owners/foundries.

- `SourceHanSansCN` is Adobe/Google's Source Han Sans (SIL OFL 1.1).
- The remaining files are distributed with DeepCool's software; their redistribution terms
  are not stated by the vendor. They are included here for interoperability only.

If you are a rights holder and want a file removed, open an issue and it will be dropped.
If the files are removed, the overlay falls back to a system sans font (DejaVu / Liberation /
Noto) or Pillow's built-in font. Custom overlays (`customize.json`) can use any `.ttf` via the `font` field.
