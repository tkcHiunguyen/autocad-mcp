"""Direct localhost transport for the in-process AutoCAD .NET bridge.

Unlike the retired file IPC adapter, this module never locates, focuses, or
types into an AutoCAD window. The bridge is loaded once with ``NETLOAD`` and
then owns a loopback-only JSON-RPC listener inside the AutoCAD process.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import structlog

from autocad_mcp import __version__
from autocad_mcp.backends.base import AutoCADBackend, BackendCapabilities, CommandResult
from autocad_mcp.errors import ErrorCode

log = structlog.get_logger()

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = max(1.0, min(300.0, float(os.environ.get("AUTOCAD_MCP_BRIDGE_TIMEOUT", "30"))))
SAFE_DIRECT_WRITES = frozenset({"batch.preview", "batch.apply", "batch.rollback"})


def default_discovery_path() -> Path:
    """Return the bridge-owned discovery record, outside any drawing folder."""
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or "."
    return Path(root) / "AutoCAD-MCP" / "bridge.json"


class DirectBridgeBackend(AutoCADBackend):
    """Persistent JSON-RPC client for ``AutoCADMcpBridge``.

    A connection is reused for sequential requests. A dropped connection is
    re-established once for idempotent reads only; writes are deliberately
    never retried because the AutoCAD-side transaction may have committed.
    """

    def __init__(
        self,
        *,
        discovery_path: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._discovery_path = discovery_path or Path(
            os.environ.get("AUTOCAD_MCP_BRIDGE_DISCOVERY", default_discovery_path())
        )
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._session_id: str | None = None
        self._document_id: str | None = None
        self._document_state: dict[str, Any] = {}
        self._bridge_info: dict[str, Any] = {}
        self._capabilities: dict[str, bool] = {}
        self._connected_at: float | None = None
        self._needs_handshake = True

    @property
    def name(self) -> str:
        return "direct_bridge"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            can_read_drawing=bool(self._capabilities.get("drawing.get_state", False)),
            # Direct bridge mutation is intentionally limited to the safe batch
            # clone path. Legacy entity/drawing writes remain unsupported.
            can_modify_entities=False,
            can_create_entities=False,
            can_screenshot=bool(self._capabilities.get("view.get_screenshot", False)),
            can_save=False,
            can_plot_pdf=False,
            can_zoom=False,
            can_query_entities=bool(self._capabilities.get("entity.query", False)),
            can_file_operations=bool(self._capabilities.get("batch.apply", False)),
            can_undo=False,
            direct_transport=True,
            can_get_drawing_state=bool(self._capabilities.get("drawing.get_state", False)),
            can_get_geometry=bool(self._capabilities.get("entity.get_geometry", False)),
            can_query_spatial=bool(self._capabilities.get("entity.query_spatial", False)),
            can_batch=bool(self._capabilities.get("batch.preview", False)),
            can_transactions=bool(self._capabilities.get("batch.apply", False)),
            source_immutable_by_default=True,
        )

    async def initialize(self) -> CommandResult:
        try:
            self._session_id = None
            self._document_id = None
            self._document_state = {}
            self._capabilities = {}
            self._needs_handshake = True
            self._close_connection()
            await self._connect()
            result = await self.session_handshake()
            if not result.ok:
                return result
            payload = result.payload if isinstance(result.payload, dict) else {}
            if not self._session_id:
                return CommandResult.failure(
                    ErrorCode.PROTOCOL_ERROR,
                    "Bridge handshake did not return a session_id",
                )
            if not self._document_id:
                return CommandResult.failure(
                    ErrorCode.DOCUMENT_NOT_RESOLVED,
                    "Bridge is connected but AutoCAD has no active drawing document",
                    details={"bridge": self._bridge_info},
                )
            return CommandResult(
                ok=True,
                payload={
                    "backend": self.name,
                    "session_id": self._session_id,
                    "document": self._document_state,
                    "bridge": self._public_bridge_info(),
                    "capabilities": self._capabilities,
                },
            )
        except BridgeConnectionError as exc:
            return CommandResult.failure(exc.code, exc.message, details=exc.details)

    async def status(self) -> CommandResult:
        result = await self.session_health()
        if not result.ok:
            return result
        payload = dict(result.payload or {})
        payload.update(
            {
                "backend": self.name,
                "session_id": self._session_id,
                "document_id": self._document_id,
                "capabilities": self.capabilities.to_dict(),
                "bridge_capabilities": self._capabilities,
                "bridge": self._public_bridge_info(),
            }
        )
        return CommandResult(ok=True, payload=payload)

    async def session_health(self) -> CommandResult:
        result = await self._request("session.health", {}, write=False)
        if not result.ok or not isinstance(result.payload, dict):
            return result
        document = result.payload.get("document")
        if isinstance(document, dict):
            mismatch = self._check_document_identity(document)
            if mismatch is not None:
                return mismatch
            self._document_state = dict(document)
        return result

    async def session_handshake(self) -> CommandResult:
        return await self._request("session.handshake", {}, write=False, allow_reconnect=False)

    async def capabilities_list(self) -> CommandResult:
        result = await self._request("capabilities.list", {}, write=False)
        if result.ok and isinstance(result.payload, dict):
            self._capabilities = {
                str(name): bool(supported)
                for name, supported in dict(result.payload.get("capabilities") or {}).items()
            }
            result.payload = {
                "backend": self.name,
                "session_id": self._session_id,
                "document_id": self._document_id,
                "capabilities": dict(self._capabilities),
                "backend_capabilities": self.capabilities.to_dict(),
            }
        return result

    async def _connect(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        discovery = self._read_discovery()
        host = str(discovery.get("host") or "127.0.0.1")
        port = discovery.get("port")
        token = str(discovery.get("token") or "")
        if host != "127.0.0.1" or not isinstance(port, int) or not token:
            raise BridgeConnectionError(
                ErrorCode.AUTOCAD_NOT_CONNECTED,
                "AutoCAD direct bridge discovery is invalid or unavailable",
                details={"discovery_path": str(self._discovery_path)},
            )
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self._timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            self._close_connection()
            raise BridgeConnectionError(
                ErrorCode.AUTOCAD_NOT_CONNECTED,
                "AutoCAD direct bridge is not accepting connections",
                details={"host": host, "port": port, "reason": str(exc)},
            ) from exc
        self._bridge_info = {"host": host, "port": port, "token": token}
        self._connected_at = time.monotonic()

    def _read_discovery(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._discovery_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeConnectionError(
                ErrorCode.AUTOCAD_NOT_CONNECTED,
                "AutoCAD direct bridge discovery file is unavailable",
                details={"discovery_path": str(self._discovery_path), "reason": str(exc)},
            ) from exc
        if not isinstance(payload, dict):
            raise BridgeConnectionError(
                ErrorCode.PROTOCOL_ERROR,
                "AutoCAD direct bridge discovery file must contain a JSON object",
                details={"discovery_path": str(self._discovery_path)},
            )
        protocol_version = payload.get("protocol_version")
        if protocol_version is not None and protocol_version != PROTOCOL_VERSION:
            raise BridgeConnectionError(
                ErrorCode.PROTOCOL_ERROR,
                "AutoCAD direct bridge protocol version is unsupported",
                details={"expected": PROTOCOL_VERSION, "actual": protocol_version},
            )
        return payload

    def _close_connection(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = None
        self._writer = None
        self._needs_handshake = True

    def _public_bridge_info(self) -> dict[str, Any]:
        """Remove the loopback authentication secret from MCP responses."""
        return {key: value for key, value in self._bridge_info.items() if key != "token"}

    async def _request(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        write: bool,
        allow_reconnect: bool = True,
    ) -> CommandResult:
        attempts = 2 if allow_reconnect and not write else 1
        last_error: CommandResult | None = None
        request_ids: list[str] = []
        for attempt in range(attempts):
            async with self._lock:
                try:
                    await self._connect()
                    if operation != "session.handshake" and self._needs_handshake:
                        handshake = await self._request_once("session.handshake", {})
                        if not handshake.ok:
                            return handshake
                        handshake_error = self._accept_handshake(
                            handshake.payload if isinstance(handshake.payload, dict) else {}
                        )
                        if handshake_error is not None:
                            return handshake_error
                    assert self._reader is not None
                    assert self._writer is not None
                    result = await self._request_once(operation, params, request_ids)
                    if operation == "session.handshake" and result.ok:
                        handshake_error = self._accept_handshake(
                            result.payload if isinstance(result.payload, dict) else {}
                        )
                        if handshake_error is not None:
                            return handshake_error
                    return result
                except asyncio.CancelledError:
                    if request_ids:
                        # The bridge discards queued work with this ID. It is
                        # intentionally best effort: an operation already on
                        # AutoCAD's main thread cannot be safely pre-empted.
                        asyncio.create_task(self._cancel_pending_request(request_ids[-1]))
                    self._close_connection()
                    raise
                except asyncio.TimeoutError as exc:
                    self._close_connection()
                    last_error = CommandResult.failure(
                        ErrorCode.REQUEST_TIMEOUT,
                        "AutoCAD direct bridge request timed out",
                        details={"operation": operation, "timeout_seconds": self._timeout},
                    )
                except (BridgeConnectionError, ConnectionError, OSError, json.JSONDecodeError) as exc:
                    self._close_connection()
                    if isinstance(exc, BridgeConnectionError):
                        last_error = CommandResult.failure(exc.code, exc.message, details=exc.details)
                    else:
                        last_error = CommandResult.failure(
                            ErrorCode.AUTOCAD_NOT_CONNECTED,
                            "AutoCAD direct bridge connection failed",
                            details={"operation": operation, "reason": str(exc)},
                        )
            if attempt + 1 < attempts:
                log.info("direct_bridge_read_reconnect", operation=operation)
        return last_error or CommandResult.failure(ErrorCode.UNKNOWN, "Direct bridge request failed")

    async def _request_once(
        self,
        operation: str,
        params: dict[str, Any],
        request_ids: list[str] | None = None,
    ) -> CommandResult:
        """Send one framed request while the connection lock is held."""
        assert self._reader is not None
        assert self._writer is not None
        request_id = uuid.uuid4().hex
        if request_ids is not None:
            request_ids.append(request_id)
        token = str(self._bridge_info.get("token") or "")
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "request",
            "request_id": request_id,
            "session_id": None if operation == "session.handshake" else self._session_id,
            "document_id": None if operation.startswith("session.") else self._document_id,
            "token": token,
            "operation": operation,
            "params": params,
            "deadline_ms": int(self._timeout * 1000),
        }
        encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES:
            return CommandResult.failure(
                ErrorCode.INVALID_REQUEST,
                "Direct bridge request exceeds the maximum frame size",
                details={"operation": operation, "size": len(encoded)},
            )
        self._writer.write(encoded + b"\n")
        await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)
        raw = await asyncio.wait_for(self._reader.readline(), timeout=self._timeout)
        if not raw:
            raise ConnectionError("bridge closed the connection")
        if len(raw) > MAX_FRAME_BYTES:
            return CommandResult.failure(
                ErrorCode.PROTOCOL_ERROR,
                "Direct bridge response exceeds the maximum frame size",
                details={"operation": operation},
            )
        response = json.loads(raw.decode("utf-8"))
        if response.get("request_id") != request_id:
            return CommandResult.failure(
                ErrorCode.PROTOCOL_ERROR,
                "Direct bridge response request_id does not match",
                details={"operation": operation, "request_id": request_id},
            )
        result = self._result_from_response(response)
        response_session = result.metadata.get("session_id")
        if (
            operation != "session.handshake"
            and self._session_id
            and response_session
            and response_session != self._session_id
        ):
            return CommandResult.failure(
                ErrorCode.PROTOCOL_ERROR,
                "Direct bridge response belongs to a different session",
                details={"expected_session_id": self._session_id, "actual_session_id": response_session},
            )
        response_document = result.metadata.get("document_id")
        if (
            operation not in {"session.handshake", "session.health", "capabilities.list"}
            and self._document_id
            and response_document
            and response_document != self._document_id
        ):
            return CommandResult.failure(
                ErrorCode.DOCUMENT_NOT_RESOLVED,
                "Direct bridge response belongs to a different drawing document",
                details={"expected_document_id": self._document_id, "actual_document_id": response_document},
            )
        return result

    async def _cancel_pending_request(self, request_id: str) -> None:
        """Best-effort cancellation over an independent bridge connection."""
        if not self._session_id:
            return
        try:
            discovery = self._read_discovery()
            token = str(discovery.get("token") or "")
            port = discovery.get("port")
            if not token or not isinstance(port, int):
                return
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=min(self._timeout, 2.0)
            )
            try:
                cancellation = {
                    "protocol_version": PROTOCOL_VERSION,
                    "kind": "request",
                    "request_id": uuid.uuid4().hex,
                    "session_id": self._session_id,
                    "document_id": None,
                    "token": token,
                    "operation": "request.cancel",
                    "params": {"request_id": request_id},
                    "deadline_ms": min(2000, int(self._timeout * 1000)),
                }
                writer.write(json.dumps(cancellation, separators=(",", ":")).encode("utf-8") + b"\n")
                await asyncio.wait_for(writer.drain(), timeout=min(self._timeout, 2.0))
                await asyncio.wait_for(reader.readline(), timeout=min(self._timeout, 2.0))
            finally:
                writer.close()
                await writer.wait_closed()
        except (BridgeConnectionError, OSError, asyncio.TimeoutError):
            log.warning("direct_bridge_cancel_failed", request_id=request_id)

    def _accept_handshake(self, payload: dict[str, Any]) -> CommandResult | None:
        incoming_session_id = str(payload.get("session_id") or "") or None
        if self._session_id and incoming_session_id and incoming_session_id != self._session_id:
            return CommandResult.failure(
                ErrorCode.PROTOCOL_ERROR,
                "Direct bridge session_id changed during a persistent connection",
                details={"expected_session_id": self._session_id, "actual_session_id": incoming_session_id},
            )
        self._session_id = incoming_session_id
        self._document_state = dict(payload.get("document") or {})
        incoming_document_id = str(self._document_state.get("document_id") or "") or None
        if self._document_id and incoming_document_id is None:
            return CommandResult.failure(
                ErrorCode.DOCUMENT_NOT_RESOLVED,
                "Direct bridge lost the active drawing document during handshake",
                details={"expected_document_id": self._document_id},
            )
        if self._document_id and incoming_document_id and incoming_document_id != self._document_id:
            return CommandResult.failure(
                ErrorCode.DOCUMENT_NOT_RESOLVED,
                "Direct bridge document_id changed during a persistent connection",
                details={"expected_document_id": self._document_id, "actual_document_id": incoming_document_id},
            )
        self._document_id = incoming_document_id
        self._bridge_info.update(dict(payload.get("bridge") or {}))
        self._capabilities = {
            str(name): bool(supported)
            for name, supported in dict(payload.get("capabilities") or {}).items()
        }
        self._needs_handshake = False
        return None

    def _check_document_identity(self, payload: dict[str, Any]) -> CommandResult | None:
        actual = str(payload.get("document_id") or "") or None
        if self._document_id and actual is None:
            return CommandResult.failure(
                ErrorCode.DOCUMENT_NOT_RESOLVED,
                "Direct bridge response did not identify the active drawing document",
                details={"expected_document_id": self._document_id},
            )
        if self._document_id and actual and actual != self._document_id:
            return CommandResult.failure(
                ErrorCode.DOCUMENT_NOT_RESOLVED,
                "The active AutoCAD document changed during this MCP session",
                details={"expected_document_id": self._document_id, "actual_document_id": actual},
            )
        return None

    @staticmethod
    def _result_from_response(response: dict[str, Any]) -> CommandResult:
        metadata = {
            key: response.get(key)
            for key in ("protocol_version", "request_id", "session_id", "document_id", "drawing_fingerprint", "capabilities", "warnings")
            if key in response
        }
        if bool(response.get("ok")):
            return CommandResult(ok=True, payload=response.get("payload"), metadata=metadata)
        error = response.get("error")
        if isinstance(error, dict):
            result = CommandResult.failure(
                str(error.get("code") or ErrorCode.UNKNOWN.value),
                str(error.get("message") or "AutoCAD bridge request failed"),
                details=dict(error.get("details") or {}),
                payload=response.get("payload"),
            )
            result.metadata = metadata
            return result
        result = CommandResult.failure(
            ErrorCode.UNKNOWN,
            str(error or "AutoCAD bridge request failed"),
            payload=response.get("payload"),
        )
        result.metadata = metadata
        return result

    async def _read(self, operation: str, params: dict[str, Any] | None = None) -> CommandResult:
        capability = self._check_capability(operation)
        if capability is not None:
            return capability
        return await self._request(operation, params or {}, write=False)

    async def _write(self, operation: str, params: dict[str, Any] | None = None) -> CommandResult:
        if operation not in SAFE_DIRECT_WRITES:
            return CommandResult.failure(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Direct bridge writes are limited to the immutable overlay batch API",
                details={"operation": operation, "allowed": sorted(SAFE_DIRECT_WRITES)},
            )
        capability = self._check_capability(operation)
        if capability is not None:
            return capability
        return await self._request(operation, params or {}, write=True)

    def _check_capability(self, operation: str) -> CommandResult | None:
        if operation.startswith("session.") or operation == "capabilities.list":
            return None
        if not self._session_id:
            return CommandResult.failure(
                ErrorCode.AUTOCAD_NOT_CONNECTED,
                "Direct bridge session has not been initialized",
            )
        if not self._capabilities.get(operation, False):
            return CommandResult.failure(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Direct bridge capability is unavailable",
                details={"operation": operation},
            )
        return None

    # Drawing observation

    async def drawing_info(self, include_entity_count: bool = False) -> CommandResult:
        return await self._read("drawing.info", {"include_entity_count": include_entity_count})

    async def drawing_get_state(self) -> CommandResult:
        result = await self._read("drawing.get_state")
        if result.ok and isinstance(result.payload, dict):
            mismatch = self._check_document_identity(result.payload)
            if mismatch is not None:
                return mismatch
            self._document_state = dict(result.payload)
            self._document_id = str(result.payload.get("document_id") or "") or self._document_id
        return result

    async def drawing_get_fingerprint(self) -> CommandResult:
        return await self._read("drawing.get_fingerprint")

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        return await self._read("drawing.get_variables", {"names": names or []})

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        return await self._write("drawing.save", {"path": path})

    async def drawing_create(self, name: str | None = None) -> CommandResult:
        return await self._write("drawing.create", {"name": name})

    async def drawing_open(self, path: str) -> CommandResult:
        return await self._write("drawing.open", {"path": path})

    # Entity observation

    async def entity_get(self, entity_id: str) -> CommandResult:
        return await self._read("entity.get", {"entity_id": entity_id})

    async def entity_get_geometry(self, entity_id: str) -> CommandResult:
        return await self._read("entity.get_geometry", {"entity_id": entity_id})

    async def entity_search_text(
        self,
        query: str,
        match_mode: str = "contains",
        limit: int = 20,
        case_sensitive: bool = False,
    ) -> CommandResult:
        return await self._read(
            "entity.search_text",
            {
                "query": query,
                "match_mode": match_mode,
                "limit": limit,
                "case_sensitive": case_sensitive,
            },
        )

    async def entity_search_text_batch(self, queries: list[dict[str, Any]]) -> CommandResult:
        return await self._read("entity.search_text_batch", {"queries": queries})

    async def entity_get_geometry_batch(self, entity_ids: list[str]) -> CommandResult:
        return await self._read("entity.get_geometry_batch", {"entity_ids": entity_ids})

    async def entity_query(self, query: dict[str, Any]) -> CommandResult:
        return await self._read("entity.query", query)

    async def entity_query_spatial(self, query: dict[str, Any]) -> CommandResult:
        return await self._read("entity.query_spatial", query)

    async def entity_count_by_layer_type(self, query: dict[str, Any] | None = None) -> CommandResult:
        return await self._read("entity.count_by_layer_type", query or {})

    async def entity_count(self, layer: str | None = None) -> CommandResult:
        return await self._read("entity.count", {"layer": layer})

    async def entity_list(self, layer: str | None = None) -> CommandResult:
        return await self._read("entity.list", {"layer": layer})

    # Minimal write surface retained for existing callers. The bridge applies
    # normal entity writes only when its policy authorizes them.

    async def create_line(self, x1: float, y1: float, x2: float, y2: float, layer: str | None = None) -> CommandResult:
        return await self._write("entity.create_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer})

    async def create_polyline(self, points: list[list[float]], closed: bool = False, layer: str | None = None) -> CommandResult:
        return await self._write("entity.create_polyline", {"points": points, "closed": closed, "layer": layer})

    async def entity_copy(self, entity_id: str, dx: float, dy: float) -> CommandResult:
        return await self._write("entity.copy", {"entity_id": entity_id, "dx": dx, "dy": dy})

    async def layer_create(self, name: str, color: str | int = "white", linetype: str = "CONTINUOUS") -> CommandResult:
        return await self._write("layer.create", {"name": name, "color": color, "linetype": linetype})

    async def layer_list(self) -> CommandResult:
        return await self._read("layer.list")

    # View

    async def get_view_state(self) -> CommandResult:
        return await self._read("view.get_state")

    async def get_screenshot(self, full_window: bool = False) -> CommandResult:
        return await self._read("view.get_screenshot", {"full_window": full_window})

    async def zoom_window(self, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return await self._write("view.zoom_window", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def zoom_extents(self) -> CommandResult:
        return await self._write("view.zoom_extents")

    # Agent batch

    async def batch_preview(self, plan: dict[str, Any]) -> CommandResult:
        # Preview materializes an isolated clone/approval record. It never
        # edits the source, but it is still a stateful write and must not be
        # replayed after an ambiguous transport failure.
        return await self._write("batch.preview", {"plan": plan})

    async def batch_apply(
        self,
        batch_id: str,
        approval_token: str | None,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        return await self._write(
            "batch.apply",
            {
                "batch_id": batch_id,
                "approval_token": approval_token,
                "idempotency_key": idempotency_key,
            },
        )

    async def batch_rollback(self, batch_id: str) -> CommandResult:
        return await self._write("batch.rollback", {"batch_id": batch_id})

    async def batch_status(self, batch_id: str) -> CommandResult:
        return await self._read("batch.status", {"batch_id": batch_id})

    async def batch_get_screenshot(self, batch_id: str) -> CommandResult:
        return await self._read("batch.get_screenshot", {"batch_id": batch_id})


class BridgeConnectionError(RuntimeError):
    """A connection-time error before a structured bridge reply is available."""

    def __init__(self, code: ErrorCode | str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.message = message
        self.details = details or {}
        super().__init__(message)
