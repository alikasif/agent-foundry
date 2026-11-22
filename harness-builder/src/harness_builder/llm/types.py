"""Framework-neutral LLM request/response models (designs.md 6).

Leaf module: imports stdlib, pydantic and the ``core.types`` leaf only.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness_builder.core.types import Message, ModelConfig, ToolCall, Usage

LLMPurpose = Literal["step", "summarize", "promotion"]


class ToolSchema(BaseModel):
    """A tool as advertised to the model; ``parameters`` is a JSON schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class LLMRequest(BaseModel):
    """One model call: system prompt, history, tools and model settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: ModelConfig
    system: str | None = None
    messages: list[Message] = Field(default_factory=list)
    tools: list[ToolSchema] = Field(default_factory=list)
    purpose: LLMPurpose = "step"


class LLMResponse(BaseModel):
    """Aggregated (non-streaming) model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    model: str | None = None
