"""What one script run produced, as the model will see it through ``render``."""

from dataclasses import dataclass


@dataclass
class RunResult:
    """Output of one run: stdout, an error text if the script failed, and notices."""

    stdout: str
    error: str = ""
    timed_out: bool = False
    truncated: bool = False
    max_output_chars: int = 0
    timeout_s: float = 0.0

    @property
    def ok(self) -> bool:
        """True when the script neither raised nor timed out."""
        return not self.error and not self.timed_out

    def render(self) -> str:
        """Return stdout, then the error if any, then one notice line if cut short."""
        parts = [self.stdout]
        if self.error:
            if parts[0] and not parts[0].endswith("\n"):
                parts.append("\n")
            parts.append(self.error)
        notices = []
        if self.truncated:
            limit = (
                f" at {self.max_output_chars} chars" if self.max_output_chars else ""
            )
            notices.append(f"[output truncated{limit}]")
        if self.timed_out:
            after = f" after {self.timeout_s:g} s" if self.timeout_s else ""
            notices.append(f"[timed out{after}]")
        if notices:
            text = "".join(parts)
            if text and not text.endswith("\n"):
                parts.append("\n")
            parts.append(" ".join(notices) + "\n")
        return "".join(parts)
