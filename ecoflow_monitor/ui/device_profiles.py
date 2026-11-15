from dataclasses import replace

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget

from ecoflow_monitor.device_profiles import builtin_catalog, write_example
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import action, hint
from ecoflow_monitor.ui.components import SelectField, SettingRow, Switch
from ecoflow_monitor.ui.confirmations import ConfirmationRequest
from ecoflow_monitor.ui.sections import CollapsibleSection


def profile_evidence(profile):
    return t("device_profile_working") if profile.source == "builtin" and profile.evidence == "working" else t("device_profile_documentation") if profile.evidence == "documentation" else t("device_profile_custom")


class DeviceProfilesPanel(QWidget):
    changed = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.catalog = builtin_catalog()
        self._assignments = {}
        self._serial = ""
        self._loading = False
        self._closed = False
        self._catalog_dirty = False
        self._scan_manual = False
        self._busy = False
        self._confirmation = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.profile = SelectField()
        self.profile.setAccessibleName(t("device_profile_selection"))
        layout.addWidget(SettingRow(t("device_profile_selection"), self.profile))
        self.detail = hint("")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setProperty("untranslated", True)
        layout.addWidget(self.detail)
        self.support = CollapsibleSection(t("device_support_title"), self)
        layout.addWidget(self.support)
        self.support.content.addWidget(hint(t("device_profiles_description")))
        self.listing = QPlainTextEdit()
        self.listing.setReadOnly(True)
        self.listing.setAccessibleName(t("device_support_title"))
        self.support.content.addWidget(self.listing)
        self.enabled = Switch(t("device_profiles_enable"))
        self.support.content.addWidget(SettingRow(t("device_profiles_enable"), self.enabled))
        self.support.content.addWidget(hint(t("device_profiles_directory_hint")))
        row = QHBoxLayout()
        self.open_button = action(t("device_profiles_open"), self.open_directory)
        self.reload_button = action(t("device_profiles_reload"), lambda: self.scan(manual=True))
        row.addWidget(self.open_button)
        row.addWidget(self.reload_button)
        self.support.content.addLayout(row)
        self.examples = SelectField()
        self.examples.setAccessibleName(t("device_profile_example"))
        for example in self.catalog.examples:
            self.examples.addItem(example["name"], example["id"])
        self.support.content.addWidget(SettingRow(t("device_profile_example"), self.examples))
        self.example_detail = hint("")
        self.example_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.example_detail.setProperty("untranslated", True)
        self.support.content.addWidget(self.example_detail)
        self.add_button = action(t("device_profiles_add_example"), self.add_example)
        self.support.content.addWidget(self.add_button)
        self.support.content.addWidget(hint(t("device_profiles_example_hint")))
        self.status = hint("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setProperty("untranslated", True)
        self.support.content.addWidget(self.status)
        self.enabled.toggled.connect(self._enabled_changed)
        self.profile.currentIndexChanged.connect(self._profile_changed)
        self.examples.currentIndexChanged.connect(self._example_changed)
        controller.profiles_ready.connect(self._scanned)
        controller.profile_example_ready.connect(self._example_written)
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.timeout.connect(self.scan)
        self._example_changed()

    @property
    def busy(self):
        return self._busy or self._confirmation is not None or self._scan_timer.isActive()

    @property
    def catalog_changed(self):
        return self._catalog_dirty and self.catalog != self.controller.profile_catalog

    def load(self, settings):
        self._loading = True
        self._assignments = dict(settings.device_profile_assignments)
        self._serial = ""
        self.enabled.setChecked(settings.custom_device_profiles)
        active = self.controller.profile_catalog
        self.catalog = active if active is not None and active.custom_enabled == settings.custom_device_profiles else replace(builtin_catalog(), custom_enabled=settings.custom_device_profiles)
        self._catalog_dirty = False
        self._loading = False
        self.support.set_expanded(False)
        self._populate()
        if settings.custom_device_profiles and active is None:
            self._scan_timer.start(0)

    def set_serial(self, serial):
        if serial == self._serial:
            return
        self._serial = serial
        self._populate()

    def settings_fields(self):
        return dict(custom_device_profiles=self.enabled.isChecked(), device_profile_assignments=tuple(sorted(self._assignments.items())))

    def _populate(self):
        reference = self._assignments.get(self._serial, "")
        with QSignalBlocker(self.profile):
            self.profile.clear()
            self.profile.addItem(t("device_profile_automatic"), "")
            for profile in self.catalog.profiles:
                if not profile.usable:
                    continue
                origin = t("device_profile_builtin") if profile.source == "builtin" else t("device_profile_user")
                self.profile.addItem(profile.name + " · " + origin, profile.reference)
                self.profile.setItemData(self.profile.count() - 1, profile_evidence(profile), Qt.ItemDataRole.ToolTipRole)
            if reference and self.profile.findData(reference) < 0:
                self.profile.addItem(t("device_profile_unavailable") + " · " + reference, reference)
            self.profile.setCurrentIndex(max(0, self.profile.findData(reference)))
        self.profile.setEnabled(bool(self._serial) and not self._busy)
        self.listing.setPlainText("\n\n".join(profile.name + "\n" + profile_evidence(profile)
                                             + ("\n" + t("device_profile_incomplete") if not profile.usable else "") for profile in self.catalog.profiles))
        self._refresh_detail()
        self._controls()

    def _refresh_detail(self):
        reference = self.profile.currentData()
        profile = self.catalog.resolve(reference) if reference else None
        text = profile_evidence(profile) if profile else t("device_profile_unavailable") if reference else t("device_profile_automatic_hint")
        self.detail.setText(text)

    def _profile_changed(self):
        if self._loading:
            return
        reference = self.profile.currentData()
        if self._serial:
            if reference:
                self._assignments[self._serial] = reference
            else:
                self._assignments.pop(self._serial, None)
        self._refresh_detail()
        self.changed.emit()

    def _example_changed(self):
        example = next((item for item in self.catalog.examples if item["id"] == self.examples.currentData()), None)
        if example is not None:
            complete = {"input_power", "output_power", "battery_level"} <= example["fields"].keys()
            self.example_detail.setText(t("device_profile_documentation") + ("\n" + t("device_profile_incomplete") if not complete else ""))

    def _controls(self):
        self.enabled.setEnabled(not self._busy and self._confirmation is None)
        self.reload_button.setEnabled(self.enabled.isChecked() and not self._busy)
        self.add_button.setEnabled(self.enabled.isChecked() and not self._busy)
        self.examples.setEnabled(self.enabled.isChecked() and not self._busy)

    def _enabled_changed(self, checked):
        if self._loading:
            return
        if not checked:
            self.catalog = builtin_catalog()
            self._catalog_dirty = True
            self._populate()
            self.changed.emit()
            return
        with QSignalBlocker(self.enabled):
            self.enabled.setChecked(False)
        key = ("device-profiles", id(self))
        self._confirmation = key
        self._controls()
        self.changed.emit()

        def prompt(parent):
            dialog = QMessageBox(parent)
            dialog.setWindowTitle(t("device_profiles_warning_title"))
            dialog.setText(t("device_profiles_warning"))
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            dialog.button(QMessageBox.StandardButton.Yes).setText(t("device_profiles_confirm"))
            dialog.button(QMessageBox.StandardButton.No).setText(t("cancel"))
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            return dialog

        def accepted(done):
            self._confirmation = None
            with QSignalBlocker(self.enabled):
                self.enabled.setChecked(True)
            self.scan()
            self.changed.emit()
            done()

        def rejected():
            self._confirmation = None
            self._controls()
            self.changed.emit()

        coordinator = self.controller.interactions
        if coordinator is not None:
            coordinator.enqueue(ConfirmationRequest(key, prompt, accepted, lambda: not self._closed, rejected))
        else:
            dialog = prompt(self)
            self._confirmation = dialog
            dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
            dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            dialog.finished.connect(lambda answer: accepted(lambda: None) if answer == QMessageBox.StandardButton.Yes else rejected())
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.show()

    def scan(self, manual=False):
        if self._closed:
            return
        self._scan_manual = manual
        self._busy = True
        self.status.setText(t("device_profiles_loading"))
        self._controls()
        self.profile.setEnabled(False)
        started = self.controller.scan_profiles(self.enabled.isChecked())
        if not started and "profiles" not in self.controller._active:
            self._busy = False
            self.status.setText(t("device_profile_read_error"))
            self._controls()
        self.changed.emit()

    def _scanned(self, catalog):
        if self._closed or not self._busy:
            return
        if catalog is not None and catalog.custom_enabled != self.enabled.isChecked():
            self._scan_timer.start(0)
            return
        self._busy = False
        if catalog is None:
            self.status.setText(t("device_profile_read_error"))
        else:
            self.catalog = catalog
            self._catalog_dirty = self._catalog_dirty or self._scan_manual
            self.status.setText("\n".join((filename + ": " if filename else "") + t(error) for filename, error in catalog.errors))
        self._populate()
        self.changed.emit()

    def open_directory(self):
        try:
            self.controller.profile_directory.mkdir(parents=True, exist_ok=True)
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.controller.profile_directory)))
        except OSError:
            opened = False
        if not opened:
            self.status.setText(t("device_profile_read_error"))

    def add_example(self):
        if self._busy or not self.enabled.isChecked() or self.controller.system_activity.paused or self.controller._stopped:
            return
        self._busy = True
        identifier = self.examples.currentData()
        self.controller._submit("profile_example", lambda: write_example(self.controller.profile_directory, identifier))
        self._controls()
        self.changed.emit()

    def _example_written(self, result, error):
        if self._closed:
            return
        self._busy = False
        if error is not None:
            self.status.setText(t("device_profile_write_error"))
            self._controls()
            self.changed.emit()
        else:
            self.scan(manual=True)

    def close_operations(self):
        self._closed = True
        self._scan_timer.stop()
        if isinstance(self._confirmation, QMessageBox):
            self._confirmation.reject()
        if self.controller.interactions is not None:
            self.controller.interactions.invalidate()
