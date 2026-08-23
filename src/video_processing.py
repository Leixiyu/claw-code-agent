from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .mcp_runtime import MCPRuntime


class VideoProcessingError(RuntimeError):
    """Raised when a video-processing operation cannot be completed."""


def submit_video_processing(
    arguments: dict[str, Any],
    *,
    workspace_root: Path,
    timeout_seconds: float,
    mcp_runtime: MCPRuntime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Submit raw videos for labeling and dataset preparation."""
    raise VideoProcessingError('submit_video_processing is registered but not implemented')


def get_video_processing_status(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
    mcp_runtime: MCPRuntime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the authoritative status of a video-processing task."""
    raise VideoProcessingError('get_video_processing_status is registered but not implemented')


def get_video_processing_result(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
    mcp_runtime: MCPRuntime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the public dataset manifest for a completed processing task."""
    raise VideoProcessingError('get_video_processing_result is registered but not implemented')
