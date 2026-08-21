from __future__ import annotations

import time

import pytest

from autocad_mcp.agent_runtime import AgentRuntime
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.errors import ErrorCode


class FakeAgentBackend:
    def __init__(self, *, geometry: dict | None = None, screenshot: bool = True) -> None:
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
        self.screenshot = screenshot
        self.fingerprint = "fp-1"
        self.preview_count = 0
        self.apply_count = 0
        self.rollback_count = 0

    async def session_health(self) -> CommandResult:
        return CommandResult(ok=True, payload={"connected": True, "document_id": "doc-1"})

    async def capabilities_list(self) -> CommandResult:
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
                    "batch.get_screenshot": self.screenshot,
                }
            },
        )

    async def drawing_get_state(self) -> CommandResult:
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
        return CommandResult(
            ok=True,
            payload={"document_id": "doc-1", "fingerprint": self.fingerprint, "dbmod": 0},
        )

    async def get_view_state(self) -> CommandResult:
        return CommandResult(ok=True, payload={"world_bounds": self.geometry.get("bounds", {})})

    async def entity_search_text_batch(self, queries) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={
                "results": [
                    {
                        "query": item["query"],
                        "matches": [
                            {
                                # Each label is a distinct source text entity.
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
        geometries = []
        for handle in handles:
            geometry = dict(self.geometry)
            geometry["handle"] = handle
            geometries.append(geometry)
        return CommandResult(ok=True, payload={"geometries": geometries})

    async def entity_query(self, query) -> CommandResult:
        return CommandResult(ok=True, payload={"entities": [self.geometry]})

    async def entity_count_by_layer_type(self, query) -> CommandResult:
        return CommandResult(ok=True, payload={"counts": {"BOUNDARY|LWPOLYLINE": 1}})

    async def batch_preview(self, plan) -> CommandResult:
        self.preview_count += 1
        return CommandResult(
            ok=True,
            payload={
                "batch_id": "batch-1",
                "approval_token": "token-1",
                "approval_expires_at": time.time() + 60,
                "document_id": "doc-1",
                "before_fingerprint": self.fingerprint,
                "before_dbmod": 0,
                "plan_hash": plan["plan_hash"],
                "source_immutable": True,
                "actions": plan["actions"],
                "source_to_overlay": {"A1": "B1", "T1": "T1"},
                "overlay_verification": {
                    "created_handles_verified": True,
                    "source_to_overlay_verified": True,
                },
                "screenshot": {"data": "preview-png", "metadata": {"scope": "output_clone"}},
            },
        )

    async def batch_apply(self, batch_id, approval_token, idempotency_key=None) -> CommandResult:
        self.apply_count += 1
        return CommandResult(
            ok=True,
            payload={
                "batch_id": batch_id,
                "output_path": "C:/tmp/factory_overlay.dwg",
                "source_unchanged": True,
                "removed_handles": [],
                "overlay_verification": {
                    "created_handles_verified": True,
                    "source_to_overlay_verified": True,
                },
                "source_to_overlay": {"A1": "B1", "T1": "T1"},
            },
        )

    async def batch_rollback(self, batch_id) -> CommandResult:
        self.rollback_count += 1
        return CommandResult(ok=True, payload={"batch_id": batch_id, "source_unchanged": True})

    async def batch_get_screenshot(self, batch_id) -> CommandResult:
        if not self.screenshot:
            return CommandResult.failure(ErrorCode.UNSUPPORTED_CAPABILITY, "no output screenshot")
        return CommandResult(
            ok=True,
            payload={
                "data": "png",
                "metadata": {"width": 10, "height": 10, "scope": "output_clone"},
            },
        )

async def make_runtime(backend: FakeAgentBackend, *, max_calls: int = 12):
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(max_calls=max_calls, mode="mutation")
    assert started.ok
    session_id = started.payload["session_id"]
    assert (await runtime.connect(session_id)).ok
    assert (await runtime.observe(session_id, labels=["AREA"])).ok
    assert (await runtime.map(session_id, boundary_handles=["A1"])).ok
    planned = await runtime.plan(
        session_id,
        actions=[{"action": "copy_to_overlay", "source_handle": "A1", "target_layer": "VIS"}],
        target_path="C:/tmp/factory_overlay.dwg",
    )
    assert planned.ok
    return runtime, session_id


async def _backend(backend):
    return backend


@pytest.mark.asyncio
async def test_apply_requires_approval_and_verifies_source() -> None:
    runtime, session_id = await make_runtime(FakeAgentBackend())
    assert (await runtime.preview(session_id)).ok
    denied = await runtime.apply(session_id, "wrong")
    assert denied.error_code == ErrorCode.INVALID_AGENT_STATE.value
    approved = await runtime.approve(session_id, "token-1", True)
    assert approved.ok
    applied = await runtime.apply(session_id, "token-1")
    assert applied.ok
    verified = await runtime.verify(session_id)
    assert verified.ok
    assert verified.payload["state"] == "VERIFIED"
    assert verified.payload["calls_used"] <= 12


@pytest.mark.asyncio
async def test_read_only_session_cannot_preview_mutation() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]
    await runtime.connect(sid)
    await runtime.observe(sid, labels=["AREA"])
    await runtime.map(sid, boundary_handles=["A1"])
    assert (
        await runtime.plan(
            sid,
            actions=[{"action": "preserve", "source_handle": "A1"}],
            target_path="x_overlay.dwg",
        )
    ).ok
    result = await runtime.preview(sid)
    assert result.error_code == ErrorCode.SOURCE_MUTATION_BLOCKED.value


@pytest.mark.asyncio
async def test_missing_geometry_stops_mapping_and_never_creates_plan() -> None:
    geometry = {"handle": "A1", "closed": False, "vertices": [[0, 0], [1, 1]]}
    runtime = AgentRuntime(backend_factory=lambda: _backend(FakeAgentBackend(geometry=geometry)))
    started = await runtime.start(mode="mutation")
    sid = started.payload["session_id"]
    await runtime.connect(sid)
    await runtime.observe(sid, labels=["AREA"])
    result = await runtime.map(sid, boundary_handles=["A1"])
    assert result.error_code == ErrorCode.GEOMETRY_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_call_budget_is_enforced() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(max_calls=1, mode="mutation")
    sid = started.payload["session_id"]
    result = await runtime.connect(sid)
    assert result.ok is False
    assert result.error_code == ErrorCode.CALL_BUDGET_EXCEEDED.value


@pytest.mark.asyncio
async def test_expired_approval_is_rejected() -> None:
    runtime, sid = await make_runtime(FakeAgentBackend())
    await runtime.preview(sid)
    runtime._sessions[sid].approval_expires_at = time.time() - 1
    result = await runtime.approve(sid, "token-1", True)
    assert result.error_code == ErrorCode.APPROVAL_EXPIRED.value


@pytest.mark.asyncio
async def test_screenshot_is_required_for_completed_verification() -> None:
    backend = FakeAgentBackend(screenshot=False)
    runtime, sid = await make_runtime(backend)
    result = await runtime.preview(sid)
    assert result.error_code == ErrorCode.UNSUPPORTED_CAPABILITY.value
    assert backend.preview_count == 0


@pytest.mark.asyncio
async def test_source_document_screenshot_cannot_verify_output() -> None:
    backend = FakeAgentBackend()
    runtime, sid = await make_runtime(backend)
    await runtime.preview(sid)
    await runtime.approve(sid, "token-1", True)
    await runtime.apply(sid, "token-1")

    async def source_screenshot(batch_id: str) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={
                "data": "png",
                "metadata": {"width": 10, "height": 10, "scope": "active_document"},
            },
        )

    backend.batch_get_screenshot = source_screenshot
    result = await runtime.verify(sid)
    assert result.error_code == ErrorCode.VERIFICATION_FAILED.value


@pytest.mark.asyncio
async def test_preview_rejects_missing_required_label_mapping() -> None:
    backend = FakeAgentBackend()
    runtime, sid = await make_runtime(backend)

    async def missing_label_preview(plan) -> CommandResult:
        payload = await FakeAgentBackend.batch_preview(backend, plan)
        assert payload.payload is not None
        payload.payload["source_to_overlay"] = {"A1": "B1"}
        return payload

    backend.batch_preview = missing_label_preview
    result = await runtime.preview(sid)

    assert result.error_code == ErrorCode.VERIFICATION_FAILED.value
    assert (await runtime.status(sid)).payload["state"] == "FAILED"


@pytest.mark.asyncio
async def test_preview_rejects_source_identity_mismatch() -> None:
    backend = FakeAgentBackend()
    runtime, sid = await make_runtime(backend)

    async def mismatched_preview(plan) -> CommandResult:
        payload = await FakeAgentBackend.batch_preview(backend, plan)
        assert payload.payload is not None
        payload.payload["before_fingerprint"] = "different-fingerprint"
        return payload

    backend.batch_preview = mismatched_preview
    result = await runtime.preview(sid)

    assert result.error_code == ErrorCode.VERIFICATION_FAILED.value
    assert (await runtime.status(sid)).payload["state"] == "FAILED"


@pytest.mark.asyncio
async def test_preview_rejects_missing_planned_source_mapping() -> None:
    backend = FakeAgentBackend()
    runtime, sid = await make_runtime(backend)

    async def missing_source_preview(plan) -> CommandResult:
        payload = await FakeAgentBackend.batch_preview(backend, plan)
        assert payload.payload is not None
        payload.payload["source_to_overlay"] = {"T1": "T1"}
        return payload

    backend.batch_preview = missing_source_preview
    result = await runtime.preview(sid)

    assert result.error_code == ErrorCode.VERIFICATION_FAILED.value
    assert "A1" in result.error_details["missing_source_handles"]


@pytest.mark.asyncio
async def test_unsaved_source_cannot_enter_mutation_preview() -> None:
    backend = FakeAgentBackend()

    async def unsaved_state() -> CommandResult:
        return CommandResult(
            ok=True,
            payload={
                "document_id": "doc-1",
                "dbmod": 1,
                "fingerprint": backend.fingerprint,
                "viewport": {},
            },
        )

    backend.drawing_get_state = unsaved_state
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="mutation")
    sid = started.payload["session_id"]
    assert (await runtime.connect(sid)).ok
    assert (await runtime.observe(sid, labels=["AREA"])).ok
    assert (await runtime.map(sid, boundary_handles=["A1"])).ok

    result = await runtime.plan(
        sid,
        actions=[{"action": "copy_to_overlay", "source_handle": "A1", "target_layer": "VIS"}],
        target_path="C:/tmp/factory_overlay.dwg",
    )
    assert result.error_code == ErrorCode.SOURCE_UNSAVED.value


@pytest.mark.asyncio
async def test_apply_rejects_unplanned_output_path_and_fails_session() -> None:
    backend = FakeAgentBackend()
    runtime, sid = await make_runtime(backend)
    assert (await runtime.preview(sid)).ok
    assert (await runtime.approve(sid, "token-1", True)).ok

    async def wrong_output(batch_id, approval_token, idempotency_key=None) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={
                "batch_id": batch_id,
                "output_path": "C:/tmp/other_overlay.dwg",
                "source_unchanged": True,
                "removed_handles": [],
                "overlay_verification": {
                    "created_handles_verified": True,
                    "source_to_overlay_verified": True,
                },
            },
        )

    backend.batch_apply = wrong_output
    result = await runtime.apply(sid, "token-1")
    assert result.error_code == ErrorCode.VERIFICATION_FAILED.value
    assert (await runtime.status(sid)).payload["state"] == "FAILED"


@pytest.mark.asyncio
async def test_preview_mapping_does_not_change_plan_hash() -> None:
    runtime, sid = await make_runtime(FakeAgentBackend())
    session = runtime._sessions[sid]
    original_hash = session.plan_hash
    assert (await runtime.preview(sid)).ok
    assert session.plan_hash == original_hash
    assert session.plan is not None
    assert AgentRuntime._hash_plan(session.plan) == original_hash
    assert session.preview is not None
    assert session.preview["change_table"][0]["new_handles"] == ["B1"]


def test_intent_interpretation_is_deterministic_and_clarifies_empty_requests() -> None:
    query = AgentRuntime.interpret("find PM4")
    assert query.ok is True
    assert query.payload["intent"] == "query"
    assert query.payload["needs_clarification"] is False

    empty = AgentRuntime.interpret("")
    assert empty.ok is True
    assert empty.payload["needs_clarification"] is True
    assert empty.payload["intent"] is None

    invalid = AgentRuntime.interpret("anything", "not-an-intent")
    assert invalid.error_code == ErrorCode.INVALID_REQUEST.value

    count = AgentRuntime.interpret("layout hiện tại có bao nhiêu khu vực chính?")
    assert count.ok is True
    assert count.payload["intent"] == "query"


@pytest.mark.asyncio
async def test_execute_query_builds_answer_without_mutation() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]

    result = await runtime.execute(sid, request="find PM4")

    assert result.ok is True
    payload = result.payload
    assert payload["intent"] == "query"
    assert payload["state"] == "OBSERVED"
    assert payload["next_action"] == "answer_query"
    assert payload["answer"]["match_count"] == 1
    assert payload["answer"]["matches"][0]["handle"] == "T1"
    assert backend.preview_count == 0
    assert backend.apply_count == 0


