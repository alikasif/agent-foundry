"""Built-in tools conform to the frozen tool contract (T024 B2).

The annotated assignments are the real check: ``pyrefly check`` fails when a
built-in tool stops satisfying ``ToolDefinition`` (including ``fn``) or, for
executor-backed tools, ``ExecutorTool``. The tests repeat the runtime side;
a non-tool object is covered by ``test_tool_registry``.
"""

import harness_builder
from harness_builder.context.store import ReferenceStore
from harness_builder.core import protocols
from harness_builder.core.protocols import ExecutorTool, ToolDefinition
from harness_builder.core.types import ChildProfile, Rule
from harness_builder.tools.builtin import run_code as run_code_module
from harness_builder.tools.builtin.deref import DerefTool
from harness_builder.tools.builtin.run_code import RunCodeTool, run_code
from harness_builder.tools.builtin.spawn import Child, SpawnAgentTool
from harness_builder.tools.registry import Tool, ToolRegistry, tool
from harness_builder.trace.diff import diff_traces
from harness_builder.trace.replay import replay


@tool
def echo(text: str) -> str:
    """Echo ``text``."""
    return text


DEREF_TOOL: ToolDefinition = DerefTool(ReferenceStore(None))
SPAWN_TOOL: ToolDefinition = SpawnAgentTool(1, {"default": Child("", 0.5, None)})
RUN_CODE_TOOL: ToolDefinition = run_code()
DECORATED_TOOL: ToolDefinition = echo
RUN_CODE_EXECUTOR_TOOL: ExecutorTool = RunCodeTool()
BUILTIN_TOOLS: tuple[ToolDefinition, ...] = (
    DEREF_TOOL,
    SPAWN_TOOL,
    RUN_CODE_TOOL,
    DECORATED_TOOL,
)


def test_builtin_tools_have_a_callable_fn_and_register() -> None:
    registry = ToolRegistry(BUILTIN_TOOLS)
    assert registry.names() == ["deref", "spawn_agent", "run_code", "echo"]
    assert all(callable(item.fn) for item in BUILTIN_TOOLS)


def test_executor_tool_is_the_protocols_one_and_run_code_is_one() -> None:
    assert run_code_module.ExecutorTool is protocols.ExecutorTool
    assert isinstance(RUN_CODE_EXECUTOR_TOOL, ExecutorTool)
    assert not isinstance(DEREF_TOOL, ExecutorTool)
    assert not isinstance(SPAWN_TOOL, ExecutorTool)
    assert not isinstance(DECORATED_TOOL, ExecutorTool)


def test_decorated_tool_is_a_tool() -> None:
    assert isinstance(DECORATED_TOOL, Tool)


def test_root_package_exports_the_designs_api_names() -> None:
    """T024 m6: designs.md 2.1/11.1 names resolve lazily to their owners."""
    assert harness_builder.tool is tool
    assert harness_builder.Tool is Tool
    assert harness_builder.Rule is Rule
    assert harness_builder.ChildProfile is ChildProfile
    assert harness_builder.replay is replay
    assert harness_builder.diff_traces is diff_traces
