"""Lazy backend singleton, _safe/_error/_json helpers, screenshot utility."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
from typing import Any

import structlog
from mcp.types import ImageContent, TextContent

from autocad_mcp.backends.base import AutoCADBackend, CommandResult
from autocad_mcp.config import ONLY_TEXT_FEEDBACK, detect_backend
from autocad_mcp.errors import BackendError, ErrorCode

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Lazy backend singleton
# ---------------------------------------------------------------------------

_backend: AutoCADBackend | None = None
_init_lock = asyncio.Lock()


async def get_backend() -> AutoCADBackend:
    """Return (and lazily initialize) the backend singleton.

    Uses an asyncio Lock to prevent concurrent initialization races
    when multiple MCP tool calls arrive simultaneously.
    """
    global _backend
    if _backend is not None:
        return _backend

    async with _init_lock:
        # Double-check after acquiring lock (another task may have initialized)
        if _backend is not None:
            return _backend

        backend_name = detect_backend()

        if backend_name == "direct_bridge":
            from autocad_mcp.backends.direct_bridge import DirectBridgeBackend

            backend = DirectBridgeBackend()
        else:
            from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

            backend = EzdxfBackend()

        result = await backend.initialize()
        if not result.ok:
            raise BackendError(
                result.error_code or ErrorCode.AUTOCAD_NOT_CONNECTED,
                result.error or "Backend initialization failed",
                details=result.error_details,
            )

        _backend = backend
        log.info("backend_initialized", backend=_backend.name)
        return _backend


# ---------------------------------------------------------------------------
# JSON serialization helper
# ---------------------------------------------------------------------------


def _json(data: Any) -> str:
    """Serialize to compact JSON string."""
    return json.dumps(data, default=str, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Error formatting with actionable hints
# ---------------------------------------------------------------------------


def _error(e: Exception, context: str = "") -> str:
    """Format an exception with an actionable hint."""
    msg = str(e)
    msg_lower = msg.lower()

    if "autocad_not_connected" in msg_lower or "not accepting connections" in msg_lower:
        hint = "Load the AutoCADMcpBridge plugin with NETLOAD, then retry the read-only request."
    elif "timeout" in msg_lower:
        hint = "The direct bridge request exceeded its deadline. Inspect session.health; write requests are never retried automatically."
    elif "not supported" in msg_lower or "backend" in msg_lower:
        hint = "Operation not supported on current backend. Check system(operation='status') for capabilities."
    elif "approval_required" in msg_lower:
        hint = "Create a preview first, then supply the approval token returned by the bridge."
    else:
        hint = "Inspect the structured error and direct-bridge health before issuing another request."

    if isinstance(e, BackendError):
        error = e.to_error().to_dict()
        if context:
            error["details"] = {**error.get("details", {}), "context": context}
        return _json({"ok": False, "error": error, "hint": hint})
    return _json({
        "ok": False,
        "error": {
            "code": ErrorCode.UNKNOWN.value,
            "message": f"[{context}] {msg}" if context else msg,
            "details": {},
        },
        "hint": hint,
    })


# ---------------------------------------------------------------------------
# _safe decorator for tool error handling
# ---------------------------------------------------------------------------


def _safe(tool_name: str):
    """Wrap an async tool handler with uniform error handling."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                op = kwargs.get("operation", "unknown")
                log.error("tool_error", tool=tool_name, operation=op, error=str(e))
                return _error(e, f"{tool_name}.{op}")

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------


def _format_result(
    result: CommandResult,
    include_screenshot: bool = False,
    screenshot_payload: Any = None,
) -> list[TextContent | ImageContent] | str:
    """Format a CommandResult for MCP response.

    Returns a list with TextContent + optional ImageContent if screenshot requested,
    or a plain JSON string if no screenshot.
    """
    screenshot_data, screenshot_metadata = _split_screenshot_payload(screenshot_payload)
    response = result.to_dict()
    if screenshot_metadata:
        response["screenshot"] = {
            "attached": True,
            "metadata": screenshot_metadata,
        }
    text = _json(response)

    if not include_screenshot or ONLY_TEXT_FEEDBACK or not screenshot_data:
        return text

    return [
        TextContent(type="text", text=text),
        ImageContent(
            type="image",
            data=screenshot_data,
            mimeType="image/png",
        ),
    ]


def _split_screenshot_payload(payload: Any) -> tuple[str | None, dict]:
    """Accept both legacy base64 strings and metadata-aware screenshot payloads."""
    if isinstance(payload, str):
        return payload, {}
    if not isinstance(payload, dict):
        return None, {}
    data = payload.get("data")
    metadata = payload.get("metadata")
    return (
        data if isinstance(data, str) else None,
        metadata if isinstance(metadata, dict) else {},
    )


async def add_screenshot_if_available(
    result: CommandResult,
    include_screenshot: bool = False,
) -> list[TextContent | ImageContent] | str:
    """Conditionally append a screenshot to the result."""
    if not include_screenshot or ONLY_TEXT_FEEDBACK:
        return _json(result.to_dict())

    backend = await get_backend()
    screenshot_result = await backend.get_screenshot()

    if screenshot_result.ok and screenshot_result.payload:
        return _format_result(result, True, screenshot_result.payload)

    return _json(result.to_dict())
