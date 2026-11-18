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

_MODULES = (
    "harness_builder.errors",
    "harness_builder.core.types",
    "harness_builder.llm.types",
    "harness_builder.executor.models",
    "harness_builder.trace.models",
    "harness_builder.config",
    "harness_builder.core.builder",
    "harness_builder.tools.registry",
    "harness_builder.trace.diff",
    "harness_builder.trace.replay",
)


def __getattr__(name: str) -> Any:
    """Import the module that defines public ``name`` on first access."""
    if name in __all__:
        for module_name in _MODULES:
            module = import_module(module_name)
            if hasattr(module, name):
                return getattr(module, name)
    raise AttributeError(f"module 'harness_builder' has no attribute {name!r}")
