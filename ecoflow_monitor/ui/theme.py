SETTINGS_STYLE = """
QDialog#settings_dialog { background: #202020; color: #f2f2f5; }
QWidget { color: #f2f2f5; background: transparent; }
QLabel, QCheckBox { background: transparent; }
QLabel#hint { color: #c2c8c5; padding: 6px; }
QLineEdit, QComboBox, QAbstractSpinBox, QListWidget, QTableWidget, QPlainTextEdit {
    background: #181c1a; color: #f2f2f5; border: 1px solid #505a55;
    border-radius: 8px; padding: 7px; selection-background-color: #275c46;
}
QPushButton, QToolButton { background: rgba(255,255,255,12); color: #f2f2f5; border: none; border-radius: 8px; padding: 6px; }
QPushButton:hover, QToolButton:hover { background: rgba(255,255,255,22); }
QPushButton:disabled { color: #9a9a9a; }
QTabWidget::pane { border: 1px solid #424e47; border-radius: 8px; background: #202020; }
QTabBar::tab { background: #202020; color: #c2c8c5; padding: 10px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { background: #253a30; color: #75edb7; border-bottom: 2px solid #75edb7; }
QTabBar::tab:hover { background: #2b332f; }
QHeaderView::section { background: #253a30; color: #f2f2f5; padding: 7px; border: none; }
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus, QListWidget:focus, QTableWidget:focus, QPlainTextEdit:focus { border: 1px solid #75edb7; }
QPushButton#primary { background: #275c46; border: 1px solid #75edb7; }
QPushButton#primary:hover { background: #34795d; }
QGroupBox { border: 1px solid #424e47; border-radius: 8px; margin-top: 12px; padding: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #75edb7; }
QComboBox QAbstractItemView { background: #202020; selection-background-color: #275c46; }
QListWidget::item { padding: 7px; }
QTableWidget::item:selected, QListWidget::item:selected { background: #275c46; color: #ffffff; }
QTableWidget { alternate-background-color: #202722; }
QCheckBox { spacing: 8px; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QToolTip { color: #f2f2f5; background: #202020; border: 1px solid #3f3f42; }
"""


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
QPushButton:checked {{ background: {accent}; }}
QPushButton:disabled {{ color: #9a9a9a; }}
QCheckBox {{ spacing: {round(8 * scale)}px; }}
QToolTip {{ color: #f2f2f5; background: #202020; border: 1px solid #3f3f42; }}
"""
