"""Custom-painted widgets and the dark minimal theme for the LT360 VISION — For Renmin GUI.

Everything here is pure presentation: no sockets, no subprocesses. Widgets paint
themselves (QPainter) instead of leaning on stock Qt controls.
"""
import math
import os

from PyQt6.QtCore import QEasingCurve, QEvent, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient, QMovie, QPainter, QPainterPath, QPen, QPixmap,
)
from PyQt6.QtWidgets import QPushButton, QSizePolicy, QSlider, QToolTip, QWidget

import lt360_fx as fx
from lt360_fx import Tween, breathe

# ---------- palette ----------
# Neutral graphite surfaces, one calm blue accent. Status colours are only used for status.
BG = "#0d0f13"
CARD = "#14171d"
CARD_HI = "#1a1e26"
FIELD = "#0f1217"          # inputs, tracks, empty wells
BORDER = "#262b35"
ACCENT = "#4f8cff"
ACCENT_HI = "#79a6ff"
TEXT = "#e6e9ef"
MUTED = "#8a93a3"
DIM = "#566071"
OK = "#34c98a"
BAD = "#ef6070"
YELLOW = "#e2b04a"
LIVE = BAD

SANS = "JZFS Sans"
PIXEL = "Pixel Numsymbol"


def qc(hex_or_color, alpha: int | None = None) -> QColor:
    c = QColor(hex_or_color)
    if alpha is not None:
        c.setAlpha(max(0, min(255, int(alpha))))
    return c


def font(size: float, weight=QFont.Weight.Normal, family: str | None = None, spacing: float = 0.0) -> QFont:
    f = QFont(family or SANS)
    f.setPointSizeF(size)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def fit_font(text: str, width: float, size: float, weight=QFont.Weight.Normal, family: str | None = None,
             spacing: float = 0.0, min_size: float = 4.5) -> QFont:
    """Largest font <= `size` pt whose rendering of `text` fits in `width` px."""
    f = font(size, weight, family, spacing)
    while size > min_size and QFontMetrics(f).horizontalAdvance(text) > width:
        size -= 0.5
        f = font(size, weight, family, spacing)
    return f


def cover_rect(src: QSize, dst: QRectF) -> QRectF:
    """Source sub-rect that fills `dst` with the same aspect (center crop)."""
    if src.isEmpty() or dst.isEmpty():
        return QRectF(0, 0, src.width(), src.height())
    s = max(dst.width() / src.width(), dst.height() / src.height())
    w, h = dst.width() / s, dst.height() / s
    return QRectF((src.width() - w) / 2, (src.height() - h) / 2, w, h)


# ---------- pump-block preview stage ----------

