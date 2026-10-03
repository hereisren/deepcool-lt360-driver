"""Animation toolkit for the LT360 GUI: pure presentation, no sockets or subprocesses.

* Clock: ONE shared timer (25 fps) that only runs while at least one idle-animated widget is visible, and that
  can be paused when the window is minimized, so an idle GUI costs almost nothing.
* IdleWidget: a QWidget that subscribes to the clock while shown and gets idle_tick(t) callbacks.
* Tween: an eased float that repaints its widget while it moves (hover glows, sliding highlights, knobs).
* reveal(): staggered fade-in for the cards of a page.
"""
import math
import time

from PyQt6.QtCore import QEasingCurve, QObject, QPropertyAnimation, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QWidget


class Clock(QObject):
    tick = pyqtSignal(float)   # seconds since the clock was created
    INTERVAL_MS = 50          # 20 fps while the window is focused
    BACKGROUND_MS = 125       # ~8 fps while it is visible but not focused
    _instance: "Clock | None" = None

    @classmethod
    def instance(cls) -> "Clock":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._t0 = time.monotonic()
        self._users = 0
        self._paused = False
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(lambda: self.tick.emit(self.now()))

    def now(self) -> float:
        return time.monotonic() - self._t0

    def _sync(self):
        run = self._users > 0 and not self._paused
        if run and not self._timer.isActive():
            self._timer.start()
        elif not run and self._timer.isActive():
            self._timer.stop()

    def acquire(self):
        self._users += 1
        self._sync()

    def release(self):
        self._users = max(0, self._users - 1)
        self._sync()

    def set_paused(self, paused: bool):
        self._paused = paused
        self._sync()

    def set_background(self, background: bool):
        """Visible but unfocused windows animate slowly."""
        self._timer.setInterval(self.BACKGROUND_MS if background else self.INTERVAL_MS)


class IdleWidget(QWidget):
    """A widget with an idle animation: idle_tick(t) is called ~25x/s while it is visible (default: repaint).
    Slow effects (breathing glows) set idle_div = 2 to run at half rate: same look, half the repaint cost."""
    idle_div = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._idle_on = False
        self._idle_n = 0

    def idle_tick(self, t: float):
        self.update()

    def _clock_cb(self, t: float):
        self._idle_n += 1
        if self._idle_n % self.idle_div == 0:
            self.idle_tick(t)

    def showEvent(self, e):
        super().showEvent(e)
        if not self._idle_on:
            self._idle_on = True
            clock = Clock.instance()
            clock.tick.connect(self._clock_cb)
            clock.acquire()

    def hideEvent(self, e):
        super().hideEvent(e)
        if self._idle_on:
            self._idle_on = False
            clock = Clock.instance()
            try:
                clock.tick.disconnect(self._clock_cb)
            except TypeError:
                pass
            clock.release()


class Tween(QObject):
    """An eased float that repaints `widget` while it moves. Jumps straight to the target while the widget is
    hidden (no animation to watch), so programmatic state changes at start-up never animate."""

    def __init__(self, widget: QWidget, value: float = 0.0):
        super().__init__(widget)
        self.widget = widget
        self.value = float(value)
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._on_value)

    def _on_value(self, v):
        self.value = float(v)
        self.widget.update()

    def to(self, target: float, ms: int = 220, curve=QEasingCurve.Type.OutCubic):
        target = float(target)
        if target == self.value and self._anim.state() != QVariantAnimation.State.Running:
            return
        self._anim.stop()
        if not self.widget.isVisible() or ms <= 0:
            self.value = target
            self.widget.update()
            return
        self._anim.setDuration(ms)
        self._anim.setEasingCurve(curve)
        self._anim.setStartValue(self.value)
        self._anim.setEndValue(target)
        self._anim.start()

    def set(self, value: float):
        self._anim.stop()
        self.value = float(value)
        self.widget.update()

    @property
    def running(self) -> bool:
        return self._anim.state() == QVariantAnimation.State.Running


def reveal(widgets, delay_ms: int = 55, duration_ms: int = 360):
    """Fade the widgets in one after another. The opacity effect is removed again when a widget has faded in, so
    custom-painted children go back to painting straight to the window (an effect forces off-screen rendering)."""
    for i, w in enumerate(widgets):
        eff = QGraphicsOpacityEffect(w)
        eff.setOpacity(0.0)
        w.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", w)
        anim.setDuration(duration_ms)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def finish(w=w):
            if w.graphicsEffect() is not None:
                w.setGraphicsEffect(None)
            sweep = getattr(w, "start_sweep", None)
            if sweep:
                sweep()
        anim.finished.connect(finish)
        QTimer.singleShot(i * delay_ms, anim.start)


def breathe(t: float, period: float = 3.2, phase: float = 0.0) -> float:
    """0..1 smooth breathing curve."""
    return 0.5 + 0.5 * math.sin(2 * math.pi * (t / period) + phase)
