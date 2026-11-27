from dataclasses import dataclass

from ecoflow_monitor.settings import WidgetPreferences


@dataclass(frozen=True)
class WidgetLayout:
    mode: str
    visible: tuple[str, ...]
    details: tuple[str, ...]
    columns: int
    wide: bool


def widget_layout(preferences: WidgetPreferences, width: float, height: float, columns: int, rows: int, geometry: dict, *, capacity: int | None = None) -> WidgetLayout:
    wide = width >= geometry["wideWidth"] and height >= geometry["smallHeight"]
    large = wide or width >= geometry["largeWidth"] and height >= geometry["largeHeight"]
    mode = "large" if large else "small" if width >= geometry["smallWidth"] and height >= geometry["smallHeight"] else "minimal"
    allowed = preferences.large_metrics if large else preferences.small_metrics
    candidates = tuple(key for key in preferences.metric_order if key in allowed)
    count = max(1, columns) * max(0, rows) if capacity is None else capacity
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


def compact_metric_geometry(sizes: dict[str, tuple[int, int]], width: int, height: int, gap: int, *,
                            preferred_widths: dict[str, int] | None = None) -> dict[str, Rectangle]:
    rows: list[list[str]] = []
    row_width = row_height = used_height = 0
    for key, (cell_width, cell_height) in sizes.items():
        if cell_width > width:
            break
        if not rows or row_width + gap + cell_width > width:
            if rows:
                used_height += row_height + gap
            if used_height + cell_height > height:
                break
            rows.append([key])
            row_width, row_height = cell_width, cell_height
        else:
            if used_height + max(row_height, cell_height) > height:
                break
            rows[-1].append(key)
            row_width += gap + cell_width
            row_height = max(row_height, cell_height)

    row_heights = [max(sizes[key][1] for key in row) for row in rows]
    extra_height = max(0, height - sum(row_heights) - gap * (len(rows) - 1)) // max(1, len(rows))
    result = {}
    y = 0
    for row, baseline_height in zip(rows, row_heights):
        row_height = baseline_height + extra_height
        room = width - sum(sizes[key][0] for key in row) - gap * (len(row) - 1)
        growth = {}
        if preferred_widths:
            needs_space = {key: preferred_widths[key] - sizes[key][0]
                           for key in row if preferred_widths.get(key, 0) > sizes[key][0]}
            share = room // max(1, len(needs_space))
            growth = {key: min(shortfall, share) for key, shortfall in needs_space.items()}
        extra_width = (room - sum(growth.values())) // len(row)
        x = 0
        for key in row:
            cell_width = sizes[key][0] + growth.get(key, 0) + extra_width
            result[key] = Rectangle(x, y, cell_width, row_height)
            x += cell_width + gap
        y += row_height + gap
    return result


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
