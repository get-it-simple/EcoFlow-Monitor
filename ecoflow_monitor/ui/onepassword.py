from PySide6.QtCore import QSignalBlocker, Qt, Signal, Slot
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLineEdit, QTabBar, QVBoxLayout, QWidget

from ecoflow_monitor.adapters.ecoflow import Credentials
from ecoflow_monitor.adapters.credentials import CredentialError
from ecoflow_monitor.adapters.onepassword import OnePasswordError, RequestResult, SavedItem, SecretRequest, create_item, read_credentials
from ecoflow_monitor.credential_refs import normalize_secret_reference
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import action, hint
from ecoflow_monitor.ui.sections import CollapsibleSection
from ecoflow_monitor.ui.components import FormLayout as QFormLayout, NumberField, SettingRow, Switch


class OnePasswordPanel(QWidget):
    source_changed = Signal()
    credentials_ready = Signal(object)
    busy_changed = Signal(bool)
    request_completed = Signal(object)

    def __init__(self, entered_credentials, manual_fields, parent=None):
        super().__init__(parent)
        self._entered_credentials = entered_credentials
        self._request: SecretRequest | None = None
        self._paused = False
        self._saving = False
        self._save_credentials = None
        self._retained_references = ("", "")
        self.credentials: Credentials | None = None
        self.credential_source = "windows"
        self.session_loader = None
        self.external_runner = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source = QTabBar()
        self.source.setDrawBase(False)
        self.source.setAccessibleName(t("credential_source"))
        self.source.setExpanding(False)
        self.source.setUsesScrollButtons(False)
        self.source.setElideMode(Qt.TextElideMode.ElideNone)
        self._windows_tab = self.source.addTab(t("credential_tab_windows"))
        self.source.setTabToolTip(self._windows_tab, t("credential_source_windows"))
        self.source.setTabData(self._windows_tab, "windows")
        self._onepassword_tab = self.source.addTab("1Password")
        self.source.setTabData(self._onepassword_tab, "1password")
        layout.addWidget(self.source)
        self.storage_switches = {}
        self.storage_rows = {}
        for source, key in (("windows", "credential_use_windows"), ("1password", "credential_use_onepassword")):
            control = Switch(t(key))
            control.setChecked(source == self.credential_source)
            control.toggled.connect(lambda checked, selected=source: self._select_storage(selected, checked))
            row = SettingRow(t(key), control)
            layout.addWidget(row)
            self.storage_switches[source] = control
            self.storage_rows[source] = row
        layout.addWidget(manual_fields)
        self.references = QWidget()
        fields = QFormLayout(self.references)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.access_ref = QLineEdit()
        self.secret_ref = QLineEdit()
        for field, key in ((self.access_ref, "onepassword_access_ref"), (self.secret_ref, "onepassword_secret_ref")):
            field.setPlaceholderText("op://Personal/EcoFlow/username" if field is self.access_ref else "op://Personal/EcoFlow/password")
            field.setAccessibleName(t(key))
            field.setEchoMode(QLineEdit.EchoMode.Password)
            fields.addRow(t(key), field)
            field.textChanged.connect(self._invalidate)
        self.show_references = QCheckBox(t("onepassword_show_references"))
        self.show_references.toggled.connect(self._show_references)
        fields.addRow(self.show_references)
        self.cache_hours = NumberField()
        self.cache_hours.setRange(1, 168)
        self.cache_hours.setDecimals(0)
        self.cache_hours.setValue(24)
        self.cache_hours.setAccessibleName(t("onepassword_cache_hours"))
        fields.addRow(t("onepassword_cache_hours"), self.cache_hours)
        fields.addRow(hint(t("onepassword_cache_hint")))
        layout.addWidget(self.references)
        self.load_button = action(t("onepassword_load"), self.load_credentials)
        self.load_button.setMinimumWidth(320)
        layout.addWidget(self.load_button, 0, Qt.AlignmentFlag.AlignRight)
        self.save_section = CollapsibleSection(t("onepassword_save_section"))
        save_form = QFormLayout()
        save_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.vault = QLineEdit()
        self.vault.setPlaceholderText("Personal")
        self.title = QLineEdit("EcoFlow Monitor")
        self.create_access = QLineEdit()
        self.create_secret = QLineEdit()
        for control, key in ((self.create_access, "access_key"), (self.create_secret, "secret_key")):
            control.setEchoMode(QLineEdit.EchoMode.Password)
            control.setAccessibleName(t(key))
            save_form.addRow(t(key), control)
        self.show_create_keys = QCheckBox(t("show_keys"))
        self.show_create_keys.toggled.connect(self._show_create_keys)
        save_form.addRow(self.show_create_keys)
        save_form.addRow(t("onepassword_vault"), self.vault)
        save_form.addRow(t("onepassword_title"), self.title)
        self.vault.setAccessibleName(t("onepassword_vault"))
        self.title.setAccessibleName(t("onepassword_title"))
        self.save_section.content.addLayout(save_form)
        self.save_section.content.addWidget(hint(t("onepassword_save_hint")))
        self.create_button = action(t("onepassword_create"), self.save_credentials)
        self.save_section.content.addWidget(self.create_button)
        layout.addWidget(self.save_section)
        self.setup_section = CollapsibleSection(t("onepassword_setup_section"))
        self.setup_section.content.addWidget(hint(t("onepassword_setup")))
        layout.addWidget(self.setup_section)
        row = QHBoxLayout()
        self.status = hint("")
        self.status.hide()
        self.status.setAccessibleName(t("onepassword_status"))
        self.cancel_button = action(t("cancel"), self.cancel)
        self.cancel_button.hide()
        row.addWidget(self.status, 1)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.request_completed.connect(self._finish_request, Qt.ConnectionType.QueuedConnection)
        self.source.currentChanged.connect(self._source_changed)
        self._source_changed()

    @property
    def uses_onepassword(self):
        return self.source.tabData(self.source.currentIndex()) == "1password"

    @property
    def busy(self):
        return self._request is not None

    def _invalidate(self):
        self.cancel(show_status=False)
        self.credentials = None
        self.show_status("")
        self.source_changed.emit()

    def _source_changed(self):
        self.cancel(show_status=False)
        self.show_references.setChecked(False)
        self.show_create_keys.setChecked(False)
        for source, row in self.storage_rows.items():
            row.setVisible(source == self.source.tabData(self.source.currentIndex()))
        self.references.setVisible(self.uses_onepassword)
        self.load_button.setVisible(self.uses_onepassword)
        self.save_section.setVisible(self.uses_onepassword)
        self.setup_section.setVisible(self.uses_onepassword)
        self.source_changed.emit()

    def _select_storage(self, source, checked):
        if checked:
            self.credential_source = source
        for name, control in self.storage_switches.items():
            with QSignalBlocker(control):
                control.setChecked(name == self.credential_source)
        self.source_changed.emit()

    def _show_references(self, visible):
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.access_ref.setEchoMode(mode)
        self.secret_ref.setEchoMode(mode)

    def _show_create_keys(self, visible):
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.create_access.setEchoMode(mode)
        self.create_secret.setEchoMode(mode)

    def load(self, settings):
        self.cancel(show_status=False)
        self._select_storage(settings.credential_source, True)
        self._retained_references = (settings.onepassword_access_ref, settings.onepassword_secret_ref)
        self.source.setCurrentIndex(self._onepassword_tab if settings.credential_source == "1password" else self._windows_tab)
        self.access_ref.setText(settings.onepassword_access_ref)
        self.secret_ref.setText(settings.onepassword_secret_ref)
        self.cache_hours.setValue(settings.onepassword_cache_ttl_seconds // 3600)
        self.credentials = None
        self.show_references.setChecked(False)
        self.show_create_keys.setChecked(False)
        self.create_access.clear()
        self.create_secret.clear()
        self.show_status("")
        self.save_section.set_expanded(False)
        self.setup_section.set_expanded(False)

    def settings_fields(self, *, viewed=False):
        source = self.source.tabData(self.source.currentIndex()) if viewed else self.credential_source
        if source == "1password":
            access = normalize_secret_reference(self.access_ref.text())
            secret = normalize_secret_reference(self.secret_ref.text())
            self._retained_references = (access, secret)
        else:
            try:
                access = normalize_secret_reference(self.access_ref.text())
                secret = normalize_secret_reference(self.secret_ref.text())
            except ValueError:
                access, secret = self._retained_references
        return dict(credential_source=source, onepassword_access_ref=access, onepassword_secret_ref=secret,
                    onepassword_cache_ttl_seconds=int(self.cache_hours.value()) * 3600)

    def load_credentials(self):
        if self.busy:
            return
        try:
            fields = self.settings_fields(viewed=True)
        except ValueError:
            self.show_status(t("onepassword_reference_error"))
            return
        access, secret = fields["onepassword_access_ref"], fields["onepassword_secret_ref"]
        self.credentials = None
        self.source_changed.emit()
        self._start(lambda cancelled: self.session_loader(fields, cancelled) if self.session_loader is not None
                    else read_credentials(access, secret, cancelled), saving=False)

    def save_credentials(self):
        if self.busy:
            return
        try:
            credentials = Credentials(self.create_access.text().strip(), self.create_secret.text().strip())
            if not credentials.access_key or not credentials.secret_key or not self.vault.text().strip() or not self.title.text().strip():
                raise OnePasswordError("onepassword_save_required")
        except (CredentialError, ValueError) as error:
            self.show_status(t(str(error)))
            return
        vault, title = self.vault.text(), self.title.text()
        self._save_credentials = credentials
        def operation(cancelled):
            create = lambda: create_item(vault, title, credentials, cancelled)
            return self.external_runner(create, cancelled) if self.external_runner is not None else create()
        self._start(operation, saving=True)

    def _start(self, operation, saving):
        self._saving = saving
        request = SecretRequest(operation, self.request_completed.emit)
        self._request = request
        self._set_busy(True)
        self.show_status(t("onepassword_waiting"))
        request.start()

    def _set_busy(self, busy):
        for widget in (self.source, self.references, self.load_button, self.save_section, *self.storage_rows.values()):
            widget.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.busy_changed.emit(busy)

    def show_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def set_paused(self, paused):
        self._paused = paused
        if not paused and self._request is not None:
            self._finish_request(self._request)

    @Slot(object)
    def _finish_request(self, request):
        if self._paused or request is not self._request:
            return
        result: RequestResult | None = request.take_result()
        if result is None:
            return
        self._request = None
        self._set_busy(False)
        if result.error:
            self._save_credentials = None
            self.show_status(t(result.error) + (" " + t("onepassword_check_created") if self._saving else ""))
            return
        if isinstance(result.value, SavedItem):
            credentials = self._save_credentials
            self._save_credentials = None
            self.source.setCurrentIndex(self._onepassword_tab)
            self.access_ref.setText(result.value.access_ref)
            self.secret_ref.setText(result.value.secret_ref)
            self.credentials = credentials
            self.create_access.clear()
            self.create_secret.clear()
            self.show_status(t("onepassword_created"))
        elif isinstance(result.value, Credentials):
            self.credentials = result.value
            self.show_status(t("onepassword_loaded"))
        self.credentials_ready.emit(self.credentials)

    def cancel(self, *, show_status=True):
        if self._request is not None:
            self._request.close()
            self._request = None
            self.show_status(t("onepassword_cancelled") + (" " + t("onepassword_check_created") if self._saving else "") if show_status else "")
        self._save_credentials = None
        self._set_busy(False)

    def hideEvent(self, event):
        self.show_references.setChecked(False)
        self.show_create_keys.setChecked(False)
        super().hideEvent(event)
