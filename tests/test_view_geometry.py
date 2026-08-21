from __future__ import annotations

import pytest

from autocad_mcp.view_geometry import pixel_rect_to_world, view_states_differ


VIEW_STATE = {
    "view_center": [100.0, 200.0],
    "view_height": 100.0,
    "view_width": 200.0,
    "screen_width": 1000,
    "screen_height": 500,
    "world_bounds": {"xmin": 0.0, "ymin": 150.0, "xmax": 200.0, "ymax": 250.0},
    "viewport_pixel_rect": {
        "left": 0,
        "top": 0,
        "right": 1000,
        "bottom": 500,
        "width": 1000,
        "height": 500,
    },
}


def test_pixel_rect_maps_to_world_coordinates() -> None:
    bounds = pixel_rect_to_world(VIEW_STATE, 250, 125, 750, 375)

    assert bounds == {
        "xmin": pytest.approx(50.0),
        "ymin": pytest.approx(175.0),
        "xmax": pytest.approx(150.0),
        "ymax": pytest.approx(225.0),
    }


def test_pixel_rect_padding_expands_each_side() -> None:
    bounds = pixel_rect_to_world(VIEW_STATE, 250, 125, 750, 375, padding=0.1)

    assert bounds == {
        "xmin": pytest.approx(40.0),
        "ymin": pytest.approx(170.0),
        "xmax": pytest.approx(160.0),
        "ymax": pytest.approx(230.0),
    }


@pytest.mark.parametrize(
    "rect",
    [
        (-1, 0, 100, 100),
        (0, 0, 1001, 100),
        (100, 100, 100, 200),
        (100, 200, 200, 100),
    ],
)
def test_pixel_rect_rejects_invalid_coordinates(rect: tuple[int, int, int, int]) -> None:
    with pytest.raises(ValueError):
        pixel_rect_to_world(VIEW_STATE, *rect)


def test_view_state_change_detects_zoom() -> None:
    after = {**VIEW_STATE, "view_height": 40.0, "view_width": 80.0}
    assert view_states_differ(VIEW_STATE, after) is True
    assert view_states_differ(VIEW_STATE, dict(VIEW_STATE)) is False
