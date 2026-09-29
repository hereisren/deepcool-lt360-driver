"""Custom-painted widgets and the neon-violet theme for the LT360 VISION — For Renmin GUI.

Everything here is pure presentation: no sockets, no subprocesses. Widgets paint
themselves (QPainter) instead of leaning on stock Qt controls.
"""
import os

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient, QMovie, QPainter,
    QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PyQt6.QtWidgets import QSizePolicy, QSlider, QWidget

# ---------- palette ----------
BG = "#08080c"
CARD = "#111119"
CARD_HI = "#171624"
BORDER = "#262038"
VIOLET = "#a855f7"
VIOLET_HI = "#c084fc"
CYAN = "#22d3ee"
TEXT = "#ebe9f5"
MUTED = "#8b86a3"
DIM = "#4a4560"
OK = "#4ade80"
BAD = "#f87171"
YELLOW = "#facc15"

SANS = "JZFS Sans"
PIXEL = "Pixel Numsymbol"


def qc(hex_or_color, alpha: int | None = None) -> QColor:
    c = QColor(hex_or_color)
    if alpha is not None:
        c.setAlpha(alpha)
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


def glow(p: QPainter, rect: QRectF, radius: float, color: QColor, layers: int = 5, spread: float = 2.0):
    """Cheap outer glow: stacked translucent rounded-rect strokes."""
    p.save()
    p.setBrush(Qt.BrushStyle.NoBrush)
    for i in range(layers, 0, -1):
        c = QColor(color)
        c.setAlpha(int(color.alpha() * (1 - i / (layers + 1)) * 0.35))
        pen = QPen(c, i * spread)
        p.setPen(pen)
        p.drawRoundedRect(rect, radius, radius)
    p.restore()


def cover_rect(src: QSize, dst: QRectF) -> QRectF:
    """Source sub-rect that fills `dst` with the same aspect (center crop)."""
    if src.isEmpty() or dst.isEmpty():
        return QRectF(0, 0, src.width(), src.height())
    s = max(dst.width() / src.width(), dst.height() / src.height())
    w, h = dst.width() / s, dst.height() / s
    return QRectF((src.width() - w) / 2, (src.height() - h) / 2, w, h)


# ---------- pump-block preview stage ----------

