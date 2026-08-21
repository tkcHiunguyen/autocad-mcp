"""Stable error codes shared by the transport and agent layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    AUTOCAD_NOT_CONNECTED = "AUTOCAD_NOT_CONNECTED"
    DOCUMENT_NOT_RESOLVED = "DOCUMENT_NOT_RESOLVED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    GEOMETRY_UNAVAILABLE = "GEOMETRY_UNAVAILABLE"
    GEOMETRY_INVALID = "GEOMETRY_INVALID"
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
    BRIDGE_AUTH_FAILED = "BRIDGE_AUTH_FAILED"
    BRIDGE_TIMEOUT = "BRIDGE_TIMEOUT"
    TRANSACTION_FAILED = "TRANSACTION_FAILED"
    BATCH_APPLY_FAILED = "BATCH_APPLY_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_AGENT_STATE = "INVALID_AGENT_STATE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    SOURCE_IMMUTABLE = "SOURCE_IMMUTABLE"
    SOURCE_UNSAVED = "SOURCE_UNSAVED"
    SOURCE_MUTATION_BLOCKED = "SOURCE_MUTATION_BLOCKED"
    SOURCE_FINGERPRINT_CHANGED = "SOURCE_FINGERPRINT_CHANGED"
    CALL_BUDGET_EXCEEDED = "CALL_BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ErrorInfo:
    """Machine-readable error envelope with optional diagnostic details."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class BackendError(RuntimeError):
    """Exception used when backend initialization or transport fails."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code.value if isinstance(code, Enum) else str(code)
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def to_error(self) -> ErrorInfo:
        return ErrorInfo(self.code, self.message, self.details)
