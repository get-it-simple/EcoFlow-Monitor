from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import hint


class _PortDisclosureButton(QToolButton):
    def paintEvent(self, event):
        if self.parentWidget().underMouse() or self.hasFocus():
            super().paintEvent(event)


class PortCard(QFrame):
    def __init__(self, group, parent=None):
        super().__init__(parent)
        self.group = group
        self.collapsed = False
        self.setObjectName("ports_card")
        box = QVBoxLayout(self)
        self.heading = QLabel(t({"inputs": "inputs", "ac": "ac_outlets", "other": "other_outputs"}[group]), self)
        self.heading.setObjectName("section_title")
        box.addWidget(self.heading)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.hide()
        box.addWidget(self.summary)
        self.body = QWidget(self)
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.body)
        self.note = hint(t("outputs_read_only"))
        box.addWidget(self.note)
        self.note.setVisible(group != "inputs")
        self.toggle = _PortDisclosureButton(self)
        self.toggle.setProperty("icon_button", True)
        self.toggle.setCheckable(True)
        self.toggle.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.toggle.installEventFilter(self)
        box.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignHCenter)
        self.toggle.setVisible(group != "inputs")
        self.toggle.clicked.connect(self.set_collapsed)
        self.set_collapsed(False)

    def set_collapsed(self, collapsed):
        self.collapsed = collapsed
        self.toggle.setChecked(collapsed)
        self.body.setVisible(not collapsed)
        self.summary.setVisible(collapsed)
        label = t("expand_section" if collapsed else "collapse_section").format(section=self.heading.text())
        self.toggle.setAccessibleName(label)
        self.toggle.setToolTip(label)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if collapsed else Qt.ArrowType.UpArrow)

    def configure(self, scale):
        self.toggle.setFixedSize(round(34 * scale), round(34 * scale))
        self.set_collapsed(self.collapsed)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.toggle.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.toggle.update()

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.FocusIn, QEvent.Type.FocusOut}:
            self.toggle.update()
        return super().eventFilter(watched, event)
