"""T040: full HarnessSpec v1 loader (module tools and hooks, sandbox, skills,
evals, meta status, flagged tools, builtin registries, mirror drift).

Named to avoid T044's ``test_spec_full.py``. Offline: docker-runtime tools
run through ``FakeExecutor`` only.
"""

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.types import ChildProfile, Rule, ToolCall
from harness_builder.errors import ConfigError, SpecError
from harness_builder.evals.models import TaskCase, ToolCase
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult, SandboxConfig
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.spec import loader
from harness_builder.spec.loader import (
    builder_from_spec,
    load_eval_cases,
    load_spec,
    register_builtin_hook,
    register_builtin_tool,
    register_spec_extension,
)
from harness_builder.spec.models import (
    ChildProfileSpec,
    RuleSpec,
    SandboxSpec,
    SkillsConfig,
)
from harness_builder.tools.container_tool import (
    TOOLS_SCHEMA_FILE,
    ContainerTool,
    ToolSchemaEntry,
    ToolSchemaFile,
    schema_key,
)
from harness_builder.tools.registry import Tool, tool

SETTINGS = Settings(model="fake/model")
TOOL_SRC = '''\
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b
'''
HOOK_SRC = '''\
from harness_builder.core.types import HookOutcome


def guard(call, ctx):
    """Allow everything."""
    return HookOutcome()
'''


@tool
def echo_tool(text: str) -> str:
    """Echo ``text``."""
    return text


def echo_factory() -> Tool:
    """Module-level builtin factory (re-registering it is a no-op)."""
    return echo_tool


def write_spec(root: Path, body: str) -> Path:
    """Write ``harness.yaml`` (schema_version 1, name t) with ``body``."""
    root.mkdir(parents=True, exist_ok=True)
    text = "schema_version: 1\nname: t\nmodel: {name: fake/model}\n" + body
    (root / "harness.yaml").write_text(text, encoding="utf-8")
    return root


def harness_with_tool(root: Path, runtime: str = "docker", extra: str = "") -> Path:
    """A harness dir with ``tools/math.py:add`` and its schema file."""
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "math.py").write_text(TOOL_SRC, encoding="utf-8")
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
        "tools:\n"
        f"  - {{module: tools/math.py, function: add, runtime: {runtime},\n"
        "     examples: [{a: 1, b: 2}]}\n"
        "permissions: {rules: [{tool: add, decision: allow}]}\n" + extra,
    )


def from_dir(root: Path, **kwargs: Any) -> HarnessBuilder:
    return HarnessBuilder.from_spec(root, settings=SETTINGS, **kwargs)


def build(builder: HarnessBuilder) -> Any:
    return builder.llm(FakeLLMClient([])).trace_dir(None).build()


# --- M1 back-compat ---------------------------------------------------------


def test_m1_spec_loads_unchanged(tmp_path: Path) -> None:
    root = write_spec(
        tmp_path,
        "strategy: {name: react}\nprompts: {system: hi}\n"
        "context: {policy: truncate, max_tokens: 100}\n"
        "budget: {max_steps: 3}\nsubagents: {max_depth: 1}\n",
    )
    harness = build(from_dir(root))
    assert harness.config.prompt_contributors == []
    assert harness.config.sandbox is None
    assert load_eval_cases(load_spec(root)[0], root) == []


# --- module tool paths ------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    ["../x.py", "/abs/x.py", "C:/x.py", "tools\\x.py", "tools/x.txt", "a/../b.py"],
)
def test_module_ref_traversal_is_a_located_spec_error(
    tmp_path: Path, module: str
) -> None:
    root = write_spec(
        tmp_path, f"tools: [{{module: {json.dumps(module)}, function: add}}]\n"
    )
    with pytest.raises(SpecError, match=r"harness\.yaml: tools\[0\]\.module: "):
        from_dir(root)


def test_missing_module_file_is_located(tmp_path: Path) -> None:
    root = write_spec(tmp_path, "tools: [{module: tools/none.py, function: add}]\n")
    with pytest.raises(SpecError, match=r"tools\[0\]\.module: tools/none\.py is not"):
        from_dir(root)


def test_bad_function_name_is_located(tmp_path: Path) -> None:
    root = write_spec(tmp_path, "tools: [{module: tools/x.py, function: 1-a}]\n")
    with pytest.raises(SpecError, match=r"tools\[0\]\.function: .*identifier"):
        from_dir(root)


