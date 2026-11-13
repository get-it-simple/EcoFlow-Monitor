from datetime import datetime
import os
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QSizePolicy, QStyle, QStyleOptionButton, QStylePainter, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.domain import ConnectionState
from ecoflow_monitor.integrations import IntegrationProfile
from ecoflow_monitor.ui.tokens import COLORS, TOKENS
from ecoflow_monitor.ui.typography import ValueLabel


def profile_tooltip(profile: IntegrationProfile) -> str:
    return "\n".join(part for part in (profile.label, profile.technical_details) if part)


def elevation_prompt(parent: QWidget, name: str) -> QMessageBox:
    prompt = QMessageBox(parent)
    prompt.setWindowTitle(t("integration_helper_title"))
    prompt.setText(t("integration_helper_prompt").format(name=name))
    prompt.setIcon(QMessageBox.Icon.Question)
    prompt.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    prompt.setDefaultButton(QMessageBox.StandardButton.No)
    prompt.setWindowModality(Qt.WindowModality.ApplicationModal)
    prompt.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    return prompt


def duration(hours: float | None) -> str:
    if hours is None:
        return "—"
    total = max(0, int(hours * 60))
    return "≈ " + t("duration_hours").format(hours=total // 60, minutes=total % 60)


def number(value: float | None, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.0f}" + ("\u00a0" + unit if unit else "")


def metric_text(controller, key: str) -> str:
    snapshot = controller.telemetry
    if key == "battery_level":
        return number(snapshot.soc_pct if snapshot else None, "%")
    if key == "input_power":
        return number(snapshot.input_w if snapshot else None, t("unit_watt"))
    if key == "output_power":
        return number(snapshot.output_w if snapshot else None, t("unit_watt"))
    if key == "estimated_runtime":
        return duration(controller.runtime)
    if key in {"minimum_discharge_level", "maximum_charge_level"}:
        return number(controller.minimum.value if key == "minimum_discharge_level" else controller.maximum.value, "%")
    return number(dict(snapshot.ports).get(key) if snapshot else None, t("unit_watt"))


def status_tooltip(controller) -> str:
    if controller.telemetry is None:
        last = t("never_updated")
    else:
        stamp = datetime.fromtimestamp(controller.telemetry.received_utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        last = t("last_update").format(timestamp=stamp)
    return t(display_connection(controller).value) + "\n" + last


def display_connection(controller):
    return ConnectionState.STALE if controller.connection == ConnectionState.LIVE and not controller.fresh else controller.connection


def status_color(controller) -> str:
    if controller.fresh:
        return COLORS["accent"]
    if controller.connection == ConnectionState.CONNECTING:
        return COLORS["input"]
    if controller.connection in {ConnectionState.LIVE, ConnectionState.STALE}:
        return COLORS["output"]
    if controller.connection in {ConnectionState.UNCONFIGURED, ConnectionState.NETWORK_UNAVAILABLE, ConnectionState.DEVICE_OFFLINE}:
        return COLORS["muted"]
    return COLORS["danger"]


def update_status(label: QLabel, controller) -> None:
    label.setText(status_text(controller))
    label.setStyleSheet(f"color: {status_color(controller)}; background: transparent;")
    label.setToolTip(status_tooltip(controller))
    label.setAccessibleName(status_tooltip(controller))


def status_text(controller):
    text = t(display_connection(controller).value)
    if controller.telemetry is not None and not controller.fresh:
        text += " · " + datetime.fromtimestamp(controller.telemetry.received_utc).astimezone().strftime("%H:%M")
    return text


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("hint")
    return label


class ActionButton(QPushButton):
    def enable_wrapping(self):
        self.setProperty("wrap_label", True)

    def wrapped_height(self, width):
        if not self.property("wrap_label") or width >= self.sizeHint().width():
            return self.sizeHint().height()
        scale = max(0.5, self.font().pixelSize() / 14)
        rect = self.rect()
        rect.setWidth(max(1, round(width - 40 * scale)))
        rect.setHeight(10000)
        text = self.fontMetrics().boundingRect(rect, Qt.TextFlag.TextWordWrap, self.text())
        return max(self.sizeHint().height(), text.height() + round(18 * scale))

    def paintEvent(self, event):
        if not self.property("wrap_label") or self.width() >= self.sizeHint().width():
            super().paintEvent(event)
            return
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        scale = max(0.5, self.font().pixelSize() / 14)
        painter.setPen(self.palette().buttonText().color())
        alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter if self.property("link") else Qt.AlignmentFlag.AlignCenter
        painter.drawText(self.rect().adjusted(round(20 * scale), 0, -round(20 * scale), 0), alignment | Qt.TextFlag.TextWordWrap, self.text())


def action(text: str, callback, parent=None) -> QPushButton:
    button = ActionButton(text, parent)
    button.clicked.connect(callback)
    return button


def app_icon() -> QIcon:
    return QIcon(str(Path(__file__).parent / "assets" / "app.ico"))


def effective_scale(window: QWidget, desired: float) -> float:
    return min(desired, max(0.5, 3 / max(1, window.devicePixelRatioF())))


def clamp_window(window: QWidget, above_taskbar: bool = False) -> None:
    screen = QApplication.screenAt(window.frameGeometry().center()) or QApplication.primaryScreen()
    if screen is None:
        return
    area = screen.geometry() if above_taskbar else screen.availableGeometry()
    size = window.size().boundedTo(area.size())
    if size != window.size():
        window.resize(size)
    x = min(max(window.x(), area.left()), max(area.left(), area.right() - window.width() + 1))
    y = min(max(window.y(), area.top()), max(area.top(), area.bottom() - window.height() + 1))
    window.move(QPoint(x, y))


class DragHeader(ValueLabel):
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.window().property("position_locked") and self.window().windowHandle() is not None:
            self.window().windowHandle().startSystemMove()
        super().mousePressEvent(event)


def font_family():
    family = TOKENS["type"]["fontFamily"]
    if not QFontDatabase.hasFamily(family):
        QFontDatabase.addApplicationFont(str(Path(__file__).parent / "assets" / "fonts" / "Inter.ttf"))
    if QFontDatabase.hasFamily(family):
        return family
    if not QFontDatabase.hasFamily("Segoe UI"):
        system = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
        if system:
            for filename in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf"):
                path = Path(system) / "Fonts" / filename
                if path.is_file():
                    QFontDatabase.addApplicationFont(str(path))
    return "Segoe UI" if QFontDatabase.hasFamily("Segoe UI") else QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def scaled_font(window: QWidget, scale: float) -> None:
    font = QFont(font_family())
    font.setPixelSize(round(14 * scale))
    font.setFeature(QFont.Tag("tnum"), 1)
    window.setFont(font)


class ScreenTracker(QObject):
    def __init__(self, window, callback):
        super().__init__(window)
        self._window = window
        self._handle = None
        self._screen = None
        self._stopped = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(callback)

    def schedule(self, *args):
        if not self._stopped:
            self._timer.start()

    def watch(self, *args):
        handle = self._window.windowHandle()
        if handle is not None and handle is not self._handle:
            if self._handle is not None:
                self._handle.screenChanged.disconnect(self.watch)
            self._handle = handle
            handle.screenChanged.connect(self.watch)
            handle.destroyed.connect(self._forget_handle)
        screen = self._window.screen()
        if screen is self._screen:
            return
        if self._screen is not None:
            self._screen.availableGeometryChanged.disconnect(self.schedule)
            self._screen.geometryChanged.disconnect(self.schedule)
            self._screen.logicalDotsPerInchChanged.disconnect(self.schedule)
        self._screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self.schedule)
            screen.geometryChanged.connect(self.schedule)
            screen.logicalDotsPerInchChanged.connect(self.schedule)
        self.schedule()

    def _forget_handle(self):
        self._handle = None

    def stop(self):
        self._stopped = True
        self._timer.stop()
