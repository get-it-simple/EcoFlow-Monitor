from typing import Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor.adapters.hotkey_binding import parse_combo
from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.reference_style import bordered_input_style, icon_path
from ecoflow_monitor.ui.theme import bind_style, settings_style


MODIFIER_OPTIONS = [
    ("", "Select"),
    ("ctrl", "Ctrl"),
    ("alt", "Alt"),
    ("shift", "Shift"),
    ("win", "Win"),
]

KEY_OPTIONS = [
    ("", "Select"),
    *[(chr(c), chr(c).upper()) for c in range(ord("a"), ord("z") + 1)],
    *[(str(d), str(d)) for d in range(10)],
    *[(f"num{d}", f"NumPad {d}") for d in range(10)],
    *[(f"f{n}", f"F{n}") for n in range(1, 25)],
    ("up", "Up"),
    ("down", "Down"),
    ("left", "Left"),
    ("right", "Right"),
    ("space", "Space"),
    ("enter", "Enter"),
    ("esc", "Esc"),
    ("tab", "Tab"),
    ("backspace", "Backspace"),
    ("delete", "Delete"),
    ("insert", "Insert"),
    ("home", "Home"),
    ("end", "End"),
    ("page up", "Page Up"),
    ("page down", "Page Down"),
    ("caps lock", "Caps Lock"),
    ("print screen", "Print Screen"),
    ("scroll lock", "Scroll Lock"),
    ("pause", "Pause"),
    ("num lock", "Num Lock"),
]

