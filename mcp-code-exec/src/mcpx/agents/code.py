"""The code arm: one ``run_python`` tool over a generated workspace of wrappers."""

from google.adk.agents import LlmAgent

from mcpx.agents.direct import AGENT_NAME
from mcpx.agents.instructions import code_instruction
from mcpx.bench.usage import UsageRecorder
from mcpx.llm.models import ModelConfig
from mcpx.sandbox.monty_runtime import MontyRuntime

# The code arm's only tool; its name is the function's name.
RUN_PYTHON = "run_python"


def build_code_agent(
    runtime: MontyRuntime, target_name: str, model: ModelConfig, recorder: UsageRecorder
) -> LlmAgent:
    """Build an agent whose only tool runs Python in ``runtime``'s workspace.

    The workspace must already hold ``servers/<target_name>/`` and the runtime
    must be open (its gateway to the MCP server ready); the caller owns both.
    """

    async def run_python(code: str) -> str:
        """Run a Python script in a restricted interpreter and return its output.

        The script runs in a restricted Python interpreter with the working
        directory's files readable.

        Args:
            code: The complete script. Each call starts a fresh interpreter.

        Returns:
            What the script printed, then the error if it raised, then a note
            if the output was truncated or the script timed out.
        """
        return (await runtime.run(code)).render()

    # LlmAgent is concrete; pyrefly misreads its pydantic/ABC base as abstract.
    return LlmAgent(  # pyrefly: ignore[direct-abstract-base-instantiation]
        name=AGENT_NAME,
        model=model.to_llm(),
        instruction=code_instruction(target_name),
        tools=[run_python],
        after_model_callback=recorder.after_model_callback,
    )
