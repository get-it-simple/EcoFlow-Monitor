from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QSizePolicy, QToolButton, QWidgetAction

from ecoflow_monitor.adapters.hotkey_binding import VALID_MODIFIERS, parse_combo
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.reference_style import icon_path


KEY_OPTIONS = [
    *[(chr(value), chr(value).upper()) for value in range(ord("a"), ord("z") + 1)],
    *[(str(value), str(value)) for value in range(10)],
    *[(f"num{value}", f"Num {value}") for value in range(10)],
    *[(f"f{value}", f"F{value}") for value in range(1, 25)],
    *[(value, value.title()) for value in ("up", "down", "left", "right", "space", "enter", "tab", "backspace", "delete", "insert", "home", "end", "page up", "page down", "esc", "caps lock", "print screen", "scroll lock", "pause", "num lock")],
]

KEY_EVENTS = {
    Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
    Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
    Qt.Key.Key_Space: "space", Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Tab: "tab", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Insert: "insert", Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "page up", Qt.Key.Key_PageDown: "page down",
    Qt.Key.Key_CapsLock: "caps lock", Qt.Key.Key_Print: "print screen",
    Qt.Key.Key_ScrollLock: "scroll lock", Qt.Key.Key_Pause: "pause", Qt.Key.Key_NumLock: "num lock",
}


class HotkeyComboEditor(QFrame):
    comboChanged = Signal()

    def __init__(self, combo, parent=None):
        super().__init__(parent)
        self._combo = combo
        self._recording = False
        self._before_recording = combo
        self._conflict = False
        self.accept_combo = None
        self.setObjectName("hotkeyComboInput")
        self.setProperty("focus_target", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMaximumWidth(620)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(12, 10, 12, 10)
        self.row.setSpacing(10)
        self.content = QHBoxLayout()
        self.row.addLayout(self.content)
        self.row.addStretch()
        self.record_button = QToolButton(self)
        self.record_button.setIcon(QIcon(icon_path("dropdown_arrow")))
        self.record_button.setProperty("icon_button", True)
        self.record_button.setToolTip(t("choose_key"))
        self.record_button.setAccessibleName(t("choose_key"))
        self.record_button.clicked.connect(self.choose_key)
        self.row.addWidget(self.record_button)
        self._render_state()

    def combo(self):
        return self._combo

    @property
    def recording(self):
        return self._recording

    @property
    def conflicted(self):
        return self._conflict

    def set_combo(self, combo):
        self._combo = "+".join(parse_combo(combo))
        self._conflict = False
        self._render_state()
        self.comboChanged.emit()

    def clear(self):
        self.set_combo("")

    def _render_state(self):
        while self.content.count():
            widget = self.content.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        self.setProperty("state", "recording" if self._recording else "conflict" if self._conflict else "assigned" if self._combo else "empty")
        try:
            tokens = parse_combo(self._combo) if self.isEnabled() and not self._recording and not self._conflict else []
        except ValueError:
            self._conflict = True
            tokens = []
        if tokens:
            labels = dict(KEY_OPTIONS)
            for token in tokens:
                label = QLabel(labels.get(token, token.title()), self)
                label.setProperty("untranslated", True)
                label.setObjectName("keycap")
                self.content.addWidget(label)
        else:
            key = "recording_shortcut" if self._recording else "hotkey_conflict" if self._conflict else "press_shortcut" if self.isEnabled() else "hotkey_disabled"
            label = QLabel(t(key), self)
            label.setWordWrap(True)
            label.setObjectName("form_error" if self._conflict else "secondary")
            self.content.addWidget(label)
        self.setAccessibleName(t("record_shortcut") + ": " + self._combo)
        self.style().unpolish(self)
        self.style().polish(self)
        self.updateGeometry()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.LanguageChange) and hasattr(self, "content"):
            if not self.isEnabled() and self._recording:
                self._recording = False
                self._combo = self._before_recording
                self.comboChanged.emit()
            self._render_state()

    def start_recording(self):
        if self._recording:
            return
        self._before_recording = self._combo
        self._recording = True
        self._conflict = False
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._render_state()
        self.comboChanged.emit()

    def assign(self, combo):
        if self.accept_combo is not None and not self.accept_combo(combo):
            self._recording = False
            self._conflict = True
            self._combo = self._before_recording
            self._render_state()
            self.comboChanged.emit()
            return
        self._recording = False
        self.set_combo(combo)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and (self._recording or self._conflict):
            self._recording = False
            self.set_combo(self._before_recording)
            event.accept()
            return
        if not self._recording and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Space):
            self.start_recording()
            event.accept()
            return
        if not self._recording and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.clear()
            event.accept()
            return
        if self._recording:
            key = event.key()
            modifiers = [(Qt.KeyboardModifier.ControlModifier, "ctrl"), (Qt.KeyboardModifier.AltModifier, "alt"), (Qt.KeyboardModifier.ShiftModifier, "shift"), (Qt.KeyboardModifier.MetaModifier, "win")]
            tokens = [name for flag, name in modifiers if event.modifiers() & flag]
            token = ""
            if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
                token = chr(key).lower()
            elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
                token = ("num" if event.modifiers() & Qt.KeyboardModifier.KeypadModifier else "") + chr(key)
            elif Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
                token = f"f{key - Qt.Key.Key_F1 + 1}"
            else:
                token = KEY_EVENTS.get(key, "")
            if token:
                self.assign("+".join(tokens + [token]))
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_recording()
        super().mousePressEvent(event)

    def choose_key(self):
        self._before_recording = self._combo
        menu = QMenu(self)
        tokens = parse_combo(self._combo)
        for modifier in ("ctrl", "alt", "shift", "win"):
            checkbox = QCheckBox(modifier.title(), menu)
            checkbox.setChecked(modifier in tokens)
            checkbox.toggled.connect(lambda checked, value=modifier: tokens.append(value) if checked else tokens.remove(value))
            action = QWidgetAction(menu)
            action.setDefaultWidget(checkbox)
            menu.addAction(action)
        menu.addSeparator()
        search = QLineEdit(menu)
        search.setPlaceholderText(t("search_keys"))
        search.setAccessibleName(t("search_keys"))
        search_action = QWidgetAction(menu)
        search_action.setDefaultWidget(search)
        menu.addAction(search_action)
        actions = []
        for value, label in KEY_OPTIONS:
            action = menu.addAction(label)
            action.triggered.connect(lambda checked=False, key=value: self.assign("+".join([token for token in tokens if token in VALID_MODIFIERS] + [key])))
            actions.append(action)
        search.textChanged.connect(lambda query: [action.setVisible(query.casefold() in action.text().casefold()) for action in actions])
        menu.setStyleSheet(menu.styleSheet() + "QMenu { menu-scrollable: 1; }")
        menu.exec(self.record_button.mapToGlobal(self.record_button.rect().bottomLeft()))
        menu.deleteLater()
        self.setFocus()
