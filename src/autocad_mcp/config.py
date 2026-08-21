"""Backend detection and environment configuration."""

from __future__ import annotations

import os
from pathlib import Path

import structlog

log = structlog.get_logger()

# Backend selection. ``auto`` deliberately selects the direct bridge rather
# than silently creating a headless document when the live bridge is absent.
BACKEND_DEFAULT = "direct_bridge"  # auto | direct_bridge | ezdxf | file_ipc(alias)

# Screenshot
ONLY_TEXT_FEEDBACK = os.environ.get("AUTOCAD_MCP_ONLY_TEXT", "").lower() in ("1", "true", "yes")

def _current_backend_env() -> str:
    """Read backend selection from env with normalization."""
    return os.environ.get("AUTOCAD_MCP_BACKEND", BACKEND_DEFAULT).strip().lower()


def _write_debug_snapshot(backend_env: str):
    """Optionally write backend detection debug information.

    Set AUTOCAD_MCP_DEBUG_DETECT_FILE to enable.
    """
    debug_file = os.environ.get("AUTOCAD_MCP_DEBUG_DETECT_FILE", "").strip()
    if not debug_file:
        return

    try:
        debug_path = Path(debug_file)
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with debug_path.open("w", encoding="utf-8") as f:
            f.write(f"BACKEND_ENV={backend_env}\n")
    except Exception:
        # Best-effort only; never fail backend detection due debug writes.
        pass


def detect_backend() -> str:
    """Return the selected safe backend without probing or focusing AutoCAD."""
    backend_env = _current_backend_env()
    _write_debug_snapshot(backend_env)

    if backend_env == "ezdxf":
        return "ezdxf"

    if backend_env in {"auto", "direct_bridge", "file_ipc"}:
        if backend_env == "file_ipc":
            log.warning("legacy_file_ipc_mapped_to_direct_bridge")
        return "direct_bridge"

    raise RuntimeError(
        "Unknown AUTOCAD_MCP_BACKEND value. Use direct_bridge, ezdxf, or auto."
    )
