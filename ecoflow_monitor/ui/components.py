from typing import cast

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import QAbstractSpinBox, QBoxLayout, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget, QMenu, QMessageBox, QScrollArea, QSizePolicy, QStyledItemDelegate, QStyle, QToolButton, QVBoxLayout, QWidget, QWidgetAction

from ecoflow_monitor.i18n import t
from ecoflow_monitor.ui.common import ActionButton, status_color, status_text, status_tooltip
from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.tokens import COLORS, GEOMETRY


class Switch(QCheckBox):
    def __init__(self, text="", parent=None):
        super().__init__("", parent)
        self.setProperty("switch", True)
        self.setAccessibleName(text)
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class NumberField(QDoubleSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    def sizeHint(self):
        size = super().sizeHint()
        size.setHeight(round(GEOMETRY["field"] * max(0.5, self.font().pixelSize() / 14)))
        return size

    def minimumSizeHint(self):
        return self.sizeHint()

    def _layout_text(self):
        padding = round(12 * max(0.5, self.font().pixelSize() / 14))
        self.lineEdit().setGeometry(self.rect().adjusted(padding, 1, -padding, -1))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_text()

    def showEvent(self, event):
        super().showEvent(event)
        self._layout_text()


class SelectField(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(1)

    def showPopup(self):
        menu = QMenu(self)
        menu.setMinimumWidth(self.width())
        menu.setStyleSheet("QMenu { menu-scrollable: 1; }")
        search = None
        if self.count() > 12:
            search = QLineEdit(menu)
            search.setPlaceholderText(t("search_options"))
            search.setAccessibleName(t("search_options"))
            field = QWidgetAction(menu)
            field.setDefaultWidget(search)
            menu.addAction(field)
        actions = []
        for index in range(self.count()):
            action = menu.addAction(self.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == self.currentIndex())
            action.setEnabled(bool(self.model().flags(self.model().index(index, self.modelColumn(), self.rootModelIndex())) & Qt.ItemFlag.ItemIsEnabled))
            action.setToolTip(self.itemData(index, Qt.ItemDataRole.ToolTipRole) or "")
            action.triggered.connect(lambda checked=False, selected=index: self.setCurrentIndex(selected))
            actions.append(action)
        if search is not None:
            search.textChanged.connect(lambda query: [action.setVisible(query.casefold() in action.text().casefold()) for action in actions])
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))
        menu.deleteLater()
        self.hidePopup()
        self.setFocus()