class PumpStage(fx.IdleWidget):
    """The live panel image framed in a DeepCool-LT360-style pump head bezel.

    In edit mode it also draws the custom HUD's element boxes (canvas coordinates, supplied by the
    GUI) over the live frame and turns mouse/keyboard input into select / drag / nudge signals.
    """
    element_pressed = pyqtSignal(int)            # index of the element under the cursor, -1 for none
    element_dragged = pyqtSignal(float, float)   # total canvas-pixel delta since the press
    drag_finished = pyqtSignal()
    nudged = pyqtSignal(int, int)                # arrow keys: canvas-pixel step

    def __init__(self):
        super().__init__()
        self.image: QImage | None = None
        self.vertical = False
        self.drop_active = False
        self.busy = False
        self.edit_mode = False
        self.edit_boxes: list[tuple[int, QRectF, str]] = []   # (element index, canvas rect, tag)
        self.edit_selected = -1
        self._edit_hover = -1
        self._press: QPointF | None = None
        self._corner: QWidget | None = None
        self._bezel_cache: tuple | None = None
        self._t = 0.0
        self.online = True            # daemon + USB up: green status LED; offline: dim red
        self.live = False             # casting: red LED, LIVE badge
        self.live_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(400, 240)

    def idle_tick(self, t: float):
        self._t = t
        if self.busy:
            self.update()                      # spinner
        elif self.live:
            for r in self._live_rects():       # only the pulsing dots, not the whole image
                self.update(r)

    def _live_rects(self) -> list:
        bezel, screen = self._layout()
        return [QRect(int(screen.left()) + 8, int(screen.top()) + 8, 260, 28),
                QRect(int(bezel.left()) + 26, int(bezel.bottom()) - 24, 20, 20)]

    def set_live(self, live: bool, text: str = ""):
        if live != self.live or text != self.live_text:
            self.live, self.live_text = live, text
            self.update()

    def set_online(self, online: bool):
        if online != self.online:
            self.online = online
            self.update()

    def set_corner_widget(self, w: QWidget):
        """Float a small control (the Edit HUD toggle) in the stage's top-right corner."""
        self._corner = w
        w.setParent(self)
        self._place_corner()

    def _place_corner(self):
        if self._corner is not None:
            hint = self._corner.sizeHint()
            self._corner.setGeometry(self.width() - hint.width() - 2, 0, hint.width(), hint.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_corner()

    # ---- edit mode ----
    def set_edit_mode(self, on: bool):
        if on != self.edit_mode:
            self.edit_mode = on
            self._press, self._edit_hover = None, -1
            self.setMouseTracking(on)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if on else Qt.FocusPolicy.NoFocus)
            self.unsetCursor()
            self.update()

    def set_edit_boxes(self, boxes: list[tuple[int, QRectF, str]], selected: int):
        self.edit_boxes, self.edit_selected = boxes, selected
        self.update()

    def canvas_size(self) -> tuple[int, int]:
        return (480, 854) if self.vertical else (854, 480)

    def _scale(self) -> float:
        return self._layout()[1].width() / self.canvas_size()[0]

    def _to_canvas(self, pos: QPointF) -> QPointF:
        screen = self._layout()[1]
        s = self._scale()
        return QPointF((pos.x() - screen.left()) / s, (pos.y() - screen.top()) / s)

    def _to_screen(self, r: QRectF) -> QRectF:
        screen = self._layout()[1]
        s = self._scale()
        return QRectF(screen.left() + r.left() * s, screen.top() + r.top() * s, r.width() * s, r.height() * s)

    def element_at(self, pos: QPointF) -> int:
        """Smallest box under the cursor wins, so a label can be grabbed off the panel it sits on."""
        c = self._to_canvas(pos)
        slack = 4 / max(self._scale(), 1e-6)   # a few screen pixels of grace for thin lines
        hits = [(r.width() * r.height(), i) for i, r, _ in self.edit_boxes if r.adjusted(-slack, -slack, slack, slack).contains(c)]
        return min(hits)[1] if hits else -1

    def mousePressEvent(self, e):
        if not self.edit_mode or e.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(e)
        self.setFocus()
        idx = self.element_at(e.position())
        self.element_pressed.emit(idx)
        self._press = e.position() if idx >= 0 else None
        if idx >= 0:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if not self.edit_mode:
            return super().mouseMoveEvent(e)
        if self._press is not None:
            s = self._scale()
            d = e.position() - self._press
            self.element_dragged.emit(d.x() / s, d.y() / s)
            return
        h = self.element_at(e.position())
        if h != self._edit_hover:
            self._edit_hover = h
            self.setCursor(Qt.CursorShape.OpenHandCursor if h >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, e):
        if self.edit_mode and self._press is not None:
            self._press = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.drag_finished.emit()
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        steps = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if self.edit_mode and self.edit_selected >= 0 and e.key() in steps:
            k = 10 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            dx, dy = steps[e.key()]
            self.nudged.emit(dx * k, dy * k)
            return
        super().keyPressEvent(e)

    def _paint_edit(self, p: QPainter, screen: QRectF):
        p.fillRect(screen, QColor(8, 10, 14, 90))
        for i, r, tag in self.edit_boxes:
            if i == self.edit_selected:
                continue
            sr = self._to_screen(r)
            hover = i == self._edit_hover
            p.setBrush(qc(ACCENT, 40) if hover else Qt.BrushStyle.NoBrush)
            p.setPen(QPen(qc(ACCENT_HI, 230 if hover else 120), 1.1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(sr, 3, 3)
        sel = next(((r, tag) for i, r, tag in self.edit_boxes if i == self.edit_selected), None)
        if sel is not None:
            sr = self._to_screen(sel[0]).adjusted(-2, -2, 2, 2)
            p.setPen(QPen(QColor(ACCENT_HI), 1.6))
            p.setBrush(qc(ACCENT, 34))
            p.drawRoundedRect(sr, 4, 4)
            p.setBrush(QColor("white"))
            p.setPen(QPen(QColor(ACCENT), 1.2))
            for c in (sr.topLeft(), sr.topRight(), sr.bottomLeft(), sr.bottomRight()):
                p.drawRect(QRectF(c.x() - 3, c.y() - 3, 6, 6))
            p.setFont(font(7, QFont.Weight.Bold, spacing=1))
            tw = QFontMetrics(p.font()).horizontalAdvance(sel[1]) + 12
            ty = sr.top() - 17 if sr.top() - 17 > screen.top() else sr.bottom() + 3
            tag_r = QRectF(max(screen.left(), min(sr.left(), screen.right() - tw)), ty, tw, 15)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(tag_r, 4, 4)
            p.setPen(QColor("white"))
            p.drawText(tag_r, Qt.AlignmentFlag.AlignCenter, sel[1])
        # mode banner
        p.setFont(font(7.5, QFont.Weight.Bold, spacing=2))
        fm = QFontMetrics(p.font())
        txt = next((t for t in ("EDIT HUD  ·  CLICK TO SELECT  ·  DRAG / ARROW KEYS TO MOVE",
                                "EDIT HUD  ·  CLICK  ·  DRAG TO MOVE") if fm.horizontalAdvance(t) + 22 <= screen.width() - 12),
                   "EDIT HUD")
        tw = fm.horizontalAdvance(txt) + 22
        banner = QRectF(screen.center().x() - tw / 2, screen.top() + 8, tw, 20)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(8, 10, 14, 215))
        p.drawRoundedRect(banner, 6, 6)
        p.setPen(qc(ACCENT_HI))
        p.drawText(banner, Qt.AlignmentFlag.AlignCenter, txt)

    def set_image(self, img: QImage):
        self.image = img
        self.update()

    def set_vertical(self, vertical: bool):
        if vertical != self.vertical:
            self.vertical = vertical
            self.update()

    def set_drop_active(self, on: bool):
        if on != self.drop_active:
            self.drop_active = on
            self.update()

    def set_busy(self, on: bool):
        if on != self.busy:
            self.busy = on
            self.update()

    # geometry: (bezel_rect, screen_rect)
    def _layout(self):
        aspect = 480 / 854 if self.vertical else 854 / 480
        pad = 34
        bez_l = bez_r = bez_t = 14.0
        bez_b = 30.0
        avail_w = self.width() - 2 * pad
        avail_h = self.height() - 2 * pad
        # solve for screen size s.t. screen + bezel fits
        sw = min(avail_w - bez_l - bez_r, (avail_h - bez_t - bez_b) * aspect)
        sh = sw / aspect
        bw, bh = sw + bez_l + bez_r, sh + bez_t + bez_b
        bx, by = (self.width() - bw) / 2, (self.height() - bh) / 2
        return QRectF(bx, by, bw, bh), QRectF(bx + bez_l, by + bez_t, sw, sh)

    def _render_bezel(self, bezel: QRectF, screen: QRectF) -> QPixmap:
        """The pump head: a soft shadow, a graphite body with a hairline rim, the black screen well and a brand
        strip. Rendered once per size."""
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = 22
        p.setPen(Qt.PenStyle.NoPen)
        for i, a in enumerate((10, 14, 18, 22)):          # shadow: stacked translucent rects, growing outward
            grow = (4 - i) * 5
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(bezel.adjusted(-grow, -grow + 8, grow, grow + 8), radius + grow, radius + grow)
        body = QLinearGradient(bezel.topLeft(), bezel.bottomLeft())
        body.setColorAt(0, QColor("#222731"))
        body.setColorAt(1, QColor("#161a21"))
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QBrush(body))
        p.drawRoundedRect(bezel, radius, radius)
        p.setPen(QPen(QColor(255, 255, 255, 14), 1))     # top highlight
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(bezel.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#000000"))
        p.drawRoundedRect(screen.adjusted(-2, -2, 2, 2), 12, 12)
        p.setFont(font(7, QFont.Weight.DemiBold, spacing=3.0))
        p.setPen(qc(DIM))
        p.drawText(QRectF(bezel.left(), bezel.bottom() - 27, bezel.width(), 24),
                   Qt.AlignmentFlag.AlignCenter, "LT360 VISION")
        p.end()
        return pm

    def paintEvent(self, _):
        bezel, screen = self._layout()
        key = (self.size(), self.vertical, self.devicePixelRatioF())
        if self._bezel_cache is None or self._bezel_cache[0] != key:
            self._bezel_cache = (key, self._render_bezel(bezel, screen))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        t = self._t
        p.drawPixmap(0, 0, self._bezel_cache[1])

        clip = QPainterPath()
        clip.addRoundedRect(screen, 10, 10)
        p.save()
        p.setClipPath(clip)
        if self.image is not None and not self.image.isNull():
            p.drawImage(screen, self.image, cover_rect(self.image.size(), screen))
        else:
            p.fillRect(screen, QColor("#07080b"))
            p.setPen(qc(MUTED))
            p.setFont(font(11, QFont.Weight.DemiBold, spacing=4))
            p.drawText(QRectF(screen.left(), screen.center().y() - 26, screen.width(), 24),
                       Qt.AlignmentFlag.AlignCenter, "NO SIGNAL")
            p.setPen(qc(DIM))
            p.setFont(font(9, spacing=0.6))
            p.drawText(QRectF(screen.left(), screen.center().y() + 4, screen.width(), 24),
                       Qt.AlignmentFlag.AlignCenter, "drop a GIF, MP4 or image here")
        if self.edit_mode:
            self._paint_edit(p, screen)
        if self.live and not self.busy:
            p.setFont(font(7.5, QFont.Weight.Bold, spacing=1.8))
            txt = "LIVE" + (f"  ·  {self.live_text}" if self.live_text else "")
            tw = QFontMetrics(p.font()).horizontalAdvance(txt) + 32
            badge = QRectF(screen.left() + 10, screen.top() + 10, tw, 22)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(10, 12, 16, 205))
            p.drawRoundedRect(badge, 6, 6)
            dot = QPointF(badge.left() + 12, badge.center().y())
            p.setBrush(qc(LIVE, 140 + 115 * breathe(t, 1.6)))
            p.drawEllipse(dot, 3.6, 3.6)
            p.setPen(QColor("#f3f5f8"))
            p.drawText(QRectF(badge.left() + 22, badge.top(), tw - 26, badge.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)
        if self.busy:
            p.fillRect(screen, QColor(8, 10, 14, 170))
            cx, cy = screen.center().x(), screen.center().y() - 10
            ring = QRectF(cx - 18, cy - 18, 36, 36)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(BORDER), 2.4))
            p.drawEllipse(ring)
            pen = QPen(QColor(ACCENT_HI), 2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(ring, int(-(t * 320 % 360) * 16), -100 * 16)
            p.setPen(qc(MUTED))
            p.setFont(font(8, QFont.Weight.DemiBold, spacing=4))
            p.drawText(QRectF(screen.left(), cy + 28, screen.width(), 22), Qt.AlignmentFlag.AlignCenter, "LOADING")
        if self.drop_active:
            p.fillRect(screen, qc(ACCENT, 55))
        p.restore()
        # status LED on the bezel strip: green = online, red = casting, dim red = offline
        led = QPointF(bezel.left() + 36, bezel.bottom() - 15)
        if self.live:
            lc = qc(LIVE, 150 + 105 * breathe(t, 1.6))
        else:
            lc = QColor(OK) if self.online else qc(BAD, 120)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(lc)
        p.drawEllipse(led, 3.2, 3.2)
        if self.drop_active:
            p.setPen(QPen(qc(ACCENT_HI), 2.0, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(screen.adjusted(4, 4, -4, -4), 8, 8)
            p.setPen(QColor("white"))
            p.setFont(font(13, QFont.Weight.Bold, spacing=4))
            p.drawText(screen, Qt.AlignmentFlag.AlignCenter, "DROP TO LOAD")


# ---------- inputs ----------


class SegmentedControl(QWidget):
    """Flat segmented control: the selected option is a solid accent block that glides between options."""
    changed = pyqtSignal(object)

    def __init__(self, options: list[tuple[str, object]], parent=None):
        super().__init__(parent)
        self.options = options
        self.index = 0
        self._hover = -1
        self._pos = Tween(self, 0.0)       # animated (fractional) index of the highlight
        self._hov = Tween(self, 0.0)       # hover fade
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(34)

    def sizeHint(self):
        fm = QFontMetrics(font(9, QFont.Weight.DemiBold))
        return QSize(sum(fm.horizontalAdvance(t) + 34 for t, _ in self.options) + 8, 34)

    def value(self):
        return self.options[self.index][1]

    def set_value(self, value):
        for i, (_, v) in enumerate(self.options):
            if v == value and i != self.index:
                self.index = i
                self._pos.to(i, 220)

    def _seg(self, i) -> QRectF:
        w = (self.width() - 6) / len(self.options)
        return QRectF(3 + i * w, 3, w, self.height() - 6)

    def mouseMoveEvent(self, e):
        h = next((i for i in range(len(self.options)) if self._seg(i).contains(e.position())), -1)
        if h != self._hover:
            self._hover = h
            self._hov.set(0.0)
            self._hov.to(1.0, 140)

    def leaveEvent(self, _):
        self._hover = -1
        self.update()

    def mousePressEvent(self, e):
        for i in range(len(self.options)):
            if self._seg(i).contains(e.position()) and i != self.index:
                self.index = i
                self._pos.to(i, 220)
                self.changed.emit(self.value())
                return

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(qc(BORDER), 1))
        p.setBrush(QColor(FIELD))
        p.drawRoundedRect(r, 9, 9)
        pos = self._pos.value
        for i in range(len(self.options)):
            if i == self._hover and i != self.index:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, int(8 + 12 * self._hov.value)))
                p.drawRoundedRect(self._seg(i), 7, 7)
        a = self._seg(0)
        w = a.width()
        pill = QRectF(a.left() + pos * w, a.top(), w, a.height())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(pill, 7, 7)
        p.setFont(font(9, QFont.Weight.DemiBold, spacing=0.4))
        for i, (label, _) in enumerate(self.options):
            near = max(0.0, 1.0 - abs(pos - i))
            p.setPen(_lerp_color(QColor(MUTED), QColor("white"), near))
            p.drawText(self._seg(i), Qt.AlignmentFlag.AlignCenter, label)


