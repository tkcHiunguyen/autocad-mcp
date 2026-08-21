"""Contract tests for the foreground-independent direct bridge transport."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from autocad_mcp.backends.base import BackendCapabilities, CommandResult
from autocad_mcp.backends.direct_bridge import DirectBridgeBackend
from autocad_mcp.errors import ErrorCode


class FakeBridge:
    def __init__(
        self,
        *,
        close_first_read: bool = False,
        close_first_operation: str | None = None,
        delay_operation: str | None = None,
    ) -> None:
        self.close_first_read = close_first_read
        self.close_first_operation = close_first_operation
        self.delay_operation = delay_operation
        self.requests: list[dict] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.token = "test-token"
        self.session_id = "stable-session"
        self.capabilities = {
            "drawing.info": True,
            "drawing.get_state": True,
            "drawing.get_fingerprint": True,
            "entity.get_geometry": True,
            "entity.query_spatial": True,
            "batch.preview": True,
            "batch.apply": True,
            "view.get_screenshot": True,
        }

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while raw := await reader.readline():
                request = json.loads(raw)
                self.requests.append(request)
                if request["operation"] == "drawing.get_state" and self.close_first_read:
                    self.close_first_read = False
                    writer.close()
                    await writer.wait_closed()
                    return
                if request["operation"] == self.close_first_operation:
                    self.close_first_operation = None
                    writer.close()
                    await writer.wait_closed()
                    return
                if request["operation"] == self.delay_operation:
                    await asyncio.sleep(0.2)
                response = self._response(request)
                writer.write(json.dumps(response).encode("utf-8") + b"\n")
                await writer.drain()
        finally:
            writer.close()

    def _response(self, request: dict) -> dict:
        if request.get("token") != self.token:
            return {
                "request_id": request.get("request_id"),
                "ok": False,
                "error": {"code": "PROTOCOL_ERROR", "message": "token invalid", "details": {}},
            }
        if request["operation"] == "session.handshake":
            payload = {
                "session_id": self.session_id,
                "document": {
                    "document_id": "document-1",
                    "absolute_path": "C:/factory/source.dwg",
                    "drawing_name": "source.dwg",
                    "active_space": "Model",
                    "units": 4,
                    "dbmod": 0,
                    "fingerprint": "database-fingerprint",
                    "current_layer": "0",
                    "viewport": {},
                },
                "bridge": {"protocol_version": 1, "transport": "loopback_tcp"},
                "capabilities": self.capabilities,
            }
        elif request["operation"] == "session.health":
            payload = {"connected": True, "document": {"document_id": "document-1"}}
        elif request["operation"] == "capabilities.list":
            payload = {"capabilities": self.capabilities}
        elif request["operation"] == "drawing.get_state":
            payload = {"document_id": "document-1", "dbmod": 0, "fingerprint": "database-fingerprint"}
        elif request["operation"] == "request.cancel":
            payload = {"request_id": request["params"]["request_id"], "cancelled": True}
        else:
            payload = {"operation": request["operation"]}
        return {"request_id": request["request_id"], "ok": True, "payload": payload}


async def _backend(fake: FakeBridge, tmp_path: Path) -> DirectBridgeBackend:
    discovery = tmp_path / "bridge.json"
    discovery.write_text(
        json.dumps({"host": "127.0.0.1", "port": fake.port, "token": fake.token}),
        encoding="utf-8",
    )
    return DirectBridgeBackend(discovery_path=discovery, timeout=0.2)


class TestCommandResult:
    def test_failure_has_legacy_message_and_structured_error(self) -> None:
        result = CommandResult.failure(
            ErrorCode.GEOMETRY_UNAVAILABLE,
            "No verified vertices",
            details={"handle": "A1"},
        )
        payload = result.to_dict()
        assert payload["ok"] is False
        assert payload["error"] == "No verified vertices"
        assert payload["error_info"] == {
            "code": "GEOMETRY_UNAVAILABLE",
            "message": "No verified vertices",
            "details": {"handle": "A1"},
        }

    def test_capabilities_default_to_safe_non_direct_mode(self) -> None:
        capabilities = BackendCapabilities()
        assert capabilities.direct_transport is False
        assert capabilities.source_immutable_by_default is True

    def test_legacy_unsupported_result_is_normalized_to_structured_error(self) -> None:
        result = CommandResult(ok=False, error="Not supported on this backend")

        assert result.to_dict()["error_info"]["code"] == ErrorCode.UNSUPPORTED_CAPABILITY.value


class TestDirectBridgeBackend:
    @pytest.mark.asyncio
    async def test_handshake_and_health_do_not_need_a_foreground_window(self, tmp_path: Path) -> None:
        fake = FakeBridge()
        await fake.start()
        try:
            backend = await _backend(fake, tmp_path)
            initialized = await backend.initialize()
            health = await backend.session_health()

            assert initialized.ok is True
            assert initialized.payload["backend"] == "direct_bridge"
            assert initialized.payload["document"]["document_id"] == "document-1"
            assert health.ok is True
            assert [request["operation"] for request in fake.requests] == ["session.handshake", "session.health"]
            assert all(request["token"] == fake.token for request in fake.requests)
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_read_reconnects_once_without_replaying_a_write(self, tmp_path: Path) -> None:
        fake = FakeBridge(close_first_read=True)
        await fake.start()
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok
            state = await backend.drawing_get_state()

            assert state.ok is True
            calls = [request["operation"] for request in fake.requests]
            assert calls.count("drawing.get_state") == 2
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_missing_discovery_returns_structured_capability_error(self, tmp_path: Path) -> None:
        backend = DirectBridgeBackend(discovery_path=tmp_path / "missing.json", timeout=0.01)
        result = await backend.initialize()
        assert result.ok is False
        assert result.error_code == ErrorCode.AUTOCAD_NOT_CONNECTED.value
        assert "discovery" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_cancellation_sends_a_best_effort_bridge_cancel(self, tmp_path: Path) -> None:
        fake = FakeBridge(delay_operation="drawing.get_state")
        await fake.start()
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok
            task = asyncio.create_task(backend.drawing_get_state())
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.05)
            assert "request.cancel" in [request["operation"] for request in fake.requests]
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_capability_contract_fails_closed(self, tmp_path: Path) -> None:
        fake = FakeBridge()
        await fake.start()
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok
            result = await backend.drawing_save("C:/not-used.dwg")
            assert result.error_code == ErrorCode.UNSUPPORTED_CAPABILITY.value
            assert "drawing.save" not in [request["operation"] for request in fake.requests]
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_drawing_info_counts_entities_only_when_explicitly_requested(self, tmp_path: Path) -> None:
        fake = FakeBridge()
        await fake.start()
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok

            assert (await backend.drawing_info()).ok
            assert fake.requests[-1]["operation"] == "drawing.info"
            assert fake.requests[-1]["params"] == {"include_entity_count": False}

            assert (await backend.drawing_info(include_entity_count=True)).ok
            assert fake.requests[-1]["params"] == {"include_entity_count": True}
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_mutation_is_blocked_when_output_clone_evidence_is_unavailable(self, tmp_path: Path) -> None:
        fake = FakeBridge()
        fake.capabilities.update(
            {
                "batch.preview": False,
                "batch.apply": False,
                "batch.get_screenshot": False,
            }
        )
        await fake.start()
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok
            capabilities = await backend.capabilities_list()
            assert capabilities.ok is True
            assert capabilities.payload["capabilities"]["batch.preview"] is False
            assert capabilities.payload["backend_capabilities"]["can_batch"] is False
            assert capabilities.payload["backend_capabilities"]["can_transactions"] is False

            result = await backend.batch_preview({"plan": {}})

            assert result.error_code == ErrorCode.UNSUPPORTED_CAPABILITY.value
            assert [request["operation"] for request in fake.requests].count("batch.preview") == 0
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_direct_bridge_write_policy_rejects_legacy_entity_mutation(self, tmp_path: Path) -> None:
        fake = FakeBridge()
        await fake.start()
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok
            result = await backend.create_line(0, 0, 1, 1)
            assert result.error_code == ErrorCode.UNSUPPORTED_CAPABILITY.value
            assert "entity.create_line" not in [request["operation"] for request in fake.requests]
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_write_is_not_replayed_after_connection_drop(self, tmp_path: Path) -> None:
        fake = FakeBridge(close_first_operation="batch.preview")
        await fake.start()
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok
            result = await backend.batch_preview({"plan": {}})
            assert result.ok is False
            assert result.error_code == ErrorCode.AUTOCAD_NOT_CONNECTED.value
            assert [request["operation"] for request in fake.requests].count("batch.preview") == 1
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_response_session_mismatch_is_rejected(self, tmp_path: Path) -> None:
        fake = FakeBridge()
        await fake.start()
        original_response = fake._response

        def mismatched_response(request):
            response = original_response(request)
            if request["operation"] == "drawing.get_state":
                response["session_id"] = "other-session"
            return response

        fake._response = mismatched_response
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok
            result = await backend.drawing_get_state()
            assert result.error_code == ErrorCode.PROTOCOL_ERROR.value
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_health_rejects_document_switch(self, tmp_path: Path) -> None:
        fake = FakeBridge()
        await fake.start()
        original_response = fake._response

        def switched_response(request):
            response = original_response(request)
            if request["operation"] == "session.health":
                response["payload"]["document"]["document_id"] = "document-2"
            return response

        fake._response = switched_response
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok
            result = await backend.session_health()
            assert result.error_code == ErrorCode.DOCUMENT_NOT_RESOLVED.value
        finally:
            await fake.close()

    @pytest.mark.asyncio
    async def test_handshake_rejects_document_disappearance_after_reconnect(self, tmp_path: Path) -> None:
        fake = FakeBridge(close_first_read=True)
        await fake.start()
        original_response = fake._response

        def disappearing_response(request):
            response = original_response(request)
            if request["operation"] == "session.handshake" and len(fake.requests) > 1:
                response["payload"]["document"] = {"document_id": None}
            return response

        fake._response = disappearing_response
        try:
            backend = await _backend(fake, tmp_path)
            assert (await backend.initialize()).ok
            result = await backend.drawing_get_state()
            assert result.error_code == ErrorCode.DOCUMENT_NOT_RESOLVED.value
        finally:
            await fake.close()


def test_runtime_path_has_no_ui_automation() -> None:
    repository_root = Path(__file__).parents[1]
    source_root = repository_root / "src" / "autocad_mcp"
    bridge_root = repository_root / "bridge" / "AutoCADMcpBridge"
    forbidden = (
        "SetForegroundWindow",
        "ShowWindow",
        "SendInput",
        "SendKeys",
        "pyautogui",
        "UIAutomationClient",
        "AttachThreadInput",
        "System.Windows.Automation",
        "Marshal.GetActiveObject",
        "Autodesk.AutoCAD.Interop",
    )
    files = [
        source_root / "config.py",
        source_root / "client.py",
        source_root / "backends" / "file_ipc.py",
        source_root / "backends" / "direct_bridge.py",
    ]
    files.extend(bridge_root.glob("*.cs"))
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert not any(token in corpus for token in forbidden)


def test_bridge_does_not_advertise_unverifiable_mutation() -> None:
    source = (Path(__file__).parents[1] / "bridge" / "AutoCADMcpBridge" / "BridgeHost.cs").read_text(
        encoding="utf-8"
    )

    assert 'const bool outputCloneScreenshot = false;' in source
    assert '["batch.preview"] = verifiedOverlayMutation' in source
    assert '["batch.apply"] = verifiedOverlayMutation' in source
    assert '["batch.get_screenshot"] = outputCloneScreenshot' in source


def test_bridge_drawing_info_does_not_enumerate_entities_by_default() -> None:
    source = (Path(__file__).parents[1] / "bridge" / "AutoCADMcpBridge" / "AutoCADQueries.cs").read_text(
        encoding="utf-8"
    )

    assert 'OptionalBool(parameters, "include_entity_count", false)' in source
    assert '["entity_count"] = includeEntityCount ? modelSpace.Cast<ObjectId>().Count() : null' in source


def test_bridge_fingerprint_includes_saved_database_version() -> None:
    source = (Path(__file__).parents[1] / "bridge" / "AutoCADMcpBridge" / "AutoCADQueries.cs").read_text(
        encoding="utf-8"
    )
    batch_source = (Path(__file__).parents[1] / "bridge" / "AutoCADMcpBridge" / "BatchStore.cs").read_text(
        encoding="utf-8"
    )

    assert 'var databaseVersion = GetDatabaseVersionGuid(database);' in source
    assert 'ComputeDrawingFingerprint(document.Name, databaseFingerprint, databaseVersion, dbmod)' in source
    assert 'typeof(Database).GetProperty("VersionGuid")' in source
    assert 'afterDocumentId = ValueAsString(afterState, "document_id")' in batch_source
