from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QSizePolicy, QToolButton, QVBoxLayout

from ecoflow_monitor.adapters.hotkey_binding import parse_combo
from ecoflow_monitor.adapters.hotkeys import HotkeyError
from ecoflow_monitor.integrations import hotkey_label
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.tokens import COLORS, RADIUS
from ecoflow_monitor.ui.scrollbar import install_scrollbars


def hotkey_error_text(error: HotkeyError, modules) -> str:
    messages = []
    for issue in error.issues:
        action = hotkey_label(issue.action, modules)
        combo = issue.combo or t("press_shortcut")
        if issue.reason == "hotkey_conflict":
            message = t("hotkey_duplicate_detail").format(combo=combo, action=action, other=hotkey_label(issue.other_action, modules))
        elif issue.reason == "hotkey_registration_failed":
            message = t("hotkey_registration_detail").format(combo=combo, action=action)
            if issue.winerror:
                message += " " + t("hotkey_windows_error").format(code=issue.winerror)
        else:
            message = action + " (" + combo + "): " + t(issue.reason)
        messages.append(message)
    return "\n".join(messages)


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


class KeyPicker(QDialog):
    def __init__(self, editor):
        super().__init__(editor, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("key_picker")
        self.setAccessibleName(t("choose_key"))
        scale = max(0.5, editor.font().pixelSize() / 14)
        self.setStyleSheet(f"QDialog#key_picker {{ background: {COLORS['surface']}; border: 1px solid {COLORS['border']}; border-radius: {round(RADIUS['control'] * scale)}px; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*([round(8 * scale)] * 4))
        layout.setSpacing(round(8 * scale))
        modifiers = QGridLayout()
        tokens = parse_combo(editor.combo())
        self.modifiers = {}
        for index, name in enumerate(("ctrl", "alt", "shift", "win")):
            checkbox = QCheckBox(name.title(), self)
            checkbox.setChecked(name in tokens)
            modifiers.addWidget(checkbox, index // 2, index % 2)
            self.modifiers[name] = checkbox
        layout.addLayout(modifiers)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(t("search_keys"))
        self.search.setAccessibleName(t("search_keys"))
        self.search.installEventFilter(self)
        layout.addWidget(self.search)
        self.keys = QListWidget(self)
        self.keys.setAccessibleName(t("choose_key"))
        self.keys.setMinimumSize(0, 0)
        layout.addWidget(self.keys, 1)
        for value, label in KEY_OPTIONS:
            item = QListWidgetItem(label, self.keys)
            item.setData(Qt.ItemDataRole.UserRole, value)
            if value in tokens:
                self.keys.setCurrentItem(item)
        if self.keys.currentRow() < 0:
            self.keys.setCurrentRow(0)
        self.keys.itemClicked.connect(self._choose)
        self.keys.itemActivated.connect(self._choose)
        self.search.textChanged.connect(self._filter)
        area = editor.screen().availableGeometry()
        self.setFixedSize(min(round(280 * scale), area.width()), min(round(480 * scale), round(area.height() * 0.6)))
        anchor = editor.record_button.mapToGlobal(editor.record_button.rect().bottomLeft())
        y = anchor.y() if anchor.y() + self.height() <= area.bottom() + 1 else anchor.y() - editor.record_button.height() - self.height()
        self.move(QPoint(min(max(anchor.x(), area.left()), area.right() - self.width() + 1), min(max(y, area.top()), area.bottom() - self.height() + 1)))
        self.combo = ""
        install_scrollbars(self)
        self.search.setFocus()

    def _filter(self, query):
        for index in range(self.keys.count()):
            item = self.keys.item(index)
            item.setHidden(query.casefold() not in item.text().casefold())
        visible = next((index for index in range(self.keys.count()) if not self.keys.item(index).isHidden()), -1)
        self.keys.setCurrentRow(visible)

    def _choose(self, item):
        if item is None or item.isHidden():
            return
        self.combo = "+".join([name for name, checkbox in self.modifiers.items() if checkbox.isChecked()] + [item.data(Qt.ItemDataRole.UserRole)])
        self.accept()

    def eventFilter(self, watched, event):
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self.keys.setFocus()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._choose(self.keys.currentItem())
                return True
        return super().eventFilter(watched, event)


class HotkeyComboEditor(QFrame):
    comboChanged = Signal()

    def __init__(self, combo, parent=None):
        super().__init__(parent)
        self._combo = combo
        self._recording = False
        self._before_recording = combo
        self._conflict = False
        self._validation_error = ""
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
        self.set_validation_error("")
        self._render_state()
        self.comboChanged.emit()

    def clear(self):
        self.set_combo("")

    def set_validation_error(self, message):
        if message != self._validation_error:
            self._validation_error = message
            self.setToolTip(message)
            self.setAccessibleDescription(message)
            self._render_state()

    def _render_state(self):
        while self.content.count():
            widget = self.content.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        conflict = self._conflict or bool(self._validation_error)
        self.setProperty("state", "recording" if self._recording else "conflict" if conflict else "assigned" if self._combo else "empty")
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
            label = QLabel(self._validation_error or t(key), self)
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
        menu = KeyPicker(self)
        if menu.exec() == QDialog.DialogCode.Accepted:
            self.assign(menu.combo)
        menu.deleteLater()
        self.window().activateWindow()
        self.setFocus(Qt.FocusReason.PopupFocusReason)
