"""CodeAct Agent -- executable Python code as the unified action space.

Phase A additions:
- FinalSignal: sentinel raised by FINAL() to end a turn.
- LanguageRegistry / LanguageExecutor: pluggable language dispatch.
- bash_resolver: Git Bash / MSYS2 tool resolver for Windows.
"""

from codeact_agent.agent import CodeActAgent
from codeact_agent.languages import LanguageExecutor, LanguageRegistry, register
from codeact_agent.loop import AgentLoop
from codeact_agent.sandbox import FinalSignal, PythonSandbox, SandboxResult

__all__ = [
    "CodeActAgent",
    "AgentLoop",
    "FinalSignal",
    "PythonSandbox",
    "SandboxResult",
    "LanguageExecutor",
    "LanguageRegistry",
    "register",
]
__version__ = "0.1.0"
