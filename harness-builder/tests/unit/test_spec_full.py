"""Full HarnessSpec v1 (T040; FR-030, FR-016, FR-032, U5) beyond the T040 tests.

Covers located errors (syntax line:col, schema version, unknown keys, nested
paths, several errors at once), path-traversal rejection at *every* file
reference site, M1 spec back-compat (YAML/JSON equivalence, snapshot
rebuild), trust-flag gating through ``HarnessBuilder.from_spec`` (untrusted
harness code is never executed on the host) and failed-harness refusal.
``test_t040_spec_loader_v1.py`` (T040) covers the per-feature basics and the
mirror drift tests; those are not repeated. Offline: ``FakeExecutor`` only.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import Harness, HarnessBuilder
from harness_builder.core.types import ToolCall
from harness_builder.errors import ConfigError, SpecError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.spec import loader
from harness_builder.spec.loader import load_spec
from harness_builder.tools.container_tool import (
    TOOLS_SCHEMA_FILE,
    ToolSchemaEntry,
    ToolSchemaFile,
    schema_key,
)
from harness_builder.trace.models import HookTraceEvent

SETTINGS = Settings(model="fake/model")
HEAD = "schema_version: 1\nname: t\nmodel: {name: fake/model}\n"


def write_spec(root: Path, body: str, head: str = HEAD) -> Path:
    """Write ``root/harness.yaml`` = ``head`` + ``body``; return ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "harness.yaml").write_text(head + body, encoding="utf-8")
    return root


def from_dir(root: Path, **kwargs: Any) -> HarnessBuilder:
    """``HarnessBuilder.from_spec(root)`` with the test settings."""
    return HarnessBuilder.from_spec(root, settings=SETTINGS, **kwargs)


def finish(builder: HarnessBuilder, *responses: LLMResponse) -> Harness:
    """Attach a fake LLM, keep traces in memory and build."""
    return (
        builder.llm(FakeLLMClient(list(responses)), retry_delay_s=0)
        .trace_dir(None)
        .build()
    )


def marker_module(marker: Path, body: str = "") -> str:
    """Module source whose top-level code writes ``marker`` when executed."""
    return (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "\n"
        "def add(a: int, b: int) -> int:\n"
        '    """Add two integers."""\n'
        "    return a + b\n" + body
    )


