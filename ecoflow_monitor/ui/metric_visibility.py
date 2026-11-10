from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag, QIcon
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import METRICS
from ecoflow_monitor.ui.components import SelectField as QComboBox, SettingRow
from ecoflow_monitor.ui.metrics import metric_tooltip
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.tokens import COLORS


MIME_METRIC = "application/x-ecoflow-metric"
MODES = ("always", "expanded", "off")


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
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 4, 12, 4)
        self.handle = MetricHandle(self)
        outer.addWidget(self.handle)
        self.mode = QComboBox(self)
        self.mode.setAccessibleName(t(key))
        for name in MODES:
            self.mode.addItem(t("metric_mode_" + name), name)
        self.field = SettingRow(t(key), self.mode, self)
        outer.addWidget(self.field, 1)
        self.mode.currentIndexChanged.connect(lambda: editor.set_mode(key, self.mode.currentData()))

    def dragEnterEvent(self, event):
        key = bytes(event.mimeData().data(MIME_METRIC)).decode("utf-8", errors="replace") if event.mimeData().hasFormat(MIME_METRIC) else ""
        if key in self.editor.modes and self.editor.modes[key] == self.editor.modes[self.key]:
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
        self.modes = {key: "always" for key in METRICS}
        self.selected = METRICS[0]
        self.rows = {key: MetricVisibilityRow(key, self) for key in METRICS}
        self.groups = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(24)
        for mode, color in zip(MODES, ("accent", "input", "muted")):
            group = QWidget(self)
            layout = QVBoxLayout(group)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            label = QLabel(t("metric_group_" + mode), group)
            label.setObjectName("row_title")
            label.setStyleSheet(f"color: {COLORS[color]};")
            layout.addWidget(label)
            outer.addWidget(group)
            self.groups[mode] = layout
        self.reflow()

    def load(self, settings):
        self.order = list(settings.metric_order)
        for key in self.order:
            self.modes[key] = "always" if key in settings.mini_metrics else "expanded" if key in settings.mini_hidden_metrics else "off"
            row = self.rows[key]
            row.mode.blockSignals(True)
            row.mode.setCurrentIndex(MODES.index(self.modes[key]))
            row.mode.blockSignals(False)
            row.setToolTip(metric_tooltip(key))
        self.reflow()

    def reflow(self):
        for row in self.rows.values():
            for layout in self.groups.values():
                layout.removeWidget(row)
        for key in self.order:
            self.groups[self.modes[key]].addWidget(self.rows[key])

    def group(self, mode):
        return tuple(key for key in self.order if self.modes[key] == mode)

    def set_mode(self, key, mode):
        if mode != self.modes[key]:
            self.modes[key] = mode
            self.reflow()
            self.changed.emit()

    def select(self, key):
        self.selected = key
        for name, row in self.rows.items():
            row.handle.setChecked(name == key)

    def move_selected(self, direction):
        group = self.group(self.modes[self.selected])
        index = group.index(self.selected)
        if 0 <= index + direction < len(group):
            other = group[index + direction]
            first, second = self.order.index(self.selected), self.order.index(other)
            self.order[first], self.order[second] = self.order[second], self.order[first]
            self.reflow()
            self.changed.emit()

    def move_before(self, key, target, after=False):
        if key not in self.modes or key == target or self.modes[key] != self.modes[target]:
            return False
        group = list(self.group(self.modes[key]))
        group.remove(key)
        group.insert(group.index(target) + int(after), key)
        positions = [index for index, item in enumerate(self.order) if self.modes[item] == self.modes[key]]
        for index, item in zip(positions, group):
            self.order[index] = item
        self.select(key)
        self.reflow()
        self.changed.emit()
        return True

    def refresh_tooltips(self, controller):
        for key, row in self.rows.items():
            row.setToolTip(metric_tooltip(key, controller))
