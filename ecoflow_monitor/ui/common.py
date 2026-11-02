from datetime import datetime

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.domain import ConnectionState
from ecoflow_monitor.integrations import IntegrationProfile


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
    return f"{value:.0f}" + (" " + unit if unit else "")


def metric_text(controller, key: str) -> str:
    snapshot = controller.telemetry
    if key == "battery_level":
        return number(snapshot.soc_pct if snapshot else None, "%")
    if key == "input_power":
        return number(snapshot.input_w if snapshot else None, "W")
    if key == "output_power":
        return number(snapshot.output_w if snapshot else None, "W")
    if key == "estimated_runtime":
        return duration(controller.runtime)
    return number(controller.minimum.value if key == "minimum_discharge_level" else controller.maximum.value, "%")


def status_tooltip(controller) -> str:
    if controller.telemetry is None:
        last = t("never_updated")
    else:
        stamp = datetime.fromtimestamp(controller.telemetry.received_utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        last = t("last_update").format(timestamp=stamp)
    return t(controller.connection.value) + "\n" + last


def status_color(controller) -> str:
    if controller.fresh:
        return "#50e9a2"
    if controller.connection in {ConnectionState.UNCONFIGURED, ConnectionState.CONNECTING}:
        return "#ffc16c"
    return "#ff6060"


def update_status(label: QLabel, controller) -> None:
    label.setText("●")
    label.setStyleSheet(f"color: {status_color(controller)}; background: transparent;")
    label.setToolTip(status_tooltip(controller))
    label.setAccessibleName(status_tooltip(controller))


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("hint")
    return label


def action(text: str, callback, parent=None) -> QPushButton:
    button = QPushButton(text, parent)
    button.clicked.connect(callback)
    return button


def app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#75edb7"), 3))
    painter.setBrush(QColor("#182d28"))
    painter.drawRoundedRect(QRect(8, 15, 46, 36), 8, 8)
    painter.fillRect(QRect(54, 27, 4, 12), QColor("#75edb7"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#14d888"))
    painter.drawRoundedRect(QRect(14, 21, 28, 24), 3, 3)
    painter.end()
    return QIcon(pixmap)


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


class DragHeader(QLabel):
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.window().windowHandle() is not None:
            self.window().windowHandle().startSystemMove()
        super().mousePressEvent(event)


def scaled_font(window: QWidget, scale: float) -> None:
    font = QFont("Segoe UI")
    font.setPixelSize(round(13 * scale))
    window.setFont(font)
    window.setStyleSheet(f"QWidget {{ font-size: {round(13 * scale)}px; }} QCheckBox::indicator {{ width: {round(17 * scale)}px; height: {round(17 * scale)}px; }}")


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
