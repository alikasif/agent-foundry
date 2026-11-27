from harness.sandbox.container import ExecResult
from harness.tools.result import result_dict


def test_only_output_when_nothing_else_is_meaningful() -> None:
    assert result_dict(ExecResult("")) == {"output": ""}


def test_exit_code_zero_is_kept() -> None:
    assert result_dict(ExecResult("a", exit_code=0)) == {"output": "a", "exit_code": 0}


def test_everything_meaningful_is_kept() -> None:
    result = ExecResult("a", error="b", exit_code=1, timed_out=True, restarted=True)

    assert result_dict(result) == {
        "output": "a",
        "error": "b",
        "exit_code": 1,
        "timed_out": True,
        "restarted": True,
    }
