"""T051: SkillsExtension contributor, skill tools, stats hook and wiring.

Named to avoid T054's ``tests/skills/test_extension.py``. Offline: scripts
run only through ``FakeExecutor``; nothing is executed on the host.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import ExecutorTool, PromptContributor
from harness_builder.core.types import Message, ModelConfig, Observation, ToolCall
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult, SandboxConfig
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.skills import extension as ext
from harness_builder.skills.extension import (
    ACTIVATE_SKILL,
    READ_SKILL_RESOURCE,
    RUN_SKILL_SCRIPT,
    ActivateSkillTool,
    ReadSkillResourceTool,
    RunSkillScriptTool,
    SkillsExtension,
    install_skills,
)
from harness_builder.skills.promotion import (
    PROMOTION_HOOK_PRIORITY,
    PromotionHook,
    PromotionService,
)
from harness_builder.skills.store import SkillStore
from harness_builder.spec.models import SkillsConfig

SETTINGS = Settings(model="fake/model")
SETTINGS_MODEL = ModelConfig(name="fake/model")
BODY_MARKER = "BODY-MARKER-7f3a"
SCRIPT_SRC = """\
import json
import pathlib
import sys

pathlib.Path("HOST-RAN-MARKER").write_text("ran")


def main(args):
    return {"merged": args.get("files", [])}


if __name__ == "__main__":
    print(json.dumps(main(json.loads(sys.stdin.read() or "{}"))))
