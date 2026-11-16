"""Host-side tests for the sandbox image (T026): hb_entry FINAL channel and tag."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness_builder.executor import image_build
from harness_builder.executor.image import hb_entry

ENTRY_SCRIPT = Path(hb_entry.__file__)


@pytest.fixture
def isolated_sys(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_python rewrites sys.argv and sys.path[0]; restore them after the test."""
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))


def _stage(tmp_path: Path, code: str) -> tuple[Path, Path]:
    hb_dir = tmp_path / ".hb"
    hb_dir.mkdir(exist_ok=True)
    code_path = hb_dir / "code.py"
    code_path.write_text(code, encoding="utf-8")
    return code_path, hb_dir / "final.json"


@pytest.mark.usefixtures("isolated_sys")
def test_final_writes_answer_and_exits_zero(tmp_path: Path) -> None:
    code_path, final_path = _stage(
        tmp_path, "FINAL({'a': 1})\nraise RuntimeError('not reached')\n"
    )
    assert hb_entry.run_file("python", code_path, final_path) == 0
    assert json.loads(final_path.read_text(encoding="utf-8")) == "{'a': 1}"


@pytest.mark.usefixtures("isolated_sys")
def test_except_exception_cannot_swallow_final(tmp_path: Path) -> None:
    code = "try:\n    FINAL(42)\nexcept Exception:\n    FINAL('swallowed')\n"
    code_path, final_path = _stage(tmp_path, code)
    assert hb_entry.run_file("python", code_path, final_path) == 0
    assert json.loads(final_path.read_text(encoding="utf-8")) == "42"


@pytest.mark.usefixtures("isolated_sys")
def test_stale_final_is_removed_when_code_does_not_call_final(
    tmp_path: Path,
) -> None:
    code_path, final_path = _stage(tmp_path, "x = 1\n")
    final_path.write_text('"old"', encoding="utf-8")
    assert hb_entry.run_file("python", code_path, final_path) == 0
    assert not final_path.exists()


@pytest.mark.usefixtures("isolated_sys")
def test_uncaught_exception_exits_one_with_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code_path, final_path = _stage(tmp_path, "raise ValueError('boom')\n")
    assert hb_entry.run_file("python", code_path, final_path) == 1
    assert "ValueError: boom" in capsys.readouterr().err
    assert not final_path.exists()


def test_main_rejects_unknown_mode_and_language(tmp_path: Path) -> None:
    assert hb_entry.main([]) == 2
    assert hb_entry.main(["nope"]) == 2
    assert hb_entry.main(["run", "awk", str(tmp_path / "x")]) == 2
    assert hb_entry.main(["run", "python"]) == 2


def test_final_mode_uses_env_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final_path = tmp_path / "out" / "final.json"
    monkeypatch.setenv(hb_entry.FINAL_PATH_ENV, str(final_path))
    assert hb_entry.main(["final", "the", "answer"]) == 0
    assert json.loads(final_path.read_text(encoding="utf-8")) == "the answer"


def _run_script(tmp_path: Path, code: str) -> tuple[int, Path]:
    code_path, final_path = _stage(tmp_path, code)
    proc = subprocess.run(
        [sys.executable, str(ENTRY_SCRIPT), "run", "python", str(code_path)],
        env={**os.environ, hb_entry.FINAL_PATH_ENV: str(final_path)},
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return proc.returncode, final_path


def test_standalone_script_final_exit_code(tmp_path: Path) -> None:
    code, final_path = _run_script(tmp_path, "print('hi')\nFINAL(7)\n")
    assert code == 0
    assert json.loads(final_path.read_text(encoding="utf-8")) == "7"


def test_standalone_script_system_exit_keeps_code(tmp_path: Path) -> None:
    code, final_path = _run_script(tmp_path, "import sys\nsys.exit(3)\n")
    assert code == 3
    assert not final_path.exists()


def _copy_image_dir(tmp_path: Path) -> Path:
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    for name in image_build.IMAGE_FILES:
        shutil.copy(image_build.IMAGE_DIR / name, image_dir / name)
    return image_dir


def test_image_tag_format_is_stable() -> None:
    tag = image_build.image_tag()
    repo, _, digest = tag.partition(":")
    assert repo == "harness-builder-sandbox"
    assert len(digest) == image_build.TAG_HASH_LEN
    assert tag == image_build.image_tag()


@pytest.mark.parametrize("name", image_build.IMAGE_FILES)
def test_image_tag_changes_when_image_file_changes(tmp_path: Path, name: str) -> None:
    image_dir = _copy_image_dir(tmp_path)
    before = image_build.image_tag(image_dir)
    with (image_dir / name).open("a", encoding="utf-8") as f:
        f.write("\n# changed\n")
    assert image_build.image_tag(image_dir) != before


def test_image_tag_ignores_caches_and_line_endings(tmp_path: Path) -> None:
    image_dir = _copy_image_dir(tmp_path)
    before = image_build.image_tag(image_dir)
    (image_dir / "__pycache__").mkdir()
    (image_dir / "__pycache__" / "hb_entry.pyc").write_bytes(b"\0")
    dockerfile = image_dir / "Dockerfile"
    lf = dockerfile.read_bytes().replace(b"\r\n", b"\n")
    dockerfile.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert image_build.image_tag(image_dir) == before


def test_build_command_argv() -> None:
    image_dir = image_build.IMAGE_DIR
    assert image_build.build_command() == [
        "docker",
        "build",
        "-t",
        image_build.image_tag(),
        "-f",
        str(image_dir / "Dockerfile"),
        str(image_dir),
    ]
