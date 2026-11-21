"""Helpers for the generator fixture tests (T045); fixtures live in conftest.

The fixture domains under ``fixtures/`` are complete harness directories that
a scripted ``FakeLLMClient`` writes through the generator's ``write_file``
tool. ``fixtures/broken_tool/<variant>/`` holds overlays: files that replace
the matching files of the ``unit_conversion`` domain.
"""

import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from harness_builder.core.types import ToolCall
from harness_builder.executor.docker import LANGUAGES
from harness_builder.executor.image import hb_entry
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.generator.pipeline import GeneratorPipeline
from harness_builder.generator.request import GenerationRequest
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.tools.container_tool import TOOLS_MOUNT

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DOMAINS: tuple[str, ...] = ("csv_triage", "repo_qa", "unit_conversion")
BROKEN_VARIANTS: tuple[str, ...] = ("bad_signature", "raising_body")
OVERLAID_DOMAIN = "unit_conversion"
"""The domain the ``broken_tool`` overlays apply to."""
SKIPPED_NAMES = frozenset({"__pycache__", ".gitkeep", "tools.schema.json"})

REQUESTS: dict[str, GenerationRequest] = {
    "csv_triage": GenerationRequest(
        description="Triage bug reports from a CSV export by severity and status"
    ),
    "repo_qa": GenerationRequest(
        description="Answer questions about a small repository folder"
    ),
    "unit_conversion": GenerationRequest(
        description="Convert lengths and temperatures between units"
    ),
}

FileSet = dict[str, str]
"""Relative POSIX path -> text, as passed to ``write_file``."""

PipelineFactory = Callable[..., GeneratorPipeline]
"""``make_pipeline(generator, *, validator=None, max_attempts=3, budget=...)``."""
STAGES: tuple[str, ...] = ("A", "B0", "B", "C")
GENERATOR_MODEL = "test/generator-model"


def fixture_files(name: str) -> FileSet:
    """Every file of ``fixtures/<name>`` keyed by relative POSIX path.

    Caches and a stale ``tools.schema.json`` are skipped; text is read with
    universal newlines, so a CRLF checkout yields the same content.
    """
    root = FIXTURES / name
    assert root.is_dir(), root
    files: FileSet = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or SKIPPED_NAMES.intersection(rel.parts):
            continue
        files[rel.as_posix()] = path.read_text(encoding="utf-8")
    return files


def broken_files(variant: str) -> FileSet:
    """The ``unit_conversion`` domain with a ``broken_tool/<variant>`` overlay."""
    return {**fixture_files(OVERLAID_DOMAIN), **fixture_files(f"broken_tool/{variant}")}


class ScriptedGenerator:
    """Scripted generator LLM: each run writes one file set, then submits.

    Run ``n`` (1-based) writes ``file_sets[min(n, len) - 1]``, so
    ``ScriptedGenerator(broken, fixed)`` drafts a broken harness and repairs
    it on the next run. A new run is detected by a user message last (the
    task), as the ReAct loop sends it on step 0 only.
    """

    def __init__(self, *file_sets: FileSet) -> None:
        """Store the file sets; at least one is required."""
        assert file_sets, "give at least one file set"
        self.file_sets = file_sets
        self.runs = 0
        self.tasks: list[str] = []
        self.client = FakeLLMClient(self.respond)

    def respond(self, req: LLMRequest) -> LLMResponse:
        """Write the run's files on its first step; submit afterwards."""
        last = req.messages[-1]
        if last.role != "user":
            submit = ToolCall(id=f"r{self.runs}-s", name="submit", args={})
            return LLMResponse(tool_calls=[submit])
        self.runs += 1
        self.tasks.append(last.content)
        files = self.file_sets[min(self.runs, len(self.file_sets)) - 1]
        calls = [
            ToolCall(
                id=f"r{self.runs}-w{i}",
                name="write_file",
                args={"path": path, "content": text},
            )
            for i, (path, text) in enumerate(files.items())
        ]
        return LLMResponse(tool_calls=calls)


def tree(root: Path) -> FileSet:
    """Text of every file under ``root`` (for comparing written harnesses)."""
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


async def emulated_docker_run(req: ExecRequest) -> ExecResult:
    """Answer one sandbox request in-process, like the container would.

    The ``schema``/``call`` entries load the module from the harness dir
    mounted at ``/tools`` with the real ``hb_entry`` functions (the fixtures
    are trusted test code). The ``run`` entry (``run_code``, PyAssert) is not
    emulated: it fails loudly instead of faking a pass.
    """
    if req.entry not in ("schema", "call"):
        raise AssertionError(f"entry {req.entry!r} is not emulated")
    (host_dir,) = [h for h, c in req.ro_mounts.items() if c == TOOLS_MOUNT]
    module = PurePosixPath(req.entry_args[0]).relative_to(TOOLS_MOUNT)
    loaded = hb_entry.load_tool(Path(host_dir) / module, req.entry_args[1])
    if req.entry == "schema":
        envelope = hb_entry.tool_schema(loaded)
    else:
        envelope = hb_entry.call_tool(loaded, json.loads(req.stdin or "{}"))
    return ExecResult(exit_code=0, stdout=json.dumps(envelope))


class EmulatedDocker:
    """An ``Executor`` that is "available" and runs ``emulated_docker_run``."""

    def __init__(self) -> None:
        """Start with no recorded requests."""
        self.requests: list[ExecRequest] = []
        self.preflight_calls = 0

    @property
    def languages(self) -> frozenset[str]:
        """Same languages as ``DockerExecutor``."""
        return LANGUAGES

    async def preflight(self) -> None:
        """Docker is "available"."""
        self.preflight_calls += 1

    async def run(self, req: ExecRequest) -> ExecResult:
        """Record and answer ``req``."""
        self.requests.append(req)
        return await emulated_docker_run(req)