class PillToggle(QWidget):
    """Switch + label; the knob slides and the track colour fades."""
    toggled = pyqtSignal(bool)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text = text
        self.on = False
        self._hover = False
        self._t = Tween(self, 0.0)     # 0 = off, 1 = on
        self._h = Tween(self, 0.0)     # hover
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        fm = QFontMetrics(font(9, QFont.Weight.DemiBold, spacing=0.4))
        return QSize(fm.horizontalAdvance(self.text) + 74, 34)

    def set_on(self, on: bool):
        if on != self.on:
            self.on = on
            self._t.to(1.0 if on else 0.0, 180)

    def mousePressEvent(self, _):
        self._flip()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return):
            self._flip()
        else:
            super().keyPressEvent(e)

    def _flip(self):
        self.set_on(not self.on)
        self.toggled.emit(self.on)

    def enterEvent(self, _):
        self._hover = True
        self._h.to(1.0, 140)

    def leaveEvent(self, _):
        self._hover = False
        self._h.to(0.0, 200)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t, h = self._t.value, self._h.value
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(_lerp_color(QColor(BORDER), QColor(MUTED), h * 0.6), 1))
        p.setBrush(QColor(FIELD))
        p.drawRoundedRect(r, 9, 9)
        track = QRectF(11, (self.height() - 16) / 2, 30, 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_lerp_color(QColor("#2b313d"), QColor(ACCENT), t))
        p.drawRoundedRect(track, 8, 8)
        kx = track.left() + 8 + (track.width() - 16) * t
        p.setBrush(_lerp_color(QColor("#aab2c0"), QColor("white"), t))
        p.drawEllipse(QPointF(kx, track.center().y()), 5.6, 5.6)
        p.setPen(_lerp_color(QColor(MUTED), QColor(TEXT), max(t, h * 0.6)))
        p.setFont(font(9, QFont.Weight.DemiBold, spacing=0.4))
        p.drawText(QRectF(50, 0, self.width() - 56, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text)


class NeonSlider(QSlider):
    """Flat slider: thin groove, solid accent fill, round handle that grows slightly on hover/drag.
    `bipolar` fills from the middle of the range (for signed values such as pan offsets).
    """

    def __init__(self, bipolar: bool = False):
        super().__init__(Qt.Orientation.Horizontal)
        self.bipolar = bipolar
        self._hover = False
        self._g = Tween(self, 0.0)   # handle emphasis: hover or drag
        self.setFixedHeight(34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    MARGIN = 12

    def _value_at(self, x: float) -> int:
        span = max(1, self.width() - 2 * self.MARGIN)
        f = min(1.0, max(0.0, (x - self.MARGIN) / span))
        return round(self.minimum() + f * (self.maximum() - self.minimum()))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self._g.to(1.0, 100)
            self.setValue(self._value_at(e.position().x()))
            e.accept()

    def mouseMoveEvent(self, e):
        if self.isSliderDown():
            self.setValue(self._value_at(e.position().x()))

    def mouseReleaseEvent(self, e):
        if self.isSliderDown():
            self.setSliderDown(False)
            self._g.to(0.5 if self._hover else 0.0, 200)

    def enterEvent(self, _):
        self._hover = True
        if not self.isSliderDown():
            self._g.to(0.5, 140)

    def leaveEvent(self, _):
        self._hover = False
        if not self.isSliderDown():
            self._g.to(0.0, 200)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        span = self.width() - 2 * self.MARGIN
        f = (self.value() - self.minimum()) / max(1, self.maximum() - self.minimum())
        cy = self.height() / 2
        groove = QRectF(self.MARGIN, cy - 2, span, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#242a35"))
        p.drawRoundedRect(groove, 2, 2)
        hx = self.MARGIN + span * f
        x0 = self.MARGIN + span / 2 if self.bipolar else self.MARGIN
        fill = QRectF(min(x0, hx), cy - 2, abs(hx - x0), 4)
        if self.bipolar:
            p.setBrush(qc(MUTED, 120))
            p.drawRect(QRectF(x0 - 0.75, cy - 6, 1.5, 12))
        if fill.width() > 1:
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(fill, 2, 2)
        g = self._g.value
        radius = 6.5 + 1.5 * g
        p.setPen(QPen(QColor(CARD), 3))
        p.setBrush(_lerp_color(QColor("#dfe4ee"), QColor("white"), g))
        p.drawEllipse(QPointF(hx, cy), radius, radius)


class Chip(fx.IdleWidget):
    """Small status pill: a coloured dot and text. With pulse=True the dot fades in and out (live states)."""
    idle_div = 2

    def __init__(self, text="", color=MUTED, dot=True, pulse=False):
        super().__init__()
        self._text, self._color, self._dot, self._pulse = text, color, dot, pulse
        self._t = 0.0
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_pulse(self, on: bool):
        if on != self._pulse:
            self._pulse = on
            self.update()

    def idle_tick(self, t: float):
        if self._pulse and self._dot:
            self._t = t
            self.update(0, 0, 28, self.height())   # only the dot

    def set(self, text: str, color: str | None = None):
        if text != self._text or (color and color != self._color):
            self._text = text
            self._color = color or self._color
            self.updateGeometry()
            self.update()

    def sizeHint(self):
        fm = QFontMetrics(font(8, QFont.Weight.DemiBold, spacing=1.0))
        return QSize(fm.horizontalAdvance(self._text) + (36 if self._dot else 22), 26)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QColor(CARD_HI))
        p.drawRoundedRect(r, 7, 7)
        x = 11
        if self._dot:
            b = breathe(self._t, 2.0) if self._pulse else 1.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(self._color, 120 + 135 * b))
            p.drawEllipse(QPointF(13, 13), 3.3, 3.3)
            x = 24
        p.setPen(QColor(TEXT) if self._color != MUTED else QColor(MUTED))
        p.setFont(font(8, QFont.Weight.DemiBold, spacing=1.0))
        p.drawText(QRectF(x, 0, self.width() - x - 8, 26), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self._text)


# ---------- header ----------


class Banner(QWidget):
    """Title header: a plain wordmark, a small outlined tag and the version, over a hairline rule."""

    def __init__(self, cjk_family: str | None, version: str = ""):
        super().__init__()
        self.cjk = cjk_family
        self.version = version
        self.setFixedHeight(68)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        tf = font(21, QFont.Weight.Bold, spacing=2.2)
        p.setFont(tf)
        p.setPen(QColor(TEXT))
        title = "LT360 VISION"
        p.drawText(QPointF(2, 34), title)
        title_w = QFontMetrics(tf).horizontalAdvance(title) + 2

        bf = font(8, QFont.Weight.Bold, spacing=1.8)
        cf = QFont(self.cjk or SANS)
        cf.setPointSizeF(8.5)
        cf.setWeight(QFont.Weight.Bold)
        fm_b, fm_c = QFontMetrics(bf), QFontMetrics(cf)
        t1, t2 = "FOR RENMIN", " 人民"
        bw = fm_b.horizontalAdvance(t1) + fm_c.horizontalAdvance(t2) + 22
        badge = QRectF(title_w + 14, 14, bw, 24)
        p.setPen(QPen(qc(ACCENT_HI, 150), 1))
        p.setBrush(qc(ACCENT, 30))
        p.drawRoundedRect(badge, 6, 6)
        p.setPen(QColor(ACCENT_HI))
        p.setFont(bf)
        x, base = badge.left() + 11, badge.top() + 16.5
        p.drawText(QPointF(x, base), t1)
        p.setFont(cf)
        p.drawText(QPointF(x + fm_b.horizontalAdvance(t1), base), t2)

        if self.version:
            vf = font(8, QFont.Weight.DemiBold, spacing=1.2)
            p.setPen(QColor(DIM))
            p.setFont(vf)
            p.drawText(QPointF(badge.right() + 12, base), f"v{self.version}")

        p.setPen(qc(MUTED))
        p.setFont(font(9, spacing=0.5))
        p.drawText(QPointF(3, 56), "Open-Source Linux Liquid Cooler Suite  ·  By Ren, For The People")
        y = self.height() - 1
        p.setPen(QPen(QColor(BORDER), 1))
        p.drawLine(0, y, w, y)


# ---------- media gallery ----------

def _cover_pixmap(img: QImage, size: QSize) -> QPixmap:
    src = cover_rect(img.size(), QRectF(0, 0, size.width(), size.height()))
    return QPixmap.fromImage(img.copy(src.toRect()).scaled(
        size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))


class MediaCard(QWidget):
    activated = pyqtSignal(str)
    removed = pyqtSignal(str)
    THUMB = QSize(136, 82)

    def __init__(self, path: str, thumb: QImage | None):
        super().__init__()
        self.path = path
        self.thumb = _cover_pixmap(thumb, self.THUMB) if thumb is not None and not thumb.isNull() else None
        self.active = False
        self.exists = os.path.isfile(path)
        self._hover = False
        self._x_hover = False
        self._movie: QMovie | None = None
        self.setFixedSize(148, 122)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(path)

    def set_thumb(self, thumb: QImage):
        if thumb is not None and not thumb.isNull():
            self.thumb = _cover_pixmap(thumb, self.THUMB)
            self.update()

    def set_active(self, on: bool):
        if on != self.active:
            self.active = on
            self.update()

    def _thumb_rect(self):
        return QRectF(6, 6, self.THUMB.width(), self.THUMB.height())

    def _x_rect(self):
        t = self._thumb_rect()
        return QRectF(t.right() - 22, t.top() + 4, 18, 18)

    def enterEvent(self, _):
        self._hover = True
        if self.path.lower().endswith(".gif") and self.exists:
            self._movie = QMovie(self.path)
            self._movie.setCacheMode(QMovie.CacheMode.CacheNone)
            self._movie.frameChanged.connect(self.update)
            self._movie.start()
        self.update()

    def leaveEvent(self, _):
        self._hover = self._x_hover = False
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
        self.update()

    def mouseMoveEvent(self, e):
        h = self._x_rect().contains(e.position())
        if h != self._x_hover:
            self._x_hover = h
            self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._x_rect().contains(e.position()):
            self.removed.emit(self.path)
        else:
            self.activated.emit(self.path)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor(ACCENT) if self.active else qc(ACCENT_HI, 140) if self._hover else qc(BORDER),
                      1.6 if self.active else 1.0))
        p.setBrush(QColor(CARD_HI))
        p.drawRoundedRect(r, 10, 10)

        t = self._thumb_rect()
        clip = QPainterPath()
        clip.addRoundedRect(t, 7, 7)
        p.save()
        p.setClipPath(clip)
        p.fillRect(t, QColor("#050508"))
        if self._movie is not None and self._movie.isValid():
            img = self._movie.currentImage()
            if not img.isNull():
                p.drawPixmap(t.toRect(), _cover_pixmap(img, self.THUMB))
        elif self.thumb is not None:
            p.drawPixmap(t.toRect(), self.thumb)
        else:
            p.setPen(qc(DIM))
            p.setFont(font(8, QFont.Weight.DemiBold, spacing=2))
            p.drawText(t, Qt.AlignmentFlag.AlignCenter, "..." if self.exists else "MISSING")
        p.restore()

        ext = os.path.splitext(self.path)[1].lstrip(".").upper()
        p.setFont(font(6.5, QFont.Weight.Bold, spacing=1))
        tag = QRectF(t.left() + 5, t.bottom() - 17, QFontMetrics(p.font()).horizontalAdvance(ext) + 12, 13)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc("#000000", 170))
        p.drawRoundedRect(tag, 6.5, 6.5)
        p.setPen(QColor("#d6dbe5"))
        p.drawText(tag, Qt.AlignmentFlag.AlignCenter, ext)

        if self._hover:
            x = self._x_rect()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(BAD, 235) if self._x_hover else qc("#000000", 190))
            p.drawEllipse(x)
            p.setPen(QPen(QColor("white"), 1.6, cap=Qt.PenCapStyle.RoundCap))
            c = x.center()
            p.drawLine(QPointF(c.x() - 3.5, c.y() - 3.5), QPointF(c.x() + 3.5, c.y() + 3.5))
            p.drawLine(QPointF(c.x() + 3.5, c.y() - 3.5), QPointF(c.x() - 3.5, c.y() + 3.5))

        p.setPen(QColor(TEXT) if self.active else qc(MUTED))
        p.setFont(font(8.5, QFont.Weight.DemiBold if self.active else QFont.Weight.Normal))
        name = QFontMetrics(p.font()).elidedText(os.path.basename(self.path), Qt.TextElideMode.ElideMiddle, 134)
        p.drawText(QRectF(8, 92, 134, 24), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)