@pytest.mark.asyncio
async def test_execute_query_reports_unmatched_labels_instead_of_guessing() -> None:
    backend = FakeAgentBackend()

    async def no_match_search(queries) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={
                "results": [
                    {"query": query["query"], "matches": [], "truncated": False}
                    for query in queries
                ]
            },
        )

    backend.entity_search_text_batch = no_match_search
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]

    result = await runtime.execute(sid, request="find DOES_NOT_EXIST")

    assert result.ok is True
    assert result.payload["answer"]["complete"] is False
    assert result.payload["next_action"] == "clarify_unmatched_labels"
    assert result.payload["answer"]["unmatched_labels"] == ["DOES_NOT_EXIST"]


@pytest.mark.asyncio
async def test_execute_generate_returns_proposal_without_touching_autocad() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]

    result = await runtime.execute(sid, request="generate a factory visualization concept")

    assert result.ok is True
    assert result.payload["intent"] == "generate"
    assert result.payload["state"] == "NEW"
    assert result.payload["concept_model"]["geometry_created"] is False
    assert result.payload["next_action"] == "review_concept_then_supply_constraints"
    assert runtime._sessions[sid].calls_used == 0


@pytest.mark.asyncio
async def test_execute_count_query_uses_major_area_defaults() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]

    result = await runtime.execute(sid, request="layout hiện tại có bao nhiêu khu vực chính?")

    assert result.ok is True
    assert result.payload["intent"] == "query"
    assert result.payload["answer"]["count"] == 10
    assert result.payload["answer"]["items"][0]["label"] == "WAREHOUSE"


