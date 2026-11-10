import json
from pathlib import Path

from PySide6.QtWidgets import QAbstractButton, QComboBox, QGroupBox, QLabel, QTabWidget, QWidget

from ecoflow_monitor import i18n


class TranslationBinding:
    def __init__(self, root):
        self.root = root
        self.keys = tuple(json.loads((Path(i18n.__file__).parent / "en" / "strings.json").read_text(encoding="utf-8")))
        self.texts = {i18n.t(key): key for key in self.keys}

    def refresh(self):
        updated = {i18n.t(key): key for key in self.keys}
        for widget in self.root.findChildren(QWidget):
            if isinstance(widget, (QLabel, QAbstractButton)):
                key = self.texts.get(widget.text())
                if key:
                    widget.setText(i18n.t(key))
            if isinstance(widget, QGroupBox):
                key = self.texts.get(widget.title())
                if key:
                    widget.setTitle(i18n.t(key))
            if isinstance(widget, QComboBox):
                for index in range(widget.count()):
                    key = self.texts.get(widget.itemText(index))
                    if key:
                        widget.setItemText(index, i18n.t(key))
            if isinstance(widget, QTabWidget):
                for index in range(widget.count()):
                    key = self.texts.get(widget.tabText(index))
                    if key:
                        widget.setTabText(index, i18n.t(key))
            for getter, setter in ((widget.toolTip, widget.setToolTip), (widget.accessibleName, widget.setAccessibleName)):
                key = self.texts.get(getter())
                if key:
                    setter(i18n.t(key))
        self.texts = updated
