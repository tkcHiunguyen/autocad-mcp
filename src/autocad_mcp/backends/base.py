"""Abstract base class for AutoCAD backends + CommandResult envelope."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from autocad_mcp.errors import ErrorCode, ErrorInfo


@dataclass
class CommandResult:
    """Structured result envelope from backend operations.

    ``error`` remains a Python string for backend ergonomics. MCP responses
    serialize it as a stable object, so callers never have to infer state from
    an error sentence.
    """

    ok: bool
    payload: Any = None
    error: str | None = None
    error_code: ErrorCode | str | None = None
    error_details: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(
        cls,
        code: ErrorCode | str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        payload: Any = None,
    ) -> "CommandResult":
        return cls(
            ok=False,
            payload=payload,
            error=message,
            error_code=code,
            error_details=details or {},
            metadata={},
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"ok": self.ok}
        if self.metadata:
            d["metadata"] = self.metadata
        if self.ok:
            d["payload"] = self.payload
        else:
            code = self.error_code
            if code is None:
                message = (self.error or "").casefold()
                code = (
                    ErrorCode.UNSUPPORTED_CAPABILITY
                    if "not supported" in message or "unsupported" in message
                    else ErrorCode.UNKNOWN
                )
            info = ErrorInfo(
                code=code.value if isinstance(code, Enum) else str(code),
                message=self.error or "Unknown backend error",
                details=self.error_details,
            ).to_dict()
            # Keep the legacy string field while exposing a stable structured
            # companion for agent/runtime consumers.
            d["error"] = info["message"]
            d["error_info"] = info
        return d


@dataclass
class BackendCapabilities:
    """Declares what a backend supports."""

    can_read_drawing: bool = False
    can_modify_entities: bool = False
    can_create_entities: bool = False
    can_screenshot: bool = False
    can_save: bool = False
    can_plot_pdf: bool = False
    can_zoom: bool = False
    can_query_entities: bool = False
    can_file_operations: bool = False
    can_undo: bool = False
    direct_transport: bool = False
    can_get_drawing_state: bool = False
    can_get_geometry: bool = False
    can_query_spatial: bool = False
    can_batch: bool = False
    can_transactions: bool = False
    source_immutable_by_default: bool = True

    def to_dict(self) -> dict[str, bool]:
        return dict(self.__dict__)


class AutoCADBackend(ABC):
    """Abstract interface for AutoCAD operation backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier, currently ``direct_bridge`` or ``ezdxf``."""

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """Declare supported operations."""

    @abstractmethod
    async def initialize(self) -> CommandResult:
        """Initialize the backend. Called once at startup."""

    @abstractmethod
    async def status(self) -> CommandResult:
        """Return backend health/status info."""

    async def session_health(self) -> CommandResult:
        """Return direct transport health without changing the drawing."""
        return await self.status()

    async def session_handshake(self) -> CommandResult:
        """Negotiate a persistent session/document context."""
        return await self.session_health()

    async def capabilities_list(self) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={"backend": self.name, "capabilities": self.capabilities.to_dict()},
        )

    # --- Drawing management ---

    async def drawing_info(self, include_entity_count: bool = False) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def drawing_get_state(self) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "drawing.get_state is not supported on this backend",
        )

    async def drawing_get_fingerprint(self) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "drawing.get_fingerprint is not supported on this backend",
        )

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def drawing_save_as_dxf(self, path: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def drawing_create(self, name: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def drawing_purge(self) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def drawing_plot_pdf(self, path: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def drawing_open(self, path: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    # --- Undo / Redo ---

    async def undo(self) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def redo(self) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    # --- Freehand LISP execution ---

    async def execute_lisp(self, code: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    # --- Entity operations ---

    async def create_line(self, x1: float, y1: float, x2: float, y2: float, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_circle(self, cx: float, cy: float, radius: float, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_polyline(self, points: list[list[float]], closed: bool = False, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_rectangle(self, x1: float, y1: float, x2: float, y2: float, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_arc(self, cx: float, cy: float, radius: float, start_angle: float, end_angle: float, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_ellipse(self, cx: float, cy: float, major_x: float, major_y: float, ratio: float, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_mtext(self, x: float, y: float, width: float, text: str, height: float = 2.5, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_hatch(self, entity_id: str, pattern: str = "ANSI31") -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_list(self, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_count(self, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_get(self, entity_id: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_get_geometry(self, entity_id: str) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.GEOMETRY_UNAVAILABLE,
            "Geometry is not available on this backend",
            details={"entity_id": entity_id},
        )

    async def entity_query(self, query: dict[str, Any]) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "entity.query is not supported on this backend",
        )

    async def entity_query_spatial(self, query: dict[str, Any]) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "entity.query_spatial is not supported on this backend",
        )

    async def entity_count_by_layer_type(self, query: dict[str, Any] | None = None) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "entity.count_by_layer_type is not supported on this backend",
        )

    async def entity_search_text(
        self,
        query: str,
        match_mode: str = "contains",
        limit: int = 20,
        case_sensitive: bool = False,
    ) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_search_text_batch(
        self,
        queries: list[dict[str, Any]],
    ) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "entity.search_text_batch is not supported on this backend",
        )

    async def entity_get_geometry_batch(self, entity_ids: list[str]) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.GEOMETRY_UNAVAILABLE,
            "entity.get_geometry_batch is not supported on this backend",
            details={"entity_ids": entity_ids},
        )

    async def entity_erase(self, entity_id: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_copy(self, entity_id: str, dx: float, dy: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_move(self, entity_id: str, dx: float, dy: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_rotate(self, entity_id: str, cx: float, cy: float, angle: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_scale(self, entity_id: str, cx: float, cy: float, factor: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_mirror(self, entity_id: str, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_offset(self, entity_id: str, distance: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_array(self, entity_id: str, rows: int, cols: int, row_dist: float, col_dist: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_fillet(self, entity_id1: str, entity_id2: str, radius: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def entity_chamfer(self, entity_id1: str, entity_id2: str, dist1: float, dist2: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    # --- Layer operations ---

    async def layer_list(self) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def layer_create(self, name: str, color: str | int = "white", linetype: str = "CONTINUOUS") -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def layer_set_current(self, name: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def layer_set_properties(self, name: str, color: str | int | None = None, linetype: str | None = None, lineweight: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def layer_freeze(self, name: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def layer_thaw(self, name: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def layer_lock(self, name: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def layer_unlock(self, name: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    # --- Block operations ---

    async def block_list(self) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def block_insert(self, name: str, x: float, y: float, scale: float = 1.0, rotation: float = 0.0, block_id: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def block_insert_with_attributes(self, name: str, x: float, y: float, scale: float = 1.0, rotation: float = 0.0, attributes: dict[str, str] | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def block_get_attributes(self, entity_id: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def block_update_attribute(self, entity_id: str, tag: str, value: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def block_define(self, name: str, entities: list[dict]) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    # --- Annotation ---

    async def create_text(self, x: float, y: float, text: str, height: float = 2.5, rotation: float = 0.0, layer: str | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_dimension_linear(self, x1: float, y1: float, x2: float, y2: float, dim_x: float, dim_y: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_dimension_aligned(self, x1: float, y1: float, x2: float, y2: float, offset: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_dimension_angular(self, cx: float, cy: float, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_dimension_radius(self, cx: float, cy: float, radius: float, angle: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def create_leader(self, points: list[list[float]], text: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    # --- P&ID ---

    async def pid_setup_layers(self) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_insert_symbol(self, category: str, symbol: str, x: float, y: float, scale: float = 1.0, rotation: float = 0.0) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_list_symbols(self, category: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_draw_process_line(self, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_connect_equipment(self, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_add_flow_arrow(self, x: float, y: float, rotation: float = 0.0) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_add_equipment_tag(self, x: float, y: float, tag: str, description: str = "") -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_add_line_number(self, x: float, y: float, line_num: str, spec: str) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_insert_valve(self, x: float, y: float, valve_type: str, rotation: float = 0.0, attributes: dict[str, str] | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_insert_instrument(self, x: float, y: float, instrument_type: str, rotation: float = 0.0, tag_id: str = "", range_value: str = "") -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_insert_pump(self, x: float, y: float, pump_type: str, rotation: float = 0.0, attributes: dict[str, str] | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def pid_insert_tank(self, x: float, y: float, tank_type: str, scale: float = 1.0, attributes: dict[str, str] | None = None) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    # --- View ---

    async def zoom_extents(self) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def zoom_window(self, x1: float, y1: float, x2: float, y2: float) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def get_view_state(self) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def zoom_pixels(
        self,
        left: float,
        top: float,
        right: float,
        bottom: float,
        padding: float = 0.0,
    ) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def focus_entities(self, handles: list[str], padding: float = 0.5) -> CommandResult:
        return CommandResult(ok=False, error="Not supported on this backend")

    async def get_screenshot(self, full_window: bool = False) -> CommandResult:
        """Return PNG data and capture metadata in payload."""
        return CommandResult(ok=False, error="Not supported on this backend")

    # --- Agent batch/transaction operations ---

    async def batch_preview(self, plan: dict[str, Any]) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "batch.preview is not supported on this backend",
        )

    async def batch_apply(
        self,
        batch_id: str,
        approval_token: str | None,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "batch.apply is not supported on this backend",
        )

    async def batch_rollback(self, batch_id: str) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "batch.rollback is not supported on this backend",
        )

    async def batch_status(self, batch_id: str) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "batch.status is not supported on this backend",
        )

    async def batch_get_screenshot(self, batch_id: str) -> CommandResult:
        return CommandResult.failure(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "batch.get_screenshot is not supported on this backend",
        )
