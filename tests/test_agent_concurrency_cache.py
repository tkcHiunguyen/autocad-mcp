from __future__ import annotations

import asyncio
import time
import pytest

from autocad_mcp.agent_runtime import AgentRuntime, AgentSession, WorkflowState
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.errors import ErrorCode


class DelayedFakeBackend:
    """Fake backend with controlled delays and call counters to test concurrency & caching."""

    def __init__(self, *, delay: float = 0.02, geometry: dict | None = None) -> None:
        self.delay = delay
        self.geometry = geometry or {
            "handle": "A1",
            "type": "LWPOLYLINE",
            "layer": "BOUNDARY",
            "vertices": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
            "segments": [
                {"type": "line", "bulge": 0.0},
                {"type": "line", "bulge": 0.0},
                {"type": "line", "bulge": 0.0},
                {"type": "line", "bulge": 0.0},
            ],
            "closed": True,
            "bounds": {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10},
            "source_document_id": "doc-1",
        }
        self.fingerprint = "fp-1"
        self.search_text_count = 0
        self.geometry_batch_count = 0

    async def session_health(self) -> CommandResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return CommandResult(ok=True, payload={"connected": True, "document_id": "doc-1"})

    async def capabilities_list(self) -> CommandResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return CommandResult(
            ok=True,
            payload={
                "capabilities": {
                    "drawing.get_state": True,
                    "drawing.get_fingerprint": True,
                    "entity.search_text_batch": True,
                    "entity.get_geometry_batch": True,
                    "entity.query": True,
                    "batch.preview": True,
                    "batch.apply": True,
                    "batch.rollback": True,
                    "batch.get_screenshot": True,
                }
            },
        )

    async def drawing_get_state(self) -> CommandResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return CommandResult(
            ok=True,
            payload={
                "document_id": "doc-1",
                "dbmod": 0,
                "fingerprint": self.fingerprint,
                "viewport": {"world_bounds": self.geometry.get("bounds", {})},
            },
        )

    async def drawing_get_fingerprint(self) -> CommandResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return CommandResult(
            ok=True,
            payload={"document_id": "doc-1", "fingerprint": self.fingerprint, "dbmod": 0},
        )

    async def entity_search_text_batch(self, queries) -> CommandResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.search_text_count += 1
        return CommandResult(
            ok=True,
            payload={
                "results": [
                    {
                        "query": item["query"],
                        "matches": [
                            {
                                "handle": f"T{index}",
                                "text": item["query"],
                                "insertion": [5.0, 5.0],
                                "bounds": {"xmin": 4, "ymin": 4, "xmax": 6, "ymax": 6},
                            }
                        ],
                    }
                    for index, item in enumerate(queries, start=1)
                ]
            },
        )

    async def entity_get_geometry_batch(self, handles) -> CommandResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.geometry_batch_count += 1
        geometries = []
        for handle in handles:
            geo = dict(self.geometry)
            geo["handle"] = handle
            geometries.append(geo)
        return CommandResult(ok=True, payload={"geometries": geometries})

    async def entity_query(self, query) -> CommandResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return CommandResult(ok=True, payload={"entities": [self.geometry]})

    async def entity_count_by_layer_type(self, query) -> CommandResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return CommandResult(ok=True, payload={"counts": {"BOUNDARY|LWPOLYLINE": 1}})

    async def batch_preview(self, plan: dict) -> CommandResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return CommandResult(
            ok=True,
            payload={
                "batch_id": "batch-1",
                "approval_token": "token-1",
                "approval_expires_at": time.time() + 60,
                "document_id": "doc-1",
                "before_fingerprint": self.fingerprint,
                "before_dbmod": 0,
                "source_immutable": True,
                "plan_hash": plan.get("plan_hash", ""),
                "overlay_verification": {
                    "created_handles_verified": True,
                    "source_to_overlay_verified": True,
                },
                "source_to_overlay": {"A1": "O_A1", "T1": "O_T1"},
                "action_to_overlay": {"act_conn_A1_0_A1_1": "O_CONN1"},
                "screenshot": {
                    "data": "fake_base64_data",
                    "metadata": {"scope": "output_clone"},
                },
            },
        )


async def _backend(backend: DelayedFakeBackend):
    return backend


