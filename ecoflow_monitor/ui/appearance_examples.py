from html import escape

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QSizePolicy, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import DASHBOARD_SUMMARY_METRICS
from ecoflow_monitor.ui.common import action, duration, number
from ecoflow_monitor.ui.readings import TONES
from ecoflow_monitor.ui.tokens import COLORS, RADIUS, SPACE, surface


SAMPLE_PERCENTAGES = {"battery_level": 72, "minimum_discharge_level": 20, "maximum_charge_level": 90}
SAMPLE_POWER = {"input_power": 640, "output_power": 185}
HOVER_PHASE_SECONDS = 1.5


def sample_reading(key, *, with_label=False):
    if key == "estimated_runtime":
        value = duration(7 + 40 / 60)
    elif key in SAMPLE_PERCENTAGES:
        value = number(SAMPLE_PERCENTAGES[key], "%")
    else:
        value = number(SAMPLE_POWER[key], t("unit_watt"))
    return (f"{t(key)} {value}" if with_label else value, TONES.get(key, "text"))


class AppearanceExample(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("appearance_example")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        policy = self.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.Policy.Maximum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.sample = QLabel(self)
        self.sample.setObjectName("appearance_sample")
        self.sample.setProperty("untranslated", True)
        self.sample.setTextFormat(Qt.TextFormat.RichText)
        self.sample.setWordWrap(True)
        self.sample.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        box = QVBoxLayout(self)
        box.setContentsMargins(1, 1, 1, 1)
        box.addWidget(self.sample)

    def set_sample(self, lines, scale, *, opacity=1, backdrop=False, value_scale=None):
        value_scale = scale if value_scale is None else value_scale
        size = max(1, round(16 * value_scale))
        radius = max(1, round(RADIUS["control"] * scale))
        inset = max(1, round(SPACE[1] * scale))
        background = (f"qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {COLORS['canvas']},"
                      f"stop:0.49 {COLORS['canvas']},stop:0.50 {COLORS['border']},stop:1 {COLORS['border']})"
                      if backdrop else COLORS["surface"])
        style = (
            f"QFrame#appearance_example {{ background: {background}; border: 1px solid {COLORS['border']}; border-radius: {radius}px; }}"
            f"QLabel#appearance_sample {{ background: {surface(opacity)}; border-radius: {radius}px; padding: {inset}px; }}"
        )
        if self.styleSheet() != style:
            self.setStyleSheet(style)
        text = "<br>".join(" &nbsp; ".join(
            f'<span style="color:{COLORS[tone]}; font-size:{size}px;">{escape(value)}</span>'
            for value, tone in line
        ) for line in lines)
        if self.sample.text() != text:
            self.sample.setText(text)
        self.setAccessibleName(t("appearance_example"))


class SummaryOrderEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.items = QListWidget(self)
        self.items.setProperty("focus_target", True)
        self.items.setUniformItemSizes(True)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.up = action(t("move_up"), lambda: self.move(-1), self)
        self.down = action(t("move_down"), lambda: self.move(1), self)
        self.up.enable_wrapping()
        self.down.enable_wrapping()
        buttons = QHBoxLayout()
        buttons.addWidget(self.up)
        buttons.addWidget(self.down)
        buttons.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.items)
        layout.addLayout(buttons)
        self._list_height_timer = QTimer(self)
        self._list_height_timer.setSingleShot(True)
        self._list_height_timer.timeout.connect(self._fit_list)
        self.items.installEventFilter(self)
        self.items.currentRowChanged.connect(self._update_buttons)
        self.set_order(DASHBOARD_SUMMARY_METRICS)

    def order(self):
        return tuple(self.items.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.items.count()))

    def set_order(self, order):
        if self.order() != order:
            self.items.clear()
            for index, key in enumerate(order):
                self.items.addItem(t(key))
                self.items.item(index).setData(Qt.ItemDataRole.UserRole, key)
            self.items.setCurrentRow(0)
        self.refresh_labels()

    def refresh_labels(self):
        self.items.setAccessibleName(t("dashboard_summary_order"))
        for index, key in enumerate(self.order()):
            item = self.items.item(index)
            label = t(key)
            if item.text() != label:
                item.setText(label)
                self._list_height_timer.start(0)
        self._update_buttons(self.items.currentRow())

    def _fit_list(self):
        self.items.doItemsLayout()
        rows = sum(self.items.sizeHintForRow(index) for index in range(self.items.count()))
        height = rows + 2 * self.items.frameWidth()
        if self.items.minimumHeight() != height:
            self.items.setFixedHeight(height)

    def move(self, direction):
        index = self.items.currentRow()
        target = index + direction
        if index >= 0 and 0 <= target < self.items.count():
            self.items.insertItem(target, self.items.takeItem(index))
            self.items.setCurrentRow(target)
            self.changed.emit()

    def _update_buttons(self, index):
        self.up.setEnabled(index > 0)
        self.down.setEnabled(0 <= index < self.items.count() - 1)

    def eventFilter(self, watched, event):
        if watched is self.items and event.type() in (QEvent.Type.Show, QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._list_height_timer.start(0)
        return super().eventFilter(watched, event)


class HoverExamples(QObject):
    def __init__(self, parent, controller, scroll):
        super().__init__(parent)
        self.controller = controller
        self.viewport = scroll.viewport()
        self.examples = []
        self.animations = True
        self._active = None
        self._started = 0
        self._system_animations = QApplication.instance().property("system_animations")
        self._reconcile = QTimer(self)
        self._reconcile.setSingleShot(True)
        self._reconcile.timeout.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(round(HOVER_PHASE_SECONDS * 1000))
        self.timer.timeout.connect(self.refresh)
        self.viewport.installEventFilter(self)
        parent.installEventFilter(self)
        scroll.verticalScrollBar().valueChanged.connect(self._schedule)
        controller.system_activity_changed.connect(self._schedule)
        controller.changed.connect(self._system_motion_changed)

    def add(self, example, control):
        self.examples.append((example, control))
        example.installEventFilter(self)
        control.toggled.connect(self._schedule)

    def _system_motion_changed(self):
        enabled = QApplication.instance().property("system_animations")
        if enabled != self._system_animations:
            self._system_animations = enabled
            self._schedule()

    def _schedule(self, *_):
        self._reconcile.start(0)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.WindowStateChange):
            self._schedule()
        return False

    def refresh(self):
        now = self.controller.clock()
        moving = (self.animations and QApplication.instance().property("system_animations") is not False
                  and not self.controller.system_activity.paused)
        active = next((example for example, control in self.examples
                       if moving and control.isChecked() and example.isVisible()
                       and not example.window().isMinimized() and not example.visibleRegion().isEmpty()), None)
        if active is not self._active:
            self._active = active
            self._started = now
        if active is None:
            self.timer.stop()
        elif not self.timer.isActive():
            self.timer.start()
        reading = (sample_reading("input_power"),)
        for example, control in self.examples:
            scale = max(0.5, example.font().pixelSize() / 14)
            resting = ((t("appearance_resting"), "muted"), ("\u2002" * 7, "input"))
            hovered = ((t("appearance_hovered"), "muted"), *reading)
            if not control.isChecked():
                lines = (reading,)
            elif not moving:
                lines = (resting, hovered)
            else:
                revealed = example is active and int((now - self._started) / HOVER_PHASE_SECONDS) % 2
                lines = (hovered if revealed else resting,)
            example.set_sample(lines, scale)
