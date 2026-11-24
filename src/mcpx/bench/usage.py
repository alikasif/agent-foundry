"""Per-model-call token usage recording and the per-run record."""

import json
from dataclasses import asdict, dataclass, field, fields

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse


@dataclass
class CallUsage:
    """Token counts of one model call; ``None`` when the provider omitted them."""

    prompt_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None


@dataclass
class RunRecord:
    """The outcome and cost data of one task run."""

    task_id: str
    arm: str
    model: str
    run_index: int
    calls: list[CallUsage]
    tool_calls: int
    wall_s: float
    answer_text: str
    correct: bool
    error: str | None

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "RunRecord":
        """Parse a JSON string made by ``to_json``.

        Raises:
            ValueError: If the data has keys this version does not know.
        """
        data = json.loads(s)
        _reject_unknown(data, cls, "RunRecord")
        calls = []
        for call in data.pop("calls"):
            _reject_unknown(call, CallUsage, "CallUsage")
            calls.append(CallUsage(**call))
        return cls(calls=calls, **data)


def _reject_unknown(data: dict[str, object], cls: type, name: str) -> None:
    """Raise ``ValueError`` naming any key of ``data`` that ``cls`` lacks."""
    extra = sorted(set(data) - {f.name for f in fields(cls)})
    if extra:
        raise ValueError(f"Unknown {name} field(s): {', '.join(extra)}")


@dataclass
class UsageRecorder:
    """Collects one ``CallUsage`` per model call via ADK's after-model hook."""

    calls: list[CallUsage] = field(default_factory=list)

    def after_model_callback(
        self, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> None:
        """Record usage of a final model response; never alters the response.

        ADK invokes this for every streamed chunk too, so partial responses are
        skipped to keep one entry per model call.
        """
        if getattr(llm_response, "partial", None):
            return
        meta = llm_response.usage_metadata
        if meta is None:
            self.calls.append(CallUsage(None, None, None))
        else:
            self.calls.append(
                CallUsage(
                    meta.prompt_token_count,
                    meta.candidates_token_count,
                    meta.cached_content_token_count,
                )
            )
        return
