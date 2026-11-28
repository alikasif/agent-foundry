"""Reads the prompt files that sit next to this module."""

import tomllib
from pathlib import Path

_FOLDER = Path(__file__).parent


def _read(name: str) -> str:
    return (_FOLDER / name).read_text(encoding="utf-8")


def load_system_prompt() -> str:
    return _read("system_prompt.md")


def load_tool_descriptions() -> dict[str, str]:
    """The description of each tool, by tool name."""
    texts = tomllib.loads(_read("tools.toml"))
    return {name: text.strip() for name, text in texts.items()}
