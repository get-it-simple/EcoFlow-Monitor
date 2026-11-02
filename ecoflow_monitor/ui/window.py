from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizeGrip, QStyle, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor import __version__
from ecoflow_monitor.adapters.windows import raise_without_focus
from ecoflow_monitor.ui.theme import SETTINGS_STYLE, bind_style, current_accent, monitor_style
from ecoflow_monitor.ui.reference_style import icon_path, toggle_switch_style
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import DragHeader, ScreenTracker, clamp_window, effective_scale, number, profile_tooltip, scaled_font
from ecoflow_monitor.ui.flow import FlowWidget


STYLE = SETTINGS_STYLE


class MonitorWindow(QWidget):
    def __init__(self, controller, open_settings):
        super().__init__()
        self.controller = controller
        self.setObjectName("monitor_window")
        self.setWindowTitle("EcoFlow Monitor")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(380, 320)
        self._quitting = False
        self._desired_visible = controller.settings.monitor_visible
        self._scale = effective_scale(self, controller.settings.scale)
        self._logical_size = None
        self._last_scaled_size = None
        self._layout_signature = None
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self.apply_appearance)
        self._screen_tracker = ScreenTracker(self, self.apply_appearance)
        outer = QVBoxLayout(self)
        self._outer_layout = outer
        outer.setContentsMargins(0, 0, 0, 0)
        self.shell = QFrame(self)
        self.shell.setObjectName("monitor_shell")
        outer.addWidget(self.shell)
        layout = QVBoxLayout(self.shell)
        self._shell_layout = layout
        header = QHBoxLayout()
        self.title = DragHeader()
        self.title.setObjectName("monitor_title")
        self.version_label = QLabel(__version__ + "  ·  DELTA 3 Max")
        self.version_label.setObjectName("monitor_version")
        title_box = QVBoxLayout()
        title_box.addWidget(self.title)
        title_box.addWidget(self.version_label)
        header.addLayout(title_box, 1)
        self.refresh_button = self._tool(QStyle.StandardPixmap.SP_BrowserReload, controller.refresh)
        self.settings_button = self._tool(QStyle.StandardPixmap.SP_FileDialogDetailedView, open_settings)
        self.close_button = self._tool(QStyle.StandardPixmap.SP_TitleBarCloseButton, lambda: self.request_visibility(False))
        self.settings_button.setIcon(QIcon(icon_path("settings")))
        self.close_button.setIcon(QIcon(icon_path("close")))
        for button in (self.refresh_button, self.settings_button, self.close_button):
            header.addWidget(button)
        layout.addLayout(header)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("color: #ffcf8c;")
        layout.addWidget(self.notice)
        self.flow = FlowWidget(controller)
        self.status = self.flow.status
        layout.addWidget(self.flow, 1)
        self.limits = QLabel()
        self.limits.setWordWrap(True)
        self.limits.setStyleSheet("color: #b7b7bd;")
        layout.addWidget(self.limits)
        self.msi_section = QFrame(self.shell)
        self.msi_section.setObjectName("monitor_msi")
        msi = QVBoxLayout(self.msi_section)
        msi.setContentsMargins(0, 6, 0, 0)
        self.integration_selector = QComboBox()
        self.integration_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.integration_selector.setMinimumContentsLength(15)
        self.integration_selector.currentIndexChanged.connect(self.refresh)
        msi.addWidget(self.integration_selector)
        self.msi_status = QLabel()
        self.msi_status.setWordWrap(True)
        msi.addWidget(self.msi_status)
        row = QHBoxLayout()
        self.auto_toggle = QCheckBox()
        self.auto_toggle.setObjectName("autoToggle")
        bind_style(self.auto_toggle, lambda color: toggle_switch_style("autoToggle", color))
        self.auto_toggle.toggled.connect(self.set_auto)
        row.addWidget(self.auto_toggle)
        self.profile_choice = QComboBox()
        self.profile_choice.currentIndexChanged.connect(lambda: self.profile_choice.setToolTip(self.profile_choice.currentData(Qt.ItemDataRole.ToolTipRole) or ""))
        self.profile_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.profile_choice.setMinimumContentsLength(12)
        row.addWidget(self.profile_choice, 1)
        self.apply_profile = QPushButton()
        self.apply_profile.clicked.connect(self.request_profile)
        row.addWidget(self.apply_profile)
        self.profile_buttons = [self.apply_profile]
        container = QWidget()
        self._profile_container = container
        self._profile_layout = row
        container.setObjectName("monitor_profiles")
        container.setLayout(row)
        profile_scroll = QScrollArea()
        profile_scroll.setWidgetResizable(True)
        profile_scroll.setFrameShape(QFrame.Shape.NoFrame)
        profile_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        profile_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        if self._quitting:
            return
        self.setVisible(self._desired_visible)
        if self._desired_visible:
            raise_without_focus(self)

    def request_visibility(self, visible):
        if self._quitting:
            return
        self._desired_visible = visible
        self.controller.update_preferences(monitor_visible=visible)
        self.setVisible(visible)
        if visible:
            raise_without_focus(self)

    def apply_appearance(self):
        if self._quitting:
            return
        self._layout_timer.stop()
        scale = effective_scale(self, self.controller.settings.scale)
        if self._logical_size is None or self.size() != self._last_scaled_size:
            self._logical_size = (self.width() / self._scale, self.height() / self._scale)
        self.refresh()
        screen = self.screen()
        area = screen.availableGeometry() if screen else self.geometry()
        self.setMinimumSize(0, 0)
        minimum = self.minimumSizeHint()
        for _ in range(6):
            self._set_scale(scale)
            minimum = self.minimumSizeHint()
            fit = min(1.0, area.width() / max(1, minimum.width()), area.height() / max(1, minimum.height()))
            if fit >= 1:
                break
            scale *= fit * 0.98
        self._scale = scale
        self.setMinimumSize(minimum)
        width, height = (round(value * scale) for value in self._logical_size)
        self.resize(max(minimum.width(), min(width, area.width())), max(minimum.height(), min(height, area.height())))
        clamp_window(self)
        self._last_scaled_size = self.size()
        self._layout_timer.stop()

    def _set_scale(self, scale):
        scaled_font(self, scale)
        self._scale = scale
        self.setStyleSheet(self.styleSheet() + monitor_style(scale, current_accent(), self.controller.settings.transparency, self.controller.settings.background_opacity))
        self._shell_layout.setContentsMargins(round(12 * scale), round(10 * scale), round(12 * scale), round(8 * scale))
        self._shell_layout.setSpacing(round(6 * scale))
        self._profile_layout.setSpacing(round(8 * scale))
        for button in (self.settings_button, self.refresh_button, self.close_button):
            button.setFixedSize(round(32 * scale), round(32 * scale))
            button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
        for button in (*self.profile_buttons, self.auto_toggle):
            button.ensurePolished()
        controls_height = max(button.sizeHint().height() for button in (*self.profile_buttons, self.auto_toggle, self.profile_choice))
        margins = self._profile_layout.contentsMargins()
        self.profile_scroll.setFixedHeight(controls_height + margins.top() + margins.bottom() + 2)
        self.profile_scroll.setMinimumWidth(self._profile_container.minimumSizeHint().width() + 2)
        self.flow.setMinimumSize(round(420 * scale), round(407 * scale))
        for widget in reversed(self.shell.findChildren(QWidget)):
            child_layout = widget.layout()
            if child_layout is not None:
                child_layout.invalidate()
                child_layout.activate()
        self._shell_layout.invalidate()
        self._shell_layout.activate()
        self._outer_layout.invalidate()
        self._outer_layout.activate()
        self.profile_scroll.setFixedHeight(max(self.profile_scroll.height(), self._profile_container.minimumSizeHint().height() + 2))

    def refresh(self):
        controller = self.controller
        self.title.setText(t("app_title"))
        self.refresh_button.setToolTip(t("refresh"))
        self.settings_button.setToolTip(t("settings"))
        self.close_button.setToolTip(t("hide"))
        self.notice.setText(t(controller.notice) if controller.notice else "")
        self.notice.setVisible(bool(controller.notice))
        parts = []
        for key, limit in (("minimum_discharge_level", controller.minimum), ("maximum_charge_level", controller.maximum)):
            parts.append(t(key) + ": " + number(limit.value, "%") + " (" + t("source_" + limit.source) + ")")
        self.limits.setText("   ·   ".join(parts))
        modules = tuple(module for module in controller.settings.integrations if module.enabled)
        self.msi_section.setVisible(bool(modules))
        choices = tuple((module.id, module.label) for module in modules)
        current_choices = tuple((self.integration_selector.itemData(index), self.integration_selector.itemText(index)) for index in range(self.integration_selector.count()))
        if choices != current_choices:
            selected = self.integration_selector.currentData()
            self.integration_selector.blockSignals(True)
            self.integration_selector.clear()
            for module in modules:
                self.integration_selector.addItem(module.label, module.id)
            self.integration_selector.setCurrentIndex(max(0, self.integration_selector.findData(selected)))
            self.integration_selector.blockSignals(False)
        integration_id = self.integration_selector.currentData()
        self.integration_selector.setToolTip(self.integration_selector.currentText())
        if integration_id:
            module = controller.module(integration_id)
            state = controller.integration_states[integration_id]
            observation = state.observation
            self.msi_status.setText(t(observation.detail or observation.availability))
            self.auto_toggle.blockSignals(True)
            self.auto_toggle.setText(t("automatic_mode"))
            self.auto_toggle.setChecked(module.auto_enabled)
            self.auto_toggle.setToolTip(t(state.auto_reason))
            self.auto_toggle.blockSignals(False)
            items = tuple((profile.id, profile.label) for profile in module.profiles)
            existing = tuple((self.profile_choice.itemData(index), self.profile_choice.itemText(index)) for index in range(self.profile_choice.count()))
            if items != existing:
                selected = self.profile_choice.currentData()
                self.profile_choice.clear()
                for profile in module.profiles:
                    self.profile_choice.addItem(profile.label, profile.id)
                self.profile_choice.setCurrentIndex(max(0, self.profile_choice.findData(selected)))
            self.apply_profile.setText(t("integration_retry") if state.result and not state.result.success else t("integration_apply"))
            for index, profile in enumerate(module.profiles):
                self.profile_choice.setItemData(index, profile_tooltip(profile), Qt.ItemDataRole.ToolTipRole)
            self.profile_choice.setToolTip(self.profile_choice.currentData(Qt.ItemDataRole.ToolTipRole) or "")
            self.apply_profile.setEnabled(observation.ready and not state.busy and bool(module.profiles))
            requested = next((profile.label for profile in module.profiles if profile.id == state.requested_profile), "")
            status = t("profile_requested").format(number=requested) if requested else t("integration_no_request")
            if state.busy:
                status += " · " + t("integration_busy")
            elif state.result and state.result.success:
                status += " · " + t("integration_command_succeeded" if state.result.completed else "integration_no_commands")
            if state.result and not state.result.success:
                status += "\n" + t("integration_failure").format(phase=t(state.result.phase) if state.result.phase else "", step=state.result.step, completed=state.result.completed, error=t(state.result.error))
                if state.result.exit_code is not None:
                    status += " · " + t("integration_exit_code").format(code=state.result.exit_code)
                if state.result.winerror is not None:
                    status += " · " + t("integration_windows_error").format(code=state.result.winerror)
            self.profile_status.setText(status)
            self.profile_status.setToolTip(status)
        self.flow.refresh()
        signature = (choices, self.notice.text(), self.title.text(), self.apply_profile.text())
        if signature != self._layout_signature:
            self._layout_signature = signature
            self._layout_timer.start()

    def set_auto(self, enabled):
        integration_id = self.integration_selector.currentData()
        if integration_id:
            self.controller.set_auto(integration_id, enabled)

    def request_profile(self):
        integration_id = self.integration_selector.currentData()
        profile_id = self.profile_choice.currentData()
        if integration_id and profile_id:
            self.controller.request_profile(integration_id, profile_id)

    def _save_geometry(self):
        if self._quitting:
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
        self._screen_tracker.watch()
        clamp_window(self)
        self.flow.refresh()

    def closeEvent(self, event):
        if self._quitting:
            self._layout_timer.stop()
            self._screen_tracker.stop()
            event.accept()
        else:
            event.ignore()
            self.request_visibility(False)

    def event(self, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "flow"):
            self._layout_timer.start()
        return super().event(event)