class DropTile(QWidget):
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._hover = False
        self.active = False
        self.setFixedSize(148, 122)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, _):
        self._hover = True
        self.update()

    def leaveEvent(self, _):
        self._hover = False
        self.update()

    def mousePressEvent(self, _):
        self.clicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(qc(ACCENT_HI, 200) if self._hover else QColor("#363d4b"), 1.2, Qt.PenStyle.DashLine))
        p.setBrush(qc(ACCENT, 20) if self._hover else QColor(FIELD))
        p.drawRoundedRect(r, 10, 10)
        c = r.center()
        p.setPen(QPen(QColor(ACCENT_HI) if self._hover else QColor(MUTED), 2.0, cap=Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(c.x() - 9, c.y() - 12), QPointF(c.x() + 9, c.y() - 12))
        p.drawLine(QPointF(c.x(), c.y() - 21), QPointF(c.x(), c.y() - 3))
        p.setPen(qc(MUTED))
        p.setFont(font(8.5, QFont.Weight.DemiBold, spacing=0.8))
        p.drawText(QRectF(r.left(), c.y() + 6, r.width(), 40), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                   "Drop or browse\nGIF · MP4 · WEBM · IMG")


# ---------- overlay theme cards ----------

THEME_STYLE = {
    "boundary": {"title": "BOUNDARY", "accent": "#ef4444", "accent2": "#ef4444", "sub": "crimson / carbon"},
    "codezero": {"title": "CODE ZERO", "accent": "#06b6d4", "accent2": "#22d3ee", "sub": "cyber cyan"},
    "pixelworld": {"title": "PIXEL WORLD", "accent": "#a855f7", "accent2": "#facc15", "sub": "neon violet / yellow"},
    "custom": {"title": "CUSTOM HUD", "accent": "#a855f7", "accent2": "#facc15", "sub": "customize.json • Hot-Reload"},
}
METRIC_SHORT = {"cpu_temp": "CPU", "gpu_temp": "GPU", "cpu_load": "CPU%", "gpu_load": "GPU%", "time": "TIME", "off": ""}


def format_metric(key: str, data: dict, celsius: bool) -> str:
    if key in (None, "off"):
        return ""
    if key == "time":
        return str(data.get("time", "--:--"))[:5]
    v = data.get(key)
    if v is None:
        return "N/A"
    if key.endswith("_temp"):
        return f"{(v if celsius else v * 9 / 5 + 32):.0f}°"
    if key in ("gpu_power", "gpu_wattage"):
        return f"{v:.0f}W"
    return f"{v:.0f}%"


class ThemeCard(QWidget):
    clicked = pyqtSignal(str)

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.style = THEME_STYLE[key]
        self.setToolTip(self.style["sub"])
        self.selected = False
        self._hover = False
        self.primary = "cpu_temp"
        self.secondary = ["gpu_temp", "cpu_load", "time"]
        self.sensors: dict = {}
        self.celsius = True
        self.setMinimumSize(100, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(158)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_state(self, selected: bool, primary: str, secondary: list, sensors: dict, celsius: bool):
        self.selected, self.primary, self.secondary, self.sensors, self.celsius = \
            selected, primary, list(secondary), sensors, celsius
        self.update()

    def enterEvent(self, _):
        self._hover = True
        self.update()

    def leaveEvent(self, _):
        self._hover = False
        self.update()

    def mousePressEvent(self, _):
        self.clicked.emit(self.key)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        acc, acc2 = QColor(self.style["accent"]), QColor(self.style["accent2"])
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setPen(QPen(QColor(ACCENT) if self.selected else qc(ACCENT_HI, 130) if self._hover else qc(BORDER), 2.0 if self.selected else 1))
        p.setBrush(QColor(CARD_HI))
        p.drawRoundedRect(r, 11, 11)

        # mini panel with the overlay bar as the daemon draws it
        scr = QRectF(r.left() + 9, r.top() + 9, r.width() - 18, r.height() - 46)
        clip = QPainterPath()
        clip.addRoundedRect(scr, 7, 7)
        p.save()
        p.setClipPath(clip)
        bgg = QLinearGradient(scr.topLeft(), scr.bottomRight())
        bgg.setColorAt(0, QColor("#1b2029"))
        bgg.setColorAt(1, QColor("#0c0f14"))
        p.fillRect(scr, QBrush(bgg))
        if self.key == "custom":
            self._paint_custom_preview(p, scr, acc, acc2)
            p.restore()
            self._paint_footer(p, r, acc, acc2)
            return
        bar_h = max(30.0, scr.height() * 0.42)
        bar = QRectF(scr.left(), scr.bottom() - bar_h, scr.width(), bar_h)
        p.fillRect(bar, QColor(6, 6, 10, 215))
        p.fillRect(QRectF(bar.left(), bar.top(), bar.width(), 2), acc2)
        pixel = self.key == "pixelworld"
        family = PIXEL if pixel else SANS
        show_primary = self.primary not in (None, "off")
        primary_w = bar.width() * 0.36 if show_primary else 0.0
        if show_primary:
            value = format_metric(self.primary, self.sensors, self.celsius)
            p.setFont(fit_font(value, primary_w - 6, 15 if not pixel else 13, QFont.Weight.Bold, family))
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(bar.left() + 6, bar.top() + 4, primary_w - 6, bar.height() - 14),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, value)
            label = METRIC_SHORT.get(self.primary, "")
            p.setFont(fit_font(label, primary_w - 6, 5.5, QFont.Weight.DemiBold, spacing=1))
            p.setPen(acc2)
            p.drawText(QRectF(bar.left() + 6, bar.bottom() - 13, primary_w - 6, 11), Qt.AlignmentFlag.AlignLeft, label)
        # secondaries share what the primary leaves; every string shrinks to its slot so nothing overlaps
        sec = [m for m in self.secondary if m and m != "off"][:3]
        slot = (bar.width() - primary_w - 8) / max(1, len(sec))
        for i, m in enumerate(sec):
            x = bar.right() - 4 - slot * (len(sec) - i)
            p.save()
            p.setClipRect(QRectF(x + 0.5, bar.top(), slot - 1, bar.height()))   # never bleed into a neighbour
            value = format_metric(m, self.sensors, self.celsius)
            p.setFont(fit_font(value, slot - 3, 6.8 if not pixel else 6, QFont.Weight.DemiBold, family, min_size=4))
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(x, bar.top() + 6, slot, bar.height() - 20),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter, value)
            label = METRIC_SHORT.get(m, "")
            p.setFont(fit_font(label, slot - 3, 5, QFont.Weight.DemiBold, min_size=4))
            p.setPen(acc2)
            p.drawText(QRectF(x, bar.bottom() - 13, slot, 11), Qt.AlignmentFlag.AlignHCenter, label)
            p.restore()
        p.restore()
        self._paint_footer(p, r, acc, acc2)

    @staticmethod
    def _paint_custom_preview(p, scr, acc, acc2):
        """Stylised HUD: a ring gauge, a sparkline, a gold clock chip and a JSON hint."""
        d = min(scr.height() * 0.62, scr.width() * 0.42)
        ring = QRectF(scr.left() + 8, scr.top() + 8, d, d).adjusted(3, 3, -3, -3)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qc("#2b313d"), 4, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(ring, -135 * 16, -270 * 16)
        p.setPen(QPen(acc, 4, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(ring, -135 * 16, -190 * 16)
        p.setPen(QColor(TEXT))
        p.setFont(font(6.5, QFont.Weight.Bold))
        p.drawText(ring, Qt.AlignmentFlag.AlignCenter, "63°")
        spark = QRectF(ring.right() + 8, scr.top() + 10, scr.right() - ring.right() - 16, d * 0.62)
        p.setPen(QPen(qc(acc2, 90), 1))
        p.setBrush(qc(QColor(8, 10, 14), 200))
        p.drawRoundedRect(spark, 4, 4)
        ys = (0.6, 0.45, 0.55, 0.3, 0.4, 0.2, 0.35, 0.25)
        path = QPainterPath()
        for i, f in enumerate(ys):
            pt = QPointF(spark.left() + 3 + i * (spark.width() - 6) / (len(ys) - 1), spark.top() + 3 + f * (spark.height() - 6))
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        p.setPen(QPen(QColor(acc2), 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        chip = QRectF(scr.right() - 50, scr.bottom() - 22, 43, 15)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(acc2, 230))
        p.drawRoundedRect(chip, 5, 5)
        p.setPen(QColor("#10131a"))
        p.setFont(font(6.5, QFont.Weight.Bold, PIXEL))
        p.drawText(chip, Qt.AlignmentFlag.AlignCenter, "12:34")
        p.setPen(qc(acc, 230))
        p.setFont(font(9, QFont.Weight.Bold, PIXEL))
        p.drawText(QRectF(scr.left() + 9, scr.bottom() - 26, scr.width() * 0.5, 22),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "{ }")

    def _paint_footer(self, p, r, acc, acc2):
        p.setPen(QColor(TEXT) if self.selected else qc(MUTED))
        p.setFont(font(7, QFont.Weight.Bold, spacing=0.6))
        p.drawText(QRectF(r.left() + 10, r.bottom() - 32, r.width() - 40, 18), Qt.AlignmentFlag.AlignVCenter, self.style["title"])
        p.setBrush(acc)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(r.right() - 11, r.bottom() - 23), 3, 3)
        if self.style["accent2"] != self.style["accent"]:
            p.setBrush(acc2)
            p.drawEllipse(QPointF(r.right() - 21, r.bottom() - 23), 3, 3)


class ReadoutTile(QWidget):
    """Live telemetry value; the number briefly turns accent-coloured whenever it changes."""

    def __init__(self, label: str):
        super().__init__()
        self.label, self.value = label, "--"
        self._flash = Tween(self, 0.0)
        self.setFixedHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, v: str):
        if v != self.value:
            first = self.value == "--"
            self.value = v
            if not first:
                self._flash.set(1.0)
                self._flash.to(0.0, 700, QEasingCurve.Type.OutQuad)
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = self._flash.value
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(_lerp_color(QColor(BORDER), QColor(ACCENT), f), 1))
        p.setBrush(QColor(FIELD))
        p.drawRoundedRect(r, 9, 9)
        p.setPen(_lerp_color(QColor(TEXT), QColor(ACCENT_HI), f))
        p.setFont(fit_font(self.value, self.width() - 12, 15, QFont.Weight.Bold))
        p.drawText(QRectF(0, 6, self.width(), 26), Qt.AlignmentFlag.AlignCenter, self.value)
        p.setPen(qc(MUTED))
        p.setFont(font(6.5, QFont.Weight.DemiBold, spacing=1.6))
        p.drawText(QRectF(0, 31, self.width(), 16), Qt.AlignmentFlag.AlignCenter, self.label)


