"""Every tool the agent gets. A new tool is a folder here and one entry below."""

from google.adk.tools.base_tool import BaseTool

from harness.sandbox.container import Container
from harness.sandbox.interpreter import Interpreter
from harness.tools.bash.tool import make_bash
from harness.tools.described import DescribedTool
from harness.tools.plan.tool import PlanState, make_plan
from harness.tools.python.tool import make_python


def build_tools(
    container: Container,
    interpreter: Interpreter,
    plan_state: PlanState,
    descriptions: dict[str, str],
) -> list[BaseTool]:
    functions = {
        "plan": make_plan(plan_state),
        "bash": make_bash(container),
        "python": make_python(interpreter),
    }
    return [
        DescribedTool(function, descriptions[name])
        for name, function in functions.items()
    ]
