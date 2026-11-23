from collections.abc import Iterator

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from ecoflow_monitor.i18n import t
from ecoflow_monitor.integrations import IntegrationDefinition
from ecoflow_monitor.telemetry_values import VALUE_UNITS
from ecoflow_monitor.ui.common import action
from ecoflow_monitor.ui.components import ActionLayout, DesignDialog
from ecoflow_monitor.ui.metrics import metric_label


def _profile_tokens(module: IntegrationDefinition) -> Iterator[tuple[str, str]]:
    yield t("integration_app_token"), "{app}"
    for command in module.mapping:
        yield command.label, "{cli:" + command.id + "}"
    if module.driver == "cli":
        for key in VALUE_UNITS:
            label = t("integration_capacity_wh") if key == "capacity_wh" else metric_label(key)
            yield label, "{value:" + key + "}"


class ProfileTokenDialog(DesignDialog):
    def __init__(self, module: IntegrationDefinition, parent=None):
        super().__init__(parent)
        self.token: str | None = None
        self.setObjectName("settings_dialog")
        self.setWindowTitle(t("integration_insert"))
        self.resize(620, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(t("integration_search_tokens"))
        self.search.setAccessibleName(t("integration_search_tokens"))
        layout.addWidget(self.search)
        self.results = QListWidget(self)
        self.results.setAccessibleName(t("integration_insert"))
        self.results.setProperty("focus_target", True)
        layout.addWidget(self.results, 1)
        self.preview = QLabel(self)
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        self.preview.setObjectName("secondary")
        layout.addWidget(self.preview)
        for label, token in _profile_tokens(module):
            item = QListWidgetItem(label, self.results)
            item.setData(Qt.ItemDataRole.UserRole, token)
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, token)
            item.setToolTip(token)

        footer = ActionLayout()
        footer.addStretch()
        footer.addWidget(action(t("cancel"), self.reject))
        self.insert_button = action(t("integration_insert"), self.accept)
        self.insert_button.setObjectName("primary")
        self.insert_button.setDefault(True)
        footer.addWidget(self.insert_button)
        self.set_footer(footer)
        self.search.textChanged.connect(self._filter)
        self.results.currentItemChanged.connect(self._update_selection)
        self.results.itemActivated.connect(lambda _: self.accept())
        self.results.setCurrentRow(0)
        self.search.setFocus()

    def _filter(self, text: str) -> None:
        query = text.strip().casefold()
        first = None
        for row in range(self.results.count()):
            item = self.results.item(row)
            token = item.data(Qt.ItemDataRole.UserRole)
            matches = query in item.text().casefold() or query in token.casefold()
            item.setHidden(not matches)
            if matches and first is None:
                first = item
        self.results.setCurrentItem(first)
        self._update_selection()

    def _update_selection(self) -> None:
        item = self.results.currentItem()
        available = item is not None and not item.isHidden()
        self.insert_button.setEnabled(available)
        self.preview.setText(item.data(Qt.ItemDataRole.UserRole) if available else t("integration_no_matching_tokens"))

    def accept(self) -> None:
        item = self.results.currentItem()
        if item is not None and not item.isHidden():
            self.token = item.data(Qt.ItemDataRole.UserRole)
            super().accept()
