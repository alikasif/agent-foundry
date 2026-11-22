"""GenerationRequest: what the user asks the generator for (designs.md 1.2)."""

from pydantic import BaseModel, ConfigDict, Field


class GenerationRequest(BaseModel):
    """Intake of ``harness new``: a task or domain description plus options.

    ``examples`` are sample inputs or tasks the harness should handle,
    ``constraints`` free-text requirements, and ``allowed_tools`` the builtin
    tool names the generated harness may reference (empty: any registered
    builtin). ``interactive`` lets the generator ask clarifying questions
    through ``ask_user`` (at most 5); otherwise ``ask_user`` is auto-denied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = Field(min_length=1)
    examples: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    interactive: bool = False

    def to_prompt(self) -> str:
        """The request rendered as the generator's first user turn."""
        lines = ["Build a harness for this task or domain:", "", self.description]
        sections = (
            ("Example inputs or tasks", self.examples),
            ("Constraints", self.constraints),
            ("Builtin tools the harness may use", self.allowed_tools),
        )
        for title, items in sections:
            if items:
                lines += ["", f"{title}:", *(f"- {item}" for item in items)]
        if not self.interactive:
            lines += [
                "",
                "This run is non-interactive: do not ask questions; make "
                "reasonable assumptions and state them in README.md.",
            ]
        return "\n".join(lines)