@pytest.mark.asyncio
async def test_factory_area_queries_use_exact_text_matching() -> None:
    backend = FakeAgentBackend()
    recorded_queries = []
    original_search = backend.entity_search_text_batch

    async def record_search(queries) -> CommandResult:
        recorded_queries.extend(queries)
        return await original_search(queries)

    backend.entity_search_text_batch = record_search
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")

    assert (await runtime.execute(started.payload["session_id"], request="layout hiện tại có bao nhiêu khu vực chính?")).ok
    assert recorded_queries
    assert {query["match_mode"] for query in recorded_queries} == {"exact"}


@pytest.mark.asyncio
async def test_missing_required_label_blocks_overlay_plan() -> None:
    backend = FakeAgentBackend()

    async def no_match_search(queries) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={
                "results": [
                    {"query": query["query"], "matches": [], "truncated": False}
                    for query in queries
                ]
            },
        )

    backend.entity_search_text_batch = no_match_search
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="mutation")
    sid = started.payload["session_id"]
    assert (await runtime.connect(sid)).ok
    assert (await runtime.observe(sid, labels=["AREA"])).ok
    assert (await runtime.map(sid, boundary_handles=["A1"])).ok

    plan = await runtime.plan(
        sid,
        actions=[{"action": "copy_to_overlay", "source_handle": "A1", "target_layer": "VIS"}],
        target_path="C:/tmp/factory_overlay.dwg",
    )

    assert plan.error_code == ErrorCode.GEOMETRY_UNAVAILABLE.value
    assert runtime._sessions[sid].plan is None


