import math
import weakref

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QFrame, QLabel
from shiboken6 import isValid

from ecoflow_monitor.motion import MotionState, RoleTransition, ValueTransition, derive_motion
from ecoflow_monitor.ui.tokens import COLORS, MOTION, RADIUS
from ecoflow_monitor.ui.common import DragHeader


class MotionDriver(QObject):
    def __init__(self, controller, parent=None):
        super().__init__(parent or controller)
        self.controller = controller
        self.state = MotionState()
        self.enabled = False
        self._key = None
        self._stopped = False
        self._visibility_pending = False
        self._views = weakref.WeakSet()
        self.roles = {key: RoleTransition() for key in ("battery", "input", "output", "balance", "ac", "dc")}
        self.values = {key: ValueTransition() for key in ("soc", "input_share")}
        self.error_started = None
        self._error_key = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self.tick)
        controller.changed.connect(self.refresh)
        controller.system_activity_changed.connect(self.schedule)
        QApplication.instance().installEventFilter(self)
        self.refresh()

    def now(self):
        return self.controller.clock() * 1000

    def phase(self, timing, now=None):
        return ((self.now() if now is None else now) % MOTION["timing"][timing]) / MOTION["timing"][timing]

    def refresh(self):
        if self._stopped:
            return
        now = self.now()
        enabled = (not self.controller.system_activity.paused and self.controller.ui_settings.animations
                   and QApplication.instance().property("system_animations") is not False)
        key = (self.controller.motion_telemetry, self.controller.connection, self.controller.fresh)
        if key != self._key:
            self.state = derive_motion(*key, self.state.mode, MOTION["powerThresholds"])
            self._key = key
        error_key = self.controller.connection if self.state.status == "error" else None
        if error_key != self._error_key:
            self.error_started = now if error_key is not None and enabled else None
            self._error_key = error_key
        if not enabled:
            self.error_started = None
        self.enabled = enabled
        targets = {"battery": self.state.mode if self.state.mode in {"charging", "discharging"} else None, "input": "input" if self.state.input_active else None, "output": "output" if self.state.output_active else None, "balance": self.state.balance, "ac": "outlet" if self.state.ac_active else None, "dc": "outlet" if self.state.dc_active else None}
        for key, role in targets.items():
            self.roles[key].set(role if enabled else None, now, MOTION["timing"], enabled and self.state.status == "online")
        for key, transition in self.values.items():
            transition.set(getattr(self.state, key), now, MOTION["timing"]["valueTransition"] if enabled else 0)
        for view in tuple(self._views):
            if isValid(view):
                view.update()
            else:
                self._views.discard(view)
        self.schedule()

    def add(self, view):
        self._views.add(view)
        view.destroyed.connect(self.schedule)
        self.schedule()

    def visible(self, view):
        return isValid(view) and view.isVisible() and not view.window().isMinimized() and not view.visibleRegion().isEmpty()

    @Slot()
    def schedule(self, *args):
        if self._stopped or not isValid(self._timer):
            return
        active = (not self.controller.system_activity.paused and self.enabled
                  and any(self.visible(view) and view.motion_active() for view in tuple(self._views)))
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active:
            self._timer.stop()

    def tick(self):
        if self._key is not None and self._key[2] != self.controller.fresh:
            self.refresh()
        for view in tuple(self._views):
            if self.visible(view) and view.motion_active():
                view.update()
        self.schedule()

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.Hide, QEvent.Type.WindowStateChange}:
            self.schedule()
        if event.type() == QEvent.Type.DynamicPropertyChange and watched is QApplication.instance() and bytes(event.propertyName()) == b"system_animations":
            self.refresh()
        elif not self._visibility_pending and event.type() in {QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.WindowStateChange, QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Wheel}:
            self._visibility_pending = True
            QTimer.singleShot(0, self, self.visibility_changed)
        return False

    def visibility_changed(self):
        self._visibility_pending = False
        self.schedule()

    def stop(self):
        self._timer.stop()
        self._stopped = True
        self.enabled = False
        QApplication.instance().removeEventFilter(self)
        self._views.clear()


def motion_driver(controller, parent=None):
    driver = getattr(controller, "motion_driver", None)
    if driver is None:
        driver = MotionDriver(controller, parent)
        controller.motion_driver = driver
    return driver


