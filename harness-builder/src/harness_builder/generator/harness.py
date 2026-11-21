"""The generator's own harness (designs.md 9.1, FR-031).

The generator is a ReAct harness built with the framework itself. Its tools
only read and write files in the staging directory and describe the spec
format; none of them imports or runs the code it writes (``uses_workdir`` is
False: no tool needs the Executor). Execution of generated code happens only
in the validation pipeline's stages.

Tools:

- ``write_file(path, content)`` / ``read_file(path)``: confined to the
  staging directory by deny rules on the ``path`` argument (defense in depth)
  and by ``confine``, the real guard, inside the tool body;
- ``list_builtin_tools()`` and ``get_spec_schema()``;
- ``ask_user(question)``: allowed (at most ``MAX_QUESTIONS``) only for an
  interactive request; otherwise a ``deny`` rule answers it (auto-denied);
- ``submit(summary)``: ends the run with ``summary`` as the final answer.
"""

from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import anyio.to_thread
from rich.console import Console
from rich.prompt import Prompt

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.types import BudgetLimits, Rule, ToolResult
from harness_builder.evals.models import EVAL_CASES
from harness_builder.generator.request import GenerationRequest
from harness_builder.permissions.approvers import AutoDenyApprover
from harness_builder.spec.loader import BUILTIN_TOOLS, IMPLIED_TOOLS, META_FILE
from harness_builder.spec.models import HarnessSpec
from harness_builder.tools.container_tool import TOOLS_SCHEMA_FILE
from harness_builder.tools.registry import Tool, tool

GENERATOR_NAME = "harness-generator"
MAX_QUESTIONS = 5
MAX_FILE_BYTES = 256 * 1024
"""Largest file ``write_file`` accepts and ``read_file`` returns."""
GENERATOR_BUDGET = BudgetLimits(max_steps=60)
"""Default limits; one ledger covers the draft and every repair attempt
(ASSUMED). No cost cap by default, so an unpriced model still builds (U10)."""
REPORTS_DIR = "reports"
RESERVED_PATHS: frozenset[str] = frozenset({REPORTS_DIR, META_FILE, TOOLS_SCHEMA_FILE})
"""Top-level names the pipeline writes; the generator may not write them."""
TOOL_NAMES: tuple[str, ...] = (
    "write_file",
    "read_file",
    "list_builtin_tools",
    "get_spec_schema",
    "ask_user",
    "submit",
)

AskFn = Callable[[str], str]
"""Asks the user one question and returns the answer (blocking)."""

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class StagingPathError(ValueError):
    """A ``write_file``/``read_file`` path that leaves the staging directory."""


def confine(staging: Path, path: str, *, for_write: bool = False) -> Path:
    """Resolve ``path`` inside ``staging`` or raise ``StagingPathError``.

    Lexical checks come first (empty, backslash, ``:`` drive or stream,
    absolute, ``..``), so Windows anchors and device paths never reach
    ``Path``. The joined path is then resolved (following links) and must lie
    strictly under the resolved staging directory. Writes may not target the
    pipeline-owned ``RESERVED_PATHS``.
    """
    rel = PurePosixPath(path)
    if (
        not path.strip()
        or "\\" in path
        or ":" in path
        or rel.is_absolute()
        or ".." in rel.parts
    ):
        raise StagingPathError(
            f"{path!r}: use a relative POSIX path inside the harness directory "
            "(no absolute path, drive, '..' or backslash)"
        )
    if for_write and rel.parts[0] in RESERVED_PATHS:
        raise StagingPathError(f"{path!r}: {rel.parts[0]} is written by the pipeline")
    root = staging.resolve()
    target = root.joinpath(*rel.parts).resolve()
    if target == root or not target.is_relative_to(root):
        raise StagingPathError(f"{path!r}: resolves outside the harness directory")
    return target


def staging_rules(interactive: bool) -> list[Rule]:
    """Permission rules of the generator; the default for anything else is deny.

    The ``path`` deny rules mirror ``confine``'s lexical checks (a glob ``*``
    also matches ``/`` and ``..``, so they are not the guard on their own).
    """
    rules: list[Rule] = []
    for name in ("write_file", "read_file"):
        for rule_id, pattern in (
            ("absolute", "/*"),
            ("backslash", "*\\*"),
            ("drive", "*:*"),
            ("parent", "*..*"),
        ):
            rules.append(
                Rule(name, "deny", args={"path": pattern}, rule_id=f"staging:{rule_id}")
            )
    rules += [
        Rule(name, "allow")
        for name in (
            "write_file",
            "read_file",
            "list_builtin_tools",
            "get_spec_schema",
            "submit",
        )
    ]
    rules.append(
        Rule(
            "ask_user",
            "allow" if interactive else "deny",
            rule_id="ask_user:interactive" if interactive else "ask_user:auto-deny",
        )
    )
    return rules


def generator_builder(
    request: GenerationRequest,
    staging: Path,
    *,
    settings: Settings | None = None,
    ask: AskFn | None = None,
    budget: BudgetLimits = GENERATOR_BUDGET,
) -> HarnessBuilder:
    """A ``HarnessBuilder`` for the generator writing into ``staging``.

    Chain ``.llm(...)``, ``.model(...)`` or ``.trace_dir(...)`` before
    ``build()``. ``ask`` answers ``ask_user`` in interactive runs (default: a
    terminal prompt on stderr). ``ask`` decisions never reach a prompt: the
    approver auto-denies (no rule answers ``ask``).
    """
    tools = generator_tools(request, staging, ask or _terminal_ask)
    return (
        HarnessBuilder(GENERATOR_NAME, settings)
        .strategy("react")
        .system_prompt(system_prompt())
        .tools(*tools)
        .permissions(
            staging_rules(request.interactive),
            default="deny",
            approver=AutoDenyApprover(),
        )
        .budget(
            max_steps=budget.max_steps,
            max_input_tokens=budget.max_input_tokens,
            max_output_tokens=budget.max_output_tokens,
            max_cost_usd=budget.max_cost_usd,
        )
    )