class PumpStage(QWidget):
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
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(400, 240)

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
        p.fillRect(screen, QColor(8, 8, 12, 70))
        for i, r, tag in self.edit_boxes:
            if i == self.edit_selected:
                continue
            sr = self._to_screen(r)
            hover = i == self._edit_hover
            p.setBrush(qc(VIOLET, 40) if hover else Qt.BrushStyle.NoBrush)
            p.setPen(QPen(qc(VIOLET_HI, 230 if hover else 130), 1.2, Qt.PenStyle.DashLine))
            p.drawRoundedRect(sr, 3, 3)
        sel = next(((r, tag) for i, r, tag in self.edit_boxes if i == self.edit_selected), None)
        if sel is not None:
            sr = self._to_screen(sel[0]).adjusted(-2, -2, 2, 2)
            glow(p, sr, 4, qc(CYAN, 255), layers=4, spread=2)
            p.setPen(QPen(QColor(CYAN), 1.8))
            p.setBrush(qc(CYAN, 28))
            p.drawRoundedRect(sr, 4, 4)
            p.setBrush(QColor(CYAN))
            p.setPen(Qt.PenStyle.NoPen)
            for c in (sr.topLeft(), sr.topRight(), sr.bottomLeft(), sr.bottomRight()):
                p.drawRect(QRectF(c.x() - 3, c.y() - 3, 6, 6))
            p.setFont(font(7, QFont.Weight.Bold, spacing=1))
            tw = QFontMetrics(p.font()).horizontalAdvance(sel[1]) + 12
            ty = sr.top() - 17 if sr.top() - 17 > screen.top() else sr.bottom() + 3
            tag_r = QRectF(max(screen.left(), min(sr.left(), screen.right() - tw)), ty, tw, 15)
            p.setBrush(QColor(CYAN))
            p.drawRoundedRect(tag_r, 4, 4)
            p.setPen(QColor("#041016"))
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
        p.setBrush(QColor(8, 8, 12, 200))
        p.drawRoundedRect(banner, 10, 10)
        p.setPen(qc(VIOLET_HI))
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
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ambient glow: violet upper-left, cyan lower-right
        c = bezel.center()
        r = min(self.width(), self.height()) * 0.62
        g = QRadialGradient(QPointF(c.x() - bezel.width() * 0.12, c.y() - bezel.height() * 0.1), r)
        g.setColorAt(0, qc(VIOLET, 105))
        g.setColorAt(0.6, qc(VIOLET, 26))
        g.setColorAt(1, qc(VIOLET, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRect(self.rect())
        g2 = QRadialGradient(QPointF(c.x() + bezel.width() * 0.22, c.y() + bezel.height() * 0.25), r * 0.7)
        g2.setColorAt(0, qc(CYAN, 70))
        g2.setColorAt(1, qc(CYAN, 0))
        p.setBrush(QBrush(g2))
        p.drawRect(self.rect())

        # pump head body
        radius = 30
        glow(p, bezel, radius, qc(VIOLET, 255), layers=7, spread=3)
        body = QLinearGradient(bezel.topLeft(), bezel.bottomRight())
        body.setColorAt(0, QColor("#2b2542"))
        body.setColorAt(0.45, QColor("#13111d"))
        body.setColorAt(1, QColor("#221c36"))
        p.setBrush(QBrush(body))
        rim = QLinearGradient(bezel.topLeft(), bezel.bottomRight())
        rim.setColorAt(0, qc(VIOLET_HI, 200))
        rim.setColorAt(0.5, qc(BORDER, 255))
        rim.setColorAt(1, qc(CYAN, 190))
        p.setPen(QPen(QBrush(rim), 1.6))
        p.drawRoundedRect(bezel, radius, radius)

        # recessed screen well
        p.setPen(QPen(QColor("#000000"), 2))
        p.setBrush(QColor("#000000"))
        p.drawRoundedRect(screen.adjusted(-2, -2, 2, 2), 13, 13)

        # screws on the bottom strip
        by = bezel.bottom() - 15
        for sx in (bezel.left() + 24, bezel.right() - 24):
            sg = QRadialGradient(QPointF(sx - 1, by - 1), 6)
            sg.setColorAt(0, QColor("#4a4366"))
            sg.setColorAt(1, QColor("#14111f"))
            p.setPen(QPen(qc(BORDER), 1))
            p.setBrush(QBrush(sg))
            p.drawEllipse(QPointF(sx, by), 4.5, 4.5)
            p.setPen(QPen(QColor("#0a0910"), 1.2))
            p.drawLine(QPointF(sx - 2.4, by + 1.4), QPointF(sx + 2.4, by - 1.4))

        # brand strip
        p.setFont(font(7.5, QFont.Weight.DemiBold, spacing=3.2))
        p.setPen(qc(VIOLET_HI, 210))
        p.drawText(QRectF(bezel.left(), bezel.bottom() - 27, bezel.width(), 24),
                   Qt.AlignmentFlag.AlignCenter, "LT360 VISION  ·  FOR RENMIN")
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
        p.drawPixmap(0, 0, self._bezel_cache[1])

        clip = QPainterPath()
        clip.addRoundedRect(screen, 11, 11)
        p.save()
        p.setClipPath(clip)
        if self.image is not None and not self.image.isNull():
            p.drawImage(screen, self.image, cover_rect(self.image.size(), screen))
        else:
            p.fillRect(screen, QColor("#050508"))
            p.setPen(qc(DIM))
            p.setFont(font(11, QFont.Weight.DemiBold, spacing=4))
            p.drawText(screen, Qt.AlignmentFlag.AlignCenter, "NO SIGNAL\n\ndrop a GIF · MP4 · image here")
        # glass reflection
        gl = QLinearGradient(screen.topLeft(), screen.bottomRight())
        gl.setColorAt(0, QColor(255, 255, 255, 26))
        gl.setColorAt(0.35, QColor(255, 255, 255, 0))
        p.fillRect(screen, QBrush(gl))
        if self.edit_mode:
            self._paint_edit(p, screen)
        if self.busy:
            p.fillRect(screen, QColor(8, 8, 12, 150))
            p.setPen(qc(VIOLET_HI))
            p.setFont(font(12, QFont.Weight.Bold, spacing=5))
            p.drawText(screen, Qt.AlignmentFlag.AlignCenter, "LOADING...")
        if self.drop_active:
            p.fillRect(screen, qc(VIOLET, 90))
        p.restore()
        if self.drop_active:
            pen = QPen(qc(VIOLET_HI), 2.4, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(screen.adjusted(4, 4, -4, -4), 9, 9)
            p.setPen(QColor("white"))
            p.setFont(font(15, QFont.Weight.Bold, spacing=5))
            p.drawText(screen, Qt.AlignmentFlag.AlignCenter, "DROP TO LOAD")


# ---------- inputs ----------

class SegmentedControl(QWidget):
    """Capsule with N options; the selected one glows violet."""
    changed = pyqtSignal(object)

    def __init__(self, options: list[tuple[str, object]], parent=None):
        super().__init__(parent)
        self.options = options
        self.index = 0
        self._hover = -1
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
                self.update()

    def _seg(self, i) -> QRectF:
        w = (self.width() - 6) / len(self.options)
        return QRectF(3 + i * w, 3, w, self.height() - 6)

    def mouseMoveEvent(self, e):
        h = next((i for i in range(len(self.options)) if self._seg(i).contains(e.position())), -1)
        if h != self._hover:
            self._hover = h
            self.update()

    def leaveEvent(self, _):
        self._hover = -1
        self.update()

    def mousePressEvent(self, e):
        for i in range(len(self.options)):
            if self._seg(i).contains(e.position()) and i != self.index:
                self.index = i
                self.update()
                self.changed.emit(self.value())
                return

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(qc(BORDER), 1))
        p.setBrush(QColor("#0b0b12"))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        for i, (label, _) in enumerate(self.options):
            seg = self._seg(i)
            sel = i == self.index
            if sel:
                glow(p, seg, seg.height() / 2, qc(VIOLET, 255), layers=3, spread=2)
                g = QLinearGradient(seg.topLeft(), seg.bottomRight())
                g.setColorAt(0, QColor(VIOLET))
                g.setColorAt(1, QColor("#7c3aed"))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(g))
                p.drawRoundedRect(seg, seg.height() / 2, seg.height() / 2)
            elif i == self._hover:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qc(VIOLET, 28))
                p.drawRoundedRect(seg, seg.height() / 2, seg.height() / 2)
            p.setPen(QColor("white") if sel else qc(MUTED))
            p.setFont(font(9, QFont.Weight.DemiBold, spacing=0.6))
            p.drawText(seg, Qt.AlignmentFlag.AlignCenter, label)