def test_tool_ref_union_error_is_located_without_tags(tmp_path: Path) -> None:
    root = write_spec(tmp_path, "tools: [{module: tools/x.py}]\n")
    with pytest.raises(SpecError) as info:
        from_dir(root)
    assert str(info.value) == "harness.yaml: tools[0].function: Field required"


def test_module_link_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "math.py").write_text(TOOL_SRC, encoding="utf-8")
    root = write_spec(
        tmp_path / "h", "tools: [{module: tools/math.py, function: add}]\n"
    )
    link = root / "tools"
    if sys.platform == "win32":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
        )
        if made.returncode != 0:
            pytest.skip(f"cannot create a junction: {made.stderr!r}")
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SpecError, match="must resolve inside the harness directory"):
        from_dir(root)


# --- module tools: docker and host ------------------------------------------


@pytest.mark.anyio
async def test_docker_tool_is_a_container_tool_bound_to_the_executor(
    tmp_path: Path,
) -> None:
    root = harness_with_tool(tmp_path, extra="sandbox: {timeout_s: 7}\n")
    envelope = json.dumps({"ok": True, "result": 3})
    fake = FakeExecutor([ExecResult(exit_code=0, stdout=envelope)])
    script = [
        LLMResponse(tool_calls=[ToolCall(id="c1", name="add", args={"a": 1, "b": 2})]),
        LLMResponse(text="3"),
    ]
    harness = (
        from_dir(root).llm(FakeLLMClient(script)).executor(fake).trace_dir(None).build()
    )
    registered = harness.loop.registry.get("add")
    assert isinstance(registered, ContainerTool)
    assert registered.exec_timeout_s == 7
    assert fake.preflight_calls == 1
    result = await harness.run("add")
    assert result.stop.reason == "done"
    (request,) = fake.requests
    assert request.entry == "call"
    assert request.entry_args == ("/tools/tools/math.py", "add")
    assert json.loads(request.stdin or "") == {"a": 1, "b": 2}
    assert harness.config.sandbox is not None
    assert harness.config.sandbox["timeout_s"] == 7


def test_docker_tool_without_schema_file_is_a_config_error(tmp_path: Path) -> None:
    root = harness_with_tool(tmp_path)
    (root / TOOLS_SCHEMA_FILE).unlink()
    with pytest.raises(ConfigError, match=r"tools\[0\]: .*harness validate"):
        from_dir(root)


def test_docker_tool_missing_from_schema_file_is_a_config_error(
    tmp_path: Path,
) -> None:
    root = harness_with_tool(tmp_path)
    (root / TOOLS_SCHEMA_FILE).write_text('{"tools": {}}', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"tools\[0\]: no schema for"):
        from_dir(root)


def test_host_tool_needs_trust(tmp_path: Path) -> None:
    root = harness_with_tool(tmp_path, runtime="host")
    with pytest.raises(ConfigError, match=r"tools\[0\]\.runtime: .*--trust-tools"):
        from_dir(root)


@pytest.mark.anyio
async def test_trusted_host_tool_runs_in_process(tmp_path: Path) -> None:
    root = harness_with_tool(tmp_path, runtime="host")
    script = [
        LLMResponse(tool_calls=[ToolCall(id="c1", name="add", args={"a": 2, "b": 3})]),
        LLMResponse(text="5"),
    ]
    harness = (
        from_dir(root, trust_host_tools=True)
        .llm(FakeLLMClient(script))
        .trace_dir(None)
        .build()
    )
    result, trace = await harness.run_traced("add")
    assert result.stop.reason == "done"
    observations = [
        event
        for step in trace.steps
        for event in step.events
        if event.kind == "tool_result"
    ]
    assert observations[0].observation.content == "5"


def test_duplicate_tool_names_across_kinds_are_located(tmp_path: Path) -> None:
    register_builtin_tool("echo_tool", echo_factory)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "e.py").write_text(
        "def echo_tool(text: str) -> str:\n    return text\n", encoding="utf-8"
    )
    root = write_spec(
        tmp_path,
        "tools:\n  - {builtin: echo_tool}\n"
        "  - {module: tools/e.py, function: echo_tool}\n",
    )
    with pytest.raises(SpecError, match=r"tools\[1\]\.function: duplicate tool"):
        from_dir(root)


