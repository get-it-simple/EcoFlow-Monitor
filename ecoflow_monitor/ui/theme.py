from PySide6.QtCore import QObject
from PySide6.QtGui import QColor

from ecoflow_monitor.ui.reference_style import icon_path
from ecoflow_monitor.ui.tokens import COLORS, RADIUS


def current_accent() -> str:
    return COLORS["accent"]


def accent_text(accent: str) -> str:
    color = QColor(accent)
    channels = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                for value in (color.redF(), color.greenF(), color.blueF())]
    luminance = sum(value * weight for value, weight in zip(channels, (0.2126, 0.7152, 0.0722)))
    return "#000000" if luminance > 0.179 else "#ffffff"


class StyleBinding(QObject):
    def __init__(self, widget, factory):
        super().__init__(widget)
        self.widget = widget
        self.factory = factory
        self.prefix = widget.styleSheet()
        self.refresh()

    def refresh(self):
        self.widget.setStyleSheet(self.prefix + self.factory(current_accent()))


def bind_style(widget, factory):
    return StyleBinding(widget, factory)


def settings_style(accent: str = "", scale: float = 1) -> str:
    c = COLORS
    px = lambda value: max(1, round(value * scale))
    check = icon_path("check_black").replace("\\", "/")
    arrow = icon_path("dropdown_arrow").replace("\\", "/")
    off = icon_path("toggle_off").replace("\\", "/")
    on = icon_path("toggle_on").replace("\\", "/")
    return f"""
QDialog#settings_dialog, QWidget#dashboard {{ background: {c['surface']}; color: {c['text']}; }}
QWidget {{ color: {c['text']}; background: transparent; font-family: Inter; font-size: {px(14)}px; }}
QLabel, QCheckBox {{ background: transparent; }}
QLabel#hint, QLabel#secondary {{ color: {c['muted']}; }}
QLabel#page_title {{ font-size: {px(26)}px; font-weight: 600; }}
QLabel#section_title {{ font-size: {px(20)}px; font-weight: 600; }}
QLabel#form_error {{ color: {c['danger']}; }}
QLabel#hint, QLabel#secondary {{ font-size: {px(12)}px; }}
QLabel#brand, QLabel#row_title {{ font-size: {px(16)}px; font-weight: 600; }}
QLineEdit, QComboBox, QAbstractSpinBox, QPlainTextEdit {{
    background: {c['raised']}; color: {c['text']}; border: {px(1)}px solid {c['border']};
    border-radius: {px(RADIUS['control'])}px; padding: {px(10)}px;
    selection-background-color: {c['successSurface']}; selection-color: {c['text']};
}}
QLineEdit, QComboBox, QAbstractSpinBox {{ min-height: {px(22)}px; }}
QComboBox {{ padding-right: {px(32)}px; }}
QComboBox::drop-down {{ border: none; width: {px(32)}px; }}
QComboBox::down-arrow {{ image: url({arrow}); width: {px(12)}px; height: {px(12)}px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0; border: none; }}
QAbstractSpinBox QLineEdit, QComboBox QLineEdit {{ border: none; background: transparent; padding: 0; min-height: 0; }}
QPushButton, QToolButton {{ background: {c['raised']}; color: {c['text']};
    border: {px(1)}px solid {c['border']}; border-radius: {px(RADIUS['control'])}px;
    padding: {px(8)}px {px(20)}px; min-height: {px(22)}px; font-weight: 600; }}
QPushButton:hover, QToolButton:hover {{ background: {c['border']}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {c['canvas']}; }}
QPushButton:disabled, QToolButton:disabled, QAbstractSpinBox:disabled, QLineEdit:disabled {{ color: {c['subtle']}; }}
QPushButton#primary {{ background: {c['accent']}; color: {c['onAccent']}; border-color: {c['accent']}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {c['accentHover']}; }}
QPushButton#primary:pressed {{ background: {c['accentPressed']}; }}
QPushButton#primary:disabled {{ background: {c['raised']}; color: {c['subtle']}; border-color: {c['border']}; }}
QPushButton#danger {{ color: {c['danger']}; background: {c['dangerSurface']}; }}
QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: {px(12)}px {px(24)}px; border-radius: {px(10)}px; }}
QTabBar::tab:selected {{ background: {c['raised']}; color: {c['text']}; }}
QTabBar::tab:selected {{ font-weight: 600; }}
QTabBar::tab:hover:!selected {{ background: {c['raised']}; }}
QHeaderView::section {{ background: {c['raised']}; color: {c['muted']}; padding: {px(12)}px; border: none; }}
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus, QPlainTextEdit:focus {{ border-color: {c['focus']}; }}
QFrame#card, QWidget#metric_visibility_row {{ border: 1px solid {c['border']}; border-radius: {px(RADIUS['card'])}px; }}
QFrame#balance_card, QFrame#ports_card {{ background: {c['canvas']}; border: 1px solid {c['border']}; border-radius: {px(RADIUS['card'])}px; }}
QGroupBox {{ border: none; margin-top: {px(32)}px; padding: {px(16)}px 0 0 0; font-size: {px(20)}px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: {px(16)}px; color: {c['text']}; }}
QComboBox QAbstractItemView {{ background: {c['raised']}; color: {c['text']}; selection-background-color: {c['successSurface']}; selection-color: {c['text']}; }}
QListWidget, QTableWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ padding: {px(12)}px; }}
QTableWidget::item:selected, QListWidget::item:selected {{ background: {c['successSurface']}; color: {c['text']}; }}
QTableWidget {{ alternate-background-color: {c['surface']}; }}
QCheckBox {{ spacing: {px(8)}px; min-height: {px(28)}px; }}
QCheckBox::indicator {{ width: {px(18)}px; height: {px(18)}px; background: {c['raised']}; border: 1px solid {c['border']}; border-radius: {px(4)}px; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; image: url({check}); }}
QCheckBox[switch="true"] {{ spacing: 0; min-height: {px(24)}px; }}
QCheckBox[switch="true"]::indicator {{ width: {px(40)}px; height: {px(24)}px; border: none; border-radius: {px(12)}px; background: {c['border']}; image: url({off}); }}
QCheckBox[switch="true"]::indicator:checked {{ background: {c['accent']}; image: url({on}); }}
QSlider::groove:horizontal {{ height: {px(4)}px; background: {c['border']}; border-radius: {px(2)}px; }}
QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: {px(2)}px; }}
QSlider::handle:horizontal {{ width: {px(20)}px; margin: -{px(8)}px 0; background: {c['accent']}; border: 1px solid {c['accent']}; border-radius: {px(10)}px; }}
QSlider::handle:horizontal:focus {{ border: 2px solid {c['focus']}; }}
QSlider::sub-page:horizontal:disabled, QSlider::handle:horizontal:disabled {{ background: {c['subtle']}; }}
QSlider::handle:horizontal:disabled {{ border-color: {c['subtle']}; }}
QToolButton:checked {{ background: {c['successSurface']}; border-color: {c['accent']}; }}
QScrollArea, QScrollArea > QWidget > QWidget, QStackedWidget {{ background: transparent; border: none; }}
QToolTip {{ color: {c['text']}; background: {c['raised']}; border: 1px solid {c['border']}; padding: {px(8)}px; }}
QListWidget#navigation {{ background: transparent; border: none; padding: 0; }}
QListWidget#navigation::item {{ padding: 0; margin: 0; border: none; background: transparent; }}
QToolButton[icon_button="true"] {{ padding: 0; min-width: {px(34)}px; max-width: {px(34)}px; min-height: {px(34)}px; max-height: {px(34)}px; }}
QToolButton[accordion="true"] {{ border: none; text-align: left; font-weight: 400; padding: {px(10)}px {px(16)}px; }}
QLabel#status_badge {{ background: {c['raised']}; border-radius: {px(14)}px; padding: {px(6)}px {px(10)}px; font-size: {px(12)}px; font-weight: 600; }}
QWidget#title_bar {{ border-bottom: 1px solid {c['border']}; }}
QWidget#sidebar {{ border-right: 1px solid {c['border']}; }}
QWidget#settings_footer {{ border-top: 1px solid {c['border']}; }}
QLabel#keycap {{ background: {c['raised']}; border: 1px solid {c['border']}; border-radius: {px(8)}px; padding: {px(7)}px {px(12)}px; font-weight: 600; }}
QFrame#hotkeyComboInput {{ background: {c['raised']}; border: 1px solid {c['border']}; border-radius: {px(10)}px; }}
QFrame#hotkeyComboInput[state="recording"] {{ border-color: {c['focus']}; }}
QFrame#hotkeyComboInput[state="conflict"] {{ border-color: {c['danger']}; }}
QFrame#hotkeyComboInput:disabled {{ color: {c['subtle']}; }}
""" + menu_style(scale)


def menu_style(scale: float = 1) -> str:
    c = COLORS
    return f"""QMenu, QMessageBox {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']}; }}
QMenu {{ padding: {round(8 * scale)}px; border-radius: {round(12 * scale)}px; }}
QMenu::item {{ padding: {round(10 * scale)}px {round(24 * scale)}px; min-height: {round(20 * scale)}px; border-radius: {round(10 * scale)}px; }}
QMenu::item:selected {{ background: {c['successSurface']}; color: {c['accent']}; }}
QMenu::item:disabled {{ color: {c['subtle']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: {round(8 * scale)}px; }}"""
