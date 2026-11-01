SETTINGS_STYLE = """
QDialog { background: #202020; color: #f2f2f5; }
QWidget { color: #f2f2f5; }
QLabel, QCheckBox { background: transparent; }
QLabel#hint { color: #b7b7bd; padding: 5px; }
QLineEdit, QComboBox, QAbstractSpinBox, QListWidget, QTableWidget {
    background: #2d2d30; color: #f2f2f5; border: 1px solid #3f3f42;
    border-radius: 4px; padding: 4px; selection-background-color: #626071;
}
QPushButton, QToolButton { background: rgba(255,255,255,12); color: #f2f2f5; border: none; border-radius: 8px; padding: 6px; }
QPushButton:hover, QToolButton:hover { background: rgba(255,255,255,22); }
QPushButton:disabled { color: #9a9a9a; }
QTabWidget::pane { border: 1px solid #3f3f42; }
QTabBar::tab { background: #202020; color: #b7b7bd; padding: 9px; }
QTabBar::tab:selected { background: #2d2d30; color: #f2f2f5; }
QHeaderView::section { background: #2d2d30; color: #f2f2f5; padding: 5px; border: 1px solid #3f3f42; }
QCheckBox { spacing: 8px; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QToolTip { color: #f2f2f5; background: #202020; border: 1px solid #3f3f42; }
"""


def monitor_style(scale: float, accent: str, transparent: bool) -> str:
    background = "rgba(32,32,32,140)" if transparent else "rgb(32,32,32)"
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
