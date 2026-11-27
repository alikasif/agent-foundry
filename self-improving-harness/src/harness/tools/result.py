"""What the model reads after a code call."""

from dataclasses import asdict

from harness.sandbox.container import ExecResult


def result_dict(result: ExecResult) -> dict:
    """``output``, plus only the other fields that say something."""
    # By identity, not equality: an exit code of 0 equals False but must stay.
    return {
        key: value
        for key, value in asdict(result).items()
        if key == "output" or (value is not None and value is not False)
    }