def wave(painter, rect, role, phase, material="Panel", gain=1, radius=None):
    if role is None or gain <= 0 or phase <= 0 or phase >= 1 or rect.isEmpty():
        return
    tokens = MOTION["materials"][material]
    color = QColor(COLORS[MOTION["roles"][role]["colorToken"]])
    reverse = MOTION["roles"][role]["direction"] == "right-to-left"
    phase = 1 - phase if reverse else phase
    width = rect.width() * MOTION["waveWidthFraction"]
    left = rect.left() - width + phase * (rect.width() + width)
    envelope = min(1, phase / 0.12, (1 - phase) / 0.22) * gain
    path = QPainterPath()
    corner = RADIUS["card"] if radius is None else radius
    path.addRoundedRect(rect, corner, corner)
    painter.save()
    painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
    for edge in (False, True):
        gradient = QLinearGradient(left, 0, left + width, 0)
        peak = 0.38 if reverse else 0.62
        for position, strength in ((0, 0), (peak / 2, 0.2), (peak, 1), (1, 0)):
            shade = QColor(color)
            shade.setAlphaF(tokens["edgeMaxAlpha" if edge else "waveMaxAlpha"] * envelope * strength)
            gradient.setColorAt(position, shade)
        if edge:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(gradient, 1.2))
            painter.drawPath(path)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRect(rect)
    painter.restore()


class MotionSurface(QFrame):
    def __init__(self, controller, channel, parent=None):
        super().__init__(parent)
        self.motion = motion_driver(controller)
        self.channel = channel
        self.material = "Panel"
        self.motion.add(self)

    def motion_active(self):
        if self.channel is None:
            return False
        role, gain = self.motion.roles[self.channel].sample(self.motion.now())
        return role is not None

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.motion.enabled and self.channel is not None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            role, gain = self.motion.roles[self.channel].sample(self.motion.now())
            wave(painter, QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6), role, self.motion.phase("outletWave" if self.channel in {"ac", "dc"} else "fieldWave"), self.material, gain, getattr(self, "motion_radius", RADIUS["card"]))


class MotionMessage(QLabel):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.motion = motion_driver(controller)
        self.motion.add(self)

    def motion_active(self):
        return self.motion.state.mode in {"charging", "discharging"}

    def paintEvent(self, event):
        if not self.motion.enabled or not self.motion_active():
            return super().paintEvent(event)
        painter = QPainter(self)
        painter.setOpacity((1 + math.cos(2 * math.pi * self.motion.phase("messageBreath"))) / 2)
        painter.setFont(self.font())
        painter.setPen(self.palette().windowText().color())
        painter.drawText(self.contentsRect(), int(self.alignment()) | int(Qt.TextFlag.TextWordWrap), self.text())


def status_motion_active(driver):
    return driver.state.status in {"online", "stale", "connecting"} or driver.error_started is not None and driver.now() - driver.error_started < MOTION["timing"]["errorEntry"]


def paint_status_mark(painter, rect, driver, outlined=False):
    status = driver.state.status
    role = {"online": "accent", "stale": "output", "connecting": "input", "error": "danger", "offline": "muted"}[status]
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.center())
    if driver.enabled:
        if status in {"online", "stale"}:
            painter.setOpacity((1 + math.cos(2 * math.pi * driver.phase("onlineMark" if status == "online" else "staleMark"))) / 2)
        elif status == "connecting":
            painter.rotate(driver.phase("connectingMark") * 360)
        elif status == "error" and driver.error_started is not None:
            progress = (driver.now() - driver.error_started) / MOTION["timing"]["errorEntry"]
            if 0 <= progress < 1:
                color = QColor(COLORS["danger"])
                color.setAlphaF(math.sin(progress * 2 * math.pi) ** 2 * 0.85)
                painter.setPen(QPen(color, 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(QRectF(-rect.width() / 2, -rect.height() / 2, rect.width(), rect.height()), 4, 4)
    painter.setPen(QPen(QColor(COLORS[role]), 1.6))
    marker = QRectF(-rect.width() / 2, -rect.height() / 2, rect.width(), rect.height())
    if status == "connecting":
        painter.drawArc(marker.adjusted(3, 3, -3, -3), 0, 260 * 16)
    elif outlined:
        from ecoflow_monitor.ui.typography import draw_outlined
        draw_outlined(painter, "!" if status == "error" else "*" if status in {"online", "stale"} else "·", painter.font(), marker, QColor(COLORS[role]), False)
    else:
        painter.drawText(marker, Qt.AlignmentFlag.AlignCenter, "!" if status == "error" else "*" if status in {"online", "stale"} else "·")
    painter.restore()


class MotionHeader(DragHeader):
    def __init__(self, controller, text, parent=None):
        super().__init__(text, parent)
        self.motion = motion_driver(controller)
        self.configure(1)
        self.motion.add(self)

    def configure(self, scale):
        self._mark_side = round(16 * scale)
        self.setContentsMargins(round(20 * scale), 0, 0, 0)

    def motion_active(self):
        return status_motion_active(self.motion)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        side = self._mark_side
        paint_status_mark(painter, QRectF(0, max(0, (self.height() - side) / 2), side, side), self.motion, self.outlined)
