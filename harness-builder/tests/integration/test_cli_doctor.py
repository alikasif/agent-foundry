"""``harness doctor [--clean]`` tests (T031, FR-044) with a ``FakeDockerCli``.

``DOCKER_CLI_FACTORY`` is monkeypatched to return one shared fake, so every
Docker call of the command (checks and ``--clean``) is recorded. The cwd is an
empty tmp dir (the integration conftest), so no developer ``.env`` is read.
"""

import os

import pytest
from click.testing import CliRunner, Result

from harness_builder.cli import main as cli_main
from harness_builder.errors import DockerUnavailableError
from harness_builder.executor import preflight as preflight_module
from harness_builder.executor.docker_cli import FakeDockerCli, FakeResponse
from harness_builder.executor.image_build import image_tag
from harness_builder.executor.preflight import format_command, sandbox_build_command

EXIT_DOCKER = 3
PS = ["ps", "-a", "-q", "--filter", "label=harness-builder=1"]


@pytest.fixture(autouse=True)
def no_harness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ``HARNESS_*`` variables so the image tag is deterministic."""
    for key in list(os.environ):
        if key.startswith("HARNESS_"):
            monkeypatch.delenv(key)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDockerCli:
    """The ``FakeDockerCli`` every ``DOCKER_CLI_FACTORY()`` call returns."""
    cli = FakeDockerCli()
    monkeypatch.setattr(cli_main, "DOCKER_CLI_FACTORY", lambda: cli)
    return cli


def doctor(*args: str, image: str = "") -> Result:
    """Invoke ``harness doctor`` with ``HARNESS_DOCKER_IMAGE=image``."""
    env = {"HARNESS_DOCKER_IMAGE": image, "COLUMNS": "200"}
    return CliRunner().invoke(cli_main.main, ["doctor", *args], env=env)


def build_line(tag: str) -> str:
    """The ``build:`` line doctor prints for ``tag``."""
    return f"build: {format_command(sandbox_build_command(tag))}"


def test_all_checks_ok_exits_zero(fake: FakeDockerCli) -> None:
    fake.script(["--version"], FakeResponse(stdout=b"Docker version 27.1\n"))
    result = doctor()
    assert result.exit_code == 0, result.output
    tag = image_tag()
    assert fake.calls == [
        ["--version"],
        ["info", "--format", "{{.ServerVersion}}"],
        ["image", "inspect", "--format", "{{.Id}}", tag],
    ]
    for name in ("cli", "daemon", "image"):
        assert name in result.stdout
    assert "Docker version 27.1" in result.stdout
    assert f"image: {tag}" in result.stdout
    assert build_line(tag) in result.stdout
    assert result.stderr == ""


def test_custom_image_tag_is_checked_and_shown(fake: FakeDockerCli) -> None:
    result = doctor(image="my/sandbox:2")
    assert result.exit_code == 0
    assert fake.calls[-1][-1] == "my/sandbox:2"
    assert "image: my/sandbox:2" in result.stdout
    assert build_line("my/sandbox:2") in result.stdout


@pytest.mark.parametrize(
    ("prefix", "response", "expected"),
    [
        (
            ["--version"],
            FakeResponse(error=DockerUnavailableError("Docker CLI missing")),
            "Docker CLI missing",
        ),
        (
            ["--version"],
            FakeResponse(exit_code=1, stderr=b"bad install"),
            "Docker CLI is not usable",
        ),
        (
            ["info"],
            FakeResponse(exit_code=1, stderr=b"cannot connect"),
            "Docker daemon is not reachable",
        ),
        (
            ["image", "inspect"],
            FakeResponse(exit_code=1, stderr=b"No such image"),
            "is not available",
        ),
    ],
)
def test_failed_check_exits_three_with_advice(
    fake: FakeDockerCli, prefix: list[str], response: FakeResponse, expected: str
) -> None:
    fake.script(prefix, response)
    result = doctor()
    assert result.exit_code == EXIT_DOCKER
    assert result.stderr.startswith("error: ")
    assert expected in result.stderr
    assert "fail" in result.stdout
    # The build command is always printed, also when a check fails.
    assert build_line(image_tag()) in result.stdout
    assert "Traceback" not in result.output


def test_missing_image_error_contains_build_command(fake: FakeDockerCli) -> None:
    fake.script(["image", "inspect"], FakeResponse(exit_code=1, stderr=b"No such"))
    result = doctor(image="x:1")
    assert result.exit_code == EXIT_DOCKER
    assert format_command(sandbox_build_command("x:1")) in result.stderr


def test_daemon_down_marks_image_skipped(fake: FakeDockerCli) -> None:
    fake.script(["info"], FakeResponse(exit_code=1, stderr=b"down"))
    result = doctor()
    assert result.exit_code == EXIT_DOCKER
    assert "skipped" in result.stdout
    assert fake.commands() == ["--version", "info"]


def test_hanging_daemon_fails_within_the_deadline(
    fake: FakeDockerCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(preflight_module.STEP_TIMEOUT_S, "daemon", 0.2)
    fake.script(["info"], FakeResponse(hang=True))
    result = doctor()
    assert result.exit_code == EXIT_DOCKER
    assert "did not answer" in result.stderr


# --- --clean ---------------------------------------------------------------------


def test_clean_removes_labelled_containers(fake: FakeDockerCli) -> None:
    fake.script(["ps"], FakeResponse(stdout=b"aaa\nbbb\n"))
    result = doctor("--clean")
    assert result.exit_code == 0, result.output
    assert fake.calls[-2:] == [PS, ["rm", "-f", "aaa", "bbb"]]
    assert "clean: removed 2 container(s)" in result.stdout


def test_clean_with_nothing_to_remove(fake: FakeDockerCli) -> None:
    result = doctor("--clean")
    assert result.exit_code == 0
    assert fake.commands()[-1] == "ps"
    assert "clean: removed 0 container(s)" in result.stdout


def test_clean_skipped_when_daemon_down(fake: FakeDockerCli) -> None:
    fake.script(["info"], FakeResponse(exit_code=1, stderr=b"down"))
    result = doctor("--clean")
    assert result.exit_code == EXIT_DOCKER
    assert "clean: skipped, Docker is not reachable" in result.stderr
    assert "ps" not in fake.commands()


def test_clean_runs_even_when_image_missing(fake: FakeDockerCli) -> None:
    fake.script(["image", "inspect"], FakeResponse(exit_code=1, stderr=b"No such"))
    fake.script(["ps"], FakeResponse(stdout=b"old1\n"))
    result = doctor("--clean")
    assert result.exit_code == EXIT_DOCKER
    assert ["rm", "-f", "old1"] in fake.calls
    assert "clean: removed 1 container(s)" in result.stdout


@pytest.mark.parametrize(
    ("prefix", "response"),
    [
        (["ps"], FakeResponse(exit_code=1, stderr=b"ps failed")),
        (["rm"], FakeResponse(exit_code=1, stderr=b"rm failed")),
    ],
)
def test_clean_failure_exits_three(
    fake: FakeDockerCli, prefix: list[str], response: FakeResponse
) -> None:
    fake.script(["ps"], FakeResponse(stdout=b"c1\n"))
    fake.script(prefix, response)
    result = doctor("--clean")
    assert result.exit_code == EXIT_DOCKER
    assert "failed" in result.stderr
    assert "Traceback" not in result.output


def test_help_lists_doctor_and_clean() -> None:
    top = CliRunner().invoke(cli_main.main, ["--help"])
    assert "doctor" in top.output
    sub = CliRunner().invoke(cli_main.main, ["doctor", "--help"])
    assert sub.exit_code == 0 and "--clean" in sub.output
