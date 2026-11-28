import json
from pathlib import Path

from harness.trace.redact import REDACTED, env_secrets
from harness.trace.writer import TraceWriter

KEY = "sk-or-0123456789abcdef"


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_one_object_per_line_with_a_time(tmp_path: Path) -> None:
    path = tmp_path / "sessions" / "t.jsonl"  # the folder does not exist yet
    writer = TraceWriter(path, [])

    writer.write("user", data={"text": "a\nb"})
    writer.write("tool_call", "bash", {"command": "ls"})

    first, second = lines(path)
    assert first["time"] and second["time"]
    assert (first["kind"], first["name"], first["data"]) == (
        "user",
        "",
        {"text": "a\nb"},
    )
    assert (second["name"], second["data"]) == ("bash", {"command": "ls"})


def test_the_set_key_is_redacted_everywhere(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path, env_secrets())

    class Odd:
        def __str__(self) -> str:
            return f"odd {KEY}"

    writer.write(
        "tool_result",
        KEY,
        {"output": f"key={KEY}", KEY: [1, {"deep": (KEY, Odd())}]},
    )

    written = path.read_text("utf-8")
    assert KEY not in written
    assert written.count(REDACTED) == 5


def test_strings_are_redacted_before_they_are_cut(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path, [KEY])

    writer.write("text", data={"text": "x" * 19_995 + KEY + "y" * 100, "n": 7})

    data = lines(path)[0]["data"]
    assert len(data["text"]) == 20_000
    assert KEY[:5] not in data["text"]
    assert data["n"] == 7


def test_a_lone_surrogate_is_written(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"

    TraceWriter(path, []).write("text", data={"text": "a\udcff"})

    assert lines(path)[0]["data"]["text"] == "a\udcff"
