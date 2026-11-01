import sys

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizeGrip, QStyle, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor import __version__
from ecoflow_monitor.adapters.windows import acrylic, raise_without_focus
from ecoflow_monitor.domain import Availability
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import DragHeader, clamp_window, effective_scale, number, scaled_font, status_tooltip
from ecoflow_monitor.ui.flow import FlowWidget


STYLE = """
QWidget { color: #eaf2f2; background: #182125; }
QDialog { background: #182125; }
QLabel { background: transparent; }
QLabel#hint { color: #aec2c2; padding: 5px; }
QLineEdit, QComboBox, QAbstractSpinBox, QListWidget, QTableWidget { background: #10191d; color: #edf7f5; border: 1px solid #3a5155; border-radius: 4px; padding: 4px; selection-background-color: #285c4a; }
QPushButton, QToolButton { background: #263a3d; color: #e7f6f0; border: 1px solid #43605e; border-radius: 5px; padding: 6px; }
QPushButton:hover, QToolButton:hover { background: #34554f; }
QPushButton:checked { background: #146447; border-color: #5ae5a7; }
QPushButton:disabled, QToolButton:disabled { color: #7e9696; background: #202c2e; border-color: #2d4243; }
QTabWidget::pane { border: 1px solid #34494c; }
QTabBar::tab { background: #213034; color: #c1d2d0; padding: 9px; }
QTabBar::tab:selected { background: #36534d; color: #ffffff; }
QHeaderView::section { background: #253b3d; color: #d4e6e1; padding: 5px; border: 1px solid #3a5155; }
QCheckBox { spacing: 8px; background: transparent; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #66837b; background: #152622; border-radius: 4px; }
QCheckBox::indicator:checked { background: #1acb80; border-color: #74efb4; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QToolTip { color: #f3f7f5; background: #263a3a; border: 1px solid #55776b; }
#flow { background: transparent; }
"""


