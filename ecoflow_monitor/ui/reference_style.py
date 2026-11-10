from pathlib import Path
from ecoflow_monitor.ui.tokens import COLORS, RADIUS


def icon_path(name: str) -> str:
    return str(Path(__file__).parent / 'assets' / f'{name}.svg')


TOGGLE_SWITCH_WIDTH_PX = 40

TOGGLE_SWITCH_HEIGHT_PX = 24

def bordered_input_style(object_name: str, accent: str = COLORS["accent"]) -> str:
    return f"""
QFrame#{object_name} {{
    border: 1px solid {COLORS['border']};
    border-radius: {RADIUS['control']}px;
    background: {COLORS['raised']};
}}
QFrame#{object_name}:focus {{
    border-color: {COLORS['focus']};
}}
"""

def toggle_switch_style(object_name: str, accent: str = COLORS["accent"]) -> str:
    off_path = icon_path("toggle_off").replace("\\", "/")
    on_path = icon_path("toggle_on").replace("\\", "/")
    return f"""
QCheckBox#{object_name}::indicator {{
    width: {TOGGLE_SWITCH_WIDTH_PX}px;
    height: {TOGGLE_SWITCH_HEIGHT_PX}px;
    background: {COLORS['border']};
    border-radius: 12px;
    image: url({off_path});
}}
QCheckBox#{object_name}::indicator:checked {{
    background: {accent};
    image: url({on_path});
}}
"""
