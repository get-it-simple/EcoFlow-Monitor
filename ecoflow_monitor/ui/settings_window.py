from dataclasses import replace
from datetime import datetime
from collections.abc import Callable
import logging
from typing import cast

from PySide6.QtCore import QPoint, QSize, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QLayout, QLineEdit, QMessageBox, QScrollArea, QSizePolicy, QTabWidget, QVBoxLayout, QWidget

from ecoflow_monitor import i18n
from ecoflow_monitor.diagnostics import event, reveal_log
from ecoflow_monitor.adapters.credentials import CredentialError
from ecoflow_monitor.adapters.ecoflow import Credentials
from ecoflow_monitor.adapters.hotkeys import HotkeyError, validate_bindings
from ecoflow_monitor.integrations import hotkey_actions, hotkey_label
from ecoflow_monitor.ui.integrations import IntegrationsEditor
from ecoflow_monitor.i18n import t
from ecoflow_monitor.settings import AppSettings, ExportPreferences, HotkeyBinding, SettingsError
from ecoflow_monitor.polling import PollingGroup, PollingPreferences
from ecoflow_monitor.ui.common import action, effective_scale, hint, scaled_font
from ecoflow_monitor.ui.rules import optional_percent
from ecoflow_monitor.ui.components import ActionFooter, FormLayout as QFormLayout, LinkButton, NumberField, SelectField as QComboBox, SettingRow, Switch
from ecoflow_monitor.ui.metric_visibility import MetricVisibilityEditor
from ecoflow_monitor.ui.hotkey_editor import HotkeyComboEditor, hotkey_error_text
from ecoflow_monitor.ui.theme import bind_style, settings_style
from ecoflow_monitor.ui.percent_editor import PercentEditor
from ecoflow_monitor.ui.polling import telemetry_source_text
from ecoflow_monitor.ui.sections import CollapsibleSection
from ecoflow_monitor.ui.metrics import metric_tooltip
from ecoflow_monitor.ui.scrollbar import install_scrollbars
from ecoflow_monitor.ui.onepassword import OnePasswordPanel
from ecoflow_monitor.ui.credential_renewal import CredentialRenewalBanner
from ecoflow_monitor.ui.focus import FocusBinding
from ecoflow_monitor.ui.localization import TranslationBinding
from ecoflow_monitor.ui.dialogs import LanguageGuideDialog
from ecoflow_monitor.ui.device_profiles import DeviceProfilesPanel
from ecoflow_monitor.ui.appearance_examples import AppearanceExample, HoverExamples, SummaryOrderEditor, sample_reading
from ecoflow_monitor.ui.tokens import SPACE


SCALE_PREVIEW_DELAY_MS = 300


