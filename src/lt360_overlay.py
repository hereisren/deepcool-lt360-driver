"""Sensor telemetry overlay compositor.

Draws a themed readout of CPU/GPU/time metrics on top of a base frame
(static image or one GIF frame) using the DeepCool fonts bundled in
assets/fonts/. Compositing happens on the pre-rotation canvas-sized image
(the same orientation the panel is viewed in), before lt360_common's
rotate+JPEG-encode step.
"""
import logging
import os
import sys

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("lt360d.overlay")


def _find_font_dir() -> str:
    """Fonts live in assets/fonts/ next to the repo checkout in dev, or under
    the venv's share/ prefix when installed via pip (see pyproject.toml
    data-files). Try both.
    """
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "fonts"),
        os.path.join(sys.prefix, "share", "deepcool-lt360", "assets", "fonts"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return candidates[0]


ASSETS_FONT_DIR = _find_font_dir()

_FONT_FILES = {
    "regular": "JZFSSans-Regular-ad9b52af.otf",
    "semibold": "JZFSSans-SemiBold-c3a8a050.otf",
    "light": "JZFSSans-Light-ea2a1e77.otf",
    "thin": "JZFSSans-Thin-aef36306.otf",
    "pixel": "Pixel-numsymbol VF-66a3e782.ttf",
}

THEMES = {
    "boundary": {
        "label": "Boundary",
        "accent": (255, 45, 60),
        "text": (240, 240, 240),
        "bar": (10, 10, 12, 165),
    },
    "codezero": {
        "label": "CodeZero",
        "accent": (0, 200, 255),
        "text": (225, 245, 255),
        "bar": (6, 14, 20, 165),
    },
    "pixelworld": {
        "label": "PixelWorld",
        "accent": (170, 70, 255),
        "text": (235, 225, 255),
        "bar": (12, 6, 20, 165),
    },
    "custom": {
        "label": "Custom (customize.json)",
        "accent": (168, 85, 247),
        "text": (255, 255, 255),
        "bar": (12, 6, 20, 165),
    },
}

METRIC_LABELS = {
    "cpu_temp": "CPU",
    "gpu_temp": "GPU",
    "cpu_load": "CPU LOAD",
    "gpu_load": "GPU LOAD",
    "time": "TIME",
    "off": "",
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _font_cache:
        path = os.path.join(ASSETS_FONT_DIR, _FONT_FILES[name])
        try:
            _font_cache[key] = ImageFont.truetype(path, size)
        except OSError:  # bundled vendor font removed: fall back to a system sans / Pillow's built-in
            for fallback in ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf"):
                try:
                    _font_cache[key] = ImageFont.truetype(fallback, size)
                    break
                except OSError:
                    continue
            else:
                _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]


def format_metric(key: str, data: dict, celsius: bool) -> str:
    if key == "off" or key is None:
        return ""
    if key == "time":
        return data.get("time", "--:--:--")
    value = data.get(key)
    if value is None:
        return "N/A"
    if key in ("cpu_temp", "gpu_temp"):
        if not celsius:
            value = value * 9 / 5 + 32
        return f"{value:.0f}°{'C' if celsius else 'F'}"
    if key in ("cpu_load", "gpu_load"):
        return f"{value:.0f}%"
    return str(value)


def default_overlay_config() -> dict:
    return {
        "enabled": False,
        "theme": "codezero",
        "text_color": None,  # None = use theme default; else [r, g, b]
        "primary": "cpu_temp",
        "secondary": ["gpu_temp", "cpu_load", "time"],
    }


class OverlayRenderer:
    """Builds the (expensive, text-heavy) overlay as its own RGBA layer, cached
    once per sensor update, so the per-frame hot path is a cheap alpha
    composite instead of re-rendering fonts on every GIF frame.
    """

    def render_layer(self, size: tuple[int, int], sensor_data: dict, config: dict, celsius: bool,
                     custom=None) -> Image.Image:
        if config.get("theme") == "custom":
            import lt360_custom  # lazy: it imports this module for the font paths
            elements = custom.elements_for(size) if custom is not None else []
            return lt360_custom.render(size, elements, sensor_data, celsius,
                                       custom.version if custom is not None else 0)
        theme = THEMES.get(config.get("theme", "codezero"), THEMES["codezero"])
        text_color = tuple(config["text_color"]) if config.get("text_color") else theme["text"]
        accent = theme["accent"]

        w, h = size
        overlay = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        bar_h = max(64, h // 6)
        draw.rectangle([0, h - bar_h, w, h], fill=theme["bar"])
        draw.rectangle([0, h - bar_h, w, h - bar_h + 3], fill=accent + (255,))

        primary_key = config.get("primary", "cpu_temp")
        primary_text = format_metric(primary_key, sensor_data, celsius)
        primary_label = METRIC_LABELS.get(primary_key, primary_key.upper())

        big_size = int(bar_h * 0.62)
        label_size = max(12, int(bar_h * 0.16))
        big_font = _font("semibold", big_size)
        label_font = _font("light", label_size)

        pad = int(bar_h * 0.18)
        draw.text((pad, h - bar_h + pad), primary_text, font=big_font, fill=text_color)
        draw.text((pad, h - int(label_size * 1.4)), primary_label, font=label_font, fill=accent)

        secondary = [m for m in config.get("secondary", []) if m and m != "off"][:3]
        if secondary:
            sec_size = int(bar_h * 0.24)
            sec_label_size = max(10, int(bar_h * 0.12))
            sec_font = _font("regular", sec_size)
            sec_label_font = _font("light", sec_label_size)
            slot_w = w // (len(secondary) + 1)
            x = w - pad
            for key in reversed(secondary):
                text = format_metric(key, sensor_data, celsius)
                label = METRIC_LABELS.get(key, key.upper())
                tw = draw.textlength(text, font=sec_font)
                lw = draw.textlength(label, font=sec_label_font)
                draw.text((x - tw, h - bar_h + pad * 0.6), text, font=sec_font, fill=text_color)
                draw.text((x - lw, h - int(sec_label_size * 1.3)), label, font=sec_label_font, fill=accent)
                x -= slot_w

        return overlay

    @staticmethod
    def composite(base: Image.Image, layer: Image.Image) -> Image.Image:
        return Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