class PillToggle(QWidget):
    """Glowing switch + label."""
    toggled = pyqtSignal(bool)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text = text
        self.on = False
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        fm = QFontMetrics(font(9, QFont.Weight.DemiBold, spacing=0.6))
        return QSize(fm.horizontalAdvance(self.text) + 74, 34)

    def set_on(self, on: bool):
        if on != self.on:
            self.on = on
            self.update()

    def mousePressEvent(self, _):
        self._flip()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return):
            self._flip()
        else:
            super().keyPressEvent(e)

    def _flip(self):
        self.on = not self.on
        self.update()
        self.toggled.emit(self.on)

    def enterEvent(self, _):
        self._hover = True
        self.update()

    def leaveEvent(self, _):
        self._hover = False
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(qc(VIOLET if self.on else BORDER, 200 if self._hover or self.on else 255), 1))
        p.setBrush(qc(VIOLET, 30) if self.on else QColor("#0b0b12"))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        track = QRectF(10, (self.height() - 16) / 2, 32, 16)
        if self.on:
            glow(p, track, 8, qc(VIOLET, 255), layers=3, spread=2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(VIOLET) if self.on else QColor("#2a2540"))
        p.drawRoundedRect(track, 8, 8)
        kx = track.right() - 8 if self.on else track.left() + 8
        p.setBrush(QColor("white") if self.on else QColor(MUTED))
        p.drawEllipse(QPointF(kx, track.center().y()), 5.5, 5.5)
        p.setPen(QColor(TEXT) if self.on else qc(MUTED))
        p.setFont(font(9, QFont.Weight.DemiBold, spacing=0.6))
        p.drawText(QRectF(50, 0, self.width() - 56, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text)


class NeonSlider(QSlider):
    """Glowing groove + handle. Click-to-jump and drag both update at paint rate.
    `bipolar` fills from the middle of the range (for signed values such as pan offsets).
    """

    def __init__(self, bipolar: bool = False):
        super().__init__(Qt.Orientation.Horizontal)
        self.bipolar = bipolar
        self._hover = False
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
            self.setValue(self._value_at(e.position().x()))
            e.accept()

    def mouseMoveEvent(self, e):
        if self.isSliderDown():
            self.setValue(self._value_at(e.position().x()))

    def mouseReleaseEvent(self, e):
        if self.isSliderDown():
            self.setSliderDown(False)

    def enterEvent(self, _):
        self._hover = True
        self.update()

    def leaveEvent(self, _):
        self._hover = False
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        span = self.width() - 2 * self.MARGIN
        f = (self.value() - self.minimum()) / max(1, self.maximum() - self.minimum())
        cy = self.height() / 2
        groove = QRectF(self.MARGIN, cy - 3, span, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1a1727"))
        p.drawRoundedRect(groove, 3, 3)
        hx = self.MARGIN + span * f
        x0 = self.MARGIN + span / 2 if self.bipolar else self.MARGIN
        fill = QRectF(min(x0, hx), cy - 3, abs(hx - x0), 6)
        if self.bipolar:
            p.setBrush(qc(MUTED, 120))
            p.drawRect(QRectF(x0 - 0.75, cy - 7, 1.5, 14))
        if fill.width() > 1:
            glow(p, fill, 3, qc(VIOLET, 255), layers=4, spread=2.5)
            g = QLinearGradient(fill.topLeft(), fill.topRight())
            g.setColorAt(0, QColor("#7c3aed"))
            g.setColorAt(1, QColor(VIOLET_HI))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(fill, 3, 3)
        radius = 9 if (self._hover or self.isSliderDown()) else 8
        halo = QRadialGradient(QPointF(hx, cy), radius + 9)
        halo.setColorAt(0.4, qc(VIOLET_HI, 120))
        halo.setColorAt(1, qc(VIOLET_HI, 0))
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(hx, cy), radius + 9, radius + 9)
        p.setPen(QPen(QColor(VIOLET_HI), 2.2))
        p.setBrush(QColor("#0b0b12"))
        p.drawEllipse(QPointF(hx, cy), radius, radius)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("white"))
        p.drawEllipse(QPointF(hx, cy), 2.6, 2.6)