class HotkeyComboEditor(QFrame):
    comboChanged = Signal()

    def __init__(self, combo: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._select_boxes: list[QComboBox] = []
        self._syncing = False
        self._recording = False
        self._before_recording = combo

        self.setObjectName("hotkeyComboInput")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bind_style(self, lambda color: bordered_input_style("hotkeyComboInput", color))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._select_layout = QHBoxLayout()
        outer.addLayout(self._select_layout)
        self._select_layout.setContentsMargins(10, 10, 10, 10)
        self._select_layout.setSpacing(4)

        self._placeholder_label = QLabel(t("press_shortcut"), self)
        self._placeholder_label.setObjectName("secondary")
        self._select_layout.addWidget(self._placeholder_label)
        self._select_layout.addStretch(1)
        self.record_button = QToolButton(self)
        self.record_button.setText(t("record_shortcut"))
        self.record_button.clicked.connect(self.start_recording)
        self._select_layout.addWidget(self.record_button)
        self.recording_hint = QLabel(t("recording_shortcut"), self)
        self.recording_hint.setWordWrap(True)
        self.recording_hint.hide()
        outer.addWidget(self.recording_hint)

        self.set_combo(combo)

    def combo(self) -> str:
        return "+".join(box.currentData() for box in self._select_boxes if box.currentData())

    def set_combo(self, combo: str) -> None:
        try:
            tokens = parse_combo(combo)
        except ValueError:
            tokens = []
        self._set_tokens(tokens)

    def clear(self) -> None:
        self._set_tokens([])
        self.setFocus()

    def keyPressEvent(self, event) -> None:
        if self._recording and event.key() == Qt.Key.Key_Escape:
            self._recording = False
            self.set_combo(self._before_recording)
            self.recording_hint.hide()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self.clear()
            event.accept()
            return
        tokens = self._tokens_from_event(event)
        if tokens:
            self._set_tokens(tokens)
            if self._recording and tokens[-1] not in {"ctrl", "alt", "shift", "win"}:
                self._recording = False
                self.recording_hint.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def start_recording(self):
        self._before_recording = self.combo()
        self._recording = True
        self.recording_hint.show()
        self.setFocus()
        self.comboChanged.emit()

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self.clear()
            return True
        if watched in self._select_boxes and event.type() == QEvent.Type.KeyPress:
            tokens = self._tokens_from_event(event)
            if len(tokens) > 1:
                self._set_tokens(tokens)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event) -> None:
        self.setFocus()
        super().mousePressEvent(event)

    def _set_tokens(self, tokens: list[str]) -> None:
        self._syncing = True
        self._clear_select_boxes()
        for token in tokens:
            box = self._create_box(self._options_for_token(token))
            self._set_box_value(box, token)
            self._select_boxes.append(box)
            self._select_layout.insertWidget(self._select_layout.count() - 1, box)

        self._placeholder_label.setVisible(not self._select_boxes)
        self._syncing = False
        self.comboChanged.emit()

    def _clear_select_boxes(self) -> None:
        while self._select_boxes:
            box = self._select_boxes.pop()
            self._select_layout.removeWidget(box)
            box.setParent(None)
            box.deleteLater()

    def _create_box(self, options: list[tuple[str, str]]) -> QComboBox:
        box = QComboBox(self)
        box.setMinimumHeight(32)
        box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        box.setMaxVisibleItems(12)
        bind_style(box, self._box_style)
        for value, label in options:
            box.addItem(label, value)
        box.currentIndexChanged.connect(self._sync_select_options)
        box.installEventFilter(self)
        return box

    @staticmethod
    def _box_style(accent):
        return settings_style(accent)

    def _options_for_token(self, token: str) -> list[tuple[str, str]]:
        if token in {"ctrl", "alt", "shift", "win"}:
            return MODIFIER_OPTIONS
        return KEY_OPTIONS

    def _set_box_value(self, box: QComboBox, value: str) -> None:
        index = box.findData(value)
        box.setCurrentIndex(index if index >= 0 else 0)

    def _sync_select_options(self) -> None:
        if self._syncing:
            return
        tokens = [box.currentData() for box in self._select_boxes if box.currentData()]
        modifiers = []
        keys = []
        for token in tokens:
            if token in {"ctrl", "alt", "shift", "win"} and token not in modifiers:
                modifiers.append(token)
            elif token not in {"ctrl", "alt", "shift", "win"} and not keys:
                keys.append(token)
        self._set_tokens(modifiers + keys)

    def _tokens_from_event(self, event) -> list[str]:
        key = self._key_token_from_event(event)
        tokens = []
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            tokens.append("ctrl")
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            tokens.append("alt")
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            tokens.append("shift")
        if event.modifiers() & Qt.KeyboardModifier.MetaModifier:
            tokens.append("win")
        if key and key not in tokens:
            tokens.append(key)
        return tokens

    def _key_token_from_event(self, event) -> str:
        key = event.key()
        text = event.text().lower()
        if len(text) == 1 and text.isalpha():
            return text
        if len(text) == 1 and text.isdigit():
            if event.modifiers() & Qt.KeyboardModifier.KeypadModifier:
                return f"num{text}"
            return text

        key_map = {
            Qt.Key.Key_Space: "space",
            Qt.Key.Key_Return: "enter",
            Qt.Key.Key_Enter: "enter",
            Qt.Key.Key_Escape: "esc",
            Qt.Key.Key_Tab: "tab",
            Qt.Key.Key_Insert: "insert",
            Qt.Key.Key_Home: "home",
            Qt.Key.Key_End: "end",
            Qt.Key.Key_PageUp: "page up",
            Qt.Key.Key_PageDown: "page down",
            Qt.Key.Key_CapsLock: "caps lock",
            Qt.Key.Key_Print: "print screen",
            Qt.Key.Key_ScrollLock: "scroll lock",
            Qt.Key.Key_Pause: "pause",
            Qt.Key.Key_NumLock: "num lock",
            Qt.Key.Key_Left: "left",
            Qt.Key.Key_Up: "up",
            Qt.Key.Key_Right: "right",
            Qt.Key.Key_Down: "down",
        }
        if int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
            return f"f{key - int(Qt.Key.Key_F1) + 1}"
        return key_map.get(key, "")
