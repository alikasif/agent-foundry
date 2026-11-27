"""The command line. No Docker, no model, no real ``.env``, no ``~/.sih``."""

import json
import os
import runpy
from pathlib import Path

import pytest

from harness import cli
from harness.sandbox.container import SandboxError

KEY = "sk-or-test-0123456789"


class Calls:
    """What ``main`` asked of Docker, the model and the chat."""

    def __init__(self) -> None:
        self.done: list[str] = []
        self.dotenv: list[Path] = []
        self.session: dict = {}


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Calls:
    found = Calls()
    monkeypatch.setattr(cli, "PROJECT", tmp_path / "harness project")
    monkeypatch.setattr(cli, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(cli, "load_dotenv", found.dotenv.append)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    def ensure_image(image_dir: Path) -> str:
        found.done.append("ensure_image")
        return "img"

    def open_session(llm: object, source: Path, **options: object) -> str:
        found.done.append("open_session")
        found.session = {"source": source, **options}
        return "context"

    def run_chat(make_session, console, trace) -> None:
        found.done.append("run_chat")
        make_session()
        trace.write("user", data={"text": f"my key is {KEY}"})

    monkeypatch.setattr(cli, "ensure_image", ensure_image)
    monkeypatch.setattr(cli, "open_session", open_session)
    monkeypatch.setattr(cli, "run_chat", run_chat)
    return found


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    out, err = capsys.readouterr()
    assert err == ""
    return out.splitlines()


@pytest.mark.parametrize("network", [True, False])
def test_chat_opens_a_session_on_the_folder(
    calls: Calls, folder: Path, tmp_path: Path, network: bool
) -> None:
    flags = [] if network else ["--no-network"]

    assert cli.main(["chat", str(folder), *flags]) == 0

    assert calls.done == ["ensure_image", "run_chat", "open_session"]
    assert calls.session == {
        "source": folder.resolve(),
        "network": network,
        "image_dir": cli.PROJECT / "image",
        "sessions_dir": tmp_path / "sessions",
    }


def test_the_dotenv_of_the_project_and_no_other_is_loaded(
    calls: Calls, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(folder)

    cli.main(["chat", "."])

    assert calls.dotenv == [cli.PROJECT / ".env"]


def test_the_trace_is_in_the_project_and_redacted(calls: Calls, folder: Path) -> None:
    cli.main(["chat", str(folder)])

    (trace,) = (cli.PROJECT / "sessions").iterdir()
    assert trace.name.endswith("Z.jsonl")
    written = trace.read_text("utf-8")
    assert KEY not in written
    assert json.loads(written)["data"]["text"] == "my key is [redacted]"


@pytest.mark.parametrize("which", ["home", "missing", "root"])
def test_a_refused_folder_is_one_line_and_nothing_starts(
    calls: Calls,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    which: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    folder = {
        "home": home,
        "missing": tmp_path / "missing [bold]",
        "root": Path(tmp_path.anchor),
    }[which]

    assert cli.main(["chat", str(folder)]) == 2

    (line,) = lines(capsys)
    assert line.startswith("sih: ")
    assert calls.done == []
    assert calls.dotenv == []
    assert not cli.PROJECT.exists()
    assert not cli.SESSIONS.exists()


def test_no_api_key_is_one_line(
    calls: Calls,
    folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")

    assert cli.main(["chat", str(folder)]) == 2

    (line,) = lines(capsys)
    assert "OPENROUTER_API_KEY" in line
    assert calls.done == []


def test_the_key_may_come_from_the_dotenv(
    calls: Calls, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setattr(
        cli, "load_dotenv", lambda path: os.environ.update(OPENROUTER_API_KEY=KEY)
    )

    assert cli.main(["chat", str(folder)]) == 0


@pytest.mark.parametrize(
    ("where", "error"),
    [
        ("ensure_image", SandboxError("docker was not found on PATH")),
        ("ensure_image", SandboxError("image build failed: no\ndaemon")),
        ("ensure_image", FileNotFoundError("no Dockerfile")),
        ("run_chat", SandboxError("container did not start: no")),
    ],
)
def test_a_setup_problem_is_one_line_and_no_traceback(
    calls: Calls,
    folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    where: str,
    error: Exception,
) -> None:
    def fail(*args: object) -> None:
        raise error

    monkeypatch.setattr(cli, where, fail)

    assert cli.main(["chat", str(folder)]) == 2

    last = lines(capsys)[-1]
    assert last == "sih: " + " ".join(str(error).split())


def test_what_blocks_is_said_first(
    calls: Calls, folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["chat", str(folder)])

    out = capsys.readouterr().out.replace("\n", "")
    assert out.index("building the sandbox image") < out.index("copying ")
    assert folder.name in out


def test_ctrl_c_during_setup_is_no_traceback(
    calls: Calls,
    folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def press(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "ensure_image", press)

    assert cli.main(["chat", str(folder)]) == 130

    assert lines(capsys)[-1] == "interrupted"


def test_the_module_runs_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "main", lambda: 7)

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("harness", run_name="__main__")

    assert stopped.value.code == 7
