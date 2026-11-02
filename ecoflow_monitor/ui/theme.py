from PySide6.QtCore import QObject
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from ecoflow_monitor.adapters.reference_effects import get_accent_color
from ecoflow_monitor.ui.reference_style import icon_path


def current_accent() -> str:
    watcher = getattr(QApplication.instance(), "ecoflow_accent", None)
    return watcher.color if watcher is not None else get_accent_color()


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
        watcher = getattr(QApplication.instance(), "ecoflow_accent", None)
        if watcher is not None:
            watcher.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        self.widget.setStyleSheet(self.prefix + self.factory(current_accent()))


def bind_style(widget, factory):
    return StyleBinding(widget, factory)


def settings_style(accent: str) -> str:
    text = accent_text(accent)
    check = icon_path("check_black" if text == "#000000" else "check_white").replace("\\", "/")
    return _SETTINGS_STYLE.replace("CHECK_ICON", check).replace("ACCENT_TEXT", text).replace("ACCENT", accent)


_SETTINGS_STYLE = """
QDialog#settings_dialog { background: #202020; color: #f2f2f5; }
QWidget { color: #f2f2f5; background: transparent; }
QLabel, QCheckBox { background: transparent; }
QLabel#hint { color: #c2c2c8; padding: 6px; }
QLineEdit, QComboBox, QAbstractSpinBox, QListWidget, QTableWidget, QPlainTextEdit {
    background: #181818; color: #f2f2f5; border: 1px solid #505050;
    border-radius: 8px; padding: 7px; selection-background-color: ACCENT; selection-color: ACCENT_TEXT;
}
QPushButton, QToolButton { background: rgba(255,255,255,12); color: #f2f2f5; border: none; border-radius: 8px; padding: 6px; }
QPushButton:hover, QToolButton:hover { background: rgba(255,255,255,22); }
QPushButton:disabled { color: #9a9a9a; }
QTabWidget::pane { border: 1px solid #424242; border-radius: 8px; background: #202020; }
QTabBar::tab { background: #202020; color: #c2c2c8; padding: 10px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { background: ACCENT; color: ACCENT_TEXT; border-bottom: 2px solid ACCENT; }
QTabBar::tab:hover:!selected { background: #303030; }
QHeaderView::section { background: #292929; color: #f2f2f5; padding: 7px; border: none; }
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus, QListWidget:focus, QTableWidget:focus, QPlainTextEdit:focus { border: 1px solid ACCENT; }
QPushButton#primary { background: ACCENT; color: ACCENT_TEXT; border: 1px solid ACCENT; }
QPushButton#primary:hover { border: 1px solid ACCENT_TEXT; }
QGroupBox { border: 1px solid #424242; border-radius: 8px; margin-top: 12px; padding: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #f2f2f5; }
QComboBox QAbstractItemView { background: #202020; selection-background-color: ACCENT; selection-color: ACCENT_TEXT; }
QListWidget::item { padding: 7px; }
QTableWidget::item:selected, QListWidget::item:selected { background: ACCENT; color: ACCENT_TEXT; }
QTableWidget { alternate-background-color: #242424; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { background: #202020; border: 1px solid #808080; border-radius: 3px; }
QCheckBox::indicator:checked { background: ACCENT; border: 1px solid ACCENT; image: url(CHECK_ICON); }
QCheckBox::indicator:focus { border: 1px solid #f2f2f5; }
QSlider::groove:horizontal { height: 4px; background: #505050; border-radius: 2px; }
QSlider::sub-page:horizontal { background: ACCENT; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; margin: -5px 0; background: ACCENT; border: 1px solid ACCENT_TEXT; border-radius: 7px; }
QSlider::handle:horizontal:focus { border: 2px solid ACCENT_TEXT; }
QSlider::sub-page:horizontal:disabled, QSlider::handle:horizontal:disabled { background: #606060; border-color: #808080; }
QAbstractSpinBox:disabled { color: #909090; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QToolTip { color: #f2f2f5; background: #202020; border: 1px solid #3f3f42; }
"""


SETTINGS_STYLE = settings_style("#3a96dd")


def monitor_style(scale: float, accent: str, transparent: bool, background_opacity: float = 140 / 255) -> str:
    background = f"rgba(32,32,32,{round(background_opacity * 255)})" if transparent else "rgb(32,32,32)"
    return f"""QWidget {{ color: #f2f2f5; background: transparent; }}
#monitor_shell {{ background: {background}; border: 1px solid {accent}; border-radius: 8px; }}
#monitor_title {{ font-size: {round(17 * scale)}px; font-weight: 600; }}
#monitor_version {{ color: #9a9a9a; font-size: {round(11 * scale)}px; }}
QToolButton {{ background: rgba(255,255,255,12); border: none; border-radius: {round(8 * scale)}px; padding: {round(6 * scale)}px; }}
QToolButton:hover {{ background: rgba(255,255,255,22); }}
QPushButton {{ background: rgba(0,0,0,70); border: 1px solid transparent; border-radius: {round(8 * scale)}px; padding: {round(6 * scale)}px; }}
QPushButton:hover {{ background: rgba(0,0,0,100); }}
QPushButton:checked {{ background: {accent}; color: {accent_text(accent)}; }}
QPushButton:disabled {{ color: #9a9a9a; }}
QCheckBox {{ spacing: {round(8 * scale)}px; }}
QComboBox {{ background: #202020; color: #f2f2f5; border: 1px solid #505050; border-radius: 4px; padding: 3px; }}
QComboBox:focus {{ border-color: {accent}; }}
QComboBox QAbstractItemView {{ background: #202020; color: #f2f2f5; selection-background-color: {accent}; selection-color: {accent_text(accent)}; }}
QToolTip {{ color: #f2f2f5; background: #202020; border: 1px solid #3f3f42; }}
"""