def test_snapshot_with_module_tools_needs_its_harness_dir(tmp_path: Path) -> None:
    root = harness_with_tool(tmp_path)
    original = build(from_dir(root).executor(FakeExecutor()))
    snapshot = original.spec_snapshot
    assert snapshot is not None
    with pytest.raises(SpecError, match="file references need a spec file"):
        HarnessBuilder.from_spec(snapshot, settings=SETTINGS)
    rebuilt = build(
        HarnessBuilder.from_spec(snapshot, settings=SETTINGS, base_dir=root).executor(
            FakeExecutor()
        )
    )
    assert rebuilt.config_hash == original.config_hash


# --- hooks ------------------------------------------------------------------


def test_module_hook_needs_trust(tmp_path: Path) -> None:
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "h.py").write_text(HOOK_SRC, encoding="utf-8")
    body = "hooks: [{module: hooks/h.py, function: guard, event: pre_tool}]\n"
    root = write_spec(tmp_path, body)
    with pytest.raises(ConfigError, match=r"hooks\[0\]\.module: .*--trust-tools"):
        from_dir(root)
    harness = build(from_dir(root, trust_host_tools=True))
    assert harness.config.hooks == {"pre_tool": ["hooks/h.py:guard"]}


def test_module_hook_missing_function_is_located(tmp_path: Path) -> None:
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "h.py").write_text(HOOK_SRC, encoding="utf-8")
    body = "hooks: [{module: hooks/h.py, function: nope, event: on_stop}]\n"
    root = write_spec(tmp_path, body)
    with pytest.raises(SpecError, match=r"hooks\[0\]\.function: .*'nope'"):
        from_dir(root, trust_host_tools=True)


def _allow_hook(call: ToolCall, ctx: Any) -> None:
    return None


def allow_hook_factory() -> Any:
    """Builtin hook factory used by the registration tests."""
    return _allow_hook


def other_hook_factory() -> Any:
    """A different factory for the replace tests."""
    return _allow_hook


def test_builtin_hook_by_registered_name(tmp_path: Path) -> None:
    register_builtin_hook("t040_allow", allow_hook_factory)
    register_builtin_hook("t040_allow", allow_hook_factory)  # identical: no-op
    with pytest.raises(ValueError, match="already registered"):
        register_builtin_hook("t040_allow", other_hook_factory)
    register_builtin_hook("t040_allow", other_hook_factory, replace=True)
    register_builtin_hook("t040_allow", allow_hook_factory, replace=True)
    root = write_spec(
        tmp_path, "hooks: [{builtin: t040_allow, event: pre_tool, priority: 3}]\n"
    )
    harness = build(from_dir(root))
    assert harness.config.hooks["pre_tool"] == [f"{__name__}._allow_hook"]


def test_builtin_registries_are_read_only_and_guard_implied_names() -> None:
    with pytest.raises(TypeError):
        loader.BUILTIN_TOOLS["x"] = echo_factory  # type: ignore[index]
    with pytest.raises(ValueError, match="implied"):
        register_builtin_tool("deref", echo_factory)
    register_builtin_tool("echo_tool", echo_factory)
    assert loader.BUILTIN_TOOLS["echo_tool"] is echo_factory


# --- sandbox ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ("sandbox: {state_mode: session}\n", "sandbox.state_mode"),
        ("sandbox: {image: evil:latest}\n", "sandbox.image"),
        ("sandbox: {timeout_s: 0}\n", "sandbox.timeout_s"),
        ("sandbox: {memory_mb: 999999}\n", "sandbox.memory_mb"),
    ],
)
def test_sandbox_spec_errors_are_located(tmp_path: Path, body: str, where: str) -> None:
    root = write_spec(tmp_path, body)
    with pytest.raises(SpecError, match=rf"harness\.yaml: {where}: "):
        from_dir(root)


def test_spec_sandbox_equals_code_sandbox(tmp_path: Path) -> None:
    root = write_spec(tmp_path, "sandbox: {timeout_s: 12, network: true}\n")
    spec_h = build(from_dir(root))
    code = HarnessBuilder("t", SETTINGS).model("fake/model")
    code_h = build(code.sandbox(SandboxConfig(timeout_s=12, network=True)))
    assert spec_h.config == code_h.config
    assert spec_h.config_hash == code_h.config_hash
    plain = build(HarnessBuilder("t", SETTINGS).model("fake/model"))
    assert plain.config_hash != code_h.config_hash