class ActionLayout(QBoxLayout):
    def __init__(self, parent=None):
        super().__init__(QBoxLayout.Direction.LeftToRight, parent)
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(8)

    def _widgets(self):
        return [self.itemAt(index).widget() for index in range(self.count()) if self.itemAt(index).widget() is not None]

    def _preferred_width(self):
        return sum(widget.sizeHint().width() for widget in self._widgets()) + self.spacing() * max(0, len(self._widgets()) - 1)

    def minimumSize(self):
        return QSize(0, max((widget.minimumSizeHint().height() for widget in self._widgets()), default=0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        widgets = self._widgets()
        if width >= self._preferred_width():
            return max((widget.sizeHint().height() for widget in widgets), default=0)
        return sum(widget.sizeHint().height() for widget in widgets) + self.spacing() * max(0, len(widgets) - 1)

    def setGeometry(self, rect):
        direction = QBoxLayout.Direction.LeftToRight if rect.width() >= self._preferred_width() else QBoxLayout.Direction.TopToBottom
        if self.direction() != direction:
            self.setDirection(direction)
        super().setGeometry(rect)


class SettingRow(QWidget):
    def __init__(self, label, control, parent=None):
        super().__init__(parent)
        self.row = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.row.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label, self)
        self.label.setWordWrap(True)
        self.row.addWidget(self.label, 1)
        if isinstance(control, QLayout):
            self.control = QWidget(self)
            self.control.setLayout(control)
        else:
            self.control = control
        self.control.setMaximumWidth(GEOMETRY["field_width"])
        self.row.addWidget(self.control)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.label.setBuddy(self.control)

    def minimumSizeHint(self):
        return QSize(0, self.control.minimumSizeHint().height())

    def sizeHint(self):
        return QSize(600, self.heightForWidth(max(300, self.width())))

    def heightForWidth(self, width):
        scale = max(0.5, self.font().pixelSize() / 14)
        control_height = self.control.sizeHint().height()
        if width < GEOMETRY["form_breakpoint"] * scale:
            return max(self.label.fontMetrics().height(), self.label.heightForWidth(max(1, width))) + round(8 * scale) + control_height
        field_width = GEOMETRY["range_width"] if self.control.property("wide_field") else GEOMETRY["field_width"]
        return max(control_height, self.label.heightForWidth(max(1, round(width - (field_width + 24) * scale))))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        scale = self.font().pixelSize() / 14 if self.font().pixelSize() > 0 else 1
        vertical = self.width() < GEOMETRY["form_breakpoint"] * scale
        self.row.setDirection(QBoxLayout.Direction.TopToBottom if vertical else QBoxLayout.Direction.LeftToRight)
        self.row.setSpacing(round((8 if vertical else 24) * scale))
        width = GEOMETRY["range_width"] if self.control.property("wide_field") else GEOMETRY["field_width"]
        self.control.setMaximumWidth(16777215 if vertical else round(width * scale))
        self.control.setMinimumWidth(0 if vertical else round(width * scale))
        if isinstance(self.control, Switch):
            self.control.setMinimumWidth(0)
            self.row.setAlignment(self.control, Qt.AlignmentFlag.AlignLeft if vertical else Qt.AlignmentFlag.AlignRight)


class FormLayout(QFormLayout):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.labels = {}
        self.setContentsMargins(0, 0, 0, 0)
        self.setVerticalSpacing(16)

    def addRow(self, *args):
        if len(args) == 2 and isinstance(args[0], str):
            row = SettingRow(*args)
            self.labels[args[1]] = row.label
            super().addRow(row)
        else:
            super().addRow(*args)

    def labelForField(self, field):
        return self.labels.get(field) or super().labelForField(field)


class ActionFooter(QScrollArea):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.setObjectName("settings_footer")
        self.body = QWidget(self)
        self.buttons = []
        while actions.count():
            item = actions.takeAt(0)
            button = item.widget()
            if isinstance(button, ActionButton):
                button.setParent(self.body)
                button.enable_wrapping()
                self.buttons.append(button)
        actions.deleteLater()
        self.scale = 1
        self._arranging = False
        self.setWidget(self.body)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.viewport().installEventFilter(self)

    def configure(self, scale):
        self.scale = scale
        self._arrange()

    def _arrange(self):
        if not hasattr(self, "_arranging") or self._arranging or not self.buttons:
            return
        self._arranging = True
        try:
            scale = self.scale
            gap = round(8 * scale)
            margin = round(32 * scale)
            width = self.viewport().width()
            preferred = [button.sizeHint() for button in self.buttons]
            vertical = width < sum(size.width() for size in preferred) + gap * (len(preferred) - 1) + margin * 2
            if vertical:
                margin = round(4 * scale)
            y = round(20 * scale)
            if vertical:
                for button in self.buttons:
                    height = button.wrapped_height(max(1, width - margin * 2))
                    button.setGeometry(margin, y, max(1, width - margin * 2), height)
                    y += height + gap
                height = y - gap + round(24 * scale)
            else:
                height = max(size.height() for size in preferred)
                self.buttons[0].setGeometry(margin, y, preferred[0].width(), height)
                x = width - margin - sum(size.width() for size in preferred[1:]) - gap * max(0, len(preferred) - 2)
                for button, size in zip(self.buttons[1:], preferred[1:]):
                    button.setGeometry(x, y, size.width(), height)
                    x += size.width() + gap
                height += y + round(24 * scale)
            self.body.setMinimumHeight(height)
            limit = max(round(40 * scale), self.window().screen().availableGeometry().height() // 3)
            self.setFixedHeight(min(height + self.frameWidth() * 2, limit))
        finally:
            self._arranging = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self._arrange()
        return super().eventFilter(watched, event)


class StatusBadge(QLabel):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("status_badge")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.refresh()

    def refresh(self):
        self.setText("●  " + status_text(self.controller))
        self.setToolTip(status_tooltip(self.controller))
        self.setAccessibleName(status_tooltip(self.controller))
        self.setStyleSheet(f"color: {status_color(self.controller)}; background: transparent; border: none;")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["raised"]))
        painter.drawRoundedRect(self.rect(), self.height() / 2, self.height() / 2)
        painter.setPen(QColor(status_color(self.controller)))
        painter.setFont(self.font())
        padding = round(10 * max(0.5, self.font().pixelSize() / 12))
        painter.drawText(self.rect().adjusted(padding, 0, -padding, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, max(1, self.width() - padding * 2)))


class NavigationDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = option.font.pixelSize() / 14
        rect = option.rect.adjusted(0, 0, 0, -round(12 * scale))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["successSurface"]))
            painter.drawRoundedRect(rect, 10 * scale, 10 * scale)
            painter.setBrush(QColor(COLORS["accent"]))
            painter.drawRoundedRect(rect.left(), round(rect.center().y() - 10 * scale), max(3, round(3 * scale)), round(20 * scale), 2, 2)
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold if selected else QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["accent"] if selected else COLORS["muted"]))
        painter.drawText(rect.adjusted(round(16 * scale), 0, -8, 0), Qt.AlignmentFlag.AlignVCenter, index.data())
        if focused and self.parent().property("keyboard_focus"):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(COLORS["focus"]), 2))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(round(184 * option.font.pixelSize() / 14), round(52 * option.font.pixelSize() / 14))


class Navigation(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navigation")
        self.setItemDelegate(NavigationDelegate(self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAccessibleName(t("navigation"))

    def focusInEvent(self, event):
        self.setProperty("keyboard_focus", event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason))
        super().focusInEvent(event)


class TitleBar(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("title_bar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        row = QHBoxLayout(self)
        row.setContentsMargins(24, 12, 16, 12)
        self.title = QLabel(t("app_title"), self)
        self.title.setObjectName("brand")
        row.addWidget(self.title, 1)
        self.close_button = QToolButton(self)
        self.close_button.setIcon(QIcon(icon_path("close")))
        self.close_button.setProperty("icon_button", True)
        self.close_button.setAccessibleName(t("close"))
        self.close_button.setToolTip(t("close"))
        self.close_button.clicked.connect(parent.close)
        row.addWidget(self.close_button)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda point: self.system_menu(self.mapToGlobal(point)))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.window().windowHandle():
            self.window().windowHandle().startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()

    def toggle_maximized(self):
        if self.window().isMaximized():
            self.window().showNormal()
        else:
            self.window().showMaximized()

    def system_menu(self, position=None):
        menu = QMenu(self)
        menu.addAction(t("window_restore"), self.window().showNormal).setEnabled(self.window().isMaximized())
        menu.addAction(t("window_minimize"), self.window().showMinimized)
        menu.addAction(t("window_maximize"), self.window().showMaximized).setEnabled(not self.window().isMaximized())
        menu.addSeparator()
        menu.addAction(t("close"), self.window().close)
        menu.exec(position or self.mapToGlobal(QPoint(0, self.height())))
        menu.deleteLater()


class DesignDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._framed = False

    def showEvent(self, event):
        if not self._framed:
            from ecoflow_monitor.ui.focus import FocusBinding
            from ecoflow_monitor.ui.theme import settings_style
            body = QWidget()
            body.setLayout(cast(QLayout, self.layout()))
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            frame = QFrame(self)
            frame.setObjectName("dialog_shell")
            outer.addWidget(frame)
            content = QVBoxLayout(frame)
            content.setContentsMargins(1, 1, 1, 1)
            content.setSpacing(0)
            self.titlebar = TitleBar(self)
            self.titlebar.title.setText(self.windowTitle())
            content.addWidget(self.titlebar)
            scroll = QScrollArea(frame)
            scroll.setWidgetResizable(True)
            scroll.setWidget(body)
            content.addWidget(scroll, 1)
            parent = self.parentWidget()
            scale = max(0.5, parent.font().pixelSize() / 14) if parent is not None else 1
            self.setStyleSheet(settings_style(scale=scale) + f"QDialog#settings_dialog {{ background: transparent; }} QFrame#dialog_shell {{ background: {COLORS['surface']}; border: 1px solid {COLORS['border']}; border-radius: 20px; }}")
            self.resize((self.size() * scale).boundedTo(self.screen().availableGeometry().size()))
            self._focus_binding = FocusBinding(self)
            from ecoflow_monitor.ui.scrollbar import install_scrollbars
            install_scrollbars(self)
            self._framed = True
        super().showEvent(event)


class DialogChrome(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Polish and isinstance(watched, QMessageBox) and not watched.property("design_chrome"):
            from ecoflow_monitor.ui.theme import settings_style
            watched.setProperty("design_chrome", True)
            watched.setWindowFlag(Qt.WindowType.FramelessWindowHint)
            watched.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            watched.setStyleSheet(settings_style() + f"QMessageBox {{ border-radius: 20px; background: {COLORS['surface']}; }}")
            layout = cast(QGridLayout, watched.layout())
            items = []
            while layout.count():
                position = layout.getItemPosition(0)
                items.append((layout.takeAt(0), position))
            for item, (row, column, rows, columns) in items:
                layout.addItem(item, row + 1, column, rows, columns)
            title = TitleBar(watched)
            title.title.setText(watched.windowTitle())
            layout.addWidget(title, 0, 0, 1, max(1, layout.columnCount()))
            layout.setContentsMargins(24, 16, 24, 24)
            layout.setSpacing(16)
        return False
