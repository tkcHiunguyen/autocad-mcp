"""Stateful, safety-first orchestration for AutoCAD layout tasks.

The runtime does not invent geometry or mutate a source drawing. It stores the
evidence collected by read operations, turns explicit user actions into a
previewable plan, and only lets a bridge apply that plan to a separate output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from autocad_mcp.backends.base import AutoCADBackend, CommandResult
from autocad_mcp.errors import ErrorCode


class WorkflowState(str, Enum):
    NEW = "NEW"
    CONNECT = "CONNECT"
    OBSERVED = "OBSERVED"
    MAPPED = "MAPPED"
    PLANNED = "PLANNED"
    PREVIEWED = "PREVIEWED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    VERIFIED = "VERIFIED"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AgentIntent(str, Enum):
    """High-level task families understood by the orchestration layer."""

    QUERY = "query"
    INSPECT = "inspect"
    OVERLAY = "overlay"
    MODIFY = "modify"
    GENERATE = "generate"


ALLOWED_ACTIONS = {
    "preserve",
    "copy_to_overlay",
    "simplify_copy",
    "create_connector_line",
}

SUPPORTED_INTENTS = {intent.value for intent in AgentIntent}

# These are the semantic labels used by the user's factory-layout workflow.
# They are query defaults only; geometry still has to be discovered and
# verified from the drawing before any plan can be built.
DEFAULT_LAYOUT_LABELS = [
    "WAREHOUSE",
    "CONVERTING 1",
    "CONVERTING 2",
    "MATERIAL STORAGE",
    "OCC1",
    "OCC2 & MW",
    "WASTEWATER",
    "TREATMENT",
    "BOILER 1-2-3",
    "GAS BOILER OLD",
    "PM4",
    "PM5",
    "TM1",
    "TM2",
    "TM3",
    "TM4",
    "TM5",
    "TM6",
    "DIP1",
    "DIP2",
]
DEFAULT_MAJOR_AREA_LABELS = DEFAULT_LAYOUT_LABELS[:10]

# Keep intent detection deterministic and cheap. The host model may pass an
# explicit intent; keyword detection is only a safe fallback for short tasks.
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    AgentIntent.QUERY.value: (
        "find",
        "search",
        "locate",
        "tìm",
        "tra cứu",
        "bao nhiêu",
        "count",
        "handle",
        "ở đâu",
    ),
    AgentIntent.INSPECT.value: (
        "inspect",
        "layout",
        "map",
        "area",
        "zone",
        "boundary",
        "khu vực",
        "tổng quan",
        "phân tích",
        "lập bản đồ",
    ),
    AgentIntent.OVERLAY.value: (
        "overlay",
        "visual",
        "visualize",
        "đường bao",
        "giữ lại",
        "lược bớt",
        "simplify",
        "copy to",
    ),
    AgentIntent.MODIFY.value: (
        "modify",
        "edit",
        "change",
        "move",
        "erase",
        "delete",
        "chỉnh sửa",
        "sửa",
        "xóa",
        "di chuyển",
    ),
    AgentIntent.GENERATE.value: (
        "generate",
        "create",
        "design",
        "idea",
        "concept",
        "tạo mới",
        "ý tưởng",
        "thiết kế",
    ),
}


@dataclass
class AgentSession:
    session_id: str
    max_calls: int = 12
    mode: str = "read_only"
    state: WorkflowState = WorkflowState.NEW
    intent: str | None = None
    request: str | None = None
    request_history: list[str] = field(default_factory=list)
    calls_used: int = 0
    document_id: str | None = None
    before_fingerprint: str | None = None
    before_dbmod: int | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    unknowns: list[dict[str, Any]] = field(default_factory=list)
    mapping: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    apply_result: dict[str, Any] | None = None
    approval_expires_at: float | None = None
    plan_hash: str | None = None
    answer: dict[str, Any] | None = None
    concept_model: dict[str, Any] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    next_action: str | None = None
    audit: list[dict[str, Any]] = field(default_factory=list)

    def public_status(self) -> str:
        """Return a stable, intent-facing status while retaining state internally."""
        if self.state is WorkflowState.FAILED:
            return "blocked"
        if self.state is WorkflowState.CANCELLED:
            return "cancelled"
        if self.state is WorkflowState.VERIFIED:
            return "verified"
        if self.state is WorkflowState.ROLLED_BACK:
            return "rolled_back"
        if self.questions or (self.next_action or "").startswith(("clarify_", "provide_")):
            return "needs_clarification"
        if self.answer is not None and self.answer.get("complete") is True:
            return "answered"
        return {
            WorkflowState.NEW: "new",
            WorkflowState.CONNECT: "connected",
            WorkflowState.OBSERVED: "observed",
            WorkflowState.MAPPED: "mapped",
            WorkflowState.PLANNED: "ready_for_preview",
            WorkflowState.PREVIEWED: "awaiting_approval",
            WorkflowState.APPROVED: "approved",
            WorkflowState.APPLIED: "applied",
        }.get(self.state, self.state.value.casefold())

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "status": self.public_status(),
            "phase": self.state.value,
            "calls_used": self.calls_used,
            "max_calls": self.max_calls,
            "mode": self.mode,
            "intent": self.intent,
            "request": self.request,
            "request_history": list(self.request_history),
            "document_id": self.document_id,
            "before_fingerprint": self.before_fingerprint,
            "before_dbmod": self.before_dbmod,
            "facts": self.facts,
            "evidence": list(self.evidence),
            "assumptions": self.assumptions,
            "unknowns": self.unknowns,
            "mapping": self.mapping,
            "plan": self.plan,
            "preview": self.preview,
            "apply_result": self.apply_result,
            "approval_expires_at": self.approval_expires_at,
            "plan_hash": self.plan_hash,
            "answer": self.answer,
            "concept_model": self.concept_model,
            "questions": self.questions,
            "next_action": self.next_action,
            "audit": self.audit,
        }


BackendFactory = Callable[[], Awaitable[AutoCADBackend]]


class AgentRuntime:
    """In-memory workflow manager scoped to a running MCP server process."""

    def __init__(self, backend_factory: BackendFactory | None = None) -> None:
        self._backend_factory = backend_factory
        self._sessions: dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, max_calls: int = 12, mode: str = "read_only") -> CommandResult:
        if not 1 <= max_calls <= 100:
            return CommandResult.failure(
                ErrorCode.INVALID_REQUEST,
                "max_calls must be between 1 and 100",
            )
        if mode not in {"read_only", "mutation"}:
            return CommandResult.failure(
                ErrorCode.INVALID_REQUEST,
                "mode must be read_only or mutation",
            )
        session = AgentSession(session_id=secrets.token_urlsafe(18), max_calls=max_calls, mode=mode)
        async with self._lock:
            self._sessions[session.session_id] = session
        return CommandResult(ok=True, payload=session.snapshot())

    async def status(self, session_id: str) -> CommandResult:
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        return CommandResult(ok=True, payload=session.snapshot())

    async def resume(
        self,
        session_id: str,
        **kwargs: Any,
    ) -> CommandResult:
        """Continue a non-terminal task using new user-supplied context."""
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state in {
            WorkflowState.CANCELLED,
            WorkflowState.ROLLED_BACK,
            WorkflowState.FAILED,
            WorkflowState.VERIFIED,
        }:
            return self._invalid_state(session, WorkflowState.NEW)
        return await self.execute(session_id, **kwargs)

    async def cancel(self, session_id: str, reason: str = "") -> CommandResult:
        """Stop a task, cleaning up an un-applied preview when one exists."""
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state is WorkflowState.CANCELLED:
            return CommandResult(ok=True, payload=session.snapshot())
        if session.state in {WorkflowState.ROLLED_BACK, WorkflowState.FAILED, WorkflowState.VERIFIED}:
            return self._invalid_state(session, WorkflowState.NEW)
        if session.state in {WorkflowState.APPLIED}:
            return CommandResult.failure(
                ErrorCode.INVALID_AGENT_STATE,
                "An applied task must be verified or explicitly rolled back; cancel will not delete its output",
                details={"state": session.state.value},
            )

        if session.state in {WorkflowState.PREVIEWED, WorkflowState.APPROVED}:
            batch_id = str((session.preview or {}).get("batch_id") or "")
            if batch_id:
                backend = await self._backend()
                capability_error = self._require_capabilities(session, ["batch.rollback"])
                if capability_error is not None:
                    return capability_error
                rollback_result = await self._call(
                    session,
                    lambda: backend.batch_rollback(batch_id),
                )
                if not rollback_result.ok:
                    return rollback_result

        if reason.strip():
            session.assumptions.append(f"Task cancelled by user: {reason.strip()}")
        session.state = WorkflowState.CANCELLED
        session.next_action = "cancelled"
        self._audit(session, "CANCEL", {"reason": reason.strip() or None})
        return CommandResult(ok=True, payload=session.snapshot())

    @staticmethod
    def interpret(request: str = "", intent: str = "auto") -> CommandResult:
        """Normalize a high-level request without touching AutoCAD.

        This deliberately does not call an LLM. The host model can provide an
        explicit intent, while the deterministic fallback gives the runtime a
        predictable clarification contract when it cannot decide safely.
        """

        requested = str(intent or "auto").strip().lower()
        text = str(request or "").strip()
        if requested != "auto":
            if requested not in SUPPORTED_INTENTS:
                return CommandResult.failure(
                    ErrorCode.INVALID_REQUEST,
                    "intent must be auto, query, inspect, overlay, modify, or generate",
                    details={"intent": requested, "supported": sorted(SUPPORTED_INTENTS)},
                )
            return CommandResult(
                ok=True,
                payload={
                    "intent": requested,
                    "confidence": 1.0,
                    "needs_clarification": False,
                    "candidates": [requested],
                },
            )

        if not text:
            return CommandResult(
                ok=True,
                payload={
                    "intent": None,
                    "confidence": 0.0,
                    "needs_clarification": True,
                    "candidates": [],
                    "questions": ["Bạn muốn tìm kiếm, phân tích layout, tạo overlay hay chỉnh sửa gì?"],
                },
            )

        normalized = text.casefold()
        count_markers = (
            "bao nhiêu",
            "how many",
            "count",
            "số lượng",
            "number of",
        )
        explicit_non_query = any(
            marker.casefold() in normalized
            for marker in (
                "overlay",
                "visual",
                "modify",
                "edit",
                "chỉnh sửa",
                "sửa",
                "tạo mới",
                "generate",
            )
        )
        if any(marker in normalized for marker in count_markers) and not explicit_non_query:
            return CommandResult(
                ok=True,
                payload={
                    "intent": AgentIntent.QUERY.value,
                    "confidence": 0.95,
                    "needs_clarification": False,
                    "candidates": [AgentIntent.QUERY.value],
                },
            )
        scores = {
            candidate: sum(
                1
                for keyword in keywords
                if keyword.casefold() in normalized
            )
            for candidate, keywords in _INTENT_KEYWORDS.items()
        }
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        top_score = ranked[0][1]
        tied = [candidate for candidate, score in ranked if score == top_score and score > 0]
        if top_score <= 0 or len(tied) != 1:
            candidates = [candidate for candidate, score in ranked if score > 0]
            return CommandResult(
                ok=True,
                payload={
                    "intent": None,
                    "confidence": 0.0,
                    "needs_clarification": True,
                    "candidates": candidates,
                    "questions": [
                        "Bạn muốn tìm kiếm đối tượng, phân tích layout, tạo overlay hay chỉnh sửa?"
                    ],
                },
            )

        second_score = ranked[1][1] if len(ranked) > 1 else 0
        confidence = min(0.99, 0.55 + 0.12 * top_score + 0.08 * (top_score - second_score))
        return CommandResult(
            ok=True,
            payload={
                "intent": ranked[0][0],
                "confidence": round(confidence, 3),
                "needs_clarification": False,
                "candidates": [ranked[0][0]],
            },
        )

    async def execute(
        self,
        session_id: str,
        *,
        request: str = "",
        intent: str = "auto",
        labels: list[str] | None = None,
        boundary_handles: list[str] | None = None,
        process_handles: list[str] | None = None,
        boundary_layers: list[str] | None = None,
        boundary_types: list[str] | None = None,
        actions: list[dict[str, Any]] | None = None,
        target_path: str = "",
        allow_uncertainties: bool = False,
    ) -> CommandResult:
        """Advance a task to the next safe boundary using one intent contract.

        The method is intentionally resumable: callers can provide missing
        scope/actions on a later turn without losing the evidence cache.
        Mutation still stops at the existing preview/approval gates.
        """

        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        previous_request = session.request
        request_changed = False
        if str(request or "").strip():
            normalized_request = str(request).strip()
            request_changed = normalized_request != previous_request
            if not session.request_history or session.request_history[-1] != normalized_request:
                session.request_history.append(normalized_request)
            session.request = normalized_request
        if session.state in {WorkflowState.FAILED, WorkflowState.VERIFIED, WorkflowState.ROLLED_BACK, WorkflowState.CANCELLED}:
            return self._invalid_state(session, WorkflowState.NEW)
        if session.state is WorkflowState.PLANNED:
            session.next_action = "review_plan_then_preview"
            return CommandResult(ok=True, payload=session.snapshot())
        if session.state is WorkflowState.PREVIEWED:
            session.next_action = "approval"
            return CommandResult(ok=True, payload=session.snapshot())
        if session.state is WorkflowState.APPROVED:
            session.next_action = "apply"
            return CommandResult(ok=True, payload=session.snapshot())
        if session.state is WorkflowState.APPLIED:
            session.next_action = "verify"
            return CommandResult(ok=True, payload=session.snapshot())

        # A new conversational query invalidates only the old observation;
        # connection and call-budget context remain reusable.
        if request_changed and session.state is WorkflowState.OBSERVED:
            session.state = WorkflowState.CONNECT
            session.answer = None
            session.concept_model = {}
            session.questions = []
            session.next_action = "observe"

        if session.intent is None:
            interpretation = self.interpret(request, intent)
            if not interpretation.ok:
                return interpretation
            interpretation_payload = self._as_dict(interpretation.payload)
            if interpretation_payload.get("needs_clarification") is True:
                session.request = request or session.request
                session.questions = [
                    str(question)
                    for question in interpretation_payload.get("questions", [])
                    if question
                ]
                session.next_action = "clarify_intent"
                payload = session.snapshot()
                payload.update(interpretation_payload)
                return CommandResult(ok=True, payload=payload)
            session.intent = self._string_or_none(interpretation_payload.get("intent"))
            session.request = request or session.request
        elif intent not in {"", "auto", session.intent}:
            return CommandResult.failure(
                ErrorCode.INVALID_REQUEST,
                "A session cannot change intent after execution has started",
                details={"session_intent": session.intent, "requested_intent": intent},
            )

        assert session.intent in SUPPORTED_INTENTS
        session.questions = []

        if (
            session.intent == AgentIntent.GENERATE.value
            and session.state is WorkflowState.NEW
            and not labels
            and not boundary_handles
            and not process_handles
            and not boundary_layers
            and not boundary_types
        ):
            session.concept_model = self._build_generation_brief(session.request or "")
            session.next_action = "review_concept_then_supply_constraints"
            return CommandResult(ok=True, payload=session.snapshot())

        if session.state is WorkflowState.NEW:
            connected = await self.connect(session_id)
            if not connected.ok:
                return connected

        effective_labels = [str(label).strip() for label in (labels or []) if str(label).strip()]
        if session.state is WorkflowState.CONNECT:
            if not effective_labels:
                effective_labels = self._extract_labels(session.request or "")
            if not effective_labels and session.intent in {
                AgentIntent.INSPECT.value,
                AgentIntent.OVERLAY.value,
            }:
                effective_labels = list(DEFAULT_LAYOUT_LABELS)
            if (
                not effective_labels
                and session.intent == AgentIntent.QUERY.value
                and self._is_count_request(session.request or "")
            ):
                effective_labels = list(DEFAULT_MAJOR_AREA_LABELS)
            if not effective_labels:
                session.questions = ["Hãy cung cấp ít nhất một nhãn hoặc từ khóa cần quan sát."]
                session.next_action = "provide_labels"
                return CommandResult(ok=True, payload=session.snapshot())
            observed = await self.observe(
                session_id,
                labels=effective_labels,
                relevant_layers=boundary_layers,
                relevant_types=boundary_types,
            )
            if not observed.ok:
                return observed

        if session.intent == AgentIntent.QUERY.value:
            session.answer = self._build_query_answer(session)
            session.next_action = (
                "answer_query"
                if session.answer.get("complete") is True
                else "clarify_unmatched_labels"
            )
            return CommandResult(ok=True, payload=session.snapshot())

        if session.intent in {AgentIntent.INSPECT.value, AgentIntent.OVERLAY.value}:
            if session.state is WorkflowState.OBSERVED:
                effective_types = boundary_types
                if not boundary_handles and not boundary_layers and not effective_types:
                    # A type-filtered query is bounded and avoids the forbidden
                    # full-drawing entity.list enumeration.
                    effective_types = ["LWPOLYLINE", "POLYLINE"]
                mapped = await self.map(
                    session_id,
                    boundary_handles=boundary_handles,
                    process_handles=process_handles,
                    boundary_layers=boundary_layers,
                    boundary_types=effective_types,
                )
                if not mapped.ok:
                    return mapped

            if session.intent == AgentIntent.INSPECT.value:
                session.concept_model = self._build_concept_model(session)
                session.next_action = "synthesize_concept"
                return CommandResult(ok=True, payload=session.snapshot())

            if session.state is WorkflowState.MAPPED and target_path:
                effective_actions = actions or self._compile_overlay_actions(session)
                if not effective_actions:
                    session.questions = [
                        "Không tìm thấy geometry đã xác minh để tạo overlay an toàn."
                    ]
                    session.next_action = "provide_verified_handles"
                    return CommandResult(ok=True, payload=session.snapshot())
                planned = await self.plan(
                    session_id,
                    actions=effective_actions,
                    target_path=target_path,
                    allow_uncertainties=allow_uncertainties,
                )
                if not planned.ok:
                    return planned
                session.next_action = "review_plan_then_preview"
            else:
                session.next_action = "provide_actions_and_target_path"
            return CommandResult(ok=True, payload=session.snapshot())

        # The direct bridge intentionally exposes only the immutable overlay
        # mutation surface. Arbitrary edit/generate writes need a future policy
        # and must not silently fall through to legacy entity tools.
        if session.intent == AgentIntent.GENERATE.value:
            session.concept_model = self._build_generation_brief(session.request or "", session)
        session.next_action = "define_supported_overlay_plan"
        return CommandResult(
            ok=True,
            payload={
                **session.snapshot(),
                "capability_status": "explicit_overlay_plan_required",
            },
        )

    @staticmethod
    def _extract_labels(request: str) -> list[str]:
        """Extract only explicit quoted/uppercase tokens; never invent labels."""

        quoted = re.findall(r"[\"']([^\"']+)[\"']", request)
        tokens = re.findall(r"\b[A-Z][A-Z0-9&._-]{1,}\b", request)
        result: list[str] = []
        for value in [*quoted, *tokens]:
            value = value.strip()
            if value and value.casefold() not in {item.casefold() for item in result}:
                result.append(value)
        return result[:32]

    @staticmethod
    def _compile_overlay_actions(session: AgentSession) -> list[dict[str, Any]]:
        """Compile only evidence-backed copy actions for a first overlay plan."""

        mapping = session.mapping if isinstance(session.mapping, dict) else {}
        actions: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        verified_handles = {
            str(item.get("handle"))
            for item in mapping.get("geometries", [])
            if isinstance(item, dict) and item.get("handle")
        }

        for boundary in mapping.get("boundaries", []) if isinstance(mapping.get("boundaries"), list) else []:
            if not isinstance(boundary, dict):
                continue
            handle = str(boundary.get("handle") or "")
            if not handle or (handle, "boundary") in seen:
                continue
            seen.add((handle, "boundary"))
            actions.append(
                {
                    "action": "copy_to_overlay",
                    "source_handle": handle,
                    "target_layer": "VIS_OVERLAY_BOUNDARY",
                    "reason": "Copy verified source boundary without modifying the source drawing",
                }
            )

        for relation in mapping.get("process_to_boundary", []) if isinstance(mapping.get("process_to_boundary"), list) else []:
            if not isinstance(relation, dict):
                continue
            handle = str(relation.get("process_handle") or "")
            if not handle or (handle, "process") in seen:
                continue
            seen.add((handle, "process"))
            actions.append(
                {
                    "action": "copy_to_overlay",
                    "source_handle": handle,
                    "target_layer": "VIS_OVERLAY_LINE",
                    "reason": "Keep the verified process line in the overlay",
                }
            )

        for label in mapping.get("label_handles", []) if isinstance(mapping.get("label_handles"), list) else []:
            handle = str(label or "")
            if not handle or handle not in verified_handles or (handle, "label") in seen:
                continue
            seen.add((handle, "label"))
            actions.append(
                {
                    "action": "preserve",
                    "source_handle": handle,
                    "reason": "Keep the verified area label in the immutable overlay clone",
                }
            )
        return actions

    @staticmethod
    def _label_handles_from_search(session: AgentSession) -> list[str]:
        search = AgentRuntime._as_dict(session.facts.get("text_search"))
        handles: list[str] = []
        for result in search.get("results", []) if isinstance(search.get("results"), list) else []:
            if not isinstance(result, dict):
                continue
            for match in result.get("matches", []) if isinstance(result.get("matches"), list) else []:
                if not isinstance(match, dict):
                    continue
                handle = str(match.get("handle") or "")
                if handle and handle not in handles:
                    handles.append(handle)
        return handles

    @staticmethod
    def _required_label_records(session: AgentSession) -> list[dict[str, Any]]:
        search = AgentRuntime._as_dict(session.facts.get("text_search"))
        records: list[dict[str, Any]] = []
        for result in search.get("results", []) if isinstance(search.get("results"), list) else []:
            if not isinstance(result, dict):
                continue
            query = str(result.get("query") or "")
            matches = result.get("matches", [])
            if not isinstance(matches, list):
                continue
            for match in matches:
                if isinstance(match, dict) and match.get("handle"):
                    records.append(
                        {
                            "query": query,
                            "source_handle": str(match["handle"]),
                            "text": match.get("text"),
                        }
                    )
        return records

    @staticmethod
    def _build_query_answer(session: AgentSession) -> dict[str, Any]:
        """Build a compact, evidence-backed answer from cached observations."""

        search = AgentRuntime._as_dict(session.facts.get("text_search"))
        results = search.get("results", [])
        matches: list[dict[str, Any]] = []
        matched_labels: list[dict[str, Any]] = []
        unmatched_labels: list[str] = []
        truncated = False
        if isinstance(results, list):
            for result in results:
                if not isinstance(result, dict):
                    continue
                query = str(result.get("query") or "")
                result_matches = result.get("matches", []) if isinstance(result.get("matches"), list) else []
                if not result_matches and query:
                    unmatched_labels.append(query)
                truncated = truncated or result.get("truncated") is True
                if result_matches:
                    first = result_matches[0] if isinstance(result_matches[0], dict) else {}
                    matched_labels.append(
                        {
                            "label": query,
                            "handle": first.get("handle"),
                            "text": first.get("text"),
                            "insertion": first.get("insertion"),
                            "bounds": first.get("bounds") or first.get("text_bounds"),
                        }
                    )
                for match in result_matches:
                    if isinstance(match, dict):
                        matches.append(
                            {
                                "query": query,
                                "handle": match.get("handle"),
                                "text": match.get("text"),
                                "insertion": match.get("insertion"),
                                "bounds": match.get("bounds") or match.get("text_bounds"),
                            }
                        )
        return {
            "kind": "query_results",
            "count": (
                len({item.get("handle") for item in matched_labels if item.get("handle")})
                if AgentRuntime._is_count_request(session.request or "")
                else len(matches)
            ),
            "items": matched_labels if AgentRuntime._is_count_request(session.request or "") else matches,
            "match_count": len(matches),
            "matched_labels": matched_labels,
            "unmatched_labels": unmatched_labels,
            "searched_label_count": len(results) if isinstance(results, list) else 0,
            "complete": not unmatched_labels and not truncated,
            "matches": matches,
            "evidence": {
                "source_document_id": session.document_id,
                "source_fingerprint": session.before_fingerprint,
                "operation": "entity.search_text_batch",
            },
        }

    @staticmethod
    def _is_count_request(request: str) -> bool:
        normalized = str(request or "").casefold()
        return any(
            marker in normalized
            for marker in ("bao nhiêu", "how many", "count", "số lượng", "number of")
        )

    @staticmethod
    def _build_concept_model(session: AgentSession) -> dict[str, Any]:
        """Expose the mapped semantic graph without inventing missing relations."""

        mapping = session.mapping if isinstance(session.mapping, dict) else {}
        return {
            "kind": "layout_concept_model",
            "document_id": session.document_id,
            "boundaries": mapping.get("boundaries", []),
            "labels": mapping.get("label_to_boundary", []),
            "process_lines": mapping.get("process_to_boundary", []),
            "topology": mapping.get("boundary_topology", []),
            "unknowns": list(session.unknowns),
            "evidence": {
                "source_fingerprint": session.before_fingerprint,
                "source_dbmod": session.before_dbmod,
                "geometry_source": "entity.get_geometry_batch",
                "relations_are_verified_only": True,
            },
        }

    @staticmethod
    def _build_generation_brief(
        request: str,
        session: AgentSession | None = None,
    ) -> dict[str, Any]:
        """Represent a generation request without pretending geometry exists."""

        return {
            "kind": "generation_brief",
            "request": request,
            "proposal_only": True,
            "geometry_created": False,
            "requires_explicit_plan": True,
            "source_document_id": session.document_id if session else None,
            "source_fingerprint": session.before_fingerprint if session else None,
            "constraints": {
                "source_immutable": True,
                "no_geometry_inference": True,
                "output_policy": "separate_overlay_or_new_file",
            },
        }

    async def connect(self, session_id: str) -> CommandResult:
        """Establish a bridge/document context without changing the drawing."""
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state is not WorkflowState.NEW:
            return self._invalid_state(session, WorkflowState.NEW)
        backend = await self._backend()
        health = await self._call(session, backend.session_health)
        if not health.ok:
            return health
        capabilities = await self._call(session, backend.capabilities_list)
        if not capabilities.ok:
            return capabilities
        session.facts["connection"] = {
            "health": self._as_dict(health.payload),
            "capabilities": self._as_dict(capabilities.payload),
        }
        session.evidence.append(
            {
                "phase": "CONNECT",
                "operation": "session.health + capabilities.list",
                "document_id": self._as_dict(health.payload).get("document_id"),
            }
        )
        session.state = WorkflowState.CONNECT
        session.next_action = "observe"
        self._audit(session, "CONNECT", {"connected": True})
        return CommandResult(ok=True, payload=session.snapshot())

    async def observe(
        self,
        session_id: str,
        *,
        labels: list[str],
        relevant_layers: list[str] | None = None,
        relevant_types: list[str] | None = None,
    ) -> CommandResult:
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state is not WorkflowState.CONNECT:
            return self._invalid_state(session, WorkflowState.CONNECT)
        if not labels:
            return CommandResult.failure(ErrorCode.INVALID_REQUEST, "labels is required for observe")
        if len(labels) > 32:
            return CommandResult.failure(ErrorCode.INVALID_REQUEST, "labels is limited to 32 items")

        backend = await self._backend()
        capability_error = self._require_capabilities(
            session,
            ["drawing.get_state", "entity.search_text_batch"],
        )
        if capability_error is not None:
            return capability_error
        state_result = await self._call(session, backend.drawing_get_state)
        if not state_result.ok:
            return state_result

        drawing_state = self._as_dict(state_result.payload)
        session.document_id = self._string_or_none(drawing_state.get("document_id"))
        session.before_fingerprint = self._string_or_none(drawing_state.get("fingerprint"))
        session.before_dbmod = self._int_or_none(drawing_state.get("dbmod"))
        if not session.document_id or not session.before_fingerprint or session.before_dbmod is None:
            return CommandResult.failure(
                ErrorCode.DOCUMENT_NOT_RESOLVED,
                "drawing.get_state must identify the current drawing, fingerprint, and DBMOD",
            )

        factory_labels = {label.casefold() for label in DEFAULT_LAYOUT_LABELS}
        queries = [
            {
                "query": label,
                # The known factory labels are semantic identifiers. Exact
                # matching avoids counting PM4 as PM40 or CONVERTING 1 as 10.
                "match_mode": "exact" if label.casefold() in factory_labels else "contains",
                "limit": 20,
                "case_sensitive": False,
            }
            for label in labels
        ]
        text_result = await self._call(session, lambda: backend.entity_search_text_batch(queries))
        if not text_result.ok:
            return text_result
        counts: dict[str, Any] | None = None
        if relevant_layers or relevant_types:
            capability_error = self._require_capabilities(session, ["entity.count_by_layer_type"])
            if capability_error is not None:
                return capability_error
            counts_result = await self._call(
                session,
                lambda: backend.entity_count_by_layer_type(
                    {"layers": relevant_layers or [], "types": relevant_types or []}
                ),
            )
            if not counts_result.ok:
                return counts_result
            counts = self._as_dict(counts_result.payload)

        connection_facts = self._as_dict(session.facts.get("connection"))
        session.facts = {
            "connection": connection_facts,
            "drawing_state": drawing_state,
            "fingerprint": {
                "document_id": session.document_id,
                "fingerprint": session.before_fingerprint,
                "dbmod": session.before_dbmod,
            },
            "capabilities": self._as_dict(
                self._as_dict(session.facts.get("connection")).get("capabilities")
            ),
            "view_state": self._as_dict(drawing_state.get("viewport")),
            "label_queries": list(labels),
            "text_search": self._as_dict(text_result.payload),
            "counts": counts,
        }
        session.evidence.append(
            {
                "phase": "OBSERVE",
                "operation": "drawing.get_state + entity.search_text_batch",
                "document_id": session.document_id,
                "fingerprint": session.before_fingerprint,
                "dbmod": session.before_dbmod,
                "label_queries": list(labels),
            }
        )
        session.state = WorkflowState.OBSERVED
        session.next_action = "map_or_answer"
        self._audit(session, "OBSERVE", {"label_queries": len(queries)})
        return CommandResult(ok=True, payload=session.snapshot())

    async def map(
        self,
        session_id: str,
        *,
        boundary_handles: list[str] | None = None,
        process_handles: list[str] | None = None,
        boundary_layers: list[str] | None = None,
        boundary_types: list[str] | None = None,
    ) -> CommandResult:
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state is not WorkflowState.OBSERVED:
            return self._invalid_state(session, WorkflowState.OBSERVED)

        backend = await self._backend()
        boundary_ids = [handle for handle in (boundary_handles or []) if handle]
        if not boundary_ids:
            if not boundary_layers and not boundary_types:
                return CommandResult.failure(
                    ErrorCode.INVALID_REQUEST,
                    "map requires boundary_handles or at least one boundary layer/type filter",
                )
            capability_error = self._require_capabilities(session, ["entity.query"])
            if capability_error is not None:
                return capability_error
            query_result = await self._call(
                session,
                lambda: backend.entity_query(
                    {
                        "layers": boundary_layers or [],
                        "types": boundary_types or ["LWPOLYLINE", "POLYLINE"],
                        "limit": 200,
                    }
                ),
            )
            if not query_result.ok:
                return query_result
            query_payload = self._as_dict(query_result.payload)
            boundary_ids = [
                str(item.get("handle"))
                for item in query_payload.get("entities", [])
                if isinstance(item, dict) and item.get("handle")
            ]
            if query_payload.get("truncated") is True:
                session.unknowns.append(
                    {
                        "reason": "Boundary candidate query reached its limit; provide a narrower boundary scope before planning",
                    }
                )
        if not boundary_ids:
            return CommandResult.failure(ErrorCode.GEOMETRY_UNAVAILABLE, "No candidate boundaries were found")

        text_results = self._as_dict(session.facts.get("text_search")).get("results", [])
        label_results: dict[str, dict[str, Any]] = {}
        if isinstance(text_results, list):
            for item in text_results:
                if isinstance(item, dict) and item.get("query"):
                    label_results.setdefault(str(item["query"]).casefold(), item)
        for label in session.facts.get("label_queries", []):
            if not isinstance(label, str):
                continue
            result = label_results.get(label.casefold())
            if result is None:
                session.unknowns.append(
                    {
                        "label": label,
                        "reason": "Required label search result is unavailable",
                    }
                )
                continue
            matches = result.get("matches")
            if result.get("truncated") is True:
                session.unknowns.append(
                    {
                        "label": label,
                        "reason": "Required label search reached its match limit",
                    }
                )
            if not isinstance(matches, list) or not matches:
                session.unknowns.append(
                    {
                        "label": label,
                        "reason": "Required label query did not match a source entity",
                    }
                )

        process_ids = [handle for handle in (process_handles or []) if handle]
        label_ids = self._label_handles_from_search(session)
        handles = list(dict.fromkeys([*boundary_ids, *process_ids, *label_ids]))
        if len(handles) > 200:
            retained = handles[:200]
            omitted = handles[200:]
            label_id_set = set(label_ids)
            for handle in omitted:
                session.unknowns.append(
                    {
                        "handle": handle,
                        "reason": (
                            "Required label geometry batch limit prevented verification"
                            if handle in label_id_set
                            else "Geometry batch limit prevented verification of this candidate"
                        ),
                    }
                )
            handles = retained
        capability_error = self._require_capabilities(session, ["entity.get_geometry_batch"])
        if capability_error is not None:
            return capability_error
        geometry_result = await self._call(session, lambda: backend.entity_get_geometry_batch(handles))
        if not geometry_result.ok:
            return geometry_result
        geometries = self._as_dict(geometry_result.payload).get("geometries", [])
        if not isinstance(geometries, list):
            return CommandResult.failure(ErrorCode.PROTOCOL_ERROR, "Geometry batch did not return geometries")
        geometry_by_handle = {
            str(item.get("handle")): item
            for item in geometries
            if isinstance(item, dict) and item.get("handle")
        }
        for handle in label_ids:
            label_geometry = geometry_by_handle.get(handle)
            if not self._belongs_to_session_document(label_geometry, session.document_id):
                session.unknowns.append(
                    {
                        "handle": handle,
                        "reason": "Required label has no verified source-document geometry record",
                    }
                )
        valid_boundaries = []
        for handle in boundary_ids:
            geometry = geometry_by_handle.get(handle)
            if not self._belongs_to_session_document(geometry, session.document_id) or not self._is_closed_polygon(geometry):
                session.unknowns.append(
                    {
                        "handle": handle,
                        "reason": "Boundary has no verified source-document closed polyline vertices",
                    }
                )
                continue
            valid_boundaries.append(geometry)
        if not valid_boundaries:
            return CommandResult.failure(
                ErrorCode.GEOMETRY_UNAVAILABLE,
                "No candidate boundary has verified closed geometry",
                details={"unknowns": session.unknowns},
            )

        mapping: list[dict[str, Any]] = []
        text_results = self._as_dict(session.facts.get("text_search")).get("results", [])
        for result in text_results if isinstance(text_results, list) else []:
            if not isinstance(result, dict):
                continue
            for match in result.get("matches", []):
                if not isinstance(match, dict):
                    continue
                point = self._label_point(match)
                if point is None:
                    session.unknowns.append(
                        {"handle": match.get("handle"), "reason": "Label has no insertion point or bounds"}
                    )
                    continue
                contained = [
                    boundary for boundary in valid_boundaries
                    if self._point_in_or_on_polygon(point, boundary["vertices"])
                ]
                candidate_pool = contained or valid_boundaries
                candidate = min(
                    candidate_pool,
                    key=lambda boundary: (
                        abs(self._polygon_area(boundary["vertices"])),
                        self._distance_to_polyline(point, boundary["vertices"]),
                    ),
                )
                relation = "contains" if contained else "nearest_boundary"
                if not contained:
                    session.unknowns.append(
                        {
                            "label_handle": match.get("handle"),
                            "boundary_handle": candidate.get("handle"),
                            "reason": "Label was not proven inside a boundary; nearest boundary is only a hypothesis",
                        }
                    )
                mapping.append(
                    {
                        "label_handle": match.get("handle"),
                        "text": match.get("text"),
                        "point": point,
                        "boundary_handle": candidate.get("handle"),
                        "relation": relation,
                        "evidence": {
                            "vertices_verified": True,
                            "closed": True,
                            "candidate_count": len(contained),
                        },
                    }
                )
        process_mapping = self._map_process_geometry(
            process_ids,
            geometry_by_handle,
            valid_boundaries,
            session,
        )
        session.mapping = {
            "boundaries": valid_boundaries,
            "geometries": list(geometry_by_handle.values()),
            "process_handles": process_ids,
            "label_handles": label_ids,
            "label_to_boundary": mapping,
            "process_to_boundary": process_mapping,
            "boundary_topology": self._map_boundary_topology(valid_boundaries),
        }
        session.evidence.append(
            {
                "phase": "MAP",
                "operation": "entity.get_geometry_batch + verified spatial relations",
                "document_id": session.document_id,
                "handles": sorted(geometry_by_handle),
                "boundary_count": len(valid_boundaries),
            }
        )
        session.state = WorkflowState.MAPPED
        session.next_action = "plan_or_synthesize_concept"
        self._audit(session, "MAP", {"boundaries": len(valid_boundaries), "labels": len(mapping)})
        return CommandResult(ok=True, payload=session.snapshot())

    async def plan(
        self,
        session_id: str,
        *,
        actions: list[dict[str, Any]],
        target_path: str,
        allow_uncertainties: bool = False,
    ) -> CommandResult:
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state is not WorkflowState.MAPPED:
            return self._invalid_state(session, WorkflowState.MAPPED)
        if not target_path:
            return CommandResult.failure(ErrorCode.INVALID_REQUEST, "target_path is required")
        if not target_path.lower().endswith("_overlay.dwg"):
            return CommandResult.failure(
                ErrorCode.INVALID_REQUEST,
                "target_path must end with _overlay.dwg so the source cannot be overwritten",
            )
        source_path = self._string_or_none(
            self._as_dict(session.facts.get("drawing_state")).get("absolute_path")
        )
        if source_path and os.path.normcase(os.path.abspath(target_path)) == os.path.normcase(os.path.abspath(source_path)):
            return CommandResult.failure(
                ErrorCode.SOURCE_IMMUTABLE,
                "target_path must be separate from the immutable source drawing",
            )
        if session.mode == "mutation" and session.before_dbmod != 0:
            return CommandResult.failure(
                ErrorCode.SOURCE_UNSAVED,
                "Mutation planning requires a saved source drawing with DBMOD=0",
                details={"before_dbmod": session.before_dbmod},
            )
        required_label_unknowns = [
            item
            for item in session.unknowns
            if isinstance(item, dict)
            and str(item.get("reason") or "").startswith("Required label")
        ]
        if required_label_unknowns:
            return CommandResult.failure(
                ErrorCode.GEOMETRY_UNAVAILABLE,
                "Required labels do not have verified source geometry records",
                details={"unknowns": required_label_unknowns},
            )
        if session.unknowns and not allow_uncertainties:
            return CommandResult.failure(
                ErrorCode.GEOMETRY_UNAVAILABLE,
                "Unresolved geometry requires explicit allow_uncertainties confirmation",
                details={"unknowns": session.unknowns},
            )
        try:
            normalized_actions = self._validate_actions(
                actions,
                session.mapping.get("geometries", []),
                session.document_id,
            )
        except ValueError as exc:
            return CommandResult.failure(ErrorCode.GEOMETRY_UNAVAILABLE, str(exc))
        session.plan = {
            "document_id": session.document_id,
            "before_fingerprint": session.before_fingerprint,
            "before_dbmod": session.before_dbmod,
            "target_path": target_path,
            "actions": normalized_actions,
            "removed_handles": [],
            "uncertainties": session.unknowns,
            "approval_required": True,
            "screenshot_required": True,
            "required_labels": self._required_label_records(session),
            "idempotency_key": secrets.token_urlsafe(18),
            "allow_uncertainties": allow_uncertainties,
            "required_capabilities": ["batch.preview", "batch.apply", "batch.get_screenshot"],
            "change_table": self._change_table(normalized_actions),
        }
        session.plan_hash = self._hash_plan(session.plan)
        session.plan["plan_hash"] = session.plan_hash
        session.evidence.append(
            {
                "phase": "PLAN",
                "operation": "agent plan validation",
                "document_id": session.document_id,
                "fingerprint": session.before_fingerprint,
                "plan_hash": session.plan_hash,
                "action_count": len(normalized_actions),
            }
        )
        if allow_uncertainties:
            session.assumptions.append("User explicitly accepted the unresolved geometry listed in plan.uncertainties")
        session.state = WorkflowState.PLANNED
        session.next_action = "preview"
        self._audit(session, "PLAN", {"actions": len(normalized_actions)})
        return CommandResult(ok=True, payload=session.snapshot())

    async def preview(self, session_id: str) -> CommandResult:
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state is not WorkflowState.PLANNED:
            return self._invalid_state(session, WorkflowState.PLANNED)
        if session.mode != "mutation":
            return CommandResult.failure(
                ErrorCode.SOURCE_MUTATION_BLOCKED,
                "Read-only agent sessions cannot create a mutation preview",
            )
        assert session.plan is not None
        backend = await self._backend()
        capability_error = self._require_capabilities(
            session,
            ["batch.preview", "batch.apply", "batch.get_screenshot"],
        )
        if capability_error is not None:
            return capability_error
        result = await self._call(session, lambda: backend.batch_preview(session.plan or {}))
        if not result.ok:
            self._fail_session(
                session,
                "PREVIEW_FAILED",
                {"error_code": result.error_code, "error": result.error},
            )
            return result
        session.preview = self._as_dict(result.payload)
        batch_id = str(session.preview.get("batch_id") or "")
        if not batch_id or not session.preview.get("approval_token"):
            self._fail_session(session, "PREVIEW_FAILED", {"reason": "missing batch or approval token"})
            return CommandResult.failure(
                ErrorCode.PROTOCOL_ERROR,
                "Preview did not return a batch_id and approval token",
            )
        preview_hash = str(session.preview.get("plan_hash") or session.plan.get("plan_hash") or "")
        if session.plan_hash and preview_hash != session.plan_hash:
            self._fail_session(session, "PREVIEW_FAILED", {"reason": "plan_hash_mismatch"})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Preview is not bound to the current plan",
                details={"expected_plan_hash": session.plan_hash, "actual_plan_hash": preview_hash},
            )
        preview_document_id = self._string_or_none(session.preview.get("document_id"))
        preview_fingerprint = self._string_or_none(session.preview.get("before_fingerprint"))
        preview_dbmod = self._int_or_none(session.preview.get("before_dbmod"))
        if (
            preview_document_id != session.document_id
            or preview_fingerprint != session.before_fingerprint
            or preview_dbmod != session.before_dbmod
        ):
            self._fail_session(session, "PREVIEW_FAILED", {"reason": "preview_source_identity_mismatch"})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Preview is not bound to the observed source document state",
                details={
                    "expected_document_id": session.document_id,
                    "actual_document_id": preview_document_id,
                    "expected_fingerprint": session.before_fingerprint,
                    "actual_fingerprint": preview_fingerprint,
                    "expected_dbmod": session.before_dbmod,
                    "actual_dbmod": preview_dbmod,
                },
            )
        if session.preview.get("source_immutable") is not True:
            self._fail_session(session, "PREVIEW_FAILED", {"reason": "source_immutability_unproven"})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Preview did not prove that the source drawing is immutable",
            )
        preview_verification = self._as_dict(session.preview.get("overlay_verification"))
        if (
            preview_verification.get("created_handles_verified") is not True
            or preview_verification.get("source_to_overlay_verified") is not True
        ):
            self._fail_session(session, "PREVIEW_FAILED", {"reason": "overlay_handles_unverified"})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Preview did not verify its output-clone handles",
                details={"overlay_verification": preview_verification},
            )
        missing_sources = self._missing_planned_source_mappings(session.plan, session.preview)
        if missing_sources:
            self._fail_session(session, "PREVIEW_FAILED", {"reason": "planned_sources_unmapped", "handles": missing_sources})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Preview did not return a source-to-overlay handle for every planned source entity",
                details={"missing_source_handles": missing_sources},
            )
        missing_labels = self._missing_required_labels(session.plan, session.preview)
        if missing_labels:
            self._fail_session(session, "PREVIEW_FAILED", {"reason": "required_labels_unmapped", "handles": missing_labels})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Preview did not preserve every required label handle",
                details={"missing_label_handles": missing_labels},
            )
        preview_screenshot = self._as_dict(session.preview.get("screenshot"))
        preview_metadata = self._as_dict(preview_screenshot.get("metadata"))
        if not isinstance(preview_screenshot.get("data"), str) or preview_metadata.get("scope") != "output_clone":
            self._fail_session(session, "PREVIEW_FAILED", {"screenshot_scope": preview_metadata.get("scope")})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Preview must include a screenshot of the isolated output clone",
                details={"screenshot_scope": preview_metadata.get("scope")},
            )
        session.approval_expires_at = self._float_or_none(session.preview.get("approval_expires_at"))
        if session.approval_expires_at is None or session.approval_expires_at <= time.time():
            self._fail_session(session, "PREVIEW_FAILED", {"reason": "missing_or_expired_approval"})
            return CommandResult.failure(
                ErrorCode.PROTOCOL_ERROR,
                "Preview did not return a valid future approval expiry",
            )
        self._attach_preview_handles(session)
        session.evidence.append(
            {
                "phase": "PREVIEW",
                "operation": "batch.preview",
                "batch_id": batch_id,
                "plan_hash": session.plan_hash,
                "screenshot_scope": "output_clone",
            }
        )
        session.state = WorkflowState.PREVIEWED
        session.next_action = "approval"
        self._audit(session, "PREVIEW", {"batch_id": batch_id})
        return CommandResult(ok=True, payload=session.snapshot())

    async def approve(self, session_id: str, approval_token: str, confirmed: bool) -> CommandResult:
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state is not WorkflowState.PREVIEWED:
            return self._invalid_state(session, WorkflowState.PREVIEWED)
        if not confirmed:
            return CommandResult.failure(ErrorCode.APPROVAL_REQUIRED, "Explicit confirmation is required before apply")
        expected = str((session.preview or {}).get("approval_token") or "")
        if not expected or not secrets.compare_digest(expected, approval_token):
            return CommandResult.failure(ErrorCode.APPROVAL_REQUIRED, "Approval token is invalid")
        if session.approval_expires_at is not None and time.time() >= session.approval_expires_at:
            return CommandResult.failure(ErrorCode.APPROVAL_EXPIRED, "Approval token has expired")
        session.state = WorkflowState.APPROVED
        session.next_action = "apply"
        self._audit(session, "APPROVAL", {"confirmed": True})
        return CommandResult(ok=True, payload=session.snapshot())

    async def apply(self, session_id: str, approval_token: str) -> CommandResult:
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state is not WorkflowState.APPROVED:
            return self._invalid_state(session, WorkflowState.APPROVED)
        assert session.plan is not None
        backend = await self._backend()
        capability_error = self._require_capabilities(session, ["batch.apply"])
        if capability_error is not None:
            return capability_error
        batch_id = str((session.preview or {}).get("batch_id") or "")
        expected_token = str((session.preview or {}).get("approval_token") or "")
        if not batch_id:
            return CommandResult.failure(ErrorCode.TRANSACTION_FAILED, "Preview did not return a batch_id")
        if not expected_token or not secrets.compare_digest(expected_token, approval_token):
            return CommandResult.failure(ErrorCode.APPROVAL_REQUIRED, "Approval token is invalid")
        if session.approval_expires_at is not None and time.time() >= session.approval_expires_at:
            return CommandResult.failure(ErrorCode.APPROVAL_EXPIRED, "Approval token has expired")
        result = await self._call(
            session,
            lambda: backend.batch_apply(
                batch_id,
                approval_token,
                idempotency_key=str(session.plan.get("idempotency_key") or ""),
            ),
        )
        if not result.ok:
            self._fail_session(
                session,
                "APPLY_FAILED",
                {"error_code": result.error_code, "error": result.error},
            )
            return result
        session.apply_result = self._as_dict(result.payload)
        if session.apply_result.get("source_unchanged") is not True:
            self._fail_session(session, "APPLY_FAILED", {"reason": "source_immutability_unproven"})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Bridge did not prove the source drawing remained unchanged",
            )
        output_path = str(session.apply_result.get("output_path") or "")
        expected_output_path = str(session.plan.get("target_path") or "")
        source_path = self._string_or_none(
            self._as_dict(session.facts.get("drawing_state")).get("absolute_path")
        )
        if (
            not output_path.lower().endswith("_overlay.dwg")
            or not expected_output_path
            or not self._same_path(output_path, expected_output_path)
            or source_path is not None and self._same_path(output_path, source_path)
        ):
            self._fail_session(session, "APPLY_FAILED", {"reason": "invalid_output_path", "output_path": output_path})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Bridge did not return the planned separate _overlay.dwg output path",
            )
        removed_handles = session.apply_result.get("removed_handles")
        if not isinstance(removed_handles, list) or removed_handles:
            self._fail_session(session, "APPLY_FAILED", {"reason": "source_handles_removed"})
            return CommandResult.failure(
                ErrorCode.SOURCE_IMMUTABLE,
                "Bridge reported source handles removed from an immutable-source batch",
            )
        missing_labels = self._missing_required_labels(session.plan, session.apply_result)
        if missing_labels:
            self._fail_session(session, "APPLY_FAILED", {"reason": "required_labels_unmapped", "handles": missing_labels})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Apply did not preserve every required label handle",
                details={"missing_label_handles": missing_labels},
            )
        missing_sources = self._missing_planned_source_mappings(session.plan, session.apply_result)
        if missing_sources:
            self._fail_session(session, "APPLY_FAILED", {"reason": "planned_sources_unmapped", "handles": missing_sources})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Apply did not return a source-to-overlay handle for every planned source entity",
                details={"missing_source_handles": missing_sources},
            )
        session.evidence.append(
            {
                "phase": "APPLY",
                "operation": "batch.apply",
                "batch_id": batch_id,
                "output_path": output_path,
                "source_unchanged": True,
            }
        )
        session.state = WorkflowState.APPLIED
        session.next_action = "verify"
        self._audit(session, "APPLY", {"batch_id": batch_id})
        return CommandResult(ok=True, payload=session.snapshot())

    async def verify(self, session_id: str) -> CommandResult:
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        if session.state is not WorkflowState.APPLIED:
            return self._invalid_state(session, WorkflowState.APPLIED)
        backend = await self._backend()
        capability_error = self._require_capabilities(
            session,
            ["drawing.get_fingerprint", "batch.get_screenshot"],
        )
        if capability_error is not None:
            return capability_error
        fingerprint_result = await self._call(session, backend.drawing_get_fingerprint)
        if not fingerprint_result.ok:
            return fingerprint_result
        fingerprint_payload = self._as_dict(fingerprint_result.payload)
        after_fingerprint = self._string_or_none(fingerprint_payload.get("fingerprint"))
        after_dbmod = self._int_or_none(fingerprint_payload.get("dbmod"))
        after_document_id = self._string_or_none(fingerprint_payload.get("document_id"))
        if (
            after_document_id != session.document_id
            or after_fingerprint != session.before_fingerprint
            or after_dbmod is None
            or after_dbmod != session.before_dbmod
        ):
            self._fail_session(session, "VERIFY_FAILED", {"reason": "source_fingerprint_changed"})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Source drawing fingerprint changed during overlay workflow",
                details={
                    "before_document_id": session.document_id,
                    "after_document_id": after_document_id,
                    "before": session.before_fingerprint,
                    "after": after_fingerprint,
                    "before_dbmod": session.before_dbmod,
                    "after_dbmod": after_dbmod,
                },
            )
        overlay_verification = self._as_dict((session.apply_result or {}).get("overlay_verification"))
        if (
            overlay_verification.get("created_handles_verified") is not True
            or overlay_verification.get("source_to_overlay_verified") is not True
        ):
            self._fail_session(session, "VERIFY_FAILED", {"reason": "overlay_handles_unverified"})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Bridge did not verify overlay handles in the output clone",
                details={"overlay_verification": overlay_verification},
            )
        batch_id = str((session.preview or {}).get("batch_id") or "")
        if not batch_id:
            return CommandResult.failure(ErrorCode.TRANSACTION_FAILED, "Preview did not return a batch_id")
        screenshot_result = await self._call(session, lambda: backend.batch_get_screenshot(batch_id))
        if not screenshot_result.ok:
            self._fail_session(session, "VERIFY_FAILED", {"reason": "output_screenshot_unavailable"})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "Cannot complete workflow without a final output-clone verification screenshot",
                details={"screenshot_error": screenshot_result.to_dict().get("error")},
            )
        screenshot = self._as_dict(screenshot_result.payload)
        screenshot_metadata = self._as_dict(screenshot.get("metadata"))
        if not isinstance(screenshot.get("data"), str) or screenshot_metadata.get("scope") != "output_clone":
            self._fail_session(session, "VERIFY_FAILED", {"reason": "invalid_screenshot_scope"})
            return CommandResult.failure(
                ErrorCode.VERIFICATION_FAILED,
                "A source-document screenshot cannot verify an immutable overlay output",
                details={"screenshot_scope": screenshot_metadata.get("scope")},
            )
        session.facts["verification"] = {
            "source_fingerprint_unchanged": True,
            "screenshot": screenshot,
        }
        session.evidence.append(
            {
                "phase": "VERIFY",
                "operation": "drawing.get_fingerprint + batch.get_screenshot",
                "document_id": after_document_id,
                "fingerprint": after_fingerprint,
                "dbmod": after_dbmod,
                "screenshot_scope": "output_clone",
            }
        )
        session.state = WorkflowState.VERIFIED
        session.next_action = "completed"
        self._audit(session, "VERIFY", {"screenshot": True})
        return CommandResult(ok=True, payload=session.snapshot())

    @staticmethod
    def _map_process_geometry(
        process_ids: list[str],
        geometry_by_handle: dict[str, dict[str, Any]],
        boundaries: list[dict[str, Any]],
        session: AgentSession,
    ) -> list[dict[str, Any]]:
        mappings: list[dict[str, Any]] = []
        for handle in process_ids:
            geometry = geometry_by_handle.get(handle)
            if not AgentRuntime._is_linear_path(geometry):
                session.unknowns.append(
                    {
                        "handle": handle,
                        "reason": "Process line has no verified linear vertices for containment mapping",
                    }
                )
                continue
            assert isinstance(geometry, dict)
            vertices = geometry["vertices"]
            candidates = [
                boundary
                for boundary in boundaries
                if AgentRuntime._path_is_inside_boundary(vertices, boundary["vertices"])
            ]
            if len(candidates) != 1:
                session.unknowns.append(
                    {
                        "handle": handle,
                        "reason": "Process line could not be assigned to exactly one verified boundary",
                        "candidate_boundary_handles": [candidate.get("handle") for candidate in candidates],
                    }
                )
                continue
            boundary = candidates[0]
            mappings.append(
                {
                    "process_handle": handle,
                    "boundary_handle": boundary.get("handle"),
                    "relation": "contained",
                    "evidence": {"vertices_verified": True, "segments_verified_linear": True},
                }
            )
        return mappings

    @staticmethod
    def _map_boundary_topology(boundaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        topology: list[dict[str, Any]] = []
        for index, first in enumerate(boundaries):
            for second in boundaries[index + 1 :]:
                first_vertices = first["vertices"]
                second_vertices = second["vertices"]
                intersects = AgentRuntime._paths_intersect(first_vertices, second_vertices)
                first_in_second = all(
                    AgentRuntime._point_in_or_on_polygon(point, second_vertices) for point in first_vertices
                )
                second_in_first = all(
                    AgentRuntime._point_in_or_on_polygon(point, first_vertices) for point in second_vertices
                )
                relation = (
                    "intersects"
                    if intersects
                    else "inside"
                    if first_in_second
                    else "contains"
                    if second_in_first
                    else "disjoint"
                )
                topology.append(
                    {
                        "first_handle": first.get("handle"),
                        "second_handle": second.get("handle"),
                        "relation": relation,
                        "evidence": {"vertices_verified": True, "segments_verified_linear": True},
                    }
                )
        return topology

    async def rollback(self, session_id: str) -> CommandResult:
        session = self._sessions.get(session_id)
        if session is None:
            return self._missing_session(session_id)
        batch_id = str((session.preview or {}).get("batch_id") or "")
        if not batch_id:
            return CommandResult.failure(ErrorCode.TRANSACTION_FAILED, "No preview batch is available to rollback")
        backend = await self._backend()
        capability_error = self._require_capabilities(session, ["batch.rollback"])
        if capability_error is not None:
            return capability_error
        result = await self._call(session, lambda: backend.batch_rollback(batch_id))
        if not result.ok:
            return result
        session.state = WorkflowState.ROLLED_BACK
        session.next_action = "rolled_back"
        self._audit(session, "ROLLBACK", {"batch_id": batch_id})
        return CommandResult(ok=True, payload=session.snapshot())

    async def _backend(self) -> AutoCADBackend:
        if self._backend_factory is not None:
            return await self._backend_factory()
        from autocad_mcp.client import get_backend

        return await get_backend()

    async def _call(
        self,
        session: AgentSession,
        operation: Callable[[], Awaitable[CommandResult]],
    ) -> CommandResult:
        if session.calls_used >= session.max_calls:
            return CommandResult.failure(
                ErrorCode.CALL_BUDGET_EXCEEDED,
                "Agent call budget has been exhausted",
                details={"calls_used": session.calls_used, "max_calls": session.max_calls},
            )
        session.calls_used += 1
        return await operation()

    @staticmethod
    def _same_path(first: str, second: str) -> bool:
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))

    def _attach_preview_handles(self, session: AgentSession) -> None:
        if session.plan is None or session.preview is None:
            return
        source_to_overlay = self._as_dict(session.preview.get("source_to_overlay"))
        resolved_table: list[dict[str, Any]] = []
        for source_row in session.plan.get("change_table", []):
            row = dict(source_row) if isinstance(source_row, dict) else {}
            if not isinstance(row, dict):
                continue
            source_handles = row.get("source_handles")
            if not isinstance(source_handles, list):
                resolved_table.append(row)
                continue
            row["new_handles"] = [
                str(source_to_overlay[handle])
                for handle in source_handles
                if handle in source_to_overlay and source_to_overlay[handle]
            ]
            resolved_table.append(row)
        session.preview["change_table"] = resolved_table

    def _fail_session(self, session: AgentSession, phase: str, details: dict[str, Any]) -> None:
        session.state = WorkflowState.FAILED
        self._audit(session, phase, details)

    @staticmethod
    def _missing_required_labels(
        plan: dict[str, Any] | None,
        result: dict[str, Any] | None,
    ) -> list[str]:
        if not isinstance(plan, dict) or not isinstance(result, dict):
            return []
        required = plan.get("required_labels", [])
        mapping = result.get("source_to_overlay", {})
        if not isinstance(required, list) or not isinstance(mapping, dict):
            return []
        return [
            str(item.get("source_handle"))
            for item in required
            if isinstance(item, dict)
            and item.get("source_handle")
            and not mapping.get(str(item.get("source_handle")))
        ]

    @staticmethod
    def _missing_planned_source_mappings(
        plan: dict[str, Any] | None,
        result: dict[str, Any] | None,
    ) -> list[str]:
        """Return planned source entities that the bridge did not map in output."""

        if not isinstance(plan, dict) or not isinstance(result, dict):
            return []
        mapping = result.get("source_to_overlay")
        if not isinstance(mapping, dict):
            return [
                str(action.get("source_handle"))
                for action in plan.get("actions", [])
                if isinstance(action, dict)
                and action.get("action") in {"preserve", "copy_to_overlay", "simplify_copy"}
                and action.get("source_handle")
            ]
        required: list[str] = []
        for action in plan.get("actions", []):
            if not isinstance(action, dict):
                continue
            if action.get("action") not in {"preserve", "copy_to_overlay", "simplify_copy"}:
                continue
            handle = str(action.get("source_handle") or "")
            if handle and handle not in required:
                required.append(handle)
        return [handle for handle in required if not mapping.get(handle)]

    @staticmethod
    def _require_capabilities(session: AgentSession, required: list[str]) -> CommandResult | None:
        # OBSERVE replaces the facts snapshot, so retain the negotiated
        # capability payload whether it is still nested under connection or not.
        capability_payload = AgentRuntime._as_dict(session.facts.get("capabilities"))
        if not capability_payload:
            connection = AgentRuntime._as_dict(session.facts.get("connection"))
            capability_payload = AgentRuntime._as_dict(connection.get("capabilities"))
        capabilities = AgentRuntime._as_dict(capability_payload.get("capabilities"))
        missing = [name for name in required if capabilities.get(name) is not True]
        if missing:
            return CommandResult.failure(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "The direct bridge does not expose all capabilities required for this workflow phase",
                details={"missing": missing},
            )
        return None

    @staticmethod
    def _validate_actions(
        actions: list[dict[str, Any]],
        boundaries: list[dict[str, Any]],
        document_id: str | None,
    ) -> list[dict[str, Any]]:
        if not actions:
            raise ValueError("At least one overlay action is required")
        known_geometry = {
            str(boundary.get("handle")): boundary
            for boundary in boundaries
            if AgentRuntime._belongs_to_session_document(boundary, document_id) and boundary.get("handle")
        }
        normalized: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError("Every action must be an object")
            kind = str(action.get("action") or "")
            if kind not in ALLOWED_ACTIONS:
                raise ValueError(f"Unsupported action: {kind}")
            item = dict(action)
            if kind in {"preserve", "copy_to_overlay", "simplify_copy"}:
                handle = str(item.get("source_handle") or "")
                if not handle or handle not in known_geometry:
                    raise ValueError(f"{kind} requires source_handle with verified geometry")
            if kind in {"copy_to_overlay", "simplify_copy", "create_connector_line"} and not item.get("target_layer"):
                raise ValueError(f"{kind} requires target_layer")
            if kind == "simplify_copy":
                geometry = known_geometry[handle]
                vertices = geometry.get("vertices")
                indices = item.get("vertex_indices")
                if not isinstance(vertices, list) or not isinstance(indices, list) or len(indices) < 2:
                    raise ValueError("simplify_copy requires vertex_indices from verified geometry")
                if any(not isinstance(index, int) or index < 0 or index >= len(vertices) for index in indices):
                    raise ValueError("simplify_copy vertex_indices are outside verified geometry")
                if not AgentRuntime._geometry_is_linear(geometry):
                    raise ValueError("simplify_copy cannot approximate curved source geometry")
                item["source_vertices"] = [list(vertices[index]) for index in indices]
                item["closed"] = bool(
                    item.get("closed", geometry.get("closed") is True and len(indices) == len(vertices))
                )
            if kind == "create_connector_line":
                for endpoint in ("start", "end"):
                    source_handle = str(item.get(f"{endpoint}_source_handle") or "")
                    vertex_index = item.get(f"{endpoint}_vertex_index")
                    geometry = known_geometry.get(source_handle)
                    vertices = geometry.get("vertices") if geometry else None
                    if not isinstance(vertices, list) or not isinstance(vertex_index, int) or not 0 <= vertex_index < len(vertices):
                        raise ValueError(
                            f"{endpoint} must reference a verified source_handle and vertex_index"
                        )
                    vertex = vertices[vertex_index]
                    if not isinstance(vertex, list) or len(vertex) < 2:
                        raise ValueError(f"{endpoint} source vertex is unavailable")
                    item[endpoint] = [float(vertex[0]), float(vertex[1])]
            normalized.append(item)
        return normalized

    @staticmethod
    def _change_table(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        table: list[dict[str, Any]] = []
        for action in actions:
            kind = str(action["action"])
            source_handles = [
                str(value)
                for key, value in action.items()
                if key in {"source_handle", "start_source_handle", "end_source_handle"} and value
            ]
            table.append(
                {
                    "source_handles": source_handles,
                    "action": kind,
                    "new_handles": [],
                    "reason": str(action.get("reason") or f"{kind} from verified source geometry"),
                }
            )
        return table

    @staticmethod
    def _is_closed_polygon(geometry: Any) -> bool:
        return (
            isinstance(geometry, dict)
            and geometry.get("closed") is True
            and isinstance(geometry.get("vertices"), list)
            and len(geometry["vertices"]) >= 3
            and AgentRuntime._geometry_is_linear(geometry)
        )

    @staticmethod
    def _is_linear_path(geometry: Any) -> bool:
        return (
            isinstance(geometry, dict)
            and isinstance(geometry.get("vertices"), list)
            and len(geometry["vertices"]) >= 2
            and AgentRuntime._geometry_is_linear(geometry)
        )

    @staticmethod
    def _belongs_to_session_document(geometry: Any, document_id: str | None) -> bool:
        return (
            isinstance(geometry, dict)
            and document_id is not None
            and geometry.get("source_document_id") == document_id
        )

    @staticmethod
    def _geometry_is_linear(geometry: dict[str, Any]) -> bool:
        segments = geometry.get("segments")
        if segments is None:
            # Missing segment metadata must not silently approximate arcs.
            return False
        try:
            return all(
                isinstance(segment, dict)
                and float(segment.get("bulge", 0.0) or 0.0) == 0.0
                for segment in segments
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _label_point(match: dict[str, Any]) -> list[float] | None:
        insertion = match.get("insertion")
        if isinstance(insertion, list) and len(insertion) >= 2:
            return [float(insertion[0]), float(insertion[1])]
        bounds = match.get("bounds")
        if isinstance(bounds, dict):
            try:
                return [
                    (float(bounds["xmin"]) + float(bounds["xmax"])) / 2.0,
                    (float(bounds["ymin"]) + float(bounds["ymax"])) / 2.0,
                ]
            except (KeyError, TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _point_in_polygon(point: list[float], vertices: list[list[float]]) -> bool:
        inside = False
        x, y = point
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            x1, y1 = float(start[0]), float(start[1])
            x2, y2 = float(end[0]), float(end[1])
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
        return inside

    @staticmethod
    def _polygon_area(vertices: list[list[float]]) -> float:
        """Return signed shoelace area from verified polygon vertices."""

        if len(vertices) < 3:
            return 0.0
        return 0.5 * sum(
            float(vertices[index][0]) * float(vertices[(index + 1) % len(vertices)][1])
            - float(vertices[(index + 1) % len(vertices)][0]) * float(vertices[index][1])
            for index in range(len(vertices))
        )

    @staticmethod
    def _distance_to_polyline(point: list[float], vertices: list[list[float]]) -> float:
        if len(vertices) < 2:
            return math.inf
        x, y = point
        best = math.inf
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            x1, y1 = float(start[0]), float(start[1])
            x2, y2 = float(end[0]), float(end[1])
            dx, dy = x2 - x1, y2 - y1
            length = dx * dx + dy * dy
            if length == 0:
                best = min(best, math.hypot(x - x1, y - y1))
                continue
            t = min(1.0, max(0.0, ((x - x1) * dx + (y - y1) * dy) / length))
            best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
        return best

    @staticmethod
    def _point_in_or_on_polygon(point: list[float], vertices: list[list[float]]) -> bool:
        return AgentRuntime._point_in_polygon(point, vertices) or AgentRuntime._distance_to_polyline(point, vertices) <= 1e-9

    @staticmethod
    def _path_is_inside_boundary(path: list[list[float]], boundary: list[list[float]]) -> bool:
        if not all(AgentRuntime._point_in_or_on_polygon(point, boundary) for point in path):
            return False
        for index, start in enumerate(path[:-1]):
            end = path[index + 1]
            midpoint = [(float(start[0]) + float(end[0])) / 2.0, (float(start[1]) + float(end[1])) / 2.0]
            if not AgentRuntime._point_in_or_on_polygon(midpoint, boundary):
                return False
        return not AgentRuntime._paths_intersect(path, boundary, first_closed=False)

    @staticmethod
    def _paths_intersect(
        first: list[list[float]],
        second: list[list[float]],
        *,
        first_closed: bool = True,
        second_closed: bool = True,
    ) -> bool:
        for first_start, first_end in AgentRuntime._segments(first, closed=first_closed):
            for second_start, second_end in AgentRuntime._segments(second, closed=second_closed):
                if AgentRuntime._segments_intersect(first_start, first_end, second_start, second_end):
                    return True
        return False

    @staticmethod
    def _segments(vertices: list[list[float]], *, closed: bool) -> list[tuple[list[float], list[float]]]:
        segment_count = len(vertices) if closed else len(vertices) - 1
        return [
            (vertices[index], vertices[(index + 1) % len(vertices)])
            for index in range(max(segment_count, 0))
        ]

    @staticmethod
    def _segments_intersect(
        first_start: list[float],
        first_end: list[float],
        second_start: list[float],
        second_end: list[float],
    ) -> bool:
        def orientation(start: list[float], end: list[float], point: list[float]) -> float:
            return (float(end[0]) - float(start[0])) * (float(point[1]) - float(start[1])) - (
                float(end[1]) - float(start[1])
            ) * (float(point[0]) - float(start[0]))

        def on_segment(start: list[float], end: list[float], point: list[float]) -> bool:
            return (
                min(float(start[0]), float(end[0])) - 1e-9 <= float(point[0]) <= max(float(start[0]), float(end[0])) + 1e-9
                and min(float(start[1]), float(end[1])) - 1e-9 <= float(point[1]) <= max(float(start[1]), float(end[1])) + 1e-9
            )

        a = orientation(first_start, first_end, second_start)
        b = orientation(first_start, first_end, second_end)
        c = orientation(second_start, second_end, first_start)
        d = orientation(second_start, second_end, first_end)
        if (a > 0) != (b > 0) and (c > 0) != (d > 0):
            return True
        return (
            abs(a) <= 1e-9 and on_segment(first_start, first_end, second_start)
            or abs(b) <= 1e-9 and on_segment(first_start, first_end, second_end)
            or abs(c) <= 1e-9 and on_segment(second_start, second_end, first_start)
            or abs(d) <= 1e-9 and on_segment(second_start, second_end, first_end)
        )

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _hash_plan(plan: dict[str, Any]) -> str:
        unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
        encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _audit(session: AgentSession, phase: str, details: Any) -> None:
        session.audit.append({"phase": phase, "at": time.time(), "details": details})

    @staticmethod
    def _missing_session(session_id: str) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.INVALID_REQUEST,
            "Unknown agent session_id",
            details={"session_id": session_id},
        )

    @staticmethod
    def _invalid_state(session: AgentSession, expected: WorkflowState) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.INVALID_AGENT_STATE,
            "Agent operation is not valid in the current workflow state",
            details={"current": session.state.value, "expected": expected.value},
        )


runtime = AgentRuntime()