def dir_link(link: Path, target: Path) -> None:
    """A directory symlink, falling back to an NTFS junction on Windows."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )


# --- located errors ---------------------------------------------------------


def test_yaml_syntax_error_has_line_and_column(tmp_path: Path) -> None:
    root = write_spec(tmp_path, "budget: {max_steps: 3\nname2: x\n")
    with pytest.raises(SpecError, match=r"^harness\.yaml:\d+:\d+: "):
        load_spec(root)


def test_json_syntax_error_has_line_and_column(tmp_path: Path) -> None:
    (tmp_path / "harness.json").write_text(
        '{\n  "schema_version": 1,\n  "name": "t",\n}\n', encoding="utf-8"
    )
    # Python 3.13 reports 3:14 (trailing comma); 3.11/3.12 report 4:1.
    with pytest.raises(SpecError, match=r"^harness\.json:(3:14|4:1): "):
        load_spec(tmp_path / "harness.json")


@pytest.mark.parametrize(
    ("head", "message"),
    [
        ("name: t\n", "schema_version: missing; expected one of [1]"),
        (
            "schema_version: 2\nname: t\n",
            "schema_version: unsupported 2; expected one of [1]",
        ),
        (
            "schema_version: '1'\nname: t\n",
            "schema_version: unsupported '1'; expected one of [1]",
        ),
    ],
)
def test_schema_version_is_checked_first(
    tmp_path: Path, head: str, message: str
) -> None:
    root = write_spec(tmp_path, "colour: blue\n", head=head)
    with pytest.raises(SpecError) as info:
        load_spec(root)
    assert str(info.value) == f"harness.yaml: {message}"


def test_root_must_be_a_mapping(tmp_path: Path) -> None:
    root = write_spec(tmp_path, "", head="- just\n- a list\n")
    with pytest.raises(SpecError, match=r"^harness\.yaml: <root>: a spec must be"):
        load_spec(root)


def test_missing_spec_path_is_a_spec_error(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="does not exist"):
        load_spec(tmp_path / "nowhere")


def test_every_error_is_reported_with_its_path(tmp_path: Path) -> None:
    root = write_spec(
        tmp_path,
        "colour: blue\n"
        "budget: {max_steps: 0}\n"
        "permissions:\n"
        "  rules:\n"
        "    - {tool: a, decision: allow}\n"
        "    - {tool: b, decision: maybe}\n"
        "subagents: {profiles: {helper: {budget_fraction: 2}}}\n"
        "sandbox: {memory_mb: 999999}\n",
    )
    with pytest.raises(SpecError) as info:
        load_spec(root)
    lines = str(info.value).splitlines()
    paths = [line.split(": ")[1] for line in lines]
    assert set(paths) == {
        "colour",
        "budget.max_steps",
        "permissions.rules[1].decision",
        "subagents.profiles.helper.budget_fraction",
        "sandbox.memory_mb",
    }
    assert all(line.startswith("harness.yaml: ") for line in lines)


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ("tools: [{module: tools/x.py}]\n", "tools[0].function"),
        ("tools: [{builtin: run_code, extra: 1}]\n", "tools[0].extra"),
        ("hooks: [{builtin: x}]\n", "hooks[0].event"),
        ("hooks: [{module: h.py, function: f, event: later}]\n", "hooks[0].event"),
        ("strategy: {name: react, bogus: 1}\n", "strategy.bogus"),
        ("context: {policy: forget, max_tokens: 10}\n", "context.policy"),
        ("skills: {top_k: 0}\n", "skills.top_k"),
        ("evals: [{kind: tool, id: a, tool: t}]\n", "evals[0].check"),
    ],
)
def test_nested_errors_are_located(tmp_path: Path, body: str, where: str) -> None:
    root = write_spec(tmp_path, body)
    with pytest.raises(SpecError, match=rf"harness\.yaml: {re.escape(where)}: "):
        from_dir(root)


# --- path traversal at every file reference site ----------------------------


def outside_abs(tmp_path: Path) -> str:
    """An absolute path to an existing file outside the harness dir."""
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    return outside.as_posix()


TRAVERSALS = ["../outside.py", "sub/../../outside.py", "ABS"]


def reference_body(site: str, ref: str) -> str:
    """A spec body that puts ``ref`` at the reference ``site``."""
    return {
        "prompts.system.file": f"prompts: {{system: {{file: '{ref}'}}}}\n",
        "tools[0].module": f"tools: [{{module: '{ref}', function: add}}]\n",
        "hooks[0].module": (
            f"hooks: [{{module: '{ref}', function: add, event: pre_tool}}]\n"
        ),
        "evals.file": f"evals: {{file: '{ref}'}}\n",
        "skills.root": f"skills: {{enabled: false, root: '{ref}'}}\n",
    }[site]


SITES = [
    "prompts.system.file",
    "tools[0].module",
    "hooks[0].module",
    "evals.file",
    "skills.root",
]


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("ref", TRAVERSALS)
def test_traversal_is_rejected_at_every_reference_site(
    tmp_path: Path, site: str, ref: str
) -> None:
    value = outside_abs(tmp_path) if ref == "ABS" else ref
    root = write_spec(tmp_path / "h", reference_body(site, value))
    with pytest.raises(SpecError, match=rf"harness\.yaml: {re.escape(site)}: "):
        from_dir(root, trust_host_tools=True)


@pytest.mark.parametrize(
    "module",
    ["C:/x/tools/a.py", "tools\\a.py", "tools/a.txt", "/tools/a.py", "tools/a"],
)
@pytest.mark.parametrize("kind", ["tools", "hooks"])
def test_module_refs_reject_drives_backslashes_and_non_py(
    tmp_path: Path, module: str, kind: str
) -> None:
    ref = f"{{module: '{module}', function: add"
    ref += ", event: pre_tool}" if kind == "hooks" else "}"
    root = write_spec(tmp_path, f"{kind}: [{ref}]\n")
    with pytest.raises(SpecError, match=rf"harness\.yaml: {kind}\[0\]\.module: "):
        load_spec(root)


@pytest.mark.parametrize("site", ["prompts.system.file", "evals.file", "skills.root"])
def test_link_leaving_the_harness_dir_is_rejected(tmp_path: Path, site: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "prompt.txt").write_text("secret prompt", encoding="utf-8")
    (outside / "cases.yaml").write_text("[]\n", encoding="utf-8")
    root = tmp_path / "h"
    root.mkdir()
    dir_link(root / "link", outside)
    ref = {
        "prompts.system.file": "link/prompt.txt",
        "evals.file": "link/cases.yaml",
        "skills.root": "link",
    }[site]
    body = reference_body(site, ref)
    write_spec(root, body)
    with pytest.raises(SpecError, match=rf"{re.escape(site)}: must resolve inside"):
        from_dir(root)


def test_untrusted_traversal_is_a_spec_error_not_a_trust_error(
    tmp_path: Path,
) -> None:
    """Location errors win over trust errors, so the path is always reported."""
    root = write_spec(
        tmp_path / "h", "tools: [{module: ../x.py, function: add, runtime: host}]\n"
    )
    with pytest.raises(SpecError, match=r"tools\[0\]\.module"):
        from_dir(root)


# --- M1 back-compat ---------------------------------------------------------

M1_SPEC: dict[str, Any] = {
    "schema_version": 1,
    "name": "m1",
    "description": "An M1-era spec with no v1 additions.",
    "model": {"name": "fake/model", "temperature": 0.0},
    "strategy": {"name": "react"},
    "prompts": {"system": {"file": "prompts/system.md"}},
    "context": {"policy": "summarize", "max_tokens": 2000, "keep_last": 4},
    "permissions": {
        "default": "deny",
        "rules": [{"tool": "deref", "decision": "allow"}],
    },
    "budget": {"max_steps": 6, "max_input_tokens": 50000},
    "subagents": {
        "max_depth": 1,
        "profiles": {"helper": {"description": "h", "budget_fraction": 0.25}},
    },
}


def m1_dir(root: Path, fmt: str) -> Path:
    """An M1 harness dir with the spec as ``harness.<fmt>`` and a prompt file."""
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "system.md").write_text("Be brief.", encoding="utf-8")
    if fmt == "json":
        text = json.dumps(M1_SPEC, indent=2)
    else:
        import yaml  # pyrefly: ignore[untyped-import]

        text = yaml.safe_dump(M1_SPEC, sort_keys=False)
    (root / f"harness.{fmt}").write_text(text, encoding="utf-8")
    return root


def test_m1_spec_yaml_and_json_build_the_same_harness(tmp_path: Path) -> None:
    from_yaml = finish(from_dir(m1_dir(tmp_path / "y", "yaml")))
    from_json = finish(from_dir(m1_dir(tmp_path / "j", "json")))
    assert from_yaml.config_hash == from_json.config_hash
    assert from_yaml.config == from_json.config
    assert from_yaml.system_prompt.startswith("Be brief.\n")
    dumped = from_yaml.config.model_dump()
    assert dumped["prompt_contributors"] == [] and dumped["sandbox"] is None
    assert not from_yaml.uses_workdir


def test_m1_spec_snapshot_rebuilds_without_its_directory(tmp_path: Path) -> None:
    harness = finish(from_dir(m1_dir(tmp_path / "y", "yaml")), LLMResponse(text="hi"))
    assert harness.spec_snapshot is not None
    assert harness.spec_snapshot["prompts"]["system"] == "Be brief."  # inlined
    rebuilt = finish(HarnessBuilder.from_spec(harness.spec_snapshot, settings=SETTINGS))
    assert rebuilt.config_hash == harness.config_hash


@pytest.mark.anyio
async def test_m1_spec_runs_and_traces_its_snapshot(tmp_path: Path) -> None:
    harness = finish(from_dir(m1_dir(tmp_path / "y", "yaml")), LLMResponse(text="ok"))
    result, trace = await harness.run_traced("hello")
    assert (result.stop.reason, result.answer) == ("done", "ok")
    assert trace.spec_snapshot == harness.spec_snapshot


def test_m1_spec_ignores_absent_meta_and_schema_files(tmp_path: Path) -> None:
    root = m1_dir(tmp_path / "y", "yaml")
    assert not (root / loader.META_FILE).exists()
    assert not (root / TOOLS_SCHEMA_FILE).exists()
    finish(from_dir(root))


# --- trust flag gating (U5) -------------------------------------------------


def tool_harness(root: Path, marker: Path, runtime: str) -> Path:
    """A harness with ``tools/math.py:add`` (top level writes ``marker``)."""
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "math.py").write_text(marker_module(marker), encoding="utf-8")
    entry = ToolSchemaEntry(
        module="tools/math.py",
        function="add",
        description="Add two integers.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    )
    schema = ToolSchemaFile(tools={schema_key(entry.module, entry.function): entry})
    (root / TOOLS_SCHEMA_FILE).write_text(schema.model_dump_json(), encoding="utf-8")
    return write_spec(
        root,
        f"tools: [{{module: tools/math.py, function: add, runtime: {runtime}}}]\n"
        "permissions: {rules: [{tool: add, decision: allow}]}\n",
    )


def hook_harness(root: Path, marker: Path) -> Path:
    """A harness with a module ``pre_tool`` hook whose import writes ``marker``."""
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "guard.py").write_text(
        marker_module(
            marker,
            '\n\ndef guard(call, ctx):\n    """Allow every call."""\n    return None\n',
        ),
        encoding="utf-8",
    )
    return write_spec(
        root,
        "tools: [{builtin: run_code}]\n"
        "hooks: [{module: hooks/guard.py, function: guard, event: pre_tool}]\n"
        "permissions: {rules: [{tool: run_code, decision: allow}]}\n",
    )


def test_untrusted_host_tool_is_refused_and_never_imported(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    root = tool_harness(tmp_path / "h", marker, "host")
    with pytest.raises(ConfigError, match=r"harness\.yaml: tools\[0\]\.runtime: "):
        from_dir(root)
    assert not marker.exists()


def test_untrusted_module_hook_is_refused_and_never_imported(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    root = hook_harness(tmp_path / "h", marker)
    with pytest.raises(ConfigError, match=r"harness\.yaml: hooks\[0\]\.module: "):
        from_dir(root)
    assert not marker.exists()


def test_docker_runtime_tool_needs_no_trust_and_is_never_imported(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ran.txt"
    root = tool_harness(tmp_path / "h", marker, "docker")
    builder = from_dir(root)
    assert builder.trust_host_tools is False
    finish(builder.executor(FakeExecutor()))
    assert not marker.exists()


@pytest.mark.anyio
async def test_trusted_host_tool_runs_in_process(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    root = tool_harness(tmp_path / "h", marker, "host")
    builder = from_dir(root, trust_host_tools=True)
    assert builder.trust_host_tools is True and builder.allow_failed is False
    call = ToolCall(id="c1", name="add", args={"a": 2, "b": 5})
    harness = finish(builder, LLMResponse(tool_calls=[call]), LLMResponse(text="seven"))
    assert marker.exists()  # loaded on the host, as trusted
    assert not harness.uses_workdir  # no executor tool, no workdir
    result, trace = await harness.run_traced("add")
    assert result.answer == "seven"
    observations = [
        e.observation
        for s in trace.steps
        for e in s.events
        if hasattr(e, "observation")
    ]
    assert observations[0].content == "7"


@pytest.mark.anyio
async def test_trusted_module_hook_runs_under_a_stable_name(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    root = hook_harness(tmp_path / "h", marker)
    call = ToolCall(id="c1", name="run_code", args={"language": "python", "code": "1"})
    harness = finish(
        from_dir(root, trust_host_tools=True).executor(FakeExecutor()),
        LLMResponse(tool_calls=[call]),
        LLMResponse(text="done"),
    )
    assert marker.exists()
    _, trace = await harness.run_traced("go")
    hooks = [e for s in trace.steps for e in s.events if isinstance(e, HookTraceEvent)]
    assert [h.hook for h in hooks] == ["hooks/guard.py:guard"]


def test_allow_failed_does_not_grant_trust(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    root = tool_harness(tmp_path / "h", marker, "host")
    with pytest.raises(ConfigError, match="trust"):
        from_dir(root, allow_failed=True)
    assert not marker.exists()


# --- failed-harness refusal -------------------------------------------------


def fail_meta(root: Path, status: str = "fail") -> None:
    """Write ``.harness-meta.json`` with ``status``."""
    (root / loader.META_FILE).write_text(
        json.dumps({"status": status, "attempts": 3}), encoding="utf-8"
    )


def test_failed_harness_is_refused_by_from_spec(tmp_path: Path) -> None:
    root = write_spec(tmp_path / "h", "")
    fail_meta(root)
    with pytest.raises(ConfigError) as info:
        from_dir(root)
    message = str(info.value)
    assert "status 'fail'" in message and "--allow-failed" in message
    finish(from_dir(root, allow_failed=True))


def test_trust_does_not_bypass_a_failed_harness(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    root = tool_harness(tmp_path / "h", marker, "host")
    fail_meta(root)
    with pytest.raises(ConfigError, match=r"\.harness-meta\.json"):
        from_dir(root, trust_host_tools=True)
    assert not marker.exists()  # refused before any harness code is loaded


@pytest.mark.parametrize(
    ("meta", "runnable"),
    [
        ({"status": "incomplete"}, True),
        ({"status": "pass", "flagged_tools": ["x"]}, True),
        ({"status": "FAIL"}, False),
        ({"status": None}, False),
        ({}, False),
        (["pass"], False),
    ],
)
def test_meta_statuses_through_from_spec(
    tmp_path: Path, meta: Any, runnable: bool
) -> None:
    root = write_spec(tmp_path / "h", "")
    (root / loader.META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    if runnable:
        finish(from_dir(root))
        return
    with pytest.raises(ConfigError, match=r"\.harness-meta\.json"):
        from_dir(root)


def test_meta_is_checked_for_a_snapshot_with_a_base_dir(tmp_path: Path) -> None:
    root = write_spec(tmp_path / "h", "")
    harness = finish(from_dir(root))
    snapshot = harness.spec_snapshot
    assert snapshot is not None
    fail_meta(root)
    with pytest.raises(ConfigError, match="status 'fail'"):
        HarnessBuilder.from_spec(snapshot, settings=SETTINGS, base_dir=root)
    rebuilt = finish(
        HarnessBuilder.from_spec(
            snapshot, settings=SETTINGS, base_dir=root, allow_failed=True
        )
    )
    assert rebuilt.config_hash == harness.config_hash
    # Without a base_dir there is no harness directory, hence no meta check.
    finish(HarnessBuilder.from_spec(snapshot, settings=SETTINGS))


def test_meta_check_runs_before_any_spec_reference_is_resolved(
    tmp_path: Path,
) -> None:
    """A failed harness is refused even when its references are broken."""
    root = write_spec(tmp_path / "h", "evals: {file: missing.yaml}\n")
    fail_meta(root)
    with pytest.raises(ConfigError, match="status 'fail'"):
        from_dir(root)
    with pytest.raises(SpecError, match=r"evals\.file"):
        from_dir(root, allow_failed=True)