# ---------- buttons, cards, navigation ----------

def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


class NeonButton(QPushButton):
    """Button that paints itself: the hover tint fades in, press dips. Variant comes from objectName
    ("primary" = solid accent, "danger" = red outline, anything else = quiet); a button with a menu shows a chevron."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._h = Tween(self, 0.0)
        self._pr = Tween(self, 0.0)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setMinimumHeight(36)

    def sizeHint(self):
        fm = QFontMetrics(font(9.5, QFont.Weight.DemiBold, spacing=0.8))
        return QSize(fm.horizontalAdvance(self.text()) + 40 + (26 if self.menu() is not None else 0), 36)

    def minimumSizeHint(self):
        return self.sizeHint()

    def enterEvent(self, e):
        super().enterEvent(e)
        self._h.to(1.0, 150)

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._h.to(0.0, 240)

    def mousePressEvent(self, e):
        self._pr.to(1.0, 70)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._pr.to(0.0, 220)
        super().mouseReleaseEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h, pr = self._h.value, self._pr.value
        variant = self.objectName()
        if variant == "primary":
            fill = _lerp_color(QColor(ACCENT), QColor(ACCENT_HI), h * 0.7)
            edge, ink = fill, QColor("white")
        elif variant == "danger":
            fill = _lerp_color(QColor(FIELD), QColor("#2a171b"), h)
            edge, ink = _lerp_color(QColor("#5a2a32"), QColor(BAD), h), QColor(BAD)
        else:
            fill = _lerp_color(QColor(CARD_HI), QColor("#232834"), h)
            edge, ink = _lerp_color(QColor(BORDER), QColor("#3b4352"), h), QColor(TEXT)
        if not self.isEnabled():
            p.setOpacity(0.4)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        r.translate(0, 1.0 * pr)
        if self.hasFocus():
            p.setPen(QPen(qc(ACCENT_HI, 160), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(-1.5, -1.5, 1.5, 1.5), 10, 10)
        p.setPen(QPen(edge, 1))
        p.setBrush(_lerp_color(fill, QColor(0, 0, 0), 0.16 * pr))
        p.drawRoundedRect(r, 9, 9)
        p.setPen(ink)
        p.setFont(font(9.5, QFont.Weight.DemiBold, spacing=0.6))
        menu = self.menu() is not None
        tr = r.adjusted(0, 0, -(16 if menu else 0), 0)
        p.drawText(tr, Qt.AlignmentFlag.AlignCenter, self.text())
        if menu:
            cx, cy = r.right() - 18, r.center().y() + 0.5
            pen = QPen(ink, 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPolyline([QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2)])


class Card(QWidget):
    """Flat panel: a hairline border on a slightly lifted surface. The layout inside is filled by the caller."""

    def __init__(self):
        super().__init__()
        self.setObjectName("card")

    def start_sweep(self):     # kept for reveal(); the flat style has nothing to sweep
        pass

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QColor(CARD))
        p.drawRoundedRect(r, 12, 12)


def draw_nav_icon(p: QPainter, key: str, box: QRectF, color: QColor):
    """Small vector icons for the nav rail (stroked, round caps)."""
    p.save()
    pen = QPen(color, 1.9)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    s, cx, cy = box.width(), box.center().x(), box.center().y()
    if key == "display":
        body = QRectF(cx - s * 0.46, cy - s * 0.38, s * 0.92, s * 0.62)
        p.drawRoundedRect(body, 3.5, 3.5)
        p.drawLine(QPointF(cx - s * 0.2, cy + s * 0.44), QPointF(cx + s * 0.2, cy + s * 0.44))
        p.drawLine(QPointF(cx, cy + s * 0.24), QPointF(cx, cy + s * 0.44))
        tri = QPainterPath()
        tri.moveTo(cx - s * 0.1, cy - s * 0.2)
        tri.lineTo(cx + s * 0.16, cy - s * 0.07)
        tri.lineTo(cx - s * 0.1, cy + s * 0.06)
        tri.closeSubpath()
        p.setBrush(color)
        p.drawPath(tri)
    elif key == "hud":
        q = s * 0.4
        gap = s * 0.1
        for i in range(2):
            for j in range(2):
                rr = QRectF(cx - q - gap / 2 + i * (q + gap), cy - q - gap / 2 + j * (q + gap), q, q)
                if (i, j) == (1, 0):
                    p.setBrush(color)
                else:
                    p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rr, 3, 3)
    elif key == "cast":
        body = QRectF(cx - s * 0.46, cy - s * 0.1, s * 0.62, s * 0.5)
        p.drawRoundedRect(body, 3, 3)
        for k, rad in enumerate((0.2, 0.36, 0.52)):
            arc = QRectF(cx - s * 0.05 - s * rad, cy - s * 0.05 - s * rad, s * rad * 2, s * rad * 2)
            p.drawArc(arc, 20 * 16, 50 * 16)
        p.setBrush(color)
        p.drawEllipse(QPointF(cx - s * 0.05, cy - s * 0.05), 1.6, 1.6)
    elif key == "system":
        p.drawEllipse(QPointF(cx, cy), s * 0.2, s * 0.2)
        for k in range(8):
            a = math.radians(k * 45)
            p.drawLine(QPointF(cx + math.cos(a) * s * 0.34, cy + math.sin(a) * s * 0.34),
                       QPointF(cx + math.cos(a) * s * 0.48, cy + math.sin(a) * s * 0.48))
        p.drawEllipse(QPointF(cx, cy), s * 0.36, s * 0.36)
    p.restore()


class NavRail(fx.IdleWidget):
    """Vertical page switcher: icon + label per page, a selection block with an accent bar that glides to the
    chosen page, a hover tint and optional status dots (e.g. LIVE on the cast page)."""
    idle_div = 2
    selected = pyqtSignal(int)
    ITEM_H = 74

    def __init__(self, items: list[tuple[str, str]]):
        super().__init__()
        self.items = items
        self.index = 0
        self._pos = Tween(self, 0.0)
        self._hover = -1
        self._hov = Tween(self, 0.0)
        self.badges: dict[int, tuple[str, str]] = {}   # index -> (text, color); text "" = status dot
        self._t = 0.0
        self.setFixedWidth(92)
        self.setMinimumHeight(len(items) * self.ITEM_H + 20)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_badge(self, i: int, text: str | None, color: str = OK):
        if text is None:
            self.badges.pop(i, None)
        else:
            self.badges[i] = (text, color)
        self.update()

    def set_index(self, i: int, animate: bool = True):
        if i != self.index:
            self.index = i
            self._pos.to(i, 260 if animate else 0, QEasingCurve.Type.OutCubic)

    def idle_tick(self, t: float):
        self._t = t
        for i in self.badges:
            ir = self._item_rect(i)
            self.update(QRect(int(ir.right()) - 24, int(ir.top()), 24, 24))   # just the dot

    def _item_rect(self, i: int) -> QRectF:
        return QRectF(8, 10 + i * self.ITEM_H, self.width() - 16, self.ITEM_H - 4)

    def mouseMoveEvent(self, e):
        h = next((i for i in range(len(self.items)) if self._item_rect(i).contains(e.position())), -1)
        if h != self._hover:
            self._hover = h
            self._hov.set(0.0)
            self._hov.to(1.0, 140)

    def leaveEvent(self, _):
        self._hover = -1
        self.update()

    def mousePressEvent(self, e):
        for i in range(len(self.items)):
            if self._item_rect(i).contains(e.position()) and i != self.index:
                self.set_index(i)
                self.selected.emit(i)
                return

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QColor(CARD))
        p.drawRoundedRect(r, 12, 12)
        pos = self._pos.value
        a = self._item_rect(0)
        hl = QRectF(a.left(), a.top() + pos * self.ITEM_H, a.width(), a.height())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(CARD_HI))
        p.drawRoundedRect(hl, 9, 9)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(QRectF(hl.left(), hl.top() + 14, 3, hl.height() - 28), 1.5, 1.5)
        for i, (label, icon) in enumerate(self.items):
            ir = self._item_rect(i)
            near = max(0.0, 1.0 - abs(pos - i))
            hv = self._hov.value if i == self._hover else 0.0
            if hv > 0 and near < 0.5:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, int(9 * hv)))
                p.drawRoundedRect(ir, 9, 9)
            col = _lerp_color(_lerp_color(QColor(MUTED), QColor(TEXT), hv * 0.7), QColor("white"), near)
            draw_nav_icon(p, icon, QRectF(ir.center().x() - 13, ir.top() + 12, 26, 26), col)
            p.setPen(col)
            p.setFont(font(7, QFont.Weight.Bold, spacing=1.6))
            p.drawText(QRectF(ir.left(), ir.top() + 42, ir.width(), 18), Qt.AlignmentFlag.AlignCenter, label)
            if i in self.badges:
                text, bc = self.badges[i]
                pt = QPointF(ir.right() - 12, ir.top() + 12)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qc(bc, 130 + 125 * breathe(self._t, 1.6)))
                p.drawEllipse(pt, 3.6, 3.6)


class WorkspacePicker(QWidget):
    """Ten workspace tiles; the selected one is the workspace the panel is locked to. Free workspaces can be picked;
    ones that have windows on a real screen, or are showing on one, are shown but blocked (with the reason).
    The solid accent block glides to a newly selected tile."""
    chosen = pyqtSignal(int)
    blocked = pyqtSignal(int, str)
    COLS, ROWS, TILE_H, GAP = 5, 2, 56, 8

    def __init__(self, count: int = 10):
        super().__init__()
        self.count = count
        self.selected = 10
        self.info: dict[int, dict] = {}
        self.real: set[str] = set()
        self.enabled_note = ""            # non-empty: picker is disabled and this explains why
        self._hover = -1
        self._hov = Tween(self, 0.0)
        self._move = Tween(self, 1.0)     # 0..1 travel from _from_ws to selected
        self._from_ws = self.selected
        self.setMouseTracking(True)
        self.setFixedHeight(self.ROWS * self.TILE_H + (self.ROWS - 1) * self.GAP + 8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_info(self, info: dict, real: list[str]):
        self.info, self.real = info, set(real)
        self.update()

    def set_selected(self, ws: int, animate: bool = True):
        if ws != self.selected:
            self._from_ws = self.selected
            self.selected = ws
            self._move.set(0.0)
            self._move.to(1.0, 300 if animate else 0, QEasingCurve.Type.OutCubic)

    def set_enabled_note(self, note: str):
        self.enabled_note = note
        self.setCursor(Qt.CursorShape.ArrowCursor if note else Qt.CursorShape.PointingHandCursor)
        self.update()

    def _tile(self, ws: int) -> QRectF:
        i = ws - 1
        col, row = i % self.COLS, i // self.COLS
        w = (self.width() - 8 - (self.COLS - 1) * self.GAP) / self.COLS
        return QRectF(4 + col * (w + self.GAP), 4 + row * (self.TILE_H + self.GAP), w, self.TILE_H)

    def block_reason(self, ws: int) -> str:
        if ws == self.selected:
            return ""
        w = self.info.get(ws)
        if not w or w.get("monitor") not in self.real:
            return ""
        if w.get("shown"):
            return f"on screen on {w['monitor']} right now"
        if w.get("windows", 0) > 0:
            return f"{w['windows']} window(s) on {w['monitor']}"
        return ""

    def _ws_at(self, pos) -> int:
        pos = QPointF(pos)   # mouse events give QPointF, tooltip (QHelpEvent) events give QPoint
        return next((ws for ws in range(1, self.count + 1) if self._tile(ws).contains(pos)), -1)

    def mouseMoveEvent(self, e):
        h = self._ws_at(e.position())
        if h != self._hover:
            self._hover = h
            self._hov.set(0.0)
            self._hov.to(1.0, 140)
            self.setCursor(Qt.CursorShape.ArrowCursor if (self.enabled_note or h < 0 or self.block_reason(h))
                           else Qt.CursorShape.PointingHandCursor)

    def leaveEvent(self, _):
        self._hover = -1
        self.update()

    def mousePressEvent(self, e):
        ws = self._ws_at(e.position())
        if ws < 0 or self.enabled_note:
            return
        why = self.block_reason(ws)
        if why:
            self.blocked.emit(ws, f"Workspace {ws} is {why}. Pick an empty one.")
        elif ws != self.selected:
            self.chosen.emit(ws)

    def event(self, e):
        if e.type() == QEvent.Type.ToolTip:
            ws = self._ws_at(e.pos())
            if ws > 0:
                why = self.block_reason(ws)
                QToolTip.showText(e.globalPos(), (f"Workspace {ws}: {why}" if why else
                                                  f"Workspace {ws}" + (" (locked to the panel)" if ws == self.selected else "")))
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dim = bool(self.enabled_note)
        if dim:
            p.setOpacity(0.35)
        for ws in range(1, self.count + 1):
            r = self._tile(ws).adjusted(0.5, 0.5, -0.5, -0.5)
            why = self.block_reason(ws)
            info = self.info.get(ws, {})
            hv = self._hov.value if ws == self._hover and not dim else 0.0
            sel = ws == self.selected
            if sel:
                continue        # drawn below as the moving block
            if why:
                ink = QColor(ACCENT_HI) if info.get("shown") else QColor(YELLOW)
                p.setPen(QPen(qc(ink, 60), 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                tag = "ON SCREEN" if info.get("shown") else f"{info.get('windows', 0)} WIN"
                num_col, tag_col = qc(ink, 150), qc(ink, 130)
            else:
                p.setPen(QPen(_lerp_color(QColor(BORDER), QColor("#3d4657"), hv), 1))
                p.setBrush(_lerp_color(QColor(FIELD), QColor("#171c25"), hv))
                tag = "FREE"
                num_col, tag_col = _lerp_color(QColor(MUTED), QColor("white"), hv), qc(DIM)
            p.drawRoundedRect(r, 9, 9)
            p.setFont(font(15, QFont.Weight.Bold))
            p.setPen(num_col)
            p.drawText(QRectF(r.left(), r.top() + 6, r.width(), 28), Qt.AlignmentFlag.AlignCenter, str(ws))
            p.setFont(font(6.5, QFont.Weight.DemiBold, spacing=1.4))
            p.setPen(tag_col)
            p.drawText(QRectF(r.left(), r.top() + 33, r.width(), 16), Qt.AlignmentFlag.AlignCenter, tag)
        # the locked tile: a solid accent block that glides from the previous one, with a padlock
        a, b = self._tile(self._from_ws), self._tile(self.selected)
        k = self._move.value
        ring = QRectF(a.left() + (b.left() - a.left()) * k, a.top() + (b.top() - a.top()) * k, b.width(), b.height()
                      ).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(ring, 9, 9)
        p.setPen(QColor("white"))
        p.setFont(font(15, QFont.Weight.Bold))
        p.drawText(QRectF(ring.left(), ring.top() + 6, ring.width(), 28), Qt.AlignmentFlag.AlignCenter, str(self.selected))
        # padlock glyph + LOCKED, centred together
        p.setFont(font(6.5, QFont.Weight.Bold, spacing=1.4))
        tw = QFontMetrics(p.font()).horizontalAdvance("LOCKED")
        x0 = ring.center().x() - (12 + tw) / 2
        cy = ring.top() + 41
        p.setPen(QPen(QColor("white"), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(x0 + 1.4, cy - 6.5, 6.4, 7), 0, 180 * 16)
        p.setBrush(QColor("white"))
        p.drawRoundedRect(QRectF(x0, cy - 3, 9, 6.5), 1.6, 1.6)
        p.drawText(QRectF(x0 + 13, ring.top() + 33, tw + 6, 16), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   "LOCKED")
        if dim:
            p.setOpacity(1.0)
            p.setPen(qc(MUTED))
            p.setFont(font(8.5, QFont.Weight.DemiBold, spacing=0.4))
            p.fillRect(self.rect().adjusted(0, self.height() // 2 - 15, 0, -(self.height() // 2 - 15)), QColor(13, 15, 19, 225))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.enabled_note)


class AmbientBG(QWidget):
    """Window backdrop: a flat dark surface with a faint top-down lift (no animation, nothing to repaint)."""

    def paintEvent(self, _):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor("#12151b"))
        g.setColorAt(0.5, QColor(BG))
        g.setColorAt(1, QColor("#0b0d11"))
        p.fillRect(self.rect(), QBrush(g))
