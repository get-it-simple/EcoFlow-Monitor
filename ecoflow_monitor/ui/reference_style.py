from pathlib import Path


def icon_path(name: str) -> str:
    return str(Path(__file__).parent / 'assets' / f'{name}.svg')


TOGGLE_SWITCH_WIDTH_PX = 36

TOGGLE_SWITCH_HEIGHT_PX = 20

def bordered_input_style(object_name: str, accent: str = "#3a96dd") -> str:
    return f"""
QFrame#{object_name} {{
    border: 1px solid #3f3f42;
    border-radius: 4px;
    background: #2d2d30;
}}
QFrame#{object_name}:focus {{
    border-color: {accent};
}}
"""

def toggle_switch_style(object_name: str, accent: str = "#3a96dd") -> str:
    off_path = icon_path("toggle_off").replace("\\", "/")
    on_path = icon_path("toggle_on").replace("\\", "/")
    return f"""
QCheckBox#{object_name}::indicator {{
    width: {TOGGLE_SWITCH_WIDTH_PX}px;
    height: {TOGGLE_SWITCH_HEIGHT_PX}px;
    background: #555555;
    border-radius: 10px;
    image: url({off_path});
}}
QCheckBox#{object_name}::indicator:checked {{
    background: {accent};
    image: url({on_path});
}}
"""
