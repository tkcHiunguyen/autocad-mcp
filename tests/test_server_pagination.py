from __future__ import annotations

import json

from autocad_mcp import server
from autocad_mcp.backends.base import CommandResult


class FakeBackend:
    async def drawing_info(self) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={"entity_count": 12, "layers": [f"Layer-{index}" for index in range(125)]},
        )

    async def layer_list(self) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={"layers": [{"name": f"Layer-{index}"} for index in range(125)]},
        )


async def _fake_get_backend() -> FakeBackend:
    return FakeBackend()


async def test_layer_list_is_paged_by_default(monkeypatch) -> None:
    monkeypatch.setattr(server, "get_backend", _fake_get_backend)

    response = json.loads(await server.layer("list"))
    payload = response["payload"]

    assert len(payload["layers"]) == 50
    assert payload["layers"][0]["name"] == "Layer-0"
    assert payload["pagination"] == {
        "total": 125,
        "offset": 0,
        "limit": 50,
        "returned": 50,
        "has_more": True,
        "next_offset": 50,
    }


async def test_layer_list_returns_the_final_page(monkeypatch) -> None:
    monkeypatch.setattr(server, "get_backend", _fake_get_backend)

    response = json.loads(await server.layer("list", {"offset": 100, "limit": 50}))
    payload = response["payload"]

    assert len(payload["layers"]) == 25
    assert payload["layers"][0]["name"] == "Layer-100"
    assert payload["pagination"]["has_more"] is False
    assert payload["pagination"]["next_offset"] is None


async def test_drawing_info_pages_its_layer_names(monkeypatch) -> None:
    monkeypatch.setattr(server, "get_backend", _fake_get_backend)

    response = json.loads(await server.drawing("info", {"offset": 25, "limit": 25}))
    payload = response["payload"]

    assert payload["entity_count"] == 12
    assert payload["layers"] == [f"Layer-{index}" for index in range(25, 50)]
    assert payload["pagination"]["total"] == 125
    assert payload["pagination"]["next_offset"] == 50
