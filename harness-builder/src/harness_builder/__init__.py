"""harness-builder: layered agent-harness framework.

Public names are re-exported lazily (PEP 562) so ``import harness_builder`` stays
cheap and never imports ADK, openai-agents or the sibling packages.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from harness_builder.config import Settings
    from harness_builder.core.builder import Harness, HarnessBuilder
    from harness_builder.core.types import (
        ApprovalRequest,
        BudgetLimits,
        BudgetSnapshot,
        ChildProfile,
        ErrorObservation,
        HookOutcome,
        Message,
        ModelConfig,
        Observation,
        PermissionDecision,
        Rule,
        RunResult,
        StepDecision,
        StopInfo,
        ToolCall,
        ToolOutcome,
        ToolResult,
        Usage,
        ValidatedCall,
    )
    from harness_builder.errors import (
        ConfigError,
        DockerUnavailableError,
        HarnessError,
        ReplayDivergence,
        SpecError,
    )
    from harness_builder.executor.models import ExecRequest, ExecResult, SandboxConfig
    from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema
    from harness_builder.tools.registry import Tool, tool
    from harness_builder.trace.diff import diff_traces
    from harness_builder.trace.models import Trace, TraceStep
    from harness_builder.trace.replay import replay

__all__ = [
    "ApprovalRequest",
    "BudgetLimits",
    "BudgetSnapshot",
    "ChildProfile",
    "ConfigError",
    "DockerUnavailableError",
    "ErrorObservation",
    "ExecRequest",
    "ExecResult",
    "Harness",
    "HarnessBuilder",
    "HarnessError",
    "HookOutcome",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "ModelConfig",
    "Observation",
    "PermissionDecision",
    "ReplayDivergence",
    "Rule",
    "RunResult",
    "SandboxConfig",
    "Settings",
    "SpecError",
    "StepDecision",
    "StopInfo",
    "Tool",
    "ToolCall",
    "ToolOutcome",
    "ToolResult",
    "ToolSchema",
    "Trace",
    "TraceStep",
    "Usage",
    "ValidatedCall",
    "diff_traces",
    "replay",
    "tool",
]

_TYPES = "harness_builder.core.types"
_ERRORS = "harness_builder.errors"
_MODULES: dict[str, str] = {
    "ApprovalRequest": _TYPES,
    "BudgetLimits": _TYPES,
    "BudgetSnapshot": _TYPES,
    "ChildProfile": _TYPES,
    "ConfigError": _ERRORS,
    "DockerUnavailableError": _ERRORS,
    "ErrorObservation": _TYPES,
    "ExecRequest": "harness_builder.executor.models",
    "ExecResult": "harness_builder.executor.models",
    "Harness": "harness_builder.core.builder",
    "HarnessBuilder": "harness_builder.core.builder",
    "HarnessError": _ERRORS,
    "HookOutcome": _TYPES,
    "LLMRequest": "harness_builder.llm.types",
    "LLMResponse": "harness_builder.llm.types",
    "Message": _TYPES,
    "ModelConfig": _TYPES,
    "Observation": _TYPES,
    "PermissionDecision": _TYPES,
    "ReplayDivergence": _ERRORS,
    "Rule": _TYPES,
    "RunResult": _TYPES,
    "SandboxConfig": "harness_builder.executor.models",
    "Settings": "harness_builder.config",
    "SpecError": _ERRORS,
    "StepDecision": _TYPES,
    "StopInfo": _TYPES,
    "Tool": "harness_builder.tools.registry",
    "ToolCall": _TYPES,
    "ToolOutcome": _TYPES,
    "ToolResult": _TYPES,
    "ToolSchema": "harness_builder.llm.types",
    "Trace": "harness_builder.trace.models",
    "TraceStep": "harness_builder.trace.models",
    "Usage": _TYPES,
    "ValidatedCall": _TYPES,
    "diff_traces": "harness_builder.trace.diff",
    "replay": "harness_builder.trace.replay",
    "tool": "harness_builder.tools.registry",
}
"""Public name -> defining module; only that module is imported on access."""


def __getattr__(name: str) -> Any:
    """Import the module that defines public ``name`` on first access."""
    module_name = _MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'harness_builder' has no attribute {name!r}")
    return getattr(import_module(module_name), name)