def generator_tools(
    request: GenerationRequest, staging: Path, ask: AskFn
) -> list[Tool]:
    """The six generator tools bound to ``staging`` (order: ``TOOL_NAMES``)."""
    asked = 0

    def write_file(path: str, content: str) -> str:
        """Write a UTF-8 text file into the harness directory (overwrites).

        ``path`` is relative, with forward slashes (for example
        ``tools/convert.py``). ``reports/``, ``.harness-meta.json`` and
        ``tools.schema.json`` are written by the pipeline, not by you.
        """
        target = confine(staging, path, for_write=True)
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{path!r}: {len(data)} bytes > {MAX_FILE_BYTES}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return f"wrote {path} ({len(data)} bytes)"

    def read_file(path: str) -> str:
        """Read a UTF-8 text file from the harness directory (relative path)."""
        target = confine(staging, path)
        data = target.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{path!r}: {len(data)} bytes > {MAX_FILE_BYTES}")
        return data.decode("utf-8", errors="replace")

    def list_builtin_tools() -> dict[str, Any]:
        """Builtin tools a harness may reference with ``{builtin: <name>}``.

        ``implied`` tools are not listed under ``tools``; the named spec
        section adds them.
        """
        allowed = set(request.allowed_tools)
        names = [n for n in sorted(BUILTIN_TOOLS) if not allowed or n in allowed]
        implied = {
            n: how for n, how in IMPLIED_TOOLS.items() if not allowed or n in allowed
        }
        return {"builtin": names, "implied": implied}

    def get_spec_schema() -> dict[str, Any]:
        """JSON schemas of ``harness.yaml`` (HarnessSpec v1) and eval cases."""
        return {
            "harness_spec": HarnessSpec.model_json_schema(),
            "eval_cases": EVAL_CASES.json_schema(),
        }

    async def ask_user(question: str) -> str:
        """Ask the user one clarifying question (at most 5 per generation)."""
        nonlocal asked
        if asked >= MAX_QUESTIONS:
            raise ValueError(
                f"question limit {MAX_QUESTIONS} reached; continue with stated "
                "assumptions"
            )
        asked += 1
        return await anyio.to_thread.run_sync(ask, question)

    def submit(summary: str = "") -> ToolResult:
        """Finish: the harness files are complete. ``summary`` says what you
        built and which assumptions you made."""
        return ToolResult(content="submitted", final=summary or "submitted")

    return [
        tool(risk=("filesystem_write",))(write_file),
        tool(read_file),
        tool(list_builtin_tools),
        tool(get_spec_schema),
        tool(ask_user),
        tool(submit),
    ]


def _terminal_ask(question: str) -> str:
    """Prompt on stderr; EOF or Ctrl-C answers with an empty string."""
    try:
        return Prompt.ask(question, console=Console(stderr=True), default="")
    except (EOFError, KeyboardInterrupt):
        return ""


def system_prompt() -> str:
    """The generator's base system prompt (ReAct adds its own guidance)."""
    skeleton = (TEMPLATE_DIR / "harness.yaml").read_text(encoding="utf-8")
    return SYSTEM_PROMPT.format(skeleton=skeleton.strip(), max_q=MAX_QUESTIONS)


SYSTEM_PROMPT = """\
You build agent harnesses for the harness-builder framework. Write every file \
with write_file into the harness directory (paths are relative, with forward \
slashes). You never run code: a validation pipeline checks your files after \
you call submit, and if it fails you get its report and fix the files.

Directory layout:
- harness.yaml: the HarnessSpec v1 (call get_spec_schema for the full schema)
- prompts/system.md: the harness's system prompt
- tools/__init__.py (empty) and tools/<module>.py: tool functions
- evals/cases.yaml: eval cases
- README.md: what the harness does, assumptions, usage

harness.yaml skeleton:
```yaml
{skeleton}
```

Tool module conventions (checked statically, then run in a Docker sandbox):
- One top-level function per tool, with a docstring (the tool description)
  and a type hint on every parameter; plain or keyword-only parameters only:
  no *args, **kwargs, positional-only parameters or a parameter named ctx.
- Return a str, number, list or dict that is JSON-serializable.
- Use the standard library and pydantic only. No network (socket, urllib,
  requests, httpx, aiohttp), subprocess, os.system/os.exec*, file deletion
  (os.remove, unlink, shutil.rmtree, rmdir), eval, exec, __import__ or
  secrets: such tools are flagged and need user approval at every call.
- Every module tool in harness.yaml needs at least one entry in examples
  (an args object); validation calls each tool once with examples[0].
- Permissions default to deny: add an allow rule for every tool.

evals/cases.yaml is a list of cases, for example:
```yaml
- kind: tool
  id: add-small
  tool: add
  args: {{a: 1, b: 2}}
  check: {{type: equals, value: 3}}
- kind: task
  id: sum
  input: "What is 1+2?"
  check: {{type: contains, text: "3"}}
```
Tool cases must pass deterministically; checks: equals, contains, regex.

Builtin tools (list_builtin_tools) are referenced as {{builtin: <name>}}.
Ask the user (ask_user, at most {max_q} questions) only when the request is \
ambiguous and only if the request says the run is interactive. When the files \
are complete, call submit with a short summary of what you built and which \
assumptions you made.
"""
