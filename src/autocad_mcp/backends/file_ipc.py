"""Compatibility adapter for the retired file IPC transport.

The old implementation injected keystrokes into AutoCAD's command line. That
transport cannot be safe or deterministic when AutoCAD is not foreground, so
it has been replaced by the in-process direct bridge. This module intentionally
keeps the import path used by older integrations while providing no UI fallback.
"""

from __future__ import annotations

import re

from autocad_mcp.backends.direct_bridge import DirectBridgeBackend


def _normalize_autocad_unicode_escapes(text: str) -> str:
    r"""Convert AutoCAD's ``\U+XXXX`` notation into valid JSON escapes.

    Kept for callers that parsed old dispatcher result files. It is a pure
    formatter and never triggers AutoCAD.
    """
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", r"\\u\1", text)


class FileIPCBackend(DirectBridgeBackend):
    """Deprecated name for the direct bridge backend.

    No filesystem polling, command-line input, window enumeration, or UI
    automation remains in this adapter. Existing ``file_ipc`` configuration
    therefore fails safely until the direct bridge is loaded, rather than
    attempting to control AutoCAD's UI.
    """

    @property
    def name(self) -> str:
        return "direct_bridge"
