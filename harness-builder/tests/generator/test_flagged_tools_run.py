"""T046 B2 / MJ4: safety-flagged tools are gated at ``harness run``/``eval``.

The loader re-screens every referenced tool module when it builds a harness,
so the FR-032 ``ask`` rule exists without any validation report. The CLI's
default approver (no TTY) denies, so a flagged tool is never executed
silently. Offline: Docker is emulated in-process, and every test is sync
because the commands call ``anyio.run`` themselves.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from harness_builder.cli import main as cli_main
from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import LLMClient
from harness_builder.core.types import ToolCall
from harness_builder.executor.models import ExecRequest
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.spec.loader import (
    builder_from_spec,
    flagged_tool_names,
    load_spec,
)

from .support import FileSet, ScriptedGenerator, fixture_files  # pyrefly: ignore[missing-import]

ENV: dict[str, str | None] = {
    "OPENROUTER_API_KEY": "",
    "HARNESS_MODEL": "test/model",
    "COLUMNS": "200",
}
CALL = ToolCall(
    id="p",
    name="convert_length",
    args={"value": 2, "from_unit": "km", "to_unit": "m"},
)
Invoke = Callable[..., Result]


@pytest.fixture
def invoke(hermetic_cli: Path) -> Invoke:
    """Invoke the ``harness`` group in the hermetic cwd with ``ENV``."""
    runner = CliRunner()

    def _invoke(*args: str | Path) -> Result:
        return runner.invoke(cli_main.main, [str(a) for a in args], env=ENV)

    return _invoke


def flagged_files() -> FileSet:
    """The unit_conversion fixture with a subprocess import in its module."""
    files = fixture_files("unit_conversion")
    files["tools/units.py"] = "import subprocess  # noqa: F401\n" + files[
        "tools/units.py"
    ].replace('"""Unit conversion tools: lengths and temperatures."""\n', "", 1)
    return files


def generate(
    invoke: Invoke, monkeypatch: pytest.MonkeyPatch, out: Path, files: FileSet
) -> None:
    """``harness new`` (emulated Docker) must pass; flagged tools validate."""
    generator = ScriptedGenerator(files)

    def factory(settings: Settings) -> LLMClient:
        return generator.client

    monkeypatch.setattr(cli_main, "LLM_FACTORY", factory)
    created = invoke("new", "Build it", "-o", out, "--non-interactive")
    assert created.exit_code == 0, created.output


def probing(settings: Settings) -> LLMClient:
    """An LLM that calls ``CALL`` once, then echoes the observation."""

    def script(req: LLMRequest) -> LLMResponse:
        if req.messages[-1].role == "user":
            return LLMResponse(tool_calls=[CALL])
        return LLMResponse(text=f"result: {req.messages[-1].content}")

    return FakeLLMClient(script)


def calls(requests: list[ExecRequest]) -> list[ExecRequest]:
    """The sandbox ``call`` requests (tool executions) among ``requests``."""
    return [r for r in requests if r.entry == "call"]


def test_run_denies_a_flagged_tool_even_without_reports(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    emulated_docker: list[ExecRequest],
) -> None:
    out = tmp_path / "units"
    generate(invoke, monkeypatch, out, flagged_files())
    shutil.rmtree(out / "reports")
    emulated_docker.clear()
    monkeypatch.setattr(cli_main, "LLM_FACTORY", probing)
    result = invoke("run", out, "convert 2 km")
    assert "permission_denied" in result.output, result.output
    assert calls(emulated_docker) == []


def test_eval_does_not_silently_approve_a_flagged_tool(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    emulated_docker: list[ExecRequest],
) -> None:
    """Eval uses the default approver (prompt on a TTY, deny without one)."""
    out = tmp_path / "units"
    generate(invoke, monkeypatch, out, flagged_files())
    shutil.rmtree(out / "reports")
    emulated_docker.clear()
    result = invoke("eval", out)
    assert result.exit_code == 1, result.output
    assert "permission_denied" in result.output
    assert calls(emulated_docker) == []


def test_clean_harness_still_runs_and_evals(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    emulated_docker: list[ExecRequest],
) -> None:
    out = tmp_path / "units"
    generate(invoke, monkeypatch, out, fixture_files("unit_conversion"))
    monkeypatch.setattr(cli_main, "LLM_FACTORY", probing)
    assert invoke("run", out, "convert 2 km").exit_code == 0
    assert invoke("eval", out).exit_code == 0
    assert calls(emulated_docker)


def write_harness(root: Path, files: FileSet) -> Path:
    """Write ``files`` under ``root`` and return the spec directory."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def build_config(root: Path) -> tuple[list[str], str]:
    """``(safety rule ids, config_hash)`` of the spec at ``root``, host-built."""
    spec, base_dir, source = load_spec(root)
    builder = builder_from_spec(
        spec,
        builder=HarnessBuilder(
            spec.name, Settings(model="test/model"), trust_host_tools=True
        ),
        base_dir=base_dir,
        source=source,
    )
    harness = builder.llm(FakeLLMClient([])).build()
    rules = harness.config.permissions["rules"]
    ids = [
        r["rule_id"] for r in rules if str(r.get("rule_id", "")).startswith("safety:")
    ]
    return ids, harness.config_hash


def test_loader_flags_modules_and_changes_config_hash(tmp_path: Path) -> None:
    clean = write_harness(tmp_path / "clean", host_files("clean"))
    risky = write_harness(tmp_path / "risky", host_files("risky"))
    clean_ids, clean_hash = build_config(clean)
    risky_ids, risky_hash = build_config(risky)
    assert clean_ids == []
    assert risky_ids == ["safety:convert_length", "safety:convert_temperature"]
    assert clean_hash != risky_hash


