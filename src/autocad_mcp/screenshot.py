"""Screenshot providers that do not depend on a foreground AutoCAD window.

The live AutoCAD backend receives document previews from the .NET bridge. The
headless DXF backend renders with matplotlib. The former foreground-window
provider is intentionally retired because it could capture the wrong desktop
state and made the MCP depend on UI focus.
"""

from __future__ import annotations

import base64
import io
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import ezdxf

log = structlog.get_logger()


class ScreenshotProvider(ABC):
    """Abstract screenshot provider."""

    @abstractmethod
    def capture(self) -> str | None:
        """Return base64-encoded PNG, or None if capture fails."""


class NullScreenshotProvider(ScreenshotProvider):
    """No-op provider - always returns None."""

    def capture(self) -> str | None:
        return None


class MatplotlibScreenshotProvider(ScreenshotProvider):
    """Render an ezdxf document to PNG via matplotlib."""

    def __init__(self, doc: ezdxf.document.Drawing | None = None):
        self._doc = doc

    @property
    def doc(self) -> ezdxf.document.Drawing | None:
        return self._doc

    @doc.setter
    def doc(self, value: ezdxf.document.Drawing):
        self._doc = value

    def capture(self) -> str | None:
        if self._doc is None:
            return None
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from ezdxf.addons.drawing import Frontend, RenderContext
            from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

            fig, ax = plt.subplots(figsize=(16, 10), dpi=150)
            ax.set_aspect("equal")
            ctx = RenderContext(self._doc)
            out = MatplotlibBackend(ax)
            Frontend(ctx, out).draw_layout(self._doc.modelspace())

            buffer = io.BytesIO()
            fig.savefig(buffer, format="png", bbox_inches="tight", pad_inches=0.1)
            plt.close(fig)
            buffer.seek(0)
            return base64.b64encode(buffer.read()).decode("ascii")
        except Exception as exc:
            log.warning("matplotlib_screenshot_failed", error=str(exc))
            return None


class Win32ScreenshotProvider(NullScreenshotProvider):
    """Compatibility name for the retired desktop-window screenshot provider.

    It deliberately captures nothing. Live AutoCAD screenshots must come from
    the direct bridge's document-preview capability.
    """
