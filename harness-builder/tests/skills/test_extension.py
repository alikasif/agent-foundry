"""T054: skills extension contract (progressive disclosure, offered-only, tools).

Complements ``tests/unit/test_skills_extension_t051.py``: runs over the
independent fixture library, pins the stable T051 surface, and adds new
angles on the offered-only rule and on path confinement. Scripts reach only
``FakeExecutor``; the one real script run is ``@pytest.mark.docker`` and
skips without a daemon or without the sandbox image.
"""

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from harness_builder.core.types import Message, Observation
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.skills import extension as ext_mod
from harness_builder.skills.extension import (
    ActivateSkillTool,
    ReadSkillResourceTool,
    RunSkillScriptTool,
    SkillsExtension,
)
from harness_builder.skills.store import SkillStore

SCRIPT = """\
import json
import pathlib
import sys

pathlib.Path("T054-HOST-MARKER").write_text("ran on host")


def main(args):
    return {"total": sum(args.get("values", []))}


if __name__ == "__main__":
    print(json.dumps(main(json.loads(sys.stdin.read() or "{}"))))
"""


@dataclass
class Ctx:
    """A minimal ``RunContextView``."""

    run_id: str = "run-a"
    depth: int = 0
    workdir: Path | None = None
    messages: list[Message] = field(default_factory=list)
    ledger: Any = None
    recorder: Any = None


def error_of(obs: object) -> tuple[str | None, str]:
    """``(error type, message)`` of an error observation, else ``(None, "")``."""
    if isinstance(obs, Observation) and obs.error is not None:
        return obs.error.type, obs.error.message
    return None, ""


@pytest.fixture
def script_store(tmp_path: Path, write_skill: Any) -> SkillStore:
    """A store with a script-bearing ``number-tally`` skill and a decoy."""
    store = SkillStore(tmp_path / "root")
    write_skill(
        store.live,
        "number-tally",
        "Add up a list of numbers and report the total.",
        body="TALLY-BODY-5e1d run scripts/tally.py",
        scripts={"tally.py": SCRIPT, "helper.sh": "echo hi\n"},
        references={"usage.md": "# Usage\n"},
    )
    write_skill(store.live, "weather-lookup", "Look up a city's weather forecast.")
    return store


async def offer(ext: SkillsExtension, task: str, run_id: str = "run-a") -> str | None:
    """Run the contributor for ``task`` under ``run_id``."""
    return await ext.contribute(task, Ctx(run_id=run_id))


# --- stable surface -----------------------------------------------------------


def test_stable_names_and_constants() -> None:
    assert ext_mod.SKILL_TOOL_NAMES == (
        "activate_skill",
        "read_skill_resource",
        "run_skill_script",
    )
    assert ext_mod.MAX_RESOURCE_BYTES == 64_000
    assert ext_mod.MAX_TRACKED_RUNS == 64
    assert ext_mod.STATS_HOOK_NAME == "skills:stats"
    assert ext_mod.SKILL_MOUNT == "/skill"
    assert RunSkillScriptTool.risk == frozenset({"code_exec"})
    assert ActivateSkillTool.risk == ReadSkillResourceTool.risk == frozenset()
    for tool in (ActivateSkillTool, ReadSkillResourceTool, RunSkillScriptTool):
        assert tool.nondeterministic is True


# --- progressive disclosure ----------------------------------------------------


@pytest.mark.anyio
async def test_no_body_enters_the_prompt_for_any_labelled_query(
    fixture_store: SkillStore, labelled_queries: list[tuple[str, str]]
) -> None:
    ext = SkillsExtension(fixture_store, top_k=3)
    offered_any = 0
    for i, (query, _) in enumerate(labelled_queries):
        text = await offer(ext, query, run_id=f"r{i}")
        if text is None:
            continue
        offered_any += 1
        assert "FIXTURE-BODY-" not in text
        assert "<available_skills>" in text and "<location>" not in text
        assert str(fixture_store.root) not in text
        assert len(ext.offered(f"r{i}")) <= 3
    assert offered_any >= len(labelled_queries) - 2


@pytest.mark.anyio
async def test_body_arrives_only_through_activate_skill(
    fixture_store: SkillStore,
) -> None:
    ext = SkillsExtension(fixture_store, top_k=3)
    text = await offer(ext, "Decode this bearer token and show its claims")
    assert text is not None and "<name>jwt-decode</name>" in text
    assert "FIXTURE-BODY-jwt-decode" not in text
    result = ActivateSkillTool(ext).fn("jwt-decode", Ctx())
    assert isinstance(result, dict)
    assert "FIXTURE-BODY-jwt-decode" in result["instructions"]
    assert result["resources"] == []


