"""Pure viewport geometry helpers shared by view operations and tests."""

from __future__ import annotations

import math
from typing import Any


def pixel_rect_to_world(
    state: dict[str, Any],
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    padding: float = 0.0,
) -> dict[str, float]:
    """Map a rectangle from viewport screenshot pixels to drawing coordinates."""
    viewport = state["viewport_pixel_rect"]
    world = state["world_bounds"]
    viewport_left = float(viewport["left"])
    viewport_top = float(viewport["top"])
    viewport_right = float(viewport["right"])
    viewport_bottom = float(viewport["bottom"])
    width = viewport_right - viewport_left
    height = viewport_bottom - viewport_top

    values = (left, top, right, bottom, padding, width, height)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("Pixel coordinates and padding must be finite numbers")
    if width <= 0 or height <= 0:
        raise ValueError("Viewport pixel rectangle has invalid dimensions")
    if padding < 0:
        raise ValueError("padding must be greater than or equal to zero")
    if right <= left or bottom <= top:
        raise ValueError("Pixel rectangle must have positive width and height")
    if (
        left < viewport_left
        or top < viewport_top
        or right > viewport_right
        or bottom > viewport_bottom
    ):
        raise ValueError("Pixel rectangle must stay inside the viewport screenshot")

    world_width = float(world["xmax"]) - float(world["xmin"])
    world_height = float(world["ymax"]) - float(world["ymin"])
    xmin = float(world["xmin"]) + ((left - viewport_left) / width) * world_width
    xmax = float(world["xmin"]) + ((right - viewport_left) / width) * world_width
    ymax = float(world["ymax"]) - ((top - viewport_top) / height) * world_height
    ymin = float(world["ymax"]) - ((bottom - viewport_top) / height) * world_height

    x_padding = (xmax - xmin) * padding
    y_padding = (ymax - ymin) * padding
    return {
        "xmin": xmin - x_padding,
        "ymin": ymin - y_padding,
        "xmax": xmax + x_padding,
        "ymax": ymax + y_padding,
    }


def view_states_differ(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    relative_tolerance: float = 1e-6,
) -> bool:
    """Return True when the viewport center or size changed materially."""
    before_center = before.get("view_center") or (0.0, 0.0)
    after_center = after.get("view_center") or (0.0, 0.0)
    pairs = (
        (float(before_center[0]), float(after_center[0])),
        (float(before_center[1]), float(after_center[1])),
        (float(before.get("view_height", 0.0)), float(after.get("view_height", 0.0))),
        (float(before.get("view_width", 0.0)), float(after.get("view_width", 0.0))),
    )
    return any(
        not math.isclose(old, new, rel_tol=relative_tolerance, abs_tol=relative_tolerance)
        for old, new in pairs
    )