# --- skills -----------------------------------------------------------------


class Recorded:
    """Captures installer calls."""

    def __init__(self) -> None:
        self.calls: list[SkillsConfig] = []

    def __call__(self, section: SkillsConfig, builder: Any) -> None:
        self.calls.append(section)


@pytest.fixture
def installer() -> Any:
    recorded = Recorded()
    register_spec_extension("skills", recorded, replace=True)
    yield recorded
    register_spec_extension(
        "skills", "harness_builder.skills.extension:install_skills", replace=True
    )


def test_skills_installer_gets_a_resolved_root(tmp_path: Path, installer: Any) -> None:
    (tmp_path / "lib").mkdir()
    root = write_spec(tmp_path, "skills: {root: lib, top_k: 2}\n")
    from_dir(root)
    (section,) = installer.calls
    assert section.top_k == 2
    assert Path(section.root) == (tmp_path / "lib").resolve()


def test_disabled_skills_install_nothing(tmp_path: Path, installer: Any) -> None:
    root = write_spec(tmp_path, "skills: {enabled: false}\n")
    harness = build(from_dir(root))
    assert installer.calls == []
    assert harness.config.prompt_contributors == []


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ("skills: {root: ../lib}\n", "skills.root"),
        ("skills: {root: missing}\n", "skills.root"),
        ("skills: {include_bundled: true}\n", "skills.include_bundled"),
        ("skills: {top_k: 0}\n", "skills.top_k"),
    ],
)
def test_skills_spec_errors_are_located(
    tmp_path: Path, installer: Any, body: str, where: str
) -> None:
    root = write_spec(tmp_path, body)
    with pytest.raises(SpecError, match=rf"harness\.yaml: {where}: "):
        from_dir(root)


def test_unavailable_skills_extension_is_a_config_error(tmp_path: Path) -> None:
    register_spec_extension("skills", "no_such_module_t040:install", replace=True)
    try:
        root = write_spec(tmp_path, "skills: {}\n")
        with pytest.raises(ConfigError, match="skills extension is not available"):
            from_dir(root)
    finally:
        register_spec_extension(
            "skills", "harness_builder.skills.extension:install_skills", replace=True
        )


def test_extension_sections_are_closed() -> None:
    with pytest.raises(ValueError, match="not an extension section"):
        register_spec_extension("tools", Recorded())


# --- evals ------------------------------------------------------------------

CASES = (
    "- {kind: tool, id: add, tool: add, args: {a: 1, b: 2},"
    " check: {type: equals, value: 3}}\n"
    "- {kind: task, id: sum, input: 'What is 1+2?', check: {type: contains,"
    " text: '3'}}\n"
)


def test_inline_eval_cases_are_validated_with_locations(tmp_path: Path) -> None:
    body = "evals:\n  - {kind: tool, id: a, tool: add, check: {type: nope}}\n"
    root = write_spec(tmp_path, body)
    with pytest.raises(SpecError, match=r"harness\.yaml: evals\[0\]\.check: "):
        from_dir(root)
    body = "evals: [{kind: tool, id: a, tool: add, check: {type: contains}}]\n"
    write_spec(tmp_path, body)
    with pytest.raises(SpecError, match=r"evals\[0\]\.check\.text: Field required"):
        from_dir(root)


def test_disabled_skills_still_check_their_root(tmp_path: Path) -> None:
    root = write_spec(tmp_path, "skills: {enabled: false, root: ../x}\n")
    with pytest.raises(SpecError, match=r"skills\.root: must be a relative"):
        from_dir(root)


def test_eval_file_is_loaded_and_confined(tmp_path: Path) -> None:
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "cases.yaml").write_text(CASES, encoding="utf-8")
    root = write_spec(tmp_path, "evals: {file: evals/cases.yaml}\n")
    spec, base_dir, source = load_spec(root)
    cases = load_eval_cases(spec, base_dir, source)
    assert [type(c) for c in cases] == [ToolCase, TaskCase]
    write_spec(tmp_path, "evals: {file: ../cases.yaml}\n")
    with pytest.raises(SpecError, match=r"evals\.file: must be a relative path"):
        from_dir(root)
    write_spec(tmp_path, "evals: {file: evals/missing.yaml}\n")
    with pytest.raises(SpecError, match=r"evals\.file: .*is not a file"):
        from_dir(root)