# --- offered-only rule ---------------------------------------------------------


@pytest.mark.anyio
async def test_a_live_skill_that_was_not_offered_is_refused_by_every_tool(
    script_store: SkillStore, tmp_path: Path
) -> None:
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "what is the weather forecast for Oslo")
    assert ext.offered("run-a") == ["weather-lookup"]
    executor = FakeExecutor()
    ctx = Ctx(workdir=tmp_path)
    results = [
        ActivateSkillTool(ext).fn("number-tally", ctx),
        ReadSkillResourceTool(ext).fn("number-tally", "references/usage.md", ctx),
        await RunSkillScriptTool(ext, executor).fn(
            "number-tally", "scripts/tally.py", {}, ctx
        ),
    ]
    for obs in results:
        kind, message = error_of(obs)
        assert kind == "validation"
        assert "not available in this run" in message
        assert isinstance(obs, Observation) and obs.error is not None
        assert obs.error.hint == "Available skills: weather-lookup."
    assert executor.requests == []


@pytest.mark.anyio
async def test_an_offer_is_scoped_to_its_run_id(script_store: SkillStore) -> None:
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "add up these numbers", run_id="parent")
    await offer(ext, "weather forecast please", run_id="child")
    assert ext.offered("parent") == ["number-tally"]
    assert ext.offered("child") == ["weather-lookup"]
    kind, _ = error_of(ActivateSkillTool(ext).fn("number-tally", Ctx(run_id="child")))
    assert kind == "validation"
    kind, _ = error_of(ActivateSkillTool(ext).fn("number-tally", Ctx(run_id="other")))
    assert kind == "validation"
    ok = ActivateSkillTool(ext).fn("number-tally", Ctx(run_id="parent"))
    assert isinstance(ok, dict) and "TALLY-BODY-5e1d" in ok["instructions"]


@pytest.mark.anyio
async def test_an_evicted_run_loses_its_offer(script_store: SkillStore) -> None:
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "add up these numbers", run_id="oldest")
    for i in range(ext_mod.MAX_TRACKED_RUNS):
        await offer(ext, "weather forecast", run_id=f"later-{i}")
    assert ext.offered("oldest") == []
    obs = ActivateSkillTool(ext).fn("number-tally", Ctx(run_id="oldest"))
    assert error_of(obs)[0] == "validation"
    assert isinstance(obs, Observation) and obs.error is not None
    assert obs.error.hint == "Available skills: (none)."


# --- confinement ------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "references/../SKILL.md",
        "../number-tally/references/usage.md",
        "./references/usage.md",
        "references\\usage.md",
        "references//usage.md",
        "/references/usage.md",
        "REFERENCES/usage.md",
        "SKILL.md",
        "references/",
    ],
)
async def test_read_resource_refuses_every_non_listed_spelling(
    script_store: SkillStore, path: str
) -> None:
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "add up these numbers")
    kind, message = error_of(ReadSkillResourceTool(ext).fn("number-tally", path, Ctx()))
    assert kind == "validation", path
    assert "is not a resource" in message


@pytest.mark.anyio
async def test_read_resource_absolute_path_to_a_listed_file_is_refused(
    script_store: SkillStore,
) -> None:
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "add up these numbers")
    absolute = str(script_store.live / "number-tally" / "references" / "usage.md")
    kind, _ = error_of(ReadSkillResourceTool(ext).fn("number-tally", absolute, Ctx()))
    assert kind == "validation"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "script",
    [
        "scripts/../SKILL.md",
        "scripts/../../weather-lookup/SKILL.md",
        "../scripts/tally.py",
        "scripts\\tally.py",
        "scripts/helper.sh",
        "references/usage.md",
        "scripts/missing.py",
        "tally",
        "/skill/scripts/tally.py",
    ],
)
async def test_run_script_refuses_non_listed_or_non_python_paths(
    script_store: SkillStore, tmp_path: Path, script: str
) -> None:
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "add up these numbers")
    executor = FakeExecutor()
    obs = await RunSkillScriptTool(ext, executor).fn(
        "number-tally", script, {}, Ctx(workdir=tmp_path)
    )
    assert error_of(obs)[0] == "validation", script
    assert executor.requests == []


