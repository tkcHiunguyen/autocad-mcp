from __future__ import annotations

import json

from autocad_mcp import server
from autocad_mcp.backends.base import CommandResult


STATE = {
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
    "ctab": "Model",
    "cvport": 2,
    "tilemode": 1,
}


class FakeBackend:
    async def get_view_state(self) -> CommandResult:
        return CommandResult(ok=True, payload=STATE)

    async def zoom_pixels(self, left, top, right, bottom, padding=0.0) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={
                "before": STATE,
                "after": {**STATE, "view_height": 40.0},
                "changed": True,
                "requested_world_bounds": {"xmin": 10, "ymin": 20, "xmax": 30, "ymax": 40},
            },
        )

    async def focus_entities(self, handles, padding=0.5) -> CommandResult:
        return CommandResult(ok=True, payload={"handles": handles, "padding": padding, "changed": True})

    async def get_screenshot(self, full_window=False) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={
                "data": "ZmFrZQ==",
                "metadata": {
                    "width": 1000,
                    "height": 500,
                    "capture_mode": "full_window" if full_window else "viewport",
                    "world_bounds": STATE["world_bounds"],
                },
            },
        )

    async def entity_search_text(self, query, match_mode="contains", limit=20, case_sensitive=False):
        return CommandResult(
            ok=True,
            payload={
                "matches": [{"handle": "ABC", "type": "MTEXT", "text": query}],
                "count": 1,
                "truncated": False,
            },
        )


async def _fake_get_backend() -> FakeBackend:
    return FakeBackend()


async def test_view_get_state(monkeypatch) -> None:
    monkeypatch.setattr(server, "get_backend", _fake_get_backend)
    response = json.loads(await server.view("get_state"))
    assert response["payload"]["world_bounds"] == STATE["world_bounds"]


async def test_view_zoom_pixels_and_focus_entities(monkeypatch) -> None:
    monkeypatch.setattr(server, "get_backend", _fake_get_backend)

    zoom = json.loads(await server.view("zoom_pixels", left=10, top=20, right=300, bottom=220))
    focus = json.loads(await server.view("focus_entities", handles=["ABC"], padding=0.75))

    assert zoom["payload"]["changed"] is True
    assert focus["payload"]["handles"] == ["ABC"]
    assert focus["payload"]["padding"] == 0.75


async def test_view_screenshot_returns_image_and_metadata(monkeypatch) -> None:
    monkeypatch.setattr(server, "get_backend", _fake_get_backend)
    response = await server.view("get_screenshot")

    metadata = json.loads(response[0].text)
    assert metadata["metadata"]["capture_mode"] == "viewport"
    assert response[1].type == "image"
    assert response[1].data == "ZmFrZQ=="


async def test_entity_search_text(monkeypatch) -> None:
    monkeypatch.setattr(server, "get_backend", _fake_get_backend)
    response = json.loads(
        await server.entity(
            "search_text",
            data={"query": "CONVERTING 2", "match_mode": "exact", "limit": 10},
        )
    )
    assert response["payload"]["matches"][0]["text"] == "CONVERTING 2"
