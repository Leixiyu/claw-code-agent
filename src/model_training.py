from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .mcp_runtime import MCPRuntime


class ModelTrainingError(RuntimeError):
    """Raised when a model-training operation cannot be completed."""


def submit_model_training(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
    mcp_runtime: MCPRuntime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Submit model training using a processed dataset reference."""
    raise ModelTrainingError('submit_model_training is registered but not implemented')


def get_model_training_status(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
    mcp_runtime: MCPRuntime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the authoritative status of a model-training task."""
    raise ModelTrainingError('get_model_training_status is registered but not implemented')


def get_model_training_result(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
    mcp_runtime: MCPRuntime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the logical model name and public training metadata."""
    raise ModelTrainingError('get_model_training_result is registered but not implemented')