class MonitorWindow(QWidget):
    def __init__(self, controller, open_settings):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("EcoFlow Monitor")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(380, 320)
        self._warming = False
        self._quitting = False
        self._desired_visible = controller.settings.monitor_visible
        self._scale = effective_scale(self, controller.settings.scale)
        self._logical_size = None
        self._last_scaled_size = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.shell = QFrame(self)
        self.shell.setObjectName("monitor_shell")
        outer.addWidget(self.shell)
        layout = QVBoxLayout(self.shell)
        header = QHBoxLayout()
        self.title = DragHeader()
        header.addWidget(self.title, 1)
        self.refresh_button = self._tool(QStyle.StandardPixmap.SP_BrowserReload, controller.refresh)
        self.settings_button = self._tool(QStyle.StandardPixmap.SP_FileDialogDetailedView, open_settings)
        self.close_button = self._tool(QStyle.StandardPixmap.SP_TitleBarCloseButton, lambda: self.request_visibility(False))
        for button in (self.refresh_button, self.settings_button, self.close_button):
            header.addWidget(button)
        layout.addLayout(header)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("color: #ffcf8c;")
        layout.addWidget(self.notice)
        self.flow = FlowWidget(controller)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.flow)
        layout.addWidget(scroll, 1)
        self.limits = QLabel()
        self.limits.setWordWrap(True)
        self.limits.setStyleSheet("color: #a6c5bb;")
        layout.addWidget(self.limits)
        self.msi_section = QFrame(self.shell)
        msi = QVBoxLayout(self.msi_section)
        msi.setContentsMargins(0, 6, 0, 0)
        self.msi_status = QLabel()
        self.msi_status.setWordWrap(True)
        msi.addWidget(self.msi_status)
        row = QHBoxLayout()
        self.auto_toggle = QCheckBox()
        self.auto_toggle.toggled.connect(controller.set_auto)
        row.addWidget(self.auto_toggle)
        self.profile_buttons = []
        for slot in range(1, 6):
            button = QPushButton(str(slot))
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, value=slot: controller.request_profile(value))
            self.profile_buttons.append(button)
            row.addWidget(button, 1)
        container = QWidget()
        container.setLayout(row)
        profile_scroll = QScrollArea()
        profile_scroll.setWidgetResizable(True)
        profile_scroll.setWidget(container)
        profile_scroll.setMaximumHeight(85)
        self.profile_scroll = profile_scroll
        msi.addWidget(profile_scroll)
        self.profile_status = QLabel()
        self.profile_status.setWordWrap(True)
        msi.addWidget(self.profile_status)
        layout.addWidget(self.msi_section)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(QSizeGrip(self))
        layout.addLayout(footer)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(500)
        self._geometry_timer.timeout.connect(self._save_geometry)
        x, y, width, height = controller.settings.monitor_geometry
        self.resize(width, height)
        if x != -1 or y != -1:
            self.move(x, y)
        else:
            screen = self.screen()
            if screen:
                self.move(screen.availableGeometry().center() - self.rect().center())
        controller.changed.connect(self.refresh)
        controller.preferences_changed.connect(self.apply_appearance)
        self.apply_appearance()

    def _tool(self, icon, callback):
        button = QToolButton()
        button.setIcon(self.style().standardIcon(icon))
        button.clicked.connect(callback)
        return button

    def startup(self):
        if sys.platform == "win32" and self.controller.settings.transparency:
            self._warming = True
            self.show()
            QTimer.singleShot(80, self._finish_warmup)
        else:
            self.setVisible(self._desired_visible)

    def _finish_warmup(self):
        self.hide()
        self._warming = False
        QTimer.singleShot(30, lambda: self.setVisible(self._desired_visible) if not self._quitting else None)

    def request_visibility(self, visible):
        self._desired_visible = visible
        self.controller.update_preferences(monitor_visible=visible)
        if not self._warming:
            self.setVisible(visible)
            if visible:
                raise_without_focus(self)

    def apply_appearance(self):
        scale = effective_scale(self, self.controller.settings.scale)
        ratio = scale / self._scale
        if self._logical_size is None or self.size() != self._last_scaled_size:
            self._logical_size = (self.width() / self._scale, self.height() / self._scale)
        width, height = (round(value * scale) for value in self._logical_size)
        self._scale = scale
        scaled_font(self, scale)
        self.setMinimumSize(round(380 * scale), round(320 * scale))
        for button in (self.settings_button, self.refresh_button, self.close_button):
            button.setFixedSize(round(32 * scale), round(32 * scale))
            button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
        self.refresh()
        for button in (*self.profile_buttons, self.auto_toggle):
            button.ensurePolished()
        controls_height = max(button.sizeHint().height() for button in (*self.profile_buttons, self.auto_toggle))
        margins = self.profile_scroll.widget().layout().contentsMargins()
        self.profile_scroll.setFixedHeight(controls_height + margins.top() + margins.bottom() + self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent) + 6)
        self.flow.setMinimumSize(round(420 * scale), round(560 * scale))
        active = acrylic(self, self.controller.settings.transparency)
        background = "rgba(16, 24, 27, 155)" if active else "#10181b"
        self.shell.setStyleSheet(f"#monitor_shell {{ background: {background}; border: 1px solid #354e49; border-radius: 12px; }}")
        self.shell.layout().activate()
        self.layout().activate()
        if ratio != 1:
            self.resize(width, height)
        clamp_window(self)
        self._last_scaled_size = self.size()

    def refresh(self):
        controller = self.controller
        self.title.setText(t("app_title") + "  ·  " + __version__ + "\nDELTA 3 Max")
        self.refresh_button.setToolTip(t("refresh"))
        self.settings_button.setToolTip(t("settings"))
        self.close_button.setToolTip(t("hide"))
        self.status.setText(("● " if controller.fresh else "○ ") + t(controller.connection.value))
        self.status.setStyleSheet("color: " + ("#68e8ab;" if controller.fresh else "#b1bdbf;"))
        self.status.setToolTip(status_tooltip(controller))
        self.notice.setText(t(controller.notice) if controller.notice else "")
        self.notice.setVisible(bool(controller.notice))
        parts = []
        for key, limit in (("minimum_discharge_level", controller.minimum), ("maximum_charge_level", controller.maximum)):
            parts.append(t(key) + ": " + number(limit.value, "%") + " (" + t("source_" + limit.source) + ")")
        self.limits.setText("   ·   ".join(parts))
        self.msi_section.setVisible(controller.settings.msi_enabled)
        if controller.settings.msi_enabled:
            observation = controller.observation
            self.msi_status.setText(t(observation.detail or observation.availability.value))
            self.auto_toggle.blockSignals(True)
            self.auto_toggle.setText(t("automatic_mode"))
            self.auto_toggle.setChecked(controller.settings.auto_enabled)
            self.auto_toggle.setToolTip(t(controller.auto_reason))
            self.auto_toggle.blockSignals(False)
            for profile, button in zip(controller.profiles, self.profile_buttons):
                label = profile.label or str(profile.slot)
                button.setText(label + ("\n" + number(profile.power_pct, "%") if profile.power_pct is not None else ""))
                button.setChecked(observation.observed_slot == profile.slot)
                button.setEnabled(observation.availability == Availability.READY and not observation.detail and "dispatch" not in controller._active)
                button.setToolTip(t("profile_expected").format(power=number(profile.power_pct), source=t("source_" + profile.source)))
            observed = t("profile_observed").format(number=observation.observed_slot) if observation.observed_slot else t("profile_unknown")
            requested = " · " + t("profile_requested").format(number=controller.requested_slot) if controller.requested_slot else ""
            mode = " · " + t(controller.auto_reason) if controller.settings.auto_enabled else ""
            self.profile_status.setText(observed + requested + mode)
        self.flow.refresh()

    def _save_geometry(self):
        if self._quitting or self._warming:
            return
        geometry = (self.x(), self.y(), self.width(), self.height())
        if geometry != self.controller.settings.monitor_geometry:
            self.controller.update_preferences(monitor_geometry=geometry)

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_geometry_timer"):
            self._geometry_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_geometry_timer"):
            self._geometry_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        clamp_window(self)
        self.flow.refresh()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
        else:
            event.ignore()
            self.request_visibility(False)

    def event(self, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "flow"):
            QTimer.singleShot(0, self.apply_appearance)
        return super().event(event)
