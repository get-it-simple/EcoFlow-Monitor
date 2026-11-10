from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLineEdit, QTabBar, QVBoxLayout, QWidget

from ecoflow_monitor.adapters.ecoflow import Credentials
from ecoflow_monitor.adapters.credentials import CredentialError
from ecoflow_monitor.adapters.onepassword import OnePasswordError, RequestResult, SavedItem, SecretRequest, create_item, read_credentials
from ecoflow_monitor.credential_refs import normalize_secret_reference
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import action, hint
from ecoflow_monitor.ui.sections import CollapsibleSection


class OnePasswordPanel(QWidget):
    source_changed = Signal()
    credentials_ready = Signal(object)
    busy_changed = Signal(bool)

    def __init__(self, entered_credentials, manual_fields, parent=None):
        super().__init__(parent)
        self._entered_credentials = entered_credentials
        self._request: SecretRequest | None = None
        self._saving = False
        self._save_credentials = None
        self.credentials: Credentials | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source = QTabBar()
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
            fields.addRow(t(key), field)
            field.textChanged.connect(self._invalidate)
        layout.addWidget(self.references)
        self.load_button = action(t("onepassword_load"), self.load_credentials)
        layout.addWidget(self.load_button)
        self.save_section = CollapsibleSection(t("onepassword_save_section"))
        save_form = QFormLayout()
        save_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.vault = QLineEdit()
        self.vault.setPlaceholderText("Personal")
        self.title = QLineEdit("EcoFlow Monitor")
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
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll)
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
        self._invalidate()
        self.references.setVisible(self.uses_onepassword)
        self.load_button.setVisible(self.uses_onepassword)
        self.save_section.setVisible(not self.uses_onepassword)

    def load(self, settings):
        self.cancel(show_status=False)
        self.source.setCurrentIndex(self._onepassword_tab if settings.credential_source == "1password" else self._windows_tab)
        self.access_ref.setText(settings.onepassword_access_ref)
        self.secret_ref.setText(settings.onepassword_secret_ref)
        self.credentials = None
        self.show_status("")
        self.save_section.set_expanded(False)
        self.setup_section.set_expanded(False)

    def settings_fields(self):
        if self.uses_onepassword:
            access = normalize_secret_reference(self.access_ref.text())
            secret = normalize_secret_reference(self.secret_ref.text())
        else:
            access = secret = ""
        return dict(credential_source=self.source.tabData(self.source.currentIndex()), onepassword_access_ref=access, onepassword_secret_ref=secret)

    def load_credentials(self):
        if self.busy:
            return
        try:
            fields = self.settings_fields()
        except ValueError:
            self.show_status(t("onepassword_reference_error"))
            return
        access, secret = fields["onepassword_access_ref"], fields["onepassword_secret_ref"]
        self.credentials = None
        self.source_changed.emit()
        self._start(lambda cancelled: read_credentials(access, secret, cancelled), saving=False)

    def save_credentials(self):
        if self.busy:
            return
        try:
            credentials = self._entered_credentials()
            if credentials is None or not self.vault.text().strip() or not self.title.text().strip():
                raise OnePasswordError("onepassword_save_required")
        except (CredentialError, ValueError) as error:
            self.show_status(t(str(error)))
            return
        vault, title = self.vault.text(), self.title.text()
        self._save_credentials = credentials
        self._start(lambda cancelled: create_item(vault, title, credentials, cancelled), saving=True)

    def _start(self, operation, saving):
        self._saving = saving
        request = SecretRequest(operation)
        self._request = request
        request.start()
        self._set_busy(True)
        self.show_status(t("onepassword_waiting"))
        self._timer.start()

    def _set_busy(self, busy):
        for widget in (self.source, self.references, self.load_button, self.save_section):
            widget.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.busy_changed.emit(busy)

    def show_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _poll(self):
        request = self._request
        if request is None:
            return
        result: RequestResult | None = request.take_result()
        if result is None:
            return
        self._timer.stop()
        request.close()
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
            self.show_status(t("onepassword_created"))
        elif isinstance(result.value, Credentials):
            self.credentials = result.value
            self.show_status(t("onepassword_loaded"))
        self.credentials_ready.emit(self.credentials)

    def cancel(self, *, show_status=True):
        self._timer.stop()
        if self._request is not None:
            self._request.close()
            self._request = None
            self.show_status(t("onepassword_cancelled") + (" " + t("onepassword_check_created") if self._saving else "") if show_status else "")
        self._save_credentials = None
        self._set_busy(False)