class Chip(QWidget):
    """Small status pill: colored dot + text."""

    def __init__(self, text="", color=MUTED, dot=True):
        super().__init__()
        self._text, self._color, self._dot = text, color, dot
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set(self, text: str, color: str | None = None):
        if text != self._text or (color and color != self._color):
            self._text = text
            self._color = color or self._color
            self.updateGeometry()
            self.update()

    def sizeHint(self):
        fm = QFontMetrics(font(8, QFont.Weight.DemiBold, spacing=1.2))
        return QSize(fm.horizontalAdvance(self._text) + (38 if self._dot else 24), 26)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(qc(self._color, 110), 1))
        p.setBrush(qc(self._color, 24))
        p.drawRoundedRect(r, 13, 13)
        x = 12
        if self._dot:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(self._color))
            p.drawEllipse(QPointF(14, 13), 3.5, 3.5)
            x = 26
        p.setPen(QColor(self._color))
        p.setFont(font(8, QFont.Weight.DemiBold, spacing=1.2))
        p.drawText(QRectF(x, 0, self.width() - x - 8, 26), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self._text)


# ---------- header ----------

class Banner(QWidget):
    def __init__(self, cjk_family: str | None, version: str = ""):
        super().__init__()
        self.cjk = cjk_family
        self.version = version
        self.setFixedHeight(92)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        # title, gradient-filled
        tf = font(30, QFont.Weight.Bold, spacing=3)
        path = QPainterPath()
        path.addText(QPointF(4, 46), tf, "LT360 VISION")
        g = QLinearGradient(0, 0, 330, 0)
        g.setColorAt(0, QColor("#ffffff"))
        g.setColorAt(0.55, QColor(VIOLET_HI))
        g.setColorAt(1, QColor(CYAN))
        p.fillPath(path, QBrush(g))
        title_w = path.boundingRect().right() + 16

        # neon badge
        bf = font(10.5, QFont.Weight.Bold, spacing=2.4)
        cf = QFont(self.cjk or SANS)
        cf.setPointSizeF(11)
        cf.setWeight(QFont.Weight.Bold)
        fm_b, fm_c = QFontMetrics(bf), QFontMetrics(cf)
        t1, t2 = "FOR RENMIN", " 人民"
        bw = fm_b.horizontalAdvance(t1) + fm_c.horizontalAdvance(t2) + 30
        badge = QRectF(title_w, 17, bw, 32)
        glow(p, badge, 16, qc(VIOLET, 255), layers=6, spread=3)
        bg = QLinearGradient(badge.topLeft(), badge.bottomRight())
        bg.setColorAt(0, QColor(VIOLET))
        bg.setColorAt(1, QColor("#7c3aed"))
        p.setPen(QPen(qc(VIOLET_HI), 1.2))
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(badge, 16, 16)
        p.setPen(QColor("white"))
        p.setFont(bf)
        x = badge.left() + 15
        base = badge.top() + 21.5
        p.drawText(QPointF(x, base), t1)
        p.setFont(cf)
        p.drawText(QPointF(x + fm_b.horizontalAdvance(t1), base), t2)

        if self.version:
            vf = font(8, QFont.Weight.Bold, spacing=1.6)
            vt = f"v{self.version}"
            vr = QRectF(badge.right() + 12, 22, QFontMetrics(vf).horizontalAdvance(vt) + 20, 22)
            p.setPen(QPen(qc(CYAN, 150), 1))
            p.setBrush(qc(CYAN, 22))
            p.drawRoundedRect(vr, 11, 11)
            p.setPen(QColor(CYAN))
            p.setFont(vf)
            p.drawText(vr, Qt.AlignmentFlag.AlignCenter, vt)

        p.setPen(qc(MUTED))
        p.setFont(font(9.5, spacing=0.8))
        p.drawText(QPointF(6, 72), "Open-Source Linux Liquid Cooler Suite  •  By Ren, For The People")

        ln = QLinearGradient(0, 0, w, 0)
        ln.setColorAt(0, qc(VIOLET, 220))
        ln.setColorAt(0.5, qc(CYAN, 120))
        ln.setColorAt(1, qc(CYAN, 0))
        p.setPen(QPen(QBrush(ln), 1.5))
        p.drawLine(0, self.height() - 2, w, self.height() - 2)


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
        if self.active:
            glow(p, r, 11, qc(VIOLET, 255), layers=4, spread=2)
        p.setPen(QPen(QColor(VIOLET) if self.active else qc(VIOLET_HI, 140) if self._hover else qc(BORDER), 1.2))
        p.setBrush(QColor(CARD_HI))
        p.drawRoundedRect(r, 11, 11)

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
        p.setPen(qc(CYAN))
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
        p.setPen(QPen(qc(VIOLET_HI if self._hover else VIOLET, 200 if self._hover else 110), 1.4, Qt.PenStyle.DashLine))
        p.setBrush(qc(VIOLET, 22 if self._hover else 10))
        p.drawRoundedRect(r, 11, 11)
        c = r.center()
        p.setPen(QPen(QColor(VIOLET_HI), 2.2, cap=Qt.PenCapStyle.RoundCap))
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
        if self.selected:
            glow(p, r, 12, qc(acc, 255), layers=5, spread=2.2)
        p.setPen(QPen(acc if self.selected else qc(acc, 150) if self._hover else qc(BORDER), 1.5 if self.selected else 1))
        p.setBrush(QColor(CARD_HI))
        p.drawRoundedRect(r, 12, 12)

        # mini panel with the overlay bar as the daemon draws it
        scr = QRectF(r.left() + 9, r.top() + 9, r.width() - 18, r.height() - 46)
        clip = QPainterPath()
        clip.addRoundedRect(scr, 7, 7)
        p.save()
        p.setClipPath(clip)
        bgg = QLinearGradient(scr.topLeft(), scr.bottomRight())
        bgg.setColorAt(0, QColor("#1a1230"))
        bgg.setColorAt(1, QColor("#08131a"))
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
        p.setPen(QPen(qc("#2a2540"), 4, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(ring, -135 * 16, -270 * 16)
        p.setPen(QPen(acc, 4, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(ring, -135 * 16, -190 * 16)
        p.setPen(QColor(TEXT))
        p.setFont(font(6.5, QFont.Weight.Bold))
        p.drawText(ring, Qt.AlignmentFlag.AlignCenter, "63°")
        spark = QRectF(ring.right() + 8, scr.top() + 10, scr.right() - ring.right() - 16, d * 0.62)
        p.setPen(QPen(qc(acc2, 90), 1))
        p.setBrush(qc(QColor(12, 6, 20), 190))
        p.drawRoundedRect(spark, 4, 4)
        ys = (0.6, 0.45, 0.55, 0.3, 0.4, 0.2, 0.35, 0.25)
        path = QPainterPath()
        for i, f in enumerate(ys):
            pt = QPointF(spark.left() + 3 + i * (spark.width() - 6) / (len(ys) - 1), spark.top() + 3 + f * (spark.height() - 6))
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        p.setPen(QPen(QColor(CYAN), 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        chip = QRectF(scr.right() - 50, scr.bottom() - 22, 43, 15)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(acc2, 230))
        p.drawRoundedRect(chip, 5, 5)
        p.setPen(QColor("#1a1230"))
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
    """Cyan live telemetry value."""

    def __init__(self, label: str):
        super().__init__()
        self.label, self.value = label, "--"
        self.setFixedHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, v: str):
        if v != self.value:
            self.value = v
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(qc(CYAN, 60), 1))
        p.setBrush(qc(CYAN, 12))
        p.drawRoundedRect(r, 9, 9)
        p.setPen(QColor(CYAN))
        p.setFont(font(15, QFont.Weight.Bold))
        p.drawText(QRectF(0, 6, self.width(), 26), Qt.AlignmentFlag.AlignCenter, self.value)
        p.setPen(qc(MUTED))
        p.setFont(font(6.5, QFont.Weight.DemiBold, spacing=1.6))
        p.drawText(QRectF(0, 31, self.width(), 16), Qt.AlignmentFlag.AlignCenter, self.label)