"""


def write_skill(
    live: Path,
    name: str,
    description: str,
    *,
    scripts: dict[str, str] | None = None,
    references: dict[str, str] | None = None,
) -> Path:
    """Write ``live/<name>`` with SKILL.md and optional resources."""
    skill = live / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        f"{BODY_MARKER} instructions for {name}.\n",
        encoding="utf-8",
    )
    for sub, files in (("scripts", scripts), ("references", references)):
        for file, text in (files or {}).items():
            (skill / sub).mkdir(exist_ok=True)
            (skill / sub / file).write_bytes(text.encode("utf-8"))
    return skill


@pytest.fixture
def store(tmp_path: Path) -> SkillStore:
    """A store with a PDF skill (script + reference) and two others."""
    store = SkillStore(tmp_path / "root")
    write_skill(
        store.live,
        "pdf-merge",
        "Merge several PDF files into one document.",
        scripts={"merge.py": SCRIPT_SRC, "notes.txt": "not python"},
        references={"guide.md": "# Guide\nUse merge.py."},
    )
    write_skill(store.live, "csv-clean", "Clean and normalise CSV tables.")
    write_skill(store.live, "image-resize", "Resize PNG and JPEG images.")
    return store


@dataclass
class Ctx:
    """A minimal ``RunContextView``."""

    run_id: str = "run-1"
    depth: int = 0
    workdir: Path | None = None
    messages: list[Message] = field(default_factory=list)
    ledger: Any = None
    recorder: Any = None


async def offer(extension: SkillsExtension, task: str, ctx: Ctx) -> str | None:
    return await extension.contribute(task, ctx)


def error_type(obs: object) -> str | None:
    assert isinstance(obs, Observation)
    assert obs.ok is False and obs.error is not None
    return obs.error.type


_CONFORMS: PromptContributor = SkillsExtension(SkillStore(Path(".")))


# --- prompt contributor -------------------------------------------------------


@pytest.mark.anyio
async def test_contributor_offers_top_k_names_and_descriptions_only(
    store: SkillStore,
) -> None:
    extension = SkillsExtension(store, top_k=1)
    text = await offer(extension, "merge these pdf files", Ctx())

    assert text is not None
    assert "<available_skills>" in text and "</available_skills>" in text
    assert "<name>pdf-merge</name>" in text
    assert "<description>Merge several PDF files into one document.</description>" in (
        text
    )
    assert "csv-clean" not in text and "image-resize" not in text
    assert BODY_MARKER not in text
    assert extension.offered("run-1") == ["pdf-merge"]


@pytest.mark.anyio
async def test_no_match_adds_nothing(store: SkillStore) -> None:
    extension = SkillsExtension(store)
    assert await offer(extension, "zzz qqq", Ctx()) is None
    assert extension.offered("run-1") == []


@pytest.mark.anyio
async def test_empty_library_adds_nothing(tmp_path: Path) -> None:
    extension = SkillsExtension(SkillStore(tmp_path / "none"))
    assert await offer(extension, "merge pdf", Ctx()) is None


@pytest.mark.anyio
async def test_descriptions_are_xml_escaped(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    write_skill(store.live, "tag-skill", "Handles <b> & </available_skills> pdf")
    text = await offer(SkillsExtension(store), "pdf", Ctx())
    assert text is not None
    assert "&lt;b&gt; &amp; &lt;/available_skills&gt;" in text
    assert text.count("</available_skills>") == 1


@pytest.mark.anyio
async def test_skill_named_in_task_is_offered_beyond_top_k(store: SkillStore) -> None:
    extension = SkillsExtension(store, top_k=1)
    text = await offer(extension, "merge pdf files, then use image-resize", Ctx())
    assert text is not None
    assert extension.offered("run-1") == ["pdf-merge", "image-resize"]
    assert "<name>image-resize</name>" in text


@pytest.mark.anyio
async def test_partial_name_is_not_an_explicit_request(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    write_skill(store.live, "csv", "Spreadsheet helper.")
    write_skill(store.live, "pdf-merge", "Merge PDF files.")
    extension = SkillsExtension(store, top_k=1)
    await offer(extension, "merge pdf files with csv-merge", Ctx())
    assert extension.offered("run-1") == ["pdf-merge"]


@pytest.mark.anyio
async def test_state_is_per_run_and_bounded(store: SkillStore) -> None:
    extension = SkillsExtension(store)
    await offer(extension, "merge pdf", Ctx(run_id="a"))
    await offer(extension, "resize png images", Ctx(run_id="b"))
    assert extension.offered("a") == ["pdf-merge"]
    assert extension.offered("b") == ["image-resize"]
    for i in range(ext.MAX_TRACKED_RUNS):
        await offer(extension, "pdf", Ctx(run_id=f"r{i}"))
    assert extension.offered("a") == []


@pytest.mark.anyio
async def test_library_is_reindexed_every_run(store: SkillStore) -> None:
    extension = SkillsExtension(store)
    assert await offer(extension, "translate audio", Ctx(run_id="a")) is None
    write_skill(store.live, "audio-translate", "Translate audio recordings.")
    text = await offer(extension, "translate audio", Ctx(run_id="b"))
    assert text is not None and "audio-translate" in text


def test_top_k_must_be_positive(store: SkillStore) -> None:
    with pytest.raises(ValueError, match="top_k"):
        SkillsExtension(store, top_k=0)


# --- activate_skill -----------------------------------------------------------


@pytest.mark.anyio
async def test_activate_returns_body_and_resources_for_an_offered_skill(
    store: SkillStore,
) -> None:
    extension = SkillsExtension(store)
    ctx = Ctx()
    await offer(extension, "merge pdf", ctx)
    out = ActivateSkillTool(extension).fn("pdf-merge", ctx)

    assert isinstance(out, dict)
    assert BODY_MARKER in out["instructions"]
    assert out["resources"] == [
        "scripts/merge.py",
        "scripts/notes.txt",
        "references/guide.md",
    ]


@pytest.mark.anyio
async def test_activate_refuses_a_skill_not_offered_to_the_run(
    store: SkillStore,
) -> None:
    extension = SkillsExtension(store)
    ctx = Ctx()
    await offer(extension, "merge pdf", ctx)
    tool = ActivateSkillTool(extension)

    out = tool.fn("csv-clean", ctx)  # exists, but was not retrieved
    assert error_type(out) == "validation"
    assert isinstance(out, Observation) and BODY_MARKER not in str(out.content)
    assert error_type(tool.fn("pdf-merge", Ctx(run_id="unknown"))) == "validation"


# --- read_skill_resource ------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_returns_a_listed_file(store: SkillStore) -> None:
    extension = SkillsExtension(store)
    ctx = Ctx()
    await offer(extension, "merge pdf", ctx)
    tool = ReadSkillResourceTool(extension)
    assert tool.fn("pdf-merge", "references/guide.md", ctx) == "# Guide\nUse merge.py."


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path", ["../csv-clean/SKILL.md", "SKILL.md", "references/missing.md", "/etc"]
)
async def test_read_resource_refuses_unlisted_paths(
    store: SkillStore, path: str
) -> None:
    extension = SkillsExtension(store)
    ctx = Ctx()
    await offer(extension, "merge pdf", ctx)
    assert error_type(ReadSkillResourceTool(extension).fn("pdf-merge", path, ctx)) == (
        "validation"
    )


@pytest.mark.anyio
async def test_read_resource_is_capped(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    big = "x" * (ext.MAX_RESOURCE_BYTES + 10)
    write_skill(store.live, "big-pdf", "Big pdf.", references={"big.md": big})
    extension = SkillsExtension(store)
    ctx = Ctx()
    await offer(extension, "pdf", ctx)
    out = ReadSkillResourceTool(extension).fn("big-pdf", "references/big.md", ctx)
    assert isinstance(out, str)
    assert out.endswith(f"[truncated at {ext.MAX_RESOURCE_BYTES} bytes]")
    assert out.count("x") == ext.MAX_RESOURCE_BYTES


def _link_dir(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    if sys.platform != "win32":
        pytest.skip("cannot create a directory symlink here")
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip("cannot create a symlink or junction here")


@pytest.mark.anyio
async def test_linked_resource_dir_leaving_the_skill_is_refused(
    tmp_path: Path,
) -> None:
    store = SkillStore(tmp_path / "root")
    skill = write_skill(store.live, "linky-pdf", "Linked pdf skill.")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    _link_dir(skill / "references", outside)
    extension = SkillsExtension(store)
    ctx = Ctx()
    await offer(extension, "pdf", ctx)

    out = ReadSkillResourceTool(extension).fn("linky-pdf", "references/secret.txt", ctx)
    assert error_type(out) == "validation"
    assert isinstance(out, Observation) and "SECRET" not in str(out.content)


# --- run_skill_script ---------------------------------------------------------


async def run_script(
    store: SkillStore,
    tmp_path: Path,
    executor: Any,
    *,
    script: str = "scripts/merge.py",
    args: dict[str, Any] | None = None,
) -> Observation:
    extension = SkillsExtension(store)
    ctx = Ctx(workdir=tmp_path / "work")
    await offer(extension, "merge pdf", ctx)
    tool = RunSkillScriptTool(extension).with_executor(executor)
    return await tool.fn("pdf-merge", script, args or {}, ctx)


@pytest.mark.anyio
async def test_script_runs_through_the_executor_only(
    store: SkillStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    executor = FakeExecutor([ExecResult(exit_code=0, stdout='{"merged": ["a"]}\n')])
    obs = await run_script(store, tmp_path, executor, args={"files": ["a"]})

    (req,) = executor.requests
    assert req.language == "python"
    assert req.entry == "run"
    assert req.ro_mounts == {(store.live / "pdf-merge").resolve(): "/skill"}
    assert json.loads(req.stdin or "") == {"files": ["a"]}
    assert '"/skill/scripts/merge.py"' in req.code and "runpy.run_path" in req.code
    assert req.workdir == tmp_path / "work" and req.run_id == "run-1"
    assert req.timeout_s == ext.DEFAULT_TIMEOUT_S and req.network is False
    assert obs.ok and isinstance(obs.content, dict)
    assert obs.content["result"] == {"merged": ["a"]}
    assert obs.content["exit_code"] == 0
    assert not (tmp_path / "HOST-RAN-MARKER").exists()


@pytest.mark.anyio
async def test_script_takes_limits_from_the_executor_config(
    store: SkillStore, tmp_path: Path
) -> None:
    executor = FakeExecutor()
    executor.config = SandboxConfig(timeout_s=7.0, network=True)  # type: ignore[attr-defined]
    await run_script(store, tmp_path, executor)
    (req,) = executor.requests
    assert req.timeout_s == 7.0 and req.network is True


@pytest.mark.anyio
async def test_bare_script_name_and_final_cleared(
    store: SkillStore, tmp_path: Path
) -> None:
    result = ExecResult(exit_code=0, stdout="not json", final_set=True, final_value="x")
    executor = FakeExecutor([result])
    obs = await run_script(store, tmp_path, executor, script="merge.py")
    assert obs.ok and obs.final is None
    assert obs.exec is not None and obs.exec.final_set is False
    assert isinstance(obs.content, dict) and "result" not in obs.content


@pytest.mark.anyio
@pytest.mark.parametrize(
    "script", ["scripts/notes.txt", "references/guide.md", "scripts/../SKILL.md"]
)
async def test_non_python_or_unlisted_scripts_are_refused(
    store: SkillStore, tmp_path: Path, script: str
) -> None:
    executor = FakeExecutor()
    obs = await run_script(store, tmp_path, executor, script=script)
    assert error_type(obs) == "validation"
    assert executor.requests == []


@pytest.mark.anyio
async def test_unbound_tool_is_an_executor_error(store: SkillStore) -> None:
    extension = SkillsExtension(store)
    ctx = Ctx(workdir=None)
    await offer(extension, "merge pdf", ctx)
    tool = RunSkillScriptTool(extension)
    assert isinstance(tool, ExecutorTool) and tool.executor is None
    obs = await tool.fn("pdf-merge", "scripts/merge.py", {}, ctx)
    assert error_type(obs) == "executor_error"


@pytest.mark.anyio
async def test_docker_unavailable_is_an_executor_error(
    store: SkillStore, tmp_path: Path
) -> None:
    class Down(FakeExecutor):
        async def run(self, req: Any) -> ExecResult:
            raise DockerUnavailableError("no daemon")

    obs = await run_script(store, tmp_path, Down())
    assert error_type(obs) == "executor_error"
    assert isinstance(obs.content, str) and "no daemon" in obs.content


def test_mount_source_refuses_dirs_outside_the_search_paths(
    store: SkillStore, tmp_path: Path
) -> None:
    extension = SkillsExtension(store)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert extension.mount_source(store.live / "pdf-merge") == (
        (store.live / "pdf-merge").resolve()
    )
    with pytest.raises(ConfigError, match="outside the allowed roots"):
        extension.mount_source(outside)


@pytest.mark.anyio
async def test_refused_mount_never_reaches_the_executor(
    store: SkillStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: SkillsExtension, skill_dir: Path) -> Path:
        raise ConfigError("read-only mount source is outside the allowed roots")

    monkeypatch.setattr(SkillsExtension, "mount_source", refuse)
    executor = FakeExecutor()
    obs = await run_script(store, tmp_path, executor)
    assert error_type(obs) == "executor_error"
    assert executor.requests == []


# --- stats hook ---------------------------------------------------------------


def _obs(ok: bool = True, exit_code: int = 0) -> Observation:
    return Observation(call_id="c", ok=ok, exec=ExecResult(exit_code=exit_code))


@pytest.mark.anyio
async def test_stats_count_uses_and_successes_of_offered_skills(
    store: SkillStore,
) -> None:
    extension = SkillsExtension(store)
    ctx = Ctx()
    await offer(extension, "merge pdf", ctx)

    def call(tool: str, name: str) -> ToolCall:
        return ToolCall(id="c", name=tool, args={"name": name})

    await extension.record_stats(call(ACTIVATE_SKILL, "pdf-merge"), _obs(), ctx)
    await extension.record_stats(
        call(RUN_SKILL_SCRIPT, "pdf-merge"), _obs(exit_code=1), ctx
    )
    await extension.record_stats(call(READ_SKILL_RESOURCE, "pdf-merge"), _obs(), ctx)
    await extension.record_stats(
        call(ACTIVATE_SKILL, "csv-clean"), _obs(), ctx
    )  # refused
    await extension.record_stats(call("run_code", "pdf-merge"), _obs(), ctx)

    stats = store.read_stats()
    assert list(stats) == ["pdf-merge"]
    assert stats["pdf-merge"].usage_count == 2
    assert stats["pdf-merge"].success_count == 1


# --- wiring -------------------------------------------------------------------


@dataclass
class RecordingBuilder:
    """Captures what the extension registers."""

    tools_: list[Any] = field(default_factory=list)
    hooks: list[tuple[str, Callable[..., Any], int, str | None]] = field(
        default_factory=list
    )
    contributors: list[Any] = field(default_factory=list)

    def tools(self, *tools: Any) -> "RecordingBuilder":
        self.tools_.extend(tools)
        return self

    def hook(
        self,
        event: Any,
        fn: Callable[..., Any],
        priority: int = 0,
        name: str | None = None,
    ) -> "RecordingBuilder":
        self.hooks.append((event, fn, priority, name))
        return self

    def prompt_contributor(self, contributor: Any) -> "RecordingBuilder":
        self.contributors.append(contributor)
        return self


def test_install_registers_contributor_tools_and_stats_hook(tmp_path: Path) -> None:
    builder = RecordingBuilder()
    install_skills(SkillsConfig(root=str(tmp_path), top_k=2), builder)

    (contributor,) = builder.contributors
    assert isinstance(contributor, SkillsExtension) and contributor.top_k == 2
    assert [t.name for t in builder.tools_] == list(ext.SKILL_TOOL_NAMES)
    assert [(e, p, n) for e, _, p, n in builder.hooks] == [
        ("post_tool", ext.STATS_HOOK_PRIORITY, ext.STATS_HOOK_NAME)
    ]


def test_disabled_config_installs_nothing(tmp_path: Path) -> None:
    builder = RecordingBuilder()
    install_skills(SkillsConfig(enabled=False, root=str(tmp_path)), builder)
    assert builder == RecordingBuilder()


def test_root_none_resolves_the_skills_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: SkillStore
) -> None:
    monkeypatch.setenv("HARNESS_SKILLS_ROOT", str(store.root))
    builder = RecordingBuilder()
    install_skills(SkillsConfig(), builder)
    (contributor,) = builder.contributors
    assert contributor._store.root == store.root


def test_promotion_hook_is_wired_last_on_post_tool_and_on_stop(
    store: SkillStore,
) -> None:
    service = PromotionService(store, FakeLLMClient([]), SETTINGS_MODEL)
    hook = PromotionHook(service, mode="deferred")
    builder = RecordingBuilder()
    install_skills(SkillsConfig(root=str(store.root)), builder, promotion=hook)

    assert [(e, p, n) for e, _, p, n in builder.hooks] == [
        ("post_tool", ext.STATS_HOOK_PRIORITY, ext.STATS_HOOK_NAME),
        ("post_tool", PROMOTION_HOOK_PRIORITY, ext.PROMOTION_HOOK_NAME),
        ("on_stop", PROMOTION_HOOK_PRIORITY, ext.PROMOTION_HOOK_NAME),
    ]
    assert builder.hooks[1][1] == hook.post_tool
    assert builder.hooks[2][1] == hook.on_stop
    assert builder.contributors[0].promotion_mode == "deferred"


# --- config hash --------------------------------------------------------------


def harness_hash(extension: SkillsExtension | None) -> str:
    builder = (
        HarnessBuilder("h", SETTINGS)
        .llm(FakeLLMClient([]))
        .executor(FakeExecutor())
        .trace_dir(None)
    )
    if extension is not None:
        extension.install(builder)
    return builder.build().config_hash


def test_skills_on_and_off_hash_differently(tmp_path: Path) -> None:
    on = harness_hash(SkillsExtension(SkillStore(tmp_path / "a")))
    assert on != harness_hash(None)
    assert on != harness_hash(SkillsExtension(SkillStore(tmp_path / "a"), top_k=5))


def test_hash_does_not_depend_on_the_library_location(tmp_path: Path) -> None:
    first = harness_hash(SkillsExtension(SkillStore(tmp_path / "a")))
    assert first == harness_hash(SkillsExtension(SkillStore(tmp_path / "b")))


def test_contributor_identity_has_no_paths(tmp_path: Path) -> None:
    builder = HarnessBuilder("h", SETTINGS).llm(FakeLLMClient([]))
    SkillsExtension(SkillStore(tmp_path)).install(builder.executor(FakeExecutor()))
    config = builder.trace_dir(None).build().config
    assert config.prompt_contributors == [
        {
            "name": "skills",
            "options": {
                "top_k": 3,
                "include_user_skills": False,
                "promotion_mode": None,
            },
        }
    ]
    assert str(tmp_path) not in json.dumps(config.model_dump(mode="json"))


# --- import cost --------------------------------------------------------------


def test_importing_the_extension_loads_no_sibling_or_adk() -> None:
    code = (
        "import sys, harness_builder.skills.extension\n"
        "bad = [m for m in sys.modules if m.startswith(("
        "'skillful_agent', 'google.adk', 'google.genai', 'litellm'))]\n"
        "print(bad)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"