@pytest.mark.anyio
async def test_file_link_listed_as_a_resource_but_pointing_outside_is_refused(
    script_store: SkillStore, tmp_path: Path
) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"TOP-SECRET")
    link = script_store.live / "number-tally" / "references" / "leak.md"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("no file-symlink privilege on this host")
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "add up these numbers")
    obs = ReadSkillResourceTool(ext).fn("number-tally", "references/leak.md", Ctx())
    kind, message = error_of(obs)
    assert kind == "validation" and "resolves outside" in message


# --- run_skill_script through the FakeExecutor ---------------------------------------


@pytest.mark.anyio
async def test_script_request_shape_and_final_cleared(
    script_store: SkillStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    reply = ExecResult(
        exit_code=0,
        stdout='{"total": 6}',
        final_set=True,
        final_value="hijack",
    )
    executor = FakeExecutor([reply])
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "add up these numbers", run_id="run-x")
    obs = await RunSkillScriptTool(ext, executor).fn(
        "number-tally",
        "tally.py",
        {"values": [1, 2, 3]},
        Ctx(run_id="run-x", workdir=workdir),
    )
    (req,) = executor.requests
    skill_dir = (script_store.live / "number-tally").resolve()
    assert dict(req.ro_mounts) == {skill_dir: "/skill"}
    assert req.language == "python" and req.entry == "run"
    assert json.loads(req.stdin or "") == {"values": [1, 2, 3]}
    assert json.dumps("/skill/scripts/tally.py") in req.code
    assert "runpy.run_path" in req.code and "__main__" in req.code
    assert req.workdir == workdir and req.run_id == "run-x"
    assert req.network is False and req.timeout_s == ext_mod.DEFAULT_TIMEOUT_S
    assert obs.ok and obs.exec is not None
    assert obs.exec.final_set is False and obs.exec.final_value is None
    assert isinstance(obs.content, dict) and obs.content["result"] == {"total": 6}
    assert "final_set" not in obs.content
    assert not (tmp_path / "T054-HOST-MARKER").exists()
    assert not (workdir / "T054-HOST-MARKER").exists()


@pytest.mark.anyio
async def test_failed_script_is_ok_observation_but_not_a_stats_success(
    script_store: SkillStore, tmp_path: Path
) -> None:
    from harness_builder.core.types import ToolCall

    executor = FakeExecutor([ExecResult(exit_code=1, stderr="boom")])
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "add up these numbers")
    ctx = Ctx(workdir=tmp_path)
    args = {"name": "number-tally", "script": "scripts/tally.py", "args": {}}
    obs = await RunSkillScriptTool(ext, executor).fn(
        "number-tally", "scripts/tally.py", {}, ctx
    )
    assert obs.ok and isinstance(obs.content, dict)
    assert obs.content["exit_code"] == 1 and "result" not in obs.content
    ext.record_stats(ToolCall(id="c1", name="run_skill_script", args=args), obs, ctx)
    stats = script_store.read_stats()["number-tally"]
    assert (stats.usage_count, stats.success_count) == (1, 0)


# --- real Docker ------------------------------------------------------------------


def _sandbox_image_or_skip() -> str:
    """The sandbox image tag, or skip when it is not built locally."""
    from harness_builder.executor.image_build import image_tag

    tag = image_tag()
    proc = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.skip(f"sandbox image {tag} is not built")
    return tag


@pytest.mark.docker
@pytest.mark.anyio
@pytest.mark.timeout(300)
async def test_real_script_runs_in_docker_with_json_stdin(
    script_store: SkillStore, tmp_path: Path
) -> None:
    from harness_builder.executor.docker import DockerExecutor

    executor = DockerExecutor(image=_sandbox_image_or_skip())
    await executor.preflight()
    ext = SkillsExtension(script_store, top_k=1)
    await offer(ext, "add up these numbers")
    workdir = tmp_path / "work"
    workdir.mkdir()
    obs = await RunSkillScriptTool(ext, executor).fn(
        "number-tally", "scripts/tally.py", {"values": [4, 5]}, Ctx(workdir=workdir)
    )
    assert obs.ok and isinstance(obs.content, dict), obs.content
    assert obs.content["exit_code"] == 0, obs.content
    assert obs.content["result"] == {"total": 9}
    # The script's marker write fails on the read-only /skill or lands in /work.
    assert not (script_store.live / "number-tally" / "T054-HOST-MARKER").exists()
