"""Tests for the call-log protocol shared by the proxy and the agent."""

from pathlib import Path

from mcpx.codemode.protocol import LoggedCall, append_call, read_call_log


def test_append_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    append_call(path, "get_ticket", {"ticket_id": 7})
    append_call(path, "list_labels", {})

    assert read_call_log(path) == [
        LoggedCall("get_ticket", {"ticket_id": 7}),
        LoggedCall("list_labels", {}),
    ]


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    assert read_call_log(tmp_path / "absent.jsonl") == []


def test_truncated_last_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    append_call(path, "get_ticket", {"ticket_id": 1})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"tool": "x", "ar')

    assert read_call_log(path) == [LoggedCall("get_ticket", {"ticket_id": 1})]


def test_blank_and_non_object_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    path.write_text(
        '\n[1, 2]\n"text"\n42\n{"tool": 5, "args": {}}\n{"tool": "a"}\n'
        '{"tool": "ok", "args": {"k": 1}}\n   \n',
        encoding="utf-8",
    )

    assert read_call_log(path) == [LoggedCall("ok", {"k": 1})]
