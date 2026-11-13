from PySide6.QtCore import QMimeData, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QDrag, QIcon
from PySide6.QtWidgets import QApplication, QCheckBox, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import METRICS
from ecoflow_monitor.ui.metrics import metric_tooltip
from ecoflow_monitor.ui.reference_style import icon_path


MIME_METRIC = "application/x-ecoflow-metric"
MODES = ("small", "large", "expanded", "off")


class MetricHandle(QToolButton):
    def __init__(self, row):
        super().__init__(row)
        self.row = row
        self.origin = None
        self.setIcon(QIcon(icon_path("drag")))
        self.setCheckable(True)
        self.setToolTip(t("move_metric"))
        self.setAccessibleName(t("move_metric") + ": " + t(row.key))
        self.setProperty("icon_button", True)
        self.clicked.connect(lambda: row.editor.select(row.key))

    def mousePressEvent(self, event):
        self.origin = event.position().toPoint() if event.button() == Qt.MouseButton.LeftButton else None
        self.row.editor.select(self.row.key)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.origin is not None and event.buttons() & Qt.MouseButton.LeftButton and (event.position().toPoint() - self.origin).manhattanLength() >= QApplication.startDragDistance():
            self.origin = None
            mime = QMimeData()
            mime.setData(MIME_METRIC, self.row.key.encode("utf-8"))
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.MoveAction)
        else:
            super().mouseMoveEvent(event)


class MetricVisibilityRow(QWidget):
    def __init__(self, key, editor):
        super().__init__(editor)
        self.key = key
        self.editor = editor
        self.setObjectName("metric_visibility_row")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setAcceptDrops(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        header = QHBoxLayout()
        self.handle = MetricHandle(self)
        header.addWidget(self.handle)
        label = QLabel(t(key), self)
        label.setWordWrap(True)
        label.setObjectName("row_title")
        header.addWidget(label, 1)
        outer.addLayout(header)
        self.controls = {}
        for mode, title in (("small", "widget_in_small"), ("large", "widget_in_large"), ("expanded", "widget_only_expanded")):
            control = QCheckBox(t(title), self)
            control.setAccessibleName(t(key) + ": " + t(title))
            control.toggled.connect(lambda checked, name=mode: self._changed(name, checked))
            self.controls[mode] = control
            outer.addWidget(control)

    def _changed(self, mode, checked):
        current = self.editor.modes[self.key]
        if checked:
            self.editor.set_mode(self.key, mode)
        elif current == mode:
            self.editor.set_mode(self.key, "large" if mode == "small" else "off")

    def refresh(self):
        mode = self.editor.modes[self.key]
        for name, control in self.controls.items():
            with QSignalBlocker(control):
                control.setChecked(mode == name or name == "large" and mode == "small")
            control.setEnabled(not (name == "large" and mode == "small"))

    def dragEnterEvent(self, event):
        key = bytes(event.mimeData().data(MIME_METRIC)).decode("utf-8", errors="replace") if event.mimeData().hasFormat(MIME_METRIC) else ""
        if key in self.editor.modes:
            event.acceptProposedAction()

    def dropEvent(self, event):
        key = bytes(event.mimeData().data(MIME_METRIC)).decode("utf-8", errors="replace")
        if self.editor.move_before(key, self.key, after=event.position().y() >= self.height() / 2):
            event.acceptProposedAction()


class MetricVisibilityEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.order = list(METRICS)
        self.modes = {key: "small" if key in METRICS[:3] else "large" for key in METRICS}
        self.selected = METRICS[0]
        self.rows = {key: MetricVisibilityRow(key, self) for key in METRICS}
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(8)
        self.reflow()

    def load(self, settings):
        preferences = settings.widget
        self.order = list(preferences.metric_order)
        for key in self.order:
            self.modes[key] = "small" if key in preferences.small_metrics else "large" if key in preferences.large_metrics else "expanded" if key in preferences.expanded_metrics else "off"
            self.rows[key].setToolTip(metric_tooltip(key))
        self.reflow()

    def reflow(self):
        for row in self.rows.values():
            self.content.removeWidget(row)
        for key in self.order:
            self.rows[key].refresh()
            self.content.addWidget(self.rows[key])

    def group(self, mode):
        mode = "small" if mode == "always" else mode
        return tuple(key for key in self.order if self.modes[key] == mode)

    def set_mode(self, key, mode):
        mode = "small" if mode == "always" else mode
        if mode not in MODES:
            raise ValueError("Unknown metric visibility")
        if mode != self.modes[key]:
            self.modes[key] = mode
            self.reflow()
            self.changed.emit()

    def select(self, key):
        self.selected = key
        for name, row in self.rows.items():
            row.handle.setChecked(name == key)

    def move_selected(self, direction):
        index = self.order.index(self.selected)
        if 0 <= index + direction < len(self.order):
            self.order[index], self.order[index + direction] = self.order[index + direction], self.order[index]
            self.reflow()
            self.changed.emit()

    def move_before(self, key, target, after=False):
        if key not in self.modes or target not in self.modes or key == target:
            return False
        self.order.remove(key)
        self.order.insert(self.order.index(target) + int(after), key)
        self.select(key)
        self.reflow()
        self.changed.emit()
        return True

    def refresh_tooltips(self, controller):
        for key, row in self.rows.items():
            row.setToolTip(metric_tooltip(key, controller))