class SettingsWindow(QDialog):
    preview_changed = Signal(object)
    diagnostics_requested = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.controller = controller
        self._loading = True
        self.preflight: Callable[[AppSettings], Callable[[], None]] | None = None
        self._baseline = controller.settings
        self._dirty = False
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(80)
        self._draft_timer.timeout.connect(self._draft_changed)
        self.setWindowTitle(t("settings"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        scale = effective_scale(self, controller.settings.scale)
        self._settings_scale = scale
        scaled_font(self, scale)
        bind_style(self, lambda accent: settings_style(accent, self._settings_scale))
        self.resize(round(850 * scale), round(780 * scale))
        outer = QVBoxLayout(self)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        self._build_connection()
        integrations = self._tab(t("integrations"), show_title=False)
        self.integrations_section = CollapsibleSection(t("integrations"))
        integrations.addWidget(self.integrations_section)
        self.integrations_editor = IntegrationsEditor(self.integrations_section.body)
        self.integrations_section.content.addWidget(self.integrations_editor)
        self.integrations_section.content.setContentsMargins(0, 0, 0, 0)
        self.integrations_section.header.hide()
        self.integrations_section.set_expanded(True)
        self._build_appearance()
        self._build_hotkeys()
        self.integrations_editor.changed.connect(self._refresh_hotkeys)
        buttons = QHBoxLayout()
        buttons.addWidget(action(t("reset"), self.load_draft))
        buttons.addStretch()
        buttons.addWidget(action(t("cancel"), self.reject))
        save_button = action(t("save"), self.save)
        self.save_button = save_button
        save_button.setObjectName("primary")
        buttons.addWidget(save_button)
        self.footer = ActionFooter(buttons, self)
        outer.addWidget(self.footer)
        self.footer.configure(scale)
        controller.devices_ready.connect(self._devices)
        self.load_draft()
        controller.changed.connect(self._refresh_metric_tooltips)
        self.finished.connect(self._close_credentials)
        self.finished.connect(self._cancel_scale_preview)
        self.finished.connect(lambda: self.preview_changed.emit(None))
        self.error_label = QLabel(self)
        self.error_label.setObjectName("form_error")
        self.error_label.setWordWrap(True)
        self.error_label.setMinimumHeight(self.fontMetrics().height())
        outer.insertWidget(1, self.error_label)
        self._loading = False
        self._connect_draft()
        self._draft_changed()
        self._focus_binding = FocusBinding(self)
        self._translations = TranslationBinding(self)
        controller.runtime_preferences_changed.connect(self.sync_runtime_preferences)
        self._log_reveal_failed = False
        self.tabs.currentChanged.connect(self.refresh_visible_log_status)
        self.connection_tabs.currentChanged.connect(self.refresh_visible_log_status)
        controller.preferences_changed.connect(self.refresh_visible_log_status)
        if controller.diagnostics is not None:
            controller.diagnostics.status.changed.connect(self.refresh_visible_log_status, Qt.ConnectionType.QueuedConnection)
        self._refresh_log_status()
        install_scrollbars(self)

    def _tab(self, title: str, show_title=True):
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(24)
        if show_title:
            heading = QLabel(title, body)
            heading.setObjectName("page_title")
            layout.addWidget(heading)
        self.tabs.addTab(scroll, title)
        return layout

    def _build_connection(self):
        layout = self._tab(t("connection_settings"))
        self.connection_tabs = QTabWidget()
        self.connection_tabs.tabBar().setDrawBase(False)
        layout.addWidget(self.connection_tabs)
        bodies = {}
        for key in ("connection_devices", "connection_service", "connection_diagnostics"):
            body = QWidget()
            content = QVBoxLayout(body)
            content.setContentsMargins(0, 24, 0, 0)
            content.setSpacing(24)
            content.setAlignment(Qt.AlignmentFlag.AlignTop)
            content.setAlignment(Qt.AlignmentFlag.AlignTop)
            self.connection_tabs.addTab(body, t(key))
            bodies[key] = content
        layout.addStretch()
        layout = bodies["connection_service"]
        self.telemetry_transport = QComboBox()
        self.telemetry_transport.setAccessibleName(t("telemetry_transport"))
        self.telemetry_transport.addItem(t("telemetry_mqtt_with_fallback"), "mqtt_with_https_fallback")
        self.telemetry_transport.addItem(t("telemetry_https_only"), "https_only")
        layout.addWidget(SettingRow(t("telemetry_transport"), self.telemetry_transport))
        self.telemetry_source = hint("")
        layout.addWidget(self.telemetry_source)
        self.mqtt_renew_button = action(t("mqtt_access_renew"), self.controller.renew_mqtt_access)
        layout.addWidget(self.mqtt_renew_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.mqtt_renew_status = hint("")
        layout.addWidget(self.mqtt_renew_status)
        self.controller.changed.connect(self._refresh_telemetry_source)
        self.export_section = self._export_section()
        layout.addWidget(self.export_section)
        layout.addWidget(hint(t("connection_description")))
        self.credential_renewal = CredentialRenewalBanner(self.controller, self)
        layout.addWidget(self.credential_renewal)
        self.manual_credentials = QWidget(self)
        manual_layout = QVBoxLayout(self.manual_credentials)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.access = QLineEdit()
        self.secret = QLineEdit()
        self.access.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.access.setAccessibleName(t("access_key"))
        self.secret.setAccessibleName(t("secret_key"))
        form.addRow(t("access_key"), self.access)
        form.addRow(t("secret_key"), self.secret)
        manual_layout.addLayout(form)
        self.show_keys = QCheckBox(t("show_keys"))
        self.show_keys.toggled.connect(self._show_keys)
        manual_layout.addWidget(self.show_keys)
        manual_layout.addWidget(hint(t("credentials_hint")))
        self.onepassword = OnePasswordPanel(self._entered_credentials, self.manual_credentials, self)
        self.onepassword.session_loader = lambda fields, cancelled: self.controller.load_session_credentials(replace(self.controller.settings, **fields), cancelled)
        self.onepassword.external_runner = lambda operation, cancelled: self.controller.interactions.run_external(operation, cancelled) if self.controller.interactions is not None else operation()
        self.onepassword.source_changed.connect(self._credential_source_changed)
        self.onepassword.credentials_ready.connect(self._fill_credentials)
        self.onepassword.busy_changed.connect(self._credential_busy_changed)
        layout.addWidget(self.onepassword)
        layout.addStretch()
        layout = bodies["connection_devices"]
        self.discover_button = action(t("load_devices"), self.discover)
        self.devices = QComboBox()
        self.devices.setEditable(True)
        form2 = QFormLayout()
        form2.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        device_row = QHBoxLayout()
        device_row.addWidget(self.discover_button)
        device_row.addStretch()
        device_row.addWidget(self.devices)
        self.devices.setMinimumWidth(240)
        self.devices.setMaximumWidth(320)
        layout.addLayout(device_row)
        layout.addLayout(form2)
        layout.addWidget(hint(t("load_devices_hint")))
        self.device_profiles = DeviceProfilesPanel(self.controller, self)
        layout.addWidget(self.device_profiles)
        self.device_profiles.changed.connect(self._schedule_draft)
        self.devices.currentTextChanged.connect(self._selected_device_changed)
        self.finished.connect(self.device_profiles.close_operations)
        self.diagnostics_button = action(t("device_diagnostics_action"), self.diagnostics_requested.emit)
        bodies["connection_diagnostics"].addWidget(self.diagnostics_button)
        self.capacity = NumberField()
        self.capacity.setRange(1, 1000000)
        self.capacity.setDecimals(0)
        self.capacity.setSuffix(" " + t("unit_watt_hour"))
        self.minimum = optional_percent()
        self.maximum = optional_percent(100)
        self.minimum.setAccessibleName(t("manual_min_label"))
        self.maximum.setAccessibleName(t("manual_max_label"))
        form3 = QFormLayout()
        form3.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form3.addRow(t("capacity_wh"), self.capacity)
        form3.addRow(t("manual_min_label"), self.minimum)
        form3.addRow(t("manual_max_label"), self.maximum)
        for widget, key in ((self.capacity, "capacity_wh"), (self.minimum, "minimum_discharge_level"), (self.maximum, "maximum_charge_level")):
            tooltip = metric_tooltip(key)
            if widget is not self.capacity:
                tooltip += "\n" + t("manual_limit_hint")
            widget.setToolTip(tooltip)
            form3.labelForField(widget).setToolTip(tooltip)
        layout.addLayout(form3)
        self.range_error = QLabel()
        self.range_error.setObjectName("form_error")
        self.range_error.setWordWrap(True)
        self.range_error.setMinimumHeight(self.fontMetrics().height())
        layout.addWidget(self.range_error)
        layout.addWidget(hint(t("capacity_hint")))
        self.capacity_status = hint("")
        layout.addWidget(self.capacity_status)
        self.controller.changed.connect(self._refresh_capacity_status)
        layout.addWidget(hint(t("manual_limit_hint")))
        self.polling_section = CollapsibleSection(t("polling_title"), self)
        layout.addWidget(self.polling_section)
        self.polling_section.content.addWidget(hint(t("polling_description")))
        self.polling_fields = {}
        for name in ("primary", "ports", "limits"):
            title = QLabel(t("polling_" + name))
            title.setObjectName("section_title")
            self.polling_section.content.addWidget(title)
            controls = []
            for label, minimum, maximum in (("polling_interval", 5, 3600), ("polling_stale", 5, 86400)):
                control = NumberField()
                control.setDecimals(0)
                control.setRange(minimum, maximum)
                control.setSuffix(" " + t("unit_seconds"))
                control.setAccessibleName(t("polling_" + name) + ": " + t(label))
                self.polling_section.content.addWidget(SettingRow(t(label), control))
                controls.append(control)
            self.polling_fields[name] = tuple(controls)
        layout.addStretch()
        layout = bodies["connection_diagnostics"]
        logging_heading = QLabel(t("logging_title"))
        logging_heading.setObjectName("section_title")
        layout.addWidget(logging_heading)
        self.logging_mode = QComboBox()
        self.logging_mode.setAccessibleName(t("logging_mode"))
        for mode in ("off", "normal", "detailed"):
            self.logging_mode.addItem(t("logging_" + mode), mode)
        layout.addWidget(SettingRow(t("logging_mode"), self.logging_mode))
        layout.addWidget(hint(t("logging_description")))
        self.log_link = LinkButton(t("logging_reveal"))
        self.log_link.clicked.connect(self._reveal_log)
        self.log_link.setAccessibleName(t("logging_reveal"))
        layout.addWidget(self.log_link, 0, Qt.AlignmentFlag.AlignLeft)
        self.log_status = QLabel()
        self.log_status.setWordWrap(True)
        self.log_status.setObjectName("form_error")
        layout.addWidget(self.log_status)
        layout.addStretch()

    def _export_section(self):
        section = CollapsibleSection(t("export_title"), self)
        self.export_enabled = Switch()
        self.export_enabled.setAccessibleName(t("export_enabled"))
        section.content.addWidget(SettingRow(t("export_enabled"), self.export_enabled))
        self.export_status = hint("")
        section.content.addWidget(self.export_status)
        self.controller.changed.connect(self._refresh_export_status)
        self.export_controls = QWidget()
        layout = QVBoxLayout(self.export_controls)
        layout.setContentsMargins(0, 0, 0, 0)
        self.export_mode = QComboBox()
        self.export_mode.setAccessibleName(t("export_mode"))
        self.export_mode.addItem(t("export_on_updates"), "updates")
        self.export_mode.addItem(t("export_periodic"), "interval")
        self.export_port, self.export_interval = NumberField(), NumberField()
        for control, label, maximum in ((self.export_port, "export_port", 65535), (self.export_interval, "export_interval", 3600)):
            control.setDecimals(0)
            control.setRange(1, maximum)
            control.setAccessibleName(t(label))
        self.export_interval.setSuffix(" " + t("unit_seconds"))
        layout.addWidget(SettingRow(t("export_port"), self.export_port))
        layout.addWidget(SettingRow(t("export_mode"), self.export_mode))
        layout.addWidget(SettingRow(t("export_interval"), self.export_interval))
        self.export_switches = {}
        for name in ("raw", "commands", "show_serial", "show_private"):
            key = "export_" + name
            control = Switch()
            control.setAccessibleName(t(key))
            layout.addWidget(SettingRow(t(key), control))
            self.export_switches[name] = control
        layout.addWidget(hint(t("export_privacy_hint")))
        section.content.addWidget(self.export_controls)
        self.export_enabled.toggled.connect(self.export_controls.setEnabled)
        self.export_mode.currentIndexChanged.connect(lambda: self.export_interval.setEnabled(self.export_mode.currentData() == "interval"))
        return section

    def _refresh_export_status(self):
        service = self.controller.telemetry_export
        preferences = self.controller.settings.telemetry_export
        if service.running:
            status = t("export_running").format(port=preferences.port)
        elif service.error:
            status = t("export_port_error").format(port=preferences.port)
        else:
            status = t("export_paused" if preferences.enabled and self.controller.system_activity.paused else "export_off")
        self.export_status.setText(status)

    def _load_export_draft(self, settings):
        self.export_enabled.setChecked(settings.enabled)
        self.export_mode.setCurrentIndex(self.export_mode.findData(settings.mode))
        self.export_controls.setEnabled(settings.enabled)
        self.export_interval.setEnabled(settings.mode == "interval")
        self.export_port.setValue(settings.port)
        self.export_interval.setValue(settings.interval_seconds)
        for name, control in self.export_switches.items():
            control.setChecked(getattr(settings, name))
        self._refresh_export_status()

    def _export_preferences(self):
        return ExportPreferences(
            enabled=self.export_enabled.isChecked(), mode=self.export_mode.currentData(),
            port=int(self.export_port.value()), interval_seconds=int(self.export_interval.value()),
            **{name: control.isChecked() for name, control in self.export_switches.items()},
        )

    def _selected_device_changed(self):
        serial = self.devices.currentData() if self.devices.currentIndex() >= 0 and self.devices.currentText() == self.devices.itemText(self.devices.currentIndex()) else self.devices.currentText().strip()
        self.device_profiles.set_serial(serial or "")

    def _refresh_log_status(self):
        diagnostics = self.controller.diagnostics
        path = self.controller.store.path.parent / "logs" / "ecoflow-monitor.log"
        inaccessible = False
        try:
            exists = path.is_file()
        except OSError:
            exists = False
            inaccessible = True
        self.log_link.setEnabled(exists)
        self.log_link.setToolTip(t("logging_reveal") if exists else t("logging_file_missing"))
        key = "logging_unavailable" if inaccessible or (diagnostics is not None and diagnostics.failed) else "logging_reveal_failed" if self._log_reveal_failed else ""
        self.log_status.setText(t(key) if key else "")

    def refresh_visible_log_status(self, *_):
        if self.log_link.isVisible() and not self.controller.system_activity.paused:
            self._refresh_log_status()

    def _reveal_log(self):
        path = self.controller.store.path.parent / "logs" / "ecoflow-monitor.log"
        self._log_reveal_failed = not reveal_log(path)
        self._refresh_log_status()

    def _build_appearance(self):
        layout = self._tab(t("appearance_settings"))
        self.appearance_scroll = self.tabs.widget(self.tabs.count() - 1)
        self.appearance_examples = {}
        self.hover_examples = HoverExamples(self, self.controller, self.appearance_scroll)
        self.appearance_tabs = QTabWidget()
        self.appearance_tabs.tabBar().setDrawBase(False)
        layout.addWidget(self.appearance_tabs)
        bodies = []
        for label in ("appearance_general", "adaptive_widget"):
            page = QWidget()
            page.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            body = QWidget(page)
            page_layout.addWidget(body)
            content = QVBoxLayout(body)
            content.setContentsMargins(0, 24, 0, 0)
            content.setSpacing(24)
            self.appearance_tabs.addTab(page, t(label))
            bodies.append((body, content))

        def show_current_body(index):
            for tab_index, (body, _) in enumerate(bodies):
                body.setHidden(tab_index != index)

        self.appearance_tabs.currentChanged.connect(show_current_body)
        show_current_body(self.appearance_tabs.currentIndex())
        (_, general), (_, main) = bodies
        form = QFormLayout()
        self.language = QComboBox()
        self.language.addItem(t("language_system"), "system")
        for code in i18n.get_available_languages():
            self.language.addItem(i18n.language_display_name(code, i18n.get_current_language()), code)
        form.addRow(t("language"), self.language)
        general.addLayout(form)
        language_help = action(t("language_guide"), self._language_guide)
        general.addWidget(language_help, 0, Qt.AlignmentFlag.AlignLeft)
        self.scale = PercentEditor(50, 300)
        self.scale.setAccessibleName(t("scale"))
        self._appearance_example(general, SettingRow(t("scale"), self.scale), "dashboard_scale")
        self.dashboard_alpha = PercentEditor()
        self.dashboard_alpha.setAccessibleName(t("dashboard_opacity"))
        self._appearance_example(general, SettingRow(t("dashboard_opacity"), self.dashboard_alpha), "dashboard_opacity")
        self.animations = Switch(t("animations"))
        from ecoflow_monitor.adapters.startup import available
        self.start_with_windows = Switch(t("start_with_windows"))
        self.start_with_windows.setEnabled(available())
        self.start_with_windows.setToolTip(t("startup_description" if available() else "startup_packaged_only"))
        general.addWidget(SettingRow(t("start_with_windows"), self.start_with_windows))
        self.pause_on_display_off = Switch(t("pause_on_display_off"))
        general.addWidget(SettingRow(t("pause_on_display_off"), self.pause_on_display_off))
        self.monitor_labels_visible = Switch(t("monitor_labels_visible"))
        self.monitor_values_on_hover = Switch(t("monitor_values_on_hover"))
        self._appearance_example(general, SettingRow(t("monitor_labels_visible"), self.monitor_labels_visible), "dashboard_labels")
        self.hover_examples.add(
            self._appearance_example(general, SettingRow(t("monitor_values_on_hover"), self.monitor_values_on_hover), "dashboard_hover"),
            self.monitor_values_on_hover,
        )
        general.addWidget(SettingRow(t("animations"), self.animations))
        self.dashboard_summary_order = SummaryOrderEditor()
        general.addWidget(hint(t("dashboard_summary_order")))
        self._appearance_example(general, self.dashboard_summary_order, "dashboard_summary_order")
        general.addStretch(1)
        self.mini_enabled = Switch(t("adaptive_widget"))
        self.mini_above = Switch(t("mini_above_taskbar"))
        self.overlay_fields = {}
        self.materials = {}
        self.main_enabled = self.mini_enabled
        self.main_scale = PercentEditor(50, 300)
        self.main_alpha = PercentEditor()
        self.main_scale.setAccessibleName(t("widget_scale"))
        self.main_alpha.setAccessibleName(t("background_opacity"))
        main.addWidget(SettingRow(t("adaptive_widget"), self.main_enabled))
        main_material = QComboBox()
        for name in ("panel", "overlay", "clear"):
            main_material.addItem(t("material_" + name), name)
        self.materials["main"] = main_material
        main_material.setAccessibleName(t("widget_material"))
        main_material.currentIndexChanged.connect(lambda: self._material_changed("main"))
        self.main_alpha.valueChanged.connect(lambda: self._opacity_changed("main"))
        main.addWidget(SettingRow(t("widget_material"), main_material))
        self._appearance_example(main, SettingRow(t("background_opacity"), self.main_alpha), "widget_opacity")
        main.addWidget(hint(t("clear_readability")))
        main.addWidget(action(t("restore_overlay"), lambda: self._restore_overlay("main")), 0, Qt.AlignmentFlag.AlignLeft)
        self._appearance_example(main, SettingRow(t("widget_scale"), self.main_scale), "widget_scale")
        self.overlay_fields["main"] = {}
        for name, label in (("pinned", "pin_widget"), ("position_locked", "position_lock"), ("click_through", "click_through")):
            control = Switch(t(label))
            self.overlay_fields["main"][name] = control
            main.addWidget(SettingRow(t(label), control))
        main.addWidget(hint(t("click_through_hint")))
        self.mini_alpha = self.main_alpha
        self.mini_scale = self.main_scale
        self.materials["mini"] = self.materials["main"]
        self.overlay_fields["mini"] = self.overlay_fields["main"]
        for name, label in (("widget_device_name", "widget_device_name"), ("widget_connection_status", "widget_connection_status"), ("widget_labels", "monitor_labels_visible"), ("widget_values_hover", "monitor_values_on_hover")):
            control = Switch(t(label))
            setattr(self, name, control)
            example = self._appearance_example(main, SettingRow(t(label), control), name)
            if name == "widget_values_hover":
                self.hover_examples.add(example, control)
        main.addWidget(hint(t("widget_status_hint")))
        main.addWidget(SettingRow(t("mini_above_taskbar"), self.mini_above))
        self.dock = QComboBox()
        for key in ("", "left", "right", "top", "bottom"):
            self.dock.addItem(t("dock_" + (key or "free")), key)
        main.addWidget(SettingRow(t("docking"), self.dock))
        heading = QLabel(t("metric_visibility_title"))
        heading.setWordWrap(True)
        heading.setObjectName("section_title")
        main.addWidget(heading)
        main.addWidget(hint(t("widget_visibility_hint")))
        self.metrics = MetricVisibilityEditor()
        metric_buttons = QHBoxLayout()
        metric_buttons.addWidget(action(t("move_up"), lambda: self._move_metric(-1)))
        metric_buttons.addWidget(action(t("move_down"), lambda: self._move_metric(1)))
        metric_buttons.addStretch()
        metrics = QWidget()
        metric_controls = QVBoxLayout(metrics)
        metric_controls.setContentsMargins(0, 0, 0, 0)
        metric_controls.addWidget(self.metrics)
        metric_controls.addLayout(metric_buttons)
        self._appearance_example(main, metrics, "widget_metric_order")
        main.addStretch(1)
        self._held_scales: dict[PercentEditor, float] = {}
        self._scale_preview_timers: dict[PercentEditor, QTimer] = {}
        for control in (self.scale, self.main_scale):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setTimerType(Qt.TimerType.PreciseTimer)
            timer.setInterval(SCALE_PREVIEW_DELAY_MS)
            timer.timeout.connect(lambda editor=control: self._apply_scale_preview(editor))
            self._scale_preview_timers[control] = timer
            control.valueChanged.connect(lambda _, editor=control: self._scale_edited(editor))
            control.spin.lineEdit().textEdited.connect(lambda _, editor=control: self._schedule_scale_preview(editor))
            control.spin.editingFinished.connect(lambda editor=control: self._resume_scale_preview(editor))
            control.slider.sliderPressed.connect(lambda editor=control: self._schedule_scale_preview(editor))
            control.slider.sliderReleased.connect(lambda editor=control: self._resume_scale_preview(editor))
        for control in (self.dashboard_alpha, self.main_alpha):
            control.valueChanged.connect(self._appearance_input_changed)
        for control in (self.monitor_labels_visible, self.widget_labels, self.widget_device_name, self.widget_connection_status, self.animations):
            control.toggled.connect(self._appearance_input_changed)
        self.dashboard_summary_order.changed.connect(self._appearance_input_changed)
        self.metrics.changed.connect(self._appearance_input_changed)
        self.tabs.currentChanged.connect(self._appearance_input_changed)

    def _appearance_example(self, layout, control, key):
        section = QWidget()
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        policy.setHeightForWidth(True)
        section.setSizePolicy(policy)
        group = QVBoxLayout(section)
        group.setContentsMargins(0, 0, 0, 0)
        group.setSpacing(SPACE[1])
        group.addWidget(control)
        example = AppearanceExample()
        group.addWidget(example)
        layout.addWidget(section)
        self.appearance_examples[key] = example
        return example

    def _appearance_input_changed(self, *_):
        if not self._loading:
            self._refresh_appearance_examples()

    def _scale_edited(self, editor):
        if not self._loading:
            if self.tabs.currentWidget() is self.appearance_scroll:
                self._refresh_scale_example(editor)
            self._schedule_scale_preview(editor)

    def _refresh_scale_example(self, editor):
        key = "dashboard_scale" if editor is self.scale else "widget_scale"
        battery = (sample_reading("battery_level"),)
        self.appearance_examples[key].set_sample((battery,), self._settings_scale, value_scale=editor.value() / 100)

    def _schedule_scale_preview(self, editor):
        if self._loading:
            return
        current = self.controller.ui_settings
        self._held_scales.setdefault(editor, current.scale if editor is self.scale else current.widget.scale)
        self._scale_preview_timers[editor].stop()
        self._resume_scale_preview(editor)

    def _resume_scale_preview(self, editor):
        if self._loading or editor not in self._held_scales:
            return
        timer = self._scale_preview_timers[editor]
        if (not timer.isActive() and self.isVisible() and not self.isMinimized() and
                not editor.slider.isSliderDown() and editor.spin.hasAcceptableInput()):
            timer.start()

    def _apply_scale_preview(self, editor):
        self._held_scales.pop(editor, None)
        self._draft_timer.stop()
        self._draft_changed()

    def _cancel_scale_preview(self):
        self._draft_timer.stop()
        for timer in self._scale_preview_timers.values():
            timer.stop()
        self._held_scales.clear()

    def _refresh_appearance_examples(self):
        if self.tabs.currentWidget() is not self.appearance_scroll:
            return
        examples = self.appearance_examples
        scale = self._settings_scale
        battery = (sample_reading("battery_level"),)
        self._refresh_scale_example(self.scale)
        self._refresh_scale_example(self.main_scale)
        examples["dashboard_opacity"].set_sample((battery,), scale, opacity=self.dashboard_alpha.value() / 100, backdrop=True)
        examples["widget_opacity"].set_sample((battery,), scale, opacity=self.main_alpha.value() / 100, backdrop=True)
        for key, control in (("dashboard_labels", self.monitor_labels_visible), ("widget_labels", self.widget_labels)):
            examples[key].set_sample(((sample_reading("battery_level", with_label=control.isChecked()),),), scale)
        name = ((t("appearance_device_example"), "text"),)
        examples["widget_device_name"].set_sample((name, battery) if self.widget_device_name.isChecked() else (battery,), scale)
        status = ((f"● {t('connected')} · MQTT", "accent"),)
        examples["widget_connection_status"].set_sample((status, battery) if self.widget_connection_status.isChecked() else (battery,), scale)
        self.dashboard_summary_order.refresh_labels()
        order = tuple(sample_reading(key) for key in self.dashboard_summary_order.order())
        examples["dashboard_summary_order"].set_sample(((*order, *status),), scale)
        widget_order = tuple(sample_reading(key, with_label=self.widget_labels.isChecked())
                             for key in self.metrics.order if self.metrics.modes[key] in {"small", "large"})
        examples["widget_metric_order"].set_sample((widget_order or ((t("missing_data"), "muted"),),), scale)
        self.hover_examples.animations = self.animations.isChecked()
        self.hover_examples.refresh()

    def _language_guide(self):
        dialog = LanguageGuideDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def _restore_overlay(self, kind):
        self.materials[kind].setCurrentIndex(1)
        self._material_changed(kind)

    def _material_changed(self, kind):
        if self._loading:
            return
        material = self.materials[kind].currentData()
        value = {"panel": 100, "overlay": 82, "clear": 0}[material]
        getattr(self, kind + "_alpha").setValue(value)

    def _opacity_changed(self, kind):
        if self._loading:
            return
        value = getattr(self, kind + "_alpha").value()
        with QSignalBlocker(self.materials[kind]):
            self.materials[kind].setCurrentIndex(0 if value == 100 else 2 if value == 0 else 1)

    def _move_metric(self, direction):
        self.metrics.move_selected(direction)

    def _build_hotkeys(self):
        layout = self._tab(t("hotkeys_settings"))
        layout.addWidget(hint(t("hotkey_hint")))
        layout.setSpacing(32)
        self.hotkey_body = QWidget()
        self.hotkey_layout = QVBoxLayout(self.hotkey_body)
        self.hotkey_layout.setContentsMargins(0, 0, 0, 0)
        self.hotkey_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.addWidget(self.hotkey_body)
        layout.addStretch()
        self.hotkey_rows = {}
        self.hotkey_sections = {}

    def _refresh_hotkeys(self):
        try:
            modules = self.integrations_editor.modules()
        except (ValueError, AttributeError):
            return
        values = {binding.action: binding for binding in self.controller.settings.hotkeys}
        values.update({name: HotkeyBinding(name, editor.combo(), enabled.isChecked()) for name, (field, editor, enabled) in self.hotkey_rows.items()})
        expanded = {key: section.header.isChecked() for key, section in self.hotkey_sections.items()}
        self._clear_hotkeys()
        self.hotkey_rows = {}
        self.hotkey_sections = {}
        enabled_modules = {module.id for module in modules if module.enabled}
        for module in modules:
            section = CollapsibleSection(module.label, self.hotkey_body)
            self.hotkey_layout.addWidget(section)
            section.set_expanded(expanded.get(module.id, False))
            section.setVisible(module.id in enabled_modules)
            self.hotkey_sections[module.id] = section
        for name in hotkey_actions(modules):
            binding = values.get(name, HotkeyBinding(name))
            field = QWidget()
            row = QVBoxLayout(field)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            label = QLabel(hotkey_label(name, modules))
            label.setObjectName("row_title")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setToolTip(label.text())
            controls = QHBoxLayout()
            enabled = Switch(hotkey_label(name, modules))
            enabled.setChecked(binding.enabled)
            editor = HotkeyComboEditor(binding.combo)
            row.addWidget(label)
            controls.addWidget(editor, 1)
            controls.addWidget(enabled, 0, Qt.AlignmentFlag.AlignVCenter)
            editor.setEnabled(binding.enabled)
            enabled.toggled.connect(editor.setEnabled)
            editor.accept_combo = lambda combo, action_name=name: self._accept_hotkey(action_name, combo)
            row.addLayout(controls)
            row.setSpacing(8)
            target = self.hotkey_sections[name.split(":")[1]].content if ":" in name else self.hotkey_layout
            if ":" not in name:
                target.insertWidget(0 if name == "toggle_monitor" else 1, field)
            else:
                target.addWidget(field)
            self.hotkey_rows[name] = (field, editor, enabled)
            field.setVisible(":" not in name or name.split(":")[1] in enabled_modules)
        if not self._loading:
            self._connect_draft()

    def load_draft(self):
        was_loading = self._loading
        self._loading = True
        self._cancel_scale_preview()
        settings = self.controller.settings
        self._baseline = settings
        self.show_keys.setChecked(False)
        self.onepassword.load(settings)
        self.device_profiles.load(settings)
        self.access.clear()
        self.secret.clear()
        self.devices.clear()
        self._loaded_device_names = {}
        self.device_profiles.set_devices(self.controller.catalog or ())
        if self.controller.catalog:
            for device in self.controller.catalog:
                self.devices.addItem(device.name, device.serial)
                self._loaded_device_names[device.serial] = device.name
        if self.devices.findData(settings.serial) < 0:
            self.devices.addItem(self.controller.metadata.name(settings.serial), settings.serial)
        self.devices.setCurrentIndex(self.devices.findData(settings.serial))
        self._selected_device_changed()
        self.capacity.setValue(settings.capacity_wh)
        self._refresh_capacity_status()
        self.capacity.setSuffix(" " + t("unit_watt_hour"))
        self.minimum.setValue(-1 if settings.manual_min is None else settings.manual_min)
        self.maximum.setValue(-1 if settings.manual_max is None else settings.manual_max)
        if settings.manual_min is None:
            self.minimum.spin.setValue(settings.manual_min_retained)
        if settings.manual_max is None:
            self.maximum.spin.setValue(settings.manual_max_retained)
        self.integrations_editor.load(settings.integrations)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.language)))
        self.logging_mode.setCurrentIndex(max(0, self.logging_mode.findData(settings.logging_mode)))
        self._load_export_draft(settings.telemetry_export)
        self.telemetry_transport.setCurrentIndex(self.telemetry_transport.findData(settings.telemetry_transport))
        self._refresh_telemetry_source()
        for name, (interval, stale) in self.polling_fields.items():
            group = getattr(settings.polling, name)
            interval.setValue(group.interval_seconds)
            stale.setValue(group.stale_after_seconds)
            for control, label in ((interval, "polling_interval"), (stale, "polling_stale")):
                control.setSuffix(" " + t("unit_seconds"))
                control.setAccessibleName(t("polling_" + name) + ": " + t(label))
        self.scale.setValue(settings.scale * 100)
        self.dashboard_alpha.setValue(settings.dashboard_opacity * 100)
        self.dashboard_summary_order.set_order(settings.dashboard_summary_order)
        self.animations.setChecked(settings.animations)
        self.start_with_windows.setChecked(settings.start_with_windows)
        self.pause_on_display_off.setChecked(settings.pause_on_display_off)
        self.main_enabled.setChecked(settings.widget.enabled)
        self.main_scale.setValue(settings.widget.scale * 100)
        self.main_alpha.setValue(settings.widget.opacity * 100)
        self.monitor_labels_visible.setChecked(settings.monitor_labels_visible)
        self.monitor_values_on_hover.setChecked(settings.monitor_values_on_hover)
        self.mini_above.setChecked(settings.widget.above_taskbar)
        self.widget_device_name.setChecked(settings.widget.show_device_name)
        self.widget_connection_status.setChecked(settings.widget.show_connection_status)
        self.widget_labels.setChecked(settings.widget.labels_visible)
        self.widget_values_hover.setChecked(settings.widget.values_on_hover)
        for kind in ("main",):
            preferences = settings.widget.overlay
            for name, control in self.overlay_fields[kind].items():
                control.setChecked(getattr(preferences, name))
            opacity = settings.widget.opacity
            self.materials[kind].setCurrentIndex(0 if opacity == 1 else 2 if opacity == 0 else 1)
        self.dock.setCurrentIndex(max(0, self.dock.findData(settings.widget.dock)))
        self.metrics.load(settings)
        self.metrics.refresh_tooltips(self.controller)
        self._refresh_appearance_examples()
        self.hotkey_rows = self._reset_hotkeys()
        self._loading = was_loading
        if not self._loading:
            self.preview_changed.emit(None)
            self._connect_draft()
            self._draft_timer.start()

    def _reset_hotkeys(self):
        self._clear_hotkeys()
        self.hotkey_rows = {}
        self.hotkey_sections = {}
        self._refresh_hotkeys()
        return self.hotkey_rows

    def _refresh_metric_tooltips(self):
        self.metrics.refresh_tooltips(self.controller)

    def _accept_hotkey(self, name, combo):
        from ecoflow_monitor.adapters.hotkey_binding import combo_to_hotkey
        try:
            candidate = combo_to_hotkey(combo)
            return all(action_name == name or not enabled.isChecked() or not editor.combo() or combo_to_hotkey(editor.combo()) != candidate for action_name, (_, editor, enabled) in self.hotkey_rows.items())
        except ValueError:
            return False

    def _clear_hotkeys(self):
        while self.hotkey_layout.count():
            item = self.hotkey_layout.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _entered_credentials(self, *, viewed=False):
        source = self.onepassword.source.tabData(self.onepassword.source.currentIndex()) if viewed else self.onepassword.credential_source
        if source == "1password":
            return self.onepassword.credentials
        access, secret = self.access.text().strip(), self.secret.text().strip()
        if bool(access) != bool(secret):
            raise CredentialError("credentials_required")
        return Credentials(access, secret) if access else None

    def _credential_source_changed(self):
        self.show_keys.setChecked(False)
        self.manual_credentials.setVisible(not self.onepassword.uses_onepassword)
        self._schedule_draft()

    def _show_keys(self, visible):
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.access.setEchoMode(mode)
        self.secret.setEchoMode(mode)

    def _fill_credentials(self, credentials):
        self._schedule_draft()

    def _credential_busy_changed(self, busy):
        if hasattr(self, "save_button"):
            self.save_button.setEnabled(not busy and self._dirty)
        if hasattr(self, "discover_button"):
            self.discover_button.setEnabled(not busy)
        if not busy and hasattr(self, "error_label"):
            self._draft_timer.stop()
            self._draft_changed()

    def _close_credentials(self):
        self.controller.discard_discovery()
        self.onepassword.cancel(show_status=False)
        self.onepassword.credentials = None
        self.onepassword.create_access.clear()
        self.onepassword.create_secret.clear()
        self.onepassword.show_references.setChecked(False)
        self.onepassword.show_create_keys.setChecked(False)
        self.access.clear()
        self.secret.clear()

    def discover(self):
        if self.onepassword.busy:
            return
        if self.onepassword.uses_onepassword:
            try:
                fields = self.onepassword.settings_fields(viewed=True)
            except ValueError:
                self.onepassword.show_status(t("onepassword_reference_error"))
                return
            if self.controller.settings.credential_source == "1password" and (fields["onepassword_access_ref"], fields["onepassword_secret_ref"]) == (self.controller.settings.onepassword_access_ref, self.controller.settings.onepassword_secret_ref):
                self.discover_button.setEnabled(False)
                self.controller.discover()
                return
            if self.onepassword.credentials is None:
                self.onepassword.show_status(t("onepassword_load_first"))
                return
        try:
            credentials = self._entered_credentials(viewed=True)
        except CredentialError as error:
            QMessageBox.warning(self, t("error_title"), t(str(error)))
            return
        self.discover_button.setEnabled(False)
        self.controller.discover(credentials, replace(self.controller.settings, **self.onepassword.settings_fields(viewed=True)))

    def _devices(self, devices):
        self.discover_button.setEnabled(not self.onepassword.busy)
        if not devices:
            self.onepassword.show_status(t(self.controller.notice or self.controller.connection.value))
            return
        self.device_profiles.set_devices(devices)
        selected = self.devices.currentData() or self.controller.settings.serial
        if self.devices.isEditable() and self.devices.currentIndex() >= 0 and self.devices.currentText() != self.devices.itemText(self.devices.currentIndex()):
            selected = self.devices.currentText().strip()
        self.devices.clear()
        self._loaded_device_names = {device.serial: device.name for device in devices}
        for device in devices:
            self.devices.addItem(device.name, device.serial)
        index = self.devices.findData(selected)
        if index >= 0:
            self.devices.setCurrentIndex(index)
        elif selected:
            self.devices.addItem(self.controller.metadata.name(selected), selected)
            self.devices.setCurrentIndex(self.devices.count() - 1)

    def _refresh_telemetry_source(self):
        source = telemetry_source_text(self.controller.telemetry_source)
        self.telemetry_source.setText(source)
        self.telemetry_source.setVisible(bool(source))
        mqtt_enabled = self.controller.settings.telemetry_transport == "mqtt_with_https_fallback"
        self.mqtt_renew_button.setVisible(mqtt_enabled)
        self.mqtt_renew_button.setEnabled(self.controller.can_renew_mqtt_access)
        status = self.controller.mqtt_access_status
        self.mqtt_renew_status.setText(t(status) if status else "")
        self.mqtt_renew_status.setVisible(mqtt_enabled and bool(status))

    def _refresh_capacity_status(self):
        if not hasattr(self, "capacity_status"):
            return
        estimate = self.controller.capacity_estimate
        self.capacity_status.setText(t("capacity_estimate_ready").format(
            value=round(estimate.watt_hours), count=estimate.count, spread=round(estimate.spread_pct),
            date=datetime.fromtimestamp(estimate.updated_utc).astimezone().strftime("%Y-%m-%d %H:%M"))
            if estimate is not None else t("capacity_estimate_waiting"))
        self.diagnostics_button.setEnabled(bool(self.controller.settings.serial) and not self.controller._stopped)

    def gather(self, validate_paths=True):
        current = self.controller.settings
        serial = self.devices.currentData() if self.devices.currentIndex() >= 0 and self.devices.currentText() == self.devices.itemText(self.devices.currentIndex()) else self.devices.currentText().strip()
        hotkeys = tuple(HotkeyBinding(action_name, editor.combo(), enabled.isChecked()) for action_name, (field, editor, enabled) in self.hotkey_rows.items())
        previous_actions = {binding.action for binding in self._baseline.hotkeys}
        hotkeys = tuple(binding for binding in hotkeys if binding.action in previous_actions or binding.combo or binding.enabled)
        modules = self.integrations_editor.modules(validate_paths)
        baseline = {module.id: module for module in self._baseline.integrations}
        latest = {module.id: module for module in current.integrations}
        merged = []
        for module in modules:
            if module.id in baseline and module.id in latest:
                edits = {name: getattr(module, name) for name in module.__dataclass_fields__ if getattr(module, name) != getattr(baseline[module.id], name)}
                module = replace(latest[module.id], **edits)
            merged.append(module)
        merged.extend(module for module in current.integrations if module.id not in baseline and module.id not in {item.id for item in modules})
        draft = replace(
            self._baseline, serial=serial or "", capacity_wh=self.capacity.value(),
            **self.onepassword.settings_fields(),
            **self.device_profiles.settings_fields(),
            manual_min=self.minimum.value() if self.minimum.value() >= 0 else None,
            manual_max=self.maximum.value() if self.maximum.value() >= 0 else None,
            integrations=tuple(merged),
            hotkeys=hotkeys, language=self.language.currentData(), scale=self.scale.value() / 100,
            logging_mode=self.logging_mode.currentData(),
            telemetry_export=self._export_preferences(),
            telemetry_transport=self.telemetry_transport.currentData(),
            polling=PollingPreferences(**{name: PollingGroup(int(interval.value()), int(stale.value())) for name, (interval, stale) in self.polling_fields.items()}),
            animations=self.animations.isChecked(),
            start_with_windows=self.start_with_windows.isChecked(),
            pause_on_display_off=self.pause_on_display_off.isChecked(),
            widget=self._widget_draft(),
            monitor_labels_visible=self.monitor_labels_visible.isChecked(),
            monitor_values_on_hover=self.monitor_values_on_hover.isChecked(),
            dashboard_opacity=self._baseline.dashboard_opacity if self.dashboard_alpha.value() == round(self._baseline.dashboard_opacity * 100) else self.dashboard_alpha.value() / 100,
            dashboard_summary_order=self.dashboard_summary_order.order(),
            manual_min_retained=self.minimum.spin.value(), manual_max_retained=self.maximum.spin.value(),
        )
        changes = {name: getattr(draft, name) for name in draft.__dataclass_fields__ if getattr(draft, name) != getattr(self._baseline, name)}
        return replace(current, **changes)

    def _overlay_draft(self, kind):
        baseline = self._baseline.widget.overlay
        current = self.controller.settings.widget.overlay
        changes = {name: control.isChecked() for name, control in self.overlay_fields[kind].items() if control.isChecked() != getattr(baseline, name)}
        return replace(current, **changes)

    def _widget_draft(self):
        baseline = self._baseline.widget
        draft = replace(
            baseline, enabled=self.main_enabled.isChecked(), scale=self.main_scale.value() / 100,
            opacity=baseline.opacity if self.main_alpha.value() == round(baseline.opacity * 100) else self.main_alpha.value() / 100,
            overlay=self._overlay_draft("main"), dock=self.dock.currentData(), above_taskbar=self.mini_above.isChecked(),
            show_device_name=self.widget_device_name.isChecked(), show_connection_status=self.widget_connection_status.isChecked(),
            labels_visible=self.widget_labels.isChecked(), values_on_hover=self.widget_values_hover.isChecked(),
            small_metrics=self.metrics.group("small"), large_metrics=tuple(key for key in self.metrics.order if self.metrics.modes[key] in {"small", "large"}),
            expanded_metrics=self.metrics.group("expanded"), metric_order=tuple(self.metrics.order),
        )
        changes = {name: getattr(draft, name) for name in draft.__dataclass_fields__ if getattr(draft, name) != getattr(baseline, name)}
        return replace(self.controller.settings.widget, **changes)

    def _connect_draft(self):
        for widget in self.findChildren(QWidget):
            if widget.property("draft_connected"):
                continue
            widget.setProperty("draft_connected", True)
            for name in ("textChanged", "valueChanged", "currentIndexChanged", "toggled", "changed", "comboChanged", "itemChanged"):
                signal = getattr(widget, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(self._schedule_draft)

    def _schedule_draft(self, *args):
        if not self._loading:
            self._draft_timer.start()

    def _draft_changed(self):
        if self._loading:
            return
        invalid_range = self.minimum.value() >= 0 and self.maximum.value() >= 0 and self.minimum.value() >= self.maximum.value()
        self.range_error.setText(t("invalid_settings") if invalid_range else "")
        try:
            draft = self.gather(False)
            draft.validate()
            validate_bindings(draft.hotkeys)
            self._entered_credentials()
            self._dirty = draft != self.controller.settings or self.device_profiles.catalog_changed or (draft.credential_source == "windows" and bool(self.access.text().strip() or self.secret.text().strip()))
            recording = any(editor.recording or editor.conflicted for _, editor, _ in self.hotkey_rows.values())
            self.save_button.setEnabled(self._dirty and not self.onepassword.busy and not self.device_profiles.busy and not recording)
            self.error_label.clear()
            self._show_hotkey_issues(())
            if self._dirty:
                preview = replace(draft, scale=self._held_scales.get(self.scale, draft.scale),
                                  widget=replace(draft.widget, scale=self._held_scales.get(self.main_scale, draft.widget.scale)))
                self.preview_changed.emit(None if preview == self.controller.settings else preview)
            else:
                self.preview_changed.emit(None)
        except (ValueError, OSError, SettingsError, CredentialError) as error:
            self._dirty = True
            self.save_button.setEnabled(False)
            self._show_hotkey_issues(error.issues if isinstance(error, HotkeyError) else ())
            self.error_label.setText(hotkey_error_text(error, self.integrations_editor.modules()) if isinstance(error, HotkeyError) else t(str(error)) if str(error).startswith("polling_") else t("invalid_settings"))
        self.error_label.setVisible(bool(self.error_label.text()) and (not hasattr(self, "navigation") or self.tabs.currentIndex() != 0))

    def _metric_group(self, mode):
        return self.metrics.group(mode)

    def save(self):
        if self.onepassword.busy or self.device_profiles.busy or any(editor.recording or editor.conflicted for _, editor, _ in self.hotkey_rows.values()):
            return
        for editor in self._scale_preview_timers:
            editor.spin.interpretText()
        rollback: Callable[[], None] | None = None
        try:
            draft = self.gather()
            validate_bindings(draft.hotkeys)
            if self.preflight is not None:
                rollback = self.preflight(draft)
            self.controller.apply_settings(draft, self._entered_credentials(), profile_catalog=self.device_profiles.catalog)
            name = self._loaded_device_names.get(draft.serial)
            if name:
                self.controller.metadata.remember_name(draft.serial, name)
                self.controller._save_metadata()
        except (ValueError, OSError, SettingsError, CredentialError) as error:
            rollback_failed = False
            if rollback is not None:
                try:
                    rollback()
                except (OSError, ValueError) as rollback_error:
                    rollback_failed = True
                    event("settings_rollback_failed", level=logging.ERROR, error_type=type(error).__name__, rollback_type=type(rollback_error).__name__)
            key = str(error)
            if isinstance(error, HotkeyError):
                message = hotkey_error_text(error, self.integrations_editor.modules())
                self._show_hotkey_issues(error.issues)
                self.error_label.setText(message)
                self.error_label.show()
            else:
                message = t(key) if t(key) != key else t("invalid_settings")
            if rollback_failed:
                message += "\n\n" + t("settings_rollback_failed")
            QMessageBox.warning(self, t("error_title"), message)
            return
        self.preview_changed.emit(None)
        self._translations.refresh()
        self.load_draft()
        self.error_label.setText(t("settings_saved"))

    def _show_hotkey_issues(self, issues):
        for name, (_, editor, _) in self.hotkey_rows.items():
            matching = tuple(issue for issue in issues if name in (issue.action, issue.other_action))
            editor.set_validation_error(hotkey_error_text(HotkeyError(matching), self.integrations_editor.modules()) if matching else "")

    def sync_runtime_preferences(self, changes):
        if self._loading or "widget" not in changes:
            return
        self._loading = True
        try:
            current = changes["widget"]
            fields = changes.get("widget_fields") or tuple(name for name in current.__dataclass_fields__ if getattr(current, name) != getattr(self._baseline.widget, name))
            if "enabled" in fields:
                with QSignalBlocker(self.main_enabled):
                    self.main_enabled.setChecked(current.enabled)
            baseline_edits = {name: getattr(current, name) for name in fields}
            for name, control in (("scale", self.main_scale), ("above_taskbar", self.mini_above), ("show_device_name", self.widget_device_name), ("show_connection_status", self.widget_connection_status), ("labels_visible", self.widget_labels), ("values_on_hover", self.widget_values_hover)):
                if name in fields:
                    with QSignalBlocker(control):
                        if name == "scale":
                            self._held_scales.pop(self.main_scale, None)
                            self._scale_preview_timers[self.main_scale].stop()
                            control.setValue(current.scale * 100)
                        else:
                            control.setChecked(getattr(current, name))
            if "dock" in fields:
                with QSignalBlocker(self.dock):
                    self.dock.setCurrentIndex(max(0, self.dock.findData(current.dock)))
            metric_fields = {"small_metrics", "large_metrics", "expanded_metrics", "metric_order"} & set(fields)
            if metric_fields:
                draft = replace(self._widget_draft(), **{name: getattr(current, name) for name in metric_fields})
                self.metrics.load(replace(self.controller.settings, widget=draft))
            if "overlay" in fields:
                overlay_fields = changes.get("widget_overlay_fields") or tuple(name for name in current.overlay.__dataclass_fields__ if getattr(current.overlay, name) != getattr(self._baseline.widget.overlay, name))
                for name, control in self.overlay_fields["main"].items():
                    if name in overlay_fields:
                        with QSignalBlocker(control):
                            control.setChecked(getattr(current.overlay, name))
                baseline_edits["overlay"] = replace(self._baseline.widget.overlay, **{name: getattr(current.overlay, name) for name in overlay_fields})
            if "opacity" in fields:
                with QSignalBlocker(self.main_alpha):
                    self.main_alpha.setValue(current.opacity * 100)
                with QSignalBlocker(self.materials["main"]):
                    self.materials["main"].setCurrentIndex(0 if current.opacity == 1 else 2 if current.opacity == 0 else 1)
            self._baseline = replace(self._baseline, widget=replace(self._baseline.widget, **baseline_edits))
        finally:
            self._loading = False
        self._refresh_appearance_examples()
        self._draft_changed()

    def sync_runtime_overlays(self):
        self._loading = True
        for kind in ("main",):
            baseline = self._baseline.widget.overlay
            current = self.controller.settings.widget.overlay
            for name, control in self.overlay_fields[kind].items():
                if control.isChecked() == getattr(baseline, name):
                    control.setChecked(getattr(current, name))
            self._baseline = replace(self._baseline, widget=replace(self._baseline.widget, overlay=current))
        self._loading = False

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_visible_log_status()
        self.integrations_section.set_expanded(True)
        self._fit_content()
        for editor in self._held_scales:
            self._resume_scale_preview(editor)

    def hideEvent(self, event):
        for timer in self._scale_preview_timers.values():
            timer.stop()
        super().hideEvent(event)

    def _fit_content(self):
        area = self.screen().availableGeometry()
        frame = self.frameGeometry().size() - self.size()
        available = area.size() - frame
        self.ensurePolished()
        self.layout().activate()
        page = cast(QScrollArea, self.tabs.currentWidget())
        chrome = self.size() - page.viewport().size()
        bodies = [cast(QScrollArea, self.tabs.widget(index)).widget() for index in range(self.tabs.count())]
        for body in bodies:
            body.ensurePolished()
            body.layout().activate()
        width = min(available.width(), max(self.width(), max(body.minimumSizeHint().width() for body in bodies) + chrome.width()))
        content_width = max(1, width - chrome.width())
        height = 0
        for body in bodies:
            layout = body.layout()
            preferred = layout.totalHeightForWidth(content_width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
            height = max(height, preferred, body.minimumSizeHint().height())
        self.resize(QSize(width, max(self.height(), height + chrome.height())).boundedTo(available))
        bounds = self.frameGeometry()
        x = min(max(bounds.left(), area.left()), area.right() - bounds.width() + 1)
        y = min(max(bounds.top(), area.top()), area.bottom() - bounds.height() + 1)
        self.move(self.pos() + QPoint(x - bounds.left(), y - bounds.top()))
