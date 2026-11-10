import json
from pathlib import Path


TOKENS = json.loads((Path(__file__).parent / "assets" / "design-tokens.json").read_text(encoding="utf-8"))
COLORS = {name: token["$value"] for name, token in TOKENS["color"].items()}
SPACE = TOKENS["space"]
RADIUS = TOKENS["radius"]
GEOMETRY = {"field_width": 320, "range_width": 536, "form_breakpoint": 620, "sidebar": 216, "header": 60, "control": 40, "field": 44}


def surface(opacity: float, role: str = "surface") -> str:
    color = COLORS[role].lstrip("#")
    red, green, blue = (int(color[index:index + 2], 16) for index in (0, 2, 4))
    return f"rgba({red},{green},{blue},{round(max(0, min(1, opacity)) * 255)})"
