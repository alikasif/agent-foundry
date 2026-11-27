"""A function tool whose description comes from data, not from its docstring."""

from collections.abc import Callable

from google.adk.tools.function_tool import FunctionTool
from google.genai import types


class DescribedTool(FunctionTool):
    def __init__(self, func: Callable, description: str) -> None:
        super().__init__(func)
        self.description = description

    def _get_declaration(self) -> types.FunctionDeclaration:
        # ADK builds and caches the declaration from the function alone, and hands
        # out a copy, so the description is set on every call.
        declaration = super()._get_declaration()
        if declaration is None:
            raise ValueError(f"no declaration for tool {self.name!r}")
        declaration.description = self.description
        return declaration