@pytest.mark.asyncio
async def test_truncated_boundary_query_blocks_overlay_plan_without_explicit_scope() -> None:
    backend = FakeAgentBackend()

    async def truncated_query(query) -> CommandResult:
        return CommandResult(ok=True, payload={"entities": [backend.geometry], "truncated": True})

    backend.entity_query = truncated_query
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="mutation")
    sid = started.payload["session_id"]
    assert (await runtime.connect(sid)).ok
    assert (await runtime.observe(sid, labels=["AREA"])).ok
    assert (await runtime.map(sid, boundary_types=["LWPOLYLINE"])).ok

    plan = await runtime.plan(
        sid,
        actions=[{"action": "copy_to_overlay", "source_handle": "A1", "target_layer": "VIS"}],
        target_path="C:/tmp/factory_overlay.dwg",
    )

    assert plan.error_code == ErrorCode.GEOMETRY_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_execute_inspect_builds_concept_model_from_verified_mapping() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]

    result = await runtime.execute(
        sid,
        intent="inspect",
        request="inspect layout",
        labels=["AREA"],
        boundary_handles=["A1"],
    )

    assert result.ok is True
    payload = result.payload
    assert payload["state"] == "MAPPED"
    assert payload["next_action"] == "synthesize_concept"
    assert payload["concept_model"]["kind"] == "layout_concept_model"
    assert payload["concept_model"]["labels"][0]["relation"] == "contains"


