from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import hint


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
        self.note.setVisible(group != "inputs")
        box.addWidget(self.note)
        self.toggle = QToolButton(self)
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
        self._update_arrow()

    def configure(self, scale):
        self.toggle.setFixedSize(round(34 * scale), round(34 * scale))
        self.set_collapsed(self.collapsed)

    def _update_arrow(self):
        visible = self.underMouse() or self.toggle.hasFocus()
        hidden = not visible
        if self.toggle.property("disclosure_hidden") != hidden:
            self.toggle.setProperty("disclosure_hidden", hidden)
            self.toggle.style().unpolish(self.toggle)
            self.toggle.style().polish(self.toggle)
            self.toggle.update()
        self.toggle.setArrowType((Qt.ArrowType.DownArrow if self.collapsed else Qt.ArrowType.UpArrow) if visible else Qt.ArrowType.NoArrow)

    def enterEvent(self, event):
        super().enterEvent(event)
        self._update_arrow()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._update_arrow()

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.FocusIn, QEvent.Type.FocusOut}:
            self._update_arrow()
        return super().eventFilter(watched, event)