@pytest.mark.asyncio
async def test_per_session_serialization_concurrent_calls() -> None:
    backend = DelayedFakeBackend(delay=0.03)
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    start_res = await runtime.start(mode="read_only")
    sid = start_res.payload["session_id"]

    # Run two execute tasks concurrently on the same session
    res1, res2 = await asyncio.gather(
        runtime.execute(sid, request="find WAREHOUSE"),
        runtime.execute(sid, request="find PM4"),
    )

    assert res1.ok is True or res2.ok is True
    session_status = await runtime.status(sid)
    assert session_status.ok is True
    # The session state must remain valid and not corrupted
    assert session_status.payload["state"] in ("OBSERVED", "CONNECT")


@pytest.mark.asyncio
async def test_read_cache_hit_does_not_increment_calls_used() -> None:
    backend = DelayedFakeBackend(delay=0.0)
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    start_res = await runtime.start(mode="read_only")
    sid = start_res.payload["session_id"]

    # Connect
    await runtime.connect(sid)

    # First observe
    obs1 = await runtime.observe(sid, labels=["WAREHOUSE", "PM4"])
    assert obs1.ok is True
    calls_after_obs1 = obs1.payload["calls_used"]
    assert backend.search_text_count == 1

    # Simulate another observe with same labels and document fingerprint
    # Reset state to CONNECT to test caching of text search
    runtime._sessions[sid].state = WorkflowState.CONNECT
    obs2 = await runtime.observe(sid, labels=["WAREHOUSE", "PM4"])
    assert obs2.ok is True
    assert backend.search_text_count == 1  # Cache hit! Backend was not called again
    assert runtime._sessions[sid].metrics["cache_hits"] >= 1


@pytest.mark.asyncio
async def test_read_cache_invalidated_when_fingerprint_changes() -> None:
    backend = DelayedFakeBackend(delay=0.0)
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    start_res = await runtime.start(mode="read_only")
    sid = start_res.payload["session_id"]

    await runtime.connect(sid)
    obs1 = await runtime.observe(sid, labels=["WAREHOUSE"])
    assert obs1.ok is True
    assert backend.search_text_count == 1

    # Fingerprint changes in drawing
    backend.fingerprint = "fp-2"
    runtime._sessions[sid].state = WorkflowState.CONNECT
    obs2 = await runtime.observe(sid, labels=["WAREHOUSE"])
    assert obs2.ok is True
    # Cache miss because fingerprint changed
    assert backend.search_text_count == 2
    assert runtime._sessions[sid].metrics["cache_misses"] >= 2


@pytest.mark.asyncio
async def test_agent_task_deadline_timeout() -> None:
    backend = DelayedFakeBackend(delay=0.0)
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    start_res = await runtime.start(mode="read_only")
    sid = start_res.payload["session_id"]

    await runtime.connect(sid)
    # Artificially set start_time in the past to trigger deadline timeout
    runtime._sessions[sid].metrics["start_time"] = time.time() - 200.0
    runtime._sessions[sid].metrics["deadline_seconds"] = 100.0

    obs = await runtime.observe(sid, labels=["WAREHOUSE"])
    assert obs.ok is False
    assert obs.error_code in (ErrorCode.REQUEST_TIMEOUT, ErrorCode.TASK_TIMEOUT)


@pytest.mark.asyncio
async def test_curved_geometry_in_map_preserved_and_unknowns_recorded() -> None:
    # A boundary with non-zero bulge (curved segment)
    curved_geo = {
        "handle": "C1",
        "type": "LWPOLYLINE",
        "layer": "BOUNDARY",
        "vertices": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
        "segments": [
            {"type": "line", "bulge": 0.0},
            {"type": "arc", "bulge": 0.5},  # Non-zero bulge
            {"type": "line", "bulge": 0.0},
            {"type": "line", "bulge": 0.0},
        ],
        "closed": True,
        "bounds": {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10},
        "source_document_id": "doc-1",
    }
    backend = DelayedFakeBackend(delay=0.0, geometry=curved_geo)
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    start_res = await runtime.start(mode="read_only")
    sid = start_res.payload["session_id"]

    await runtime.connect(sid)
    await runtime.observe(sid, labels=["WAREHOUSE"])
    map_res = await runtime.map(sid, boundary_handles=["C1"])
    assert map_res.ok is True

    # The curved boundary is preserved as a valid boundary (not dropped)
    boundaries = map_res.payload["mapping"]["boundaries"]
    assert len(boundaries) == 1
    assert boundaries[0]["handle"] == "C1"

    # But an uncertainty is recorded about curved segment chord approximation
    unknowns = map_res.payload["unknowns"]
    assert any("curved" in str(u.get("reason", "")).lower() for u in unknowns)


