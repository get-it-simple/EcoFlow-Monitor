from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QScrollArea, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor.ui.common import ScreenTracker, clamp_window, effective_scale, scaled_font
from ecoflow_monitor.ui.components import Navigation, StatusBadge, TitleBar
from ecoflow_monitor.ui.monitoring import MonitoringPage
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.resize import WindowResize
from ecoflow_monitor.ui.scrollbar import install_scrollbars
from ecoflow_monitor.ui.settings_window import SettingsWindow
from ecoflow_monitor.ui.theme import settings_style
from ecoflow_monitor.ui.tokens import COLORS, GEOMETRY, RADIUS
from ecoflow_monitor.i18n import t


class Dashboard(SettingsWindow):
    visibility_changed = Signal(bool)

    def __init__(self, controller, open_mini):
        super().__init__(controller)
        self._quitting = False
        self._scale = 1
        self._screen_tracker = ScreenTracker(self, self._screen_changed)
        self.setObjectName("dashboard")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(t("app_title"))
        self.monitoring = MonitoringPage(controller, open_mini, self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.monitoring)
        scroll.viewport().installEventFilter(self.monitoring)
        self.tabs.insertTab(0, scroll, t("monitoring"))
        self.tabs.tabBar().moveTab(2, 3)
        self.tabs.tabBar().hide()
        self.page_keys = ("monitoring", "connection_settings", "appearance_settings", "integrations", "hotkeys_settings")
        outer = self.layout()
        outer.removeWidget(self.tabs)
        outer.removeWidget(self.error_label)
        outer.takeAt(outer.count() - 1)
        self.shell = QFrame(self)
        self.shell.setObjectName("app_shell")
        outer.addWidget(self.shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        shell_layout = QVBoxLayout(self.shell)
        shell_layout.setContentsMargins(1, 1, 1, 1)
        shell_layout.setSpacing(0)
        self.titlebar = TitleBar(self)
        self.brand = self.titlebar.title
        self.close_button = self.titlebar.close_button
        self.navigation_button = QToolButton(self.titlebar)
        self.navigation_button.setIcon(QIcon(icon_path("more")))
        self.navigation_button.setProperty("icon_button", True)
        self.navigation_button.setAccessibleName(t("navigation"))
        self.navigation_button.clicked.connect(self.navigation_menu)
        self.titlebar.layout().insertWidget(0, self.navigation_button)
        shell_layout.addWidget(self.titlebar)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = QWidget(self.shell)
        self.sidebar.setObjectName("sidebar")
        sidebar = QVBoxLayout(self.sidebar)
        sidebar.setContentsMargins(16, 24, 16, 16)
        sidebar.setSpacing(8)
        self.navigation = Navigation(self.sidebar)
        for key in self.page_keys:
            self.navigation.addItem(t(key))
        sidebar.addWidget(self.navigation, 1)
        self.device_label = QLabel(self.controller.device_name, self.sidebar)
        self.device_label.setTextFormat(Qt.TextFormat.PlainText)
        self.device_label.setObjectName("secondary")
        sidebar.addWidget(self.device_label)
        self.sidebar_status = StatusBadge(controller, self.sidebar, retry_credentials=True)
        sidebar.addWidget(self.sidebar_status)
        body.addWidget(self.sidebar)
        self.page_container = QWidget(self.shell)
        pages = QVBoxLayout(self.page_container)
        pages.setContentsMargins(0, 0, 0, 0)
        pages.setSpacing(0)
        pages.addWidget(self.tabs, 1)
        self.error_label.setContentsMargins(32, 0, 32, 8)
        pages.addWidget(self.error_label)
        pages.addWidget(self.footer)
        body.addWidget(self.page_container, 1)
        shell_layout.addLayout(body, 1)
        self.navigation.currentRowChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self._page_changed)
        self.navigation.setCurrentRow(0)
        self.resize_controls = WindowResize(self)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(500)
        self._geometry_timer.timeout.connect(self._save_geometry)
        self._last_language = controller.settings.language
        controller.changed.connect(self.refresh)
        self.monitor_readings = self.monitoring.readings
        self.connection_status = self.monitoring.status
        self.apply_appearance()
        x, y, width, height = controller.settings.dashboard_geometry
        self.resize(width, height)
        if x != -1 or y != -1:
            self.move(x, y)
        clamp_window(self)
        install_scrollbars(self)

    def navigation_menu(self):
        menu = QMenu(self)
        for index, key in enumerate(self.page_keys):
            action = menu.addAction(t(key))
            action.setCheckable(True)
            action.setChecked(index == self.navigation.currentRow())
            action.triggered.connect(lambda checked=False, page=index: self.navigation.setCurrentRow(page))
        menu.exec(self.navigation_button.mapToGlobal(self.navigation_button.rect().bottomLeft()))
        menu.deleteLater()

    def apply_appearance(self):
        scale = effective_scale(self, self.controller.ui_settings.scale)
        self._apply_minimum(scale)
        signature = (scale, self.controller.settings.language)
        if getattr(self, "_appearance_signature", None) == signature:
            self.refresh()
            return
        self._appearance_signature = signature
        self._scale = scale
        self._settings_scale = scale
        scaled_font(self, scale)
        self.setStyleSheet(settings_style(scale=scale) + f"""
QWidget#dashboard {{ background: transparent; }}
QFrame#app_shell {{ background: {COLORS['surface']}; border: 1px solid {COLORS['border']}; border-radius: {round(RADIUS['window'] * scale)}px; }}
""")
        if not hasattr(self, "sidebar"):
            return
        self.titlebar.setFixedHeight(round(GEOMETRY["header"] * scale))
        self.sidebar.setFixedWidth(round(GEOMETRY["sidebar"] * scale))
        self.sidebar.layout().setContentsMargins(round(16 * scale), round(24 * scale), round(16 * scale), round(16 * scale))
        self.footer.configure(scale)
        for index in range(self.tabs.count()):
            body = self.tabs.widget(index).widget()
            body.layout().setContentsMargins(round(32 * scale), round(24 * scale), round(32 * scale), round(24 * scale))
            body.layout().setSpacing(round(24 * scale))
        self.resize_controls.scale = scale
        self.resize_controls.layout()
        self._update_navigation_layout()
        self.refresh()

    def _apply_minimum(self, scale):
        area = self.screen().availableGeometry()
        context = (scale, area.width(), area.height())
        if getattr(self, "_minimum_context", None) == context:
            return
        self._minimum_context = context
        size = self.size()
        constrained = getattr(self, "_size_constraint", None)
        preferred = constrained[0] if constrained is not None and size == constrained[1] else size
        minimum = QSize(round(GEOMETRY["dashboard_min_width"] * scale), round(GEOMETRY["dashboard_min_height"] * scale)).boundedTo(area.size())
        target = preferred.expandedTo(minimum).boundedTo(area.size())
        self.setMinimumSize(minimum)
        if not self.isMaximized() and not self.isMinimized():
            self.resize(target)
            self._size_constraint = (preferred, target) if preferred != target else None

    def _page_changed(self, index):
        self.navigation.setCurrentRow(index)
        if hasattr(self, "footer"):
            self.footer.setVisible(index != 0)
        self.error_label.setVisible(index != 0 and bool(self.error_label.text()))
        self._connect_draft()

    def refresh(self):
        if not hasattr(self, "monitoring"):
            return
        self.monitoring.refresh()
        self.device_label.setText(self.controller.device_name)
        if hasattr(self, "sidebar_status"):
            self.sidebar_status.refresh()
        if self._last_language != self.controller.settings.language:
            self._last_language = self.controller.settings.language
            self._translations.refresh()
            for index, key in enumerate(self.page_keys):
                self.navigation.item(index).setText(t(key))
        if hasattr(self, "footer"):
            self.footer.configure(self._scale)
        self.error_label.setVisible(self.tabs.currentIndex() != 0 and bool(self.error_label.text()))

    def refresh_monitoring(self):
        self.refresh()

    def request_visibility(self, visible):
        if self._quitting:
            return
        if visible:
            self.show()
        else:
            self.hide()

    def startup(self):
        self.request_visibility(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "sidebar"):
            return
        self._update_navigation_layout()
        if hasattr(self, "_geometry_timer"):
            self._geometry_timer.start()

    def _update_navigation_layout(self):
        expanded = self.width() >= round(800 * self._scale)
        self.sidebar.setVisible(expanded)
        self.navigation_button.setVisible(not expanded)

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_geometry_timer"):
            self._geometry_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self._screen_tracker.watch()
        self.visibility_changed.emit(True)
        self.monitoring.balance.refresh()

    def _screen_changed(self):
        self.apply_appearance()
        clamp_window(self)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.monitoring.msi_section.set_expanded(False)
        self.visibility_changed.emit(False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and event.modifiers() & Qt.KeyboardModifier.AltModifier:
            self.titlebar.system_menu()
            event.accept()
            return
        super().keyPressEvent(event)

    def _save_geometry(self):
        if self._quitting or self.controller.preview is not None or self.isMaximized() or self.isMinimized():
            return
        geometry = (self.x(), self.y(), self.width(), self.height())
        if geometry != self.controller.settings.dashboard_geometry:
            self.controller.update_preferences(dashboard_geometry=geometry)

    def _fit_content(self):
        clamp_window(self)

    def reject(self):
        self.load_draft()
        self.error_label.clear()
        self._draft_changed()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
            return
        self._draft_changed()
        if self._dirty:
            prompt = QMessageBox(self)
            prompt.setWindowTitle(t("unsaved_title"))
            prompt.setText(t("unsaved_message"))
            save = prompt.addButton(t("save"), QMessageBox.ButtonRole.AcceptRole)
            discard = prompt.addButton(t("discard"), QMessageBox.ButtonRole.DestructiveRole)
            cancel = prompt.addButton(t("cancel"), QMessageBox.ButtonRole.RejectRole)
            prompt.setDefaultButton(cancel)
            prompt.exec()
            selected = prompt.clickedButton()
            prompt.deleteLater()
            if selected is save:
                self.save()
                self._draft_changed()
                if self._dirty:
                    event.ignore()
                    return
            elif selected is not discard:
                event.ignore()
                return
        self.preview_changed.emit(None)
        self.load_draft()
        self._close_credentials()
        self._save_geometry()
        event.accept()
