"""Per-model-call token usage recording and the per-run record.

Limitation: the counts are what ADK's ``LiteLlm`` puts in ``usage_metadata``,
and ADK fills in what the provider left out. When a response carries a usage
block, a missing prompt, output or cached-token count becomes ``0``
(``lite_llm.py`` defaults ``prompt_tokens`` and ``completion_tokens`` to 0, and
``_extract_cached_prompt_tokens`` returns 0 when the field is absent), and a
reasoning count of 0 becomes ``None`` (``thoughts_token_count`` is set only when
non-zero). So on the real path ``None`` here mostly means "the response had no
usage block at all", a ``0`` cached count may mean "not reported", and a
``None`` reasoning count may mean "zero". The unit tests build
``usage_metadata`` objects directly and so also exercise the ``None`` paths.
"""

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse


@dataclass
class CallUsage:
    """Token counts of one model call; ``None`` when none was reported.

    ``reasoning_tokens`` comes from ``usage_metadata.thoughts_token_count``; it
    defaults to ``None`` so records written before it was recorded still load.
    """

    prompt_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None = None


@dataclass
class RunRecord:
    """The outcome and cost data of one task run.

    ``tool_calls`` counts model-level function calls (for the code arm, each
    ``run_python`` call is one). ``mcp_calls`` counts the MCP tool calls actually
    executed (direct arm: one per function call; code arm: one per call the
    script made through the Bridge); ``None`` in records written before it was
    recorded. ``trace`` is what the run did, in order: one item per function
    call (direct arm), per ``run_python`` script and per Bridge MCP call (code
    arm); empty in older records. ``settings`` describes how the run was
    configured (``key=value;...``, see ``mcpx.agents.batch.run_settings``);
    empty in older records, which the report groups as "not recorded".

    Fields added after the first results were published have defaults, so
    older JSONL lines still load.
    """

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
    mcp_calls: int | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    settings: str = ""

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "RunRecord":
        """Parse a JSON string made by ``to_json`` (this or an older version).

        Keys added in later versions are optional; unknown keys are rejected.

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
                    meta.thoughts_token_count,
                )
            )
        return