@pytest.mark.asyncio
async def test_execute_overlay_stops_at_plan_review_boundary() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="mutation")
    sid = started.payload["session_id"]

    result = await runtime.execute(
        sid,
        intent="overlay",
        request="copy the verified boundary to an overlay",
        labels=["AREA"],
        boundary_handles=["A1"],
        actions=[{"action": "copy_to_overlay", "source_handle": "A1", "target_layer": "VIS"}],
        target_path="C:/tmp/factory_overlay.dwg",
    )

    assert result.ok is True
    assert result.payload["state"] == "PLANNED"
    assert result.payload["next_action"] == "review_plan_then_preview"
    assert result.payload["plan"]["removed_handles"] == []
    assert backend.preview_count == 0
    assert backend.apply_count == 0


@pytest.mark.asyncio
async def test_execute_overlay_compiles_safe_actions_when_actions_are_omitted() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="mutation")
    sid = started.payload["session_id"]

    result = await runtime.execute(
        sid,
        intent="overlay",
        request="create a safe overlay",
        labels=["AREA"],
        boundary_handles=["A1"],
        target_path="C:/tmp/factory_overlay.dwg",
    )

    assert result.ok is True
    assert result.payload["state"] == "PLANNED"
    assert result.payload["plan"]["actions"][0]["action"] == "copy_to_overlay"
    assert result.payload["plan"]["actions"][0]["source_handle"] == "A1"
    assert any(
        action["action"] == "preserve" and action["source_handle"] == "T1"
        for action in result.payload["plan"]["actions"]
    )
    assert result.payload["plan"]["required_labels"][0]["source_handle"] == "T1"
    assert result.payload["plan"]["removed_handles"] == []


@pytest.mark.asyncio
async def test_execute_returns_clarification_without_connecting() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]

    result = await runtime.execute(sid, request="")

    assert result.ok is True
    assert result.payload["needs_clarification"] is True
    assert result.payload["next_action"] == "clarify_intent"
    assert result.payload["state"] == "NEW"
    assert runtime._sessions[sid].calls_used == 0


@pytest.mark.asyncio
async def test_session_snapshot_exposes_agent_status_and_evidence() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]

    result = await runtime.execute(sid, request="find PM4")

    assert result.ok is True
    assert result.payload["status"] == "answered"
    assert result.payload["phase"] == "OBSERVED"
    assert result.payload["request_history"] == ["find PM4"]
    assert [item["phase"] for item in result.payload["evidence"]] == ["CONNECT", "OBSERVE"]


@pytest.mark.asyncio
async def test_resume_reobserves_when_the_user_changes_the_request() -> None:
    backend = FakeAgentBackend()
    runtime = AgentRuntime(backend_factory=lambda: _backend(backend))
    started = await runtime.start(mode="read_only")
    sid = started.payload["session_id"]
    assert (await runtime.execute(sid, request="find PM4")).ok

    resumed = await runtime.resume(sid, request="find PM5")

    assert resumed.ok is True
    assert resumed.payload["request_history"] == ["find PM4", "find PM5"]
    assert resumed.payload["answer"]["matches"][0]["query"] == "PM5"


@pytest.mark.asyncio
async def test_cancel_rolls_back_unapplied_preview() -> None:
    backend = FakeAgentBackend()
    runtime, sid = await make_runtime(backend)
    assert (await runtime.preview(sid)).ok

    cancelled = await runtime.cancel(sid, "user stopped the draft")

    assert cancelled.ok is True
    assert cancelled.payload["state"] == "CANCELLED"
    assert cancelled.payload["status"] == "cancelled"
    assert backend.rollback_count == 1