def test_unparsable_module_flags_every_tool_of_it(tmp_path: Path) -> None:
    root = write_harness(tmp_path / "broken", host_files("clean"))
    (root / "tools" / "units.py").write_text("def broken(:\n", encoding="utf-8")
    spec, base_dir, source = load_spec(root)
    assert flagged_tool_names(spec, base_dir, source) == [
        "convert_length",
        "convert_temperature",
    ]


ALL_TOOLS = ["convert_length", "convert_temperature"]
RISKY_HELPER = "import socket  # noqa: F401\nimport subprocess  # noqa: F401\n"
SYS_PATH_LOADER = (
    "import sys\nfrom pathlib import Path\n\n"
    "sys.path.insert(0, str(Path(__file__).parent))\n"
    "import helper  # noqa: E402, F401\n"
)
SPEC_LOADER = (
    "import importlib.util\nfrom pathlib import Path\n\n"
    "_spec = importlib.util.spec_from_file_location(\n"
    '    "helper", Path(__file__).parent / "helper.py"\n'
    ")\n"
    "_mod = importlib.util.module_from_spec(_spec)\n"
    "_spec.loader.exec_module(_mod)\n"
)


def flagged_after(root: Path) -> list[str]:
    """``flagged_tool_names`` of the harness at ``root``."""
    spec, base_dir, source = load_spec(root)
    return flagged_tool_names(spec, base_dir, source)


def with_loader(tmp_path: Path, loader: str, helper: str) -> Path:
    """Host-runtime unit_conversion whose module loads an unnamed helper."""
    files = host_files("clean")
    files["tools/units.py"] = loader + files["tools/units.py"]
    files["tools/helper.py"] = helper
    return write_harness(tmp_path / "split", files)


@pytest.mark.parametrize("loader", [SYS_PATH_LOADER, SPEC_LOADER])
def test_risky_helper_loaded_by_a_clean_module_flags_every_tool(
    tmp_path: Path, loader: str
) -> None:
    """T046 MJ5: the helper is not in the spec but is still screened."""
    assert flagged_after(with_loader(tmp_path, loader, RISKY_HELPER)) == ALL_TOOLS


def test_risky_helper_flags_even_without_a_loader(tmp_path: Path) -> None:
    root = write_harness(tmp_path / "h", host_files("clean"))
    (root / "helper.py").write_text(RISKY_HELPER, encoding="utf-8")
    assert flagged_after(root) == ALL_TOOLS


def test_unparsable_helper_fails_closed(tmp_path: Path) -> None:
    root = write_harness(tmp_path / "h", host_files("clean"))
    (root / "tools" / "helper.py").write_text("def broken(:\n", encoding="utf-8")
    assert flagged_after(root) == ALL_TOOLS


def test_clean_helper_flags_nothing(tmp_path: Path) -> None:
    root = write_harness(tmp_path / "h", host_files("clean"))
    (root / "tools" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert flagged_after(root) == []


def test_non_python_and_out_of_scope_files_are_ignored(tmp_path: Path) -> None:
    root = write_harness(tmp_path / "h", host_files("clean"))
    (root / "tools" / "notes.txt").write_text("import socket\n", encoding="utf-8")
    for rel in ("tests/test_x.py", "reports/r.py", "skills/s/run.py"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RISKY_HELPER, encoding="utf-8")
    assert flagged_after(root) == []


@pytest.mark.parametrize(
    "rel",
    [
        "tools/tests/helper.py",
        "tools/reports/helper.py",
        "tools/__pycache__/helper.py",
        "tools/.hidden/helper.py",
        "tools/sub/tests/helper.py",
    ],
)
def test_nested_excluded_looking_dirs_under_tools_are_screened(
    tmp_path: Path, rel: str
) -> None:
    """T046 MJ5a: exclusions apply to the top-level directory only."""
    root = write_harness(tmp_path / "h", host_files("clean"))
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(RISKY_HELPER, encoding="utf-8")
    assert flagged_after(root) == ALL_TOOLS


TXT_SUFFIX_LOADER = (
    "import importlib.machinery as m\nimport sys\nfrom pathlib import Path\n\n"
    'm.SOURCE_SUFFIXES.append(".txt")\nsys.path_importer_cache.clear()\n'
    "sys.path.insert(0, str(Path(__file__).parent))\n"
    "import helper  # noqa: E402, F401\n"
)


def test_txt_suffix_import_trick_flags_every_tool(tmp_path: Path) -> None:
    """T046 MJ5b: the loader itself is flagged, so ``helper.txt`` cannot hide."""
    root = write_harness(
        tmp_path / "h", {**host_files("clean"), "tools/helper.txt": RISKY_HELPER}
    )
    units = root / "tools" / "units.py"
    units.write_text(TXT_SUFFIX_LOADER + units.read_text("utf-8"), encoding="utf-8")
    assert flagged_after(root) == ALL_TOOLS


def test_loader_flags_the_helper_through_the_run_time_rules(
    tmp_path: Path,
) -> None:
    root = with_loader(tmp_path, SYS_PATH_LOADER, RISKY_HELPER)
    ids, _ = build_config(root)
    assert ids == [f"safety:{name}" for name in ALL_TOOLS]


def host_files(kind: str) -> FileSet:
    """Fixture files with host-runtime tools; ``risky`` imports ``socket``."""
    files = fixture_files("unit_conversion")
    files["harness.yaml"] = files["harness.yaml"].replace(
        "function: ", "runtime: host\n    function: "
    )
    if kind == "risky":
        files["tools/units.py"] = "import socket  # noqa: F401\n" + files[
            "tools/units.py"
        ].replace('"""Unit conversion tools: lengths and temperatures."""\n', "", 1)
    return files