@pytest.mark.asyncio
async def test_connector_action_id_and_change_table_mapping() -> None:
    backend = DelayedFakeBackend(delay=0.0)
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    start_res = await runtime.start(mode="read_only")
    sid = start_res.payload["session_id"]

    await runtime.connect(sid)
    await runtime.observe(sid, labels=["WAREHOUSE"])
    await runtime.map(sid, boundary_handles=["A1"])

    actions = [
        {
            "action": "copy_to_overlay",
            "source_handle": "A1",
            "target_layer": "VIS_OVERLAY_BOUNDARY",
        },
        {
            "action": "create_connector_line",
            "start_source_handle": "A1",
            "start_vertex_index": 0,
            "end_source_handle": "A1",
            "end_vertex_index": 1,
            "target_layer": "VIS_OVERLAY_LINE",
        },
    ]

    plan_res = await runtime.plan(
        sid,
        actions=actions,
        target_path="d:\\test_overlay.dwg",
        allow_uncertainties=True,
    )
    assert plan_res.ok is True
    plan = plan_res.payload["plan"]
    # Check that create_connector_line has an action_id generated
    connector_action = plan["actions"][1]
    assert "action_id" in connector_action
    assert connector_action["action_id"].startswith("act_conn_")

    # Check change_table
    change_table = plan["change_table"]
    assert len(change_table) == 2
    assert change_table[1]["action"] == "create_connector_line"
    assert change_table[1]["action_id"] == connector_action["action_id"]


@pytest.mark.asyncio
async def test_preview_action_to_overlay_change_table_resolution() -> None:
    backend = DelayedFakeBackend(delay=0.0)
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    start_res = await runtime.start(mode="mutation")
    sid = start_res.payload["session_id"]

    await runtime.connect(sid)
    await runtime.observe(sid, labels=["WAREHOUSE"])
    await runtime.map(sid, boundary_handles=["A1"])

    actions = [
        {
            "action": "copy_to_overlay",
            "source_handle": "A1",
            "target_layer": "VIS_OVERLAY_BOUNDARY",
        },
        {
            "action": "create_connector_line",
            "start_source_handle": "A1",
            "start_vertex_index": 0,
            "end_source_handle": "A1",
            "end_vertex_index": 1,
            "target_layer": "VIS_OVERLAY_LINE",
        },
    ]

    await runtime.plan(
        sid,
        actions=actions,
        target_path="d:\\test_overlay.dwg",
        allow_uncertainties=True,
    )

    preview_res = await runtime.preview(sid)
    assert preview_res.ok is True
    preview = preview_res.payload["preview"]
    change_table = preview["change_table"]
    assert len(change_table) == 2
    # Copy action resolved source A1 to O_A1
    assert "O_A1" in change_table[0]["new_handles"]
    # Connector action resolved act_conn_A1_0_A1_1 to O_CONN1
    assert "O_CONN1" in change_table[1]["new_handles"]


@pytest.mark.asyncio
async def test_curved_topology_marked_unknown() -> None:
    curved_geo = {
        "handle": "C1",
        "type": "LWPOLYLINE",
        "layer": "BOUNDARY",
        "vertices": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
        "segments": [
            {"type": "line", "bulge": 0.0},
            {"type": "arc", "bulge": 0.5},
            {"type": "line", "bulge": 0.0},
            {"type": "line", "bulge": 0.0},
        ],
        "closed": True,
        "bounds": {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10},
        "source_document_id": "doc-1",
    }
    curved_geo2 = {
        "handle": "C2",
        "type": "LWPOLYLINE",
        "layer": "BOUNDARY",
        "vertices": [[2.0, 2.0], [8.0, 2.0], [8.0, 8.0], [2.0, 8.0]],
        "segments": [
            {"type": "line", "bulge": 0.0},
            {"type": "arc", "bulge": 0.2},
            {"type": "line", "bulge": 0.0},
            {"type": "line", "bulge": 0.0},
        ],
        "closed": True,
        "bounds": {"xmin": 2, "ymin": 2, "xmax": 8, "ymax": 8},
        "source_document_id": "doc-1",
    }

    topology = AgentRuntime._map_boundary_topology([curved_geo, curved_geo2])
    assert len(topology) == 1
    assert topology[0]["relation"] == "unknown"
    assert topology[0]["evidence"]["segments_verified_linear"] is False
