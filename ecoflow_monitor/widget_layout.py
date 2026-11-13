from dataclasses import dataclass

from ecoflow_monitor.settings import METRICS, WidgetPreferences


@dataclass(frozen=True)
class WidgetLayout:
    mode: str
    visible: tuple[str, ...]
    details: tuple[str, ...]
    columns: int
    wide: bool


def widget_layout(preferences: WidgetPreferences, width: float, height: float, columns: int, rows: int, geometry: dict) -> WidgetLayout:
    wide = width >= geometry["wideWidth"] and height >= geometry["smallHeight"]
    large = wide or width >= geometry["largeWidth"] and height >= geometry["largeHeight"]
    mode = "large" if large else "small" if width >= geometry["smallWidth"] and height >= geometry["smallHeight"] else "minimal"
    allowed = preferences.large_metrics if large else preferences.small_metrics
    candidates = tuple(key for key in preferences.metric_order if key in allowed and (mode != "minimal" or key in METRICS[:3]))
    count = max(1, columns) * max(0, rows)
    visible = candidates[:count]
    details = tuple(key for key in preferences.metric_order if key in preferences.expanded_metrics or key in allowed and key not in visible)
    return WidgetLayout(mode, visible, details, max(1, columns), wide)


@dataclass(frozen=True)
class Rectangle:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height


def details_placement(core: Rectangle, area: Rectangle, desired: tuple[int, int], dock: str = "", gap: int = 8) -> tuple[str, Rectangle]:
    distances = {"left": core.x - area.x, "right": area.right - core.right, "top": core.y - area.y, "bottom": area.bottom - core.bottom}
    edge = dock or min(distances, key=distances.get)
    direction = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}[edge]
    if distances[direction] - gap < 80:
        direction = max(distances, key=distances.get)
    room = max(0, distances[direction] - gap)
    width, height = min(desired[0], area.width), min(desired[1], area.height)
    if room >= 80:
        if direction in {"left", "right"}:
            width = min(width, room)
        else:
            height = min(height, room)
    x = core.x - width - gap if direction == "left" else core.right + gap if direction == "right" else core.x
    y = core.y - height - gap if direction == "top" else core.bottom + gap if direction == "bottom" else core.y
    x = max(area.x, min(x, area.right - width))
    y = max(area.y, min(y, area.bottom - height))
    return direction, Rectangle(x, y, width, height)
