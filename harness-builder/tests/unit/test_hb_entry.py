"""hb_entry tests on the host (T031, FR-045): FINAL channel, modes, stdlib-only.

Complements ``test_sandbox_image.py`` (T026). The last test runs the real
entry script as a stand-in for the container, behind a ``FakeDockerCli`` that
maps ``/work`` to the host workdir, so the code and FINAL file names that
``DockerExecutor`` passes in the argv are proven to match what it reads back.
"""

import ast
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest

from harness_builder.executor.docker import DockerExecutor, read_final
from harness_builder.executor.docker_cli import CliResult, FakeDockerCli, FakeResponse
from harness_builder.executor.image import hb_entry
from harness_builder.executor.models import ExecRequest

ENTRY_SCRIPT = Path(hb_entry.__file__)


@pytest.fixture
def isolated_sys(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_python`` rewrites ``sys.argv`` and ``sys.path[0]``; restore them."""
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))


def stage(tmp_path: Path, code: str, name: str = "code.py") -> tuple[Path, Path]:
    """Write ``code`` under ``<tmp_path>/.hb`` and return (code, final) paths."""
    hb_dir = tmp_path / ".hb"
    hb_dir.mkdir(exist_ok=True)
    code_path = hb_dir / name
    code_path.write_text(code, encoding="utf-8")
    return code_path, hb_dir / "final.json"


def final_of(path: Path) -> Any:
    """The JSON value stored in a FINAL file."""
    return json.loads(path.read_text(encoding="utf-8"))


# --- FINAL path and value -------------------------------------------------------


def test_final_path_default_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(hb_entry.FINAL_PATH_ENV, raising=False)
    assert hb_entry.resolve_final_path() == Path("/work/.hb/final.json")
    monkeypatch.setenv(hb_entry.FINAL_PATH_ENV, "")
    assert hb_entry.resolve_final_path() == hb_entry.DEFAULT_FINAL_PATH
    monkeypatch.setenv(hb_entry.FINAL_PATH_ENV, str(tmp_path / "f.json"))
    assert hb_entry.resolve_final_path() == tmp_path / "f.json"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [(42, "42"), (None, "None"), ([1, "a"], "[1, 'a']"), ("x", "x"), (1.5, "1.5")],
)
def test_write_final_stores_str_of_answer(
    tmp_path: Path, answer: object, expected: str
) -> None:
    path = tmp_path / "deep" / "dir" / "final.json"
    hb_entry.write_final(answer, path)
    assert final_of(path) == expected


@pytest.mark.parametrize("answer", ["héllo wörld ✓", 'quote " and \\ slash', ""])
def test_final_round_trips_to_the_executor_reader(tmp_path: Path, answer: str) -> None:
    """Host half of the FINAL channel: hb_entry writes, DockerExecutor reads."""
    path = tmp_path / "final.json"
    hb_entry.write_final(answer, path)
    assert read_final(path, 65536) == answer


def test_final_signal_is_not_an_exception(tmp_path: Path) -> None:
    final = hb_entry.make_final(tmp_path / "f.json")
    with pytest.raises(BaseException) as info:
        final("x")
    assert not isinstance(info.value, Exception)
    assert final_of(tmp_path / "f.json") == "x"


# --- python runner ---------------------------------------------------------------


@pytest.mark.usefixtures("isolated_sys")
def test_python_runs_as_main_with_argv_and_file(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "if __name__ == '__main__':\n"
        "    FINAL(f'{__file__}|{sys.argv[0]}')\n"
    )
    code_path, final_path = stage(tmp_path, code)
    assert hb_entry.run_file("python", code_path, final_path) == 0
    assert final_of(final_path) == f"{code_path}|{code_path}"


@pytest.mark.usefixtures("isolated_sys")
def test_modules_in_cwd_are_importable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hb_t031_helper.py").write_text("VALUE = 'from /work'\n")
    monkeypatch.delitem(sys.modules, "hb_t031_helper", raising=False)
    code_path, final_path = stage(
        tmp_path, "import hb_t031_helper\nFINAL(hb_t031_helper.VALUE)\n"
    )
    try:
        assert hb_entry.run_file("python", code_path, final_path) == 0
    finally:
        sys.modules.pop("hb_t031_helper", None)
    assert final_of(final_path) == "from /work"


@pytest.mark.usefixtures("isolated_sys")
def test_first_final_wins_and_stops_the_code(tmp_path: Path) -> None:
    code = "def f():\n    FINAL('first')\nf()\nFINAL('second')\n"
    code_path, final_path = stage(tmp_path, code)
    assert hb_entry.run_file("python", code_path, final_path) == 0
    assert final_of(final_path) == "first"


@pytest.mark.usefixtures("isolated_sys")
def test_system_exit_propagates_in_process(tmp_path: Path) -> None:
    code_path, final_path = stage(tmp_path, "raise SystemExit(4)\n")
    with pytest.raises(SystemExit) as info:
        hb_entry.run_file("python", code_path, final_path)
    assert info.value.code == 4


@pytest.mark.usefixtures("isolated_sys")
def test_syntax_error_exits_one_with_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code_path, final_path = stage(tmp_path, "def broken(:\n")
    assert hb_entry.run_file("python", code_path, final_path) == 1
    assert "SyntaxError" in capsys.readouterr().err


@pytest.mark.usefixtures("isolated_sys")
def test_stdout_goes_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code_path, final_path = stage(tmp_path, "print('out')\n")
    assert hb_entry.run_file("python", code_path, final_path) == 0
    assert capsys.readouterr().out == "out\n"
    assert not final_path.exists()


# --- bash runner (no real bash: subprocess.run is replaced) ------------------------


@pytest.mark.parametrize("stale", [True, False])
def test_bash_runner_passes_final_path_and_clears_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    code_path, final_path = stage(tmp_path, "hb-final x; exit 0\n", "code.sh")
    if stale:
        final_path.write_text('"old"')
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        seen["final_existed"] = final_path.exists()
        return subprocess.CompletedProcess(argv, 5)

    monkeypatch.setattr(hb_entry.subprocess, "run", fake_run)
    monkeypatch.setenv("KEEP_ME", "1")
    assert hb_entry.run_file("bash", code_path, final_path) == 5
    assert seen["argv"] == ["bash", str(code_path)]
    assert seen["env"][hb_entry.FINAL_PATH_ENV] == str(final_path)
    assert seen["env"]["KEEP_ME"] == "1"
    assert seen["final_existed"] is False


# --- modes -------------------------------------------------------------------------


def test_modes_and_runners() -> None:
    assert set(hb_entry.MODES) == {"run", "final"}
    assert set(hb_entry.RUNNERS) == {"python", "bash"}


@pytest.mark.parametrize(
    ("args", "expected"),
    [([], ""), (["one"], "one"), (["a", "b", "c d"], "a b c d")],
)
def test_final_mode_joins_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], expected: str
) -> None:
    path = tmp_path / "final.json"
    monkeypatch.setenv(hb_entry.FINAL_PATH_ENV, str(path))
    assert hb_entry.main(["final", *args]) == 0
    assert final_of(path) == expected


def test_main_defaults_to_sys_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["hb_entry.py", "bogus"])
    assert hb_entry.main() == 2
    err = capsys.readouterr().err
    assert "missing or unknown mode" in err and "usage:" in err


def test_run_mode_with_extra_args_is_usage_error(tmp_path: Path) -> None:
    assert hb_entry.main(["run", "python", "a.py", "extra"]) == 2


# --- standalone script contract ------------------------------------------------------


def test_entry_script_imports_only_the_standard_library() -> None:
    tree = ast.parse(ENTRY_SCRIPT.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports break the standalone script"
            modules.add((node.module or "").split(".")[0])
    assert "harness_builder" not in modules
    assert modules <= set(sys.stdlib_module_names)


def test_standalone_usage_error_exit_code(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(ENTRY_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 2
    assert "usage:" in proc.stderr


def test_standalone_final_mode_writes_env_path(tmp_path: Path) -> None:
    final_path = tmp_path / "out.json"
    proc = subprocess.run(
        [sys.executable, str(ENTRY_SCRIPT), "final", "the", "answer"],
        env={**os.environ, hb_entry.FINAL_PATH_ENV: str(final_path)},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    assert final_of(final_path) == "the answer"


def test_standalone_uncaught_exception_exit_one(tmp_path: Path) -> None:
    code_path, final_path = stage(tmp_path, "raise KeyError('k')\n")
    proc = subprocess.run(
        [sys.executable, str(ENTRY_SCRIPT), "run", "python", str(code_path)],
        env={**os.environ, hb_entry.FINAL_PATH_ENV: str(final_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 1
    assert "KeyError" in proc.stderr
    assert not final_path.exists()


# --- end to end: executor argv -> entry script -> FINAL read back ---------------------


@dataclass
class HostContainer(FakeDockerCli):
    """Fake CLI that runs the create command's entry script on the host.

    ``/work`` in the container argv is mapped to ``workdir``; ``HB_FINAL_PATH``
    and the code path come from the recorded ``docker create`` argv.
    """

    workdir: Path = Path()

    def _host(self, container_path: str) -> str:
        assert container_path.startswith("/work/")
        return str(self.workdir / container_path.removeprefix("/work/"))

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        """On ``start``, run hb_entry with the create argv mapped to the host."""
        if not args or args[0] != "start":
            return await super().run(
                args, timeout_s=timeout_s, cap_bytes=cap_bytes, stdin=stdin
            )
        self.calls.append(list(args))
        self.stdins.append(stdin)
        create = next(c for c in self.calls if c[0] == "create")
        env = dict(
            create[i + 1].split("=", 1) for i, arg in enumerate(create) if arg == "-e"
        )
        entry = create.index("/opt/hb/hb_entry.py")
        mode, language, code = create[entry + 1 : entry + 4]
        host_env = {
            **os.environ,
            hb_entry.FINAL_PATH_ENV: self._host(env[hb_entry.FINAL_PATH_ENV]),
        }
        proc = await anyio.run_process(
            [sys.executable, str(ENTRY_SCRIPT), mode, language, self._host(code)],
            env=host_env,
            cwd=self.workdir,
            check=False,
        )
        return CliResult(
            exit_code=proc.returncode,
            stdout=proc.stdout.decode(),
            stderr=proc.stderr.decode(),
        )


@pytest.mark.anyio
async def test_final_channel_end_to_end_through_the_executor(tmp_path: Path) -> None:
    cli = HostContainer(workdir=tmp_path)
    cli.script(["inspect"], FakeResponse(exit_code=1))  # fall back to start exit
    executor = DockerExecutor(cli=cli, image="i:1", user="10001")
    req = ExecRequest(
        language="python",
        code="print('hi')\nFINAL({'n': 7})\n",
        workdir=tmp_path,
        run_id="e2e",
    )
    result = await executor.run(req)
    assert result.exit_code == 0
    assert result.stdout.replace("\r\n", "\n") == "hi\n"
    assert result.final_set and result.final_value == "{'n': 7}"
    assert list((tmp_path / ".hb").iterdir()) == []


@pytest.mark.anyio
async def test_no_final_end_to_end_reports_exit_code(tmp_path: Path) -> None:
    cli = HostContainer(workdir=tmp_path)
    cli.script(["inspect"], FakeResponse(exit_code=1))
    executor = DockerExecutor(cli=cli, image="i:1", user="10001")
    req = ExecRequest(
        language="python", code="raise ValueError('no')\n", workdir=tmp_path
    )
    result = await executor.run(req)
    assert result.exit_code == 1 and not result.final_set
    assert "ValueError: no" in result.stderr