def test_eval_file_errors_are_located_in_the_file(tmp_path: Path) -> None:
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "cases.yaml").write_text(
        "- {kind: task, id: x, check: {type: contains, text: a}}\n",
        encoding="utf-8",
    )
    root = write_spec(tmp_path, "evals: {file: evals/cases.yaml}\n")
    with pytest.raises(SpecError, match=r"cases\.yaml: \[0\]\.input: Field required"):
        from_dir(root)


# --- .harness-meta.json -----------------------------------------------------


@pytest.mark.parametrize(
    ("meta", "allowed"),
    [
        (None, True),
        ('{"status": "pass"}', True),
        ('{"status": "incomplete"}', True),
        ('{"status": "fail"}', False),
        ('{"status": "weird"}', False),
        ("not json", False),
    ],
)
def test_meta_status_gates_builder_from_spec(
    tmp_path: Path, meta: str | None, allowed: bool
) -> None:
    root = write_spec(tmp_path, "")
    if meta is not None:
        (root / loader.META_FILE).write_text(meta, encoding="utf-8")
    spec, base_dir, source = load_spec(root)
    builder = HarnessBuilder(spec.name, SETTINGS)
    if allowed:
        builder_from_spec(spec, builder=builder, base_dir=base_dir, source=source)
    else:
        with pytest.raises(ConfigError, match=r"\.harness-meta\.json"):
            builder_from_spec(spec, builder=builder, base_dir=base_dir, source=source)
    permissive = HarnessBuilder(spec.name, SETTINGS, allow_failed=True)
    builder_from_spec(spec, builder=permissive, base_dir=base_dir, source=source)


# --- flagged tools ----------------------------------------------------------


def test_flagged_tools_get_prepended_ask_rules(tmp_path: Path) -> None:
    root = harness_with_tool(tmp_path)
    harness = build(
        from_dir(root, flagged_tools=["add", "add"]).executor(FakeExecutor())
    )
    rules = harness.config.permissions["rules"]
    assert rules[0] == {
        "tool": "add",
        "decision": "ask",
        "args": None,
        "rule_id": "safety:add",
    }
    assert [r["rule_id"] for r in rules].count("safety:add") == 1
    decision = harness.loop.permissions.decide(ToolCall(id="c", name="add"))
    assert decision.decision == "ask"


# --- mirror drift (T024 MJ2) ------------------------------------------------


def _pydantic_defaults(model: Any, skip: frozenset[str] = frozenset()) -> dict:
    return {
        name: info.get_default(call_default_factory=True)
        for name, info in model.model_fields.items()
        if name not in skip
    }


def test_rule_spec_matches_rule() -> None:
    rule_fields = {f.name: f for f in dataclasses.fields(Rule)}
    assert list(RuleSpec.model_fields) == list(rule_fields)
    for name, info in RuleSpec.model_fields.items():
        field = rule_fields[name]
        expected = (
            info.get_default() if info.is_required() is False else dataclasses.MISSING
        )
        assert field.default == expected, name
        assert info.annotation == field.type, name


def test_child_profile_spec_matches_child_profile() -> None:
    assert list(ChildProfileSpec.model_fields) == list(ChildProfile.model_fields)
    assert _pydantic_defaults(ChildProfileSpec) == _pydantic_defaults(ChildProfile)
    for name, info in ChildProfileSpec.model_fields.items():
        if name == "permissions":
            continue  # list[RuleSpec] mirrors list[Rule]
        assert info.annotation == ChildProfile.model_fields[name].annotation, name
        assert info.metadata == ChildProfile.model_fields[name].metadata, name


def test_sandbox_spec_matches_sandbox_config() -> None:
    skip = frozenset({"image"})
    config_fields = [n for n in SandboxConfig.model_fields if n not in skip]
    assert list(SandboxSpec.model_fields) == config_fields
    assert _pydantic_defaults(SandboxSpec) == _pydantic_defaults(SandboxConfig, skip)
    SandboxConfig(**SandboxSpec().model_dump())  # every spec value is valid
