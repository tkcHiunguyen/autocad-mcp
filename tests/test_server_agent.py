from __future__ import annotations

import json

from autocad_mcp import server
from autocad_mcp.agent_runtime import AgentRuntime
from autocad_mcp.backends.base import CommandResult
from tests.test_agent_runtime import FakeAgentBackend, _backend


async def test_agent_interpret_is_local_and_read_only() -> None:
    response = json.loads(await server.agent("interpret", {"request": "find PM4"}))

    assert response["ok"] is True
    assert response["payload"]["intent"] == "query"


async def test_agent_execute_can_start_a_session_and_stop_at_safe_boundary(monkeypatch) -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    monkeypatch.setattr(server, "agent_runtime", runtime)

    response = json.loads(
        await server.agent(
            "execute",
            {
                "request": "find PM4",
                "mode": "read_only",
            },
        )
    )

    assert response["ok"] is True
    assert response["payload"]["intent"] == "query"
    assert response["payload"]["state"] == "OBSERVED"
    assert response["payload"]["next_action"] == "answer_query"
    assert response["payload"]["session_id"]
    assert backend.preview_count == 0
    assert backend.apply_count == 0


async def test_agent_resume_requires_an_existing_session() -> None:
    response = json.loads(await server.agent("resume", {"request": "find PM4"}))

    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_REQUEST"


async def test_agent_cancel_exposes_terminal_status(monkeypatch) -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    monkeypatch.setattr(server, "agent_runtime", runtime)
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]

    response = json.loads(
        await server.agent("cancel", {"session_id": sid, "reason": "stop"})
    )

    assert response["ok"] is True
    assert response["payload"]["status"] == "cancelled"


async def test_session_exposes_handshake(monkeypatch) -> None:
    class Backend:
        async def session_handshake(self) -> CommandResult:
            return CommandResult(ok=True, payload={"session_id": "s1", "document_id": "d1"})

        async def session_health(self) -> CommandResult:
            return CommandResult(ok=True, payload={"connected": True})

        async def capabilities_list(self) -> CommandResult:
            return CommandResult(ok=True, payload={"capabilities": {}})

    async def get_backend():
        return Backend()

    monkeypatch.setattr(server, "get_backend", get_backend)
    response = json.loads(await server.session("handshake"))
    assert response["payload"]["session_id"] == "s1"
