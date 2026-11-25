"""Model configuration: builds ADK's LiteLlm for an OpenRouter model."""

from dataclasses import dataclass

from google.adk.models.lite_llm import LiteLlm

# Values OpenRouter accepts for ``reasoning_effort`` (LiteLLM also maps ``max`` to
# ``xhigh``; it is left out so one level has one spelling).
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


@dataclass(frozen=True)
class ModelConfig:
    """Which OpenRouter model to benchmark and how to call it.

    Attributes:
        model_id: OpenRouter model id, for example ``deepseek/deepseek-v4.1-flash``.
        temperature: Sampling temperature sent with every request.
        provider_order: OpenRouter providers to try, in order. When non-empty the
            request pins routing to them and disables fallbacks, so a run cannot
            silently move to another provider.
        timeout_s: Per-request timeout in seconds forwarded to LiteLLM. ``None``
            keeps LiteLLM's default.
        reasoning_effort: How hard the model reasons, one of ``REASONING_EFFORTS``.
            LiteLLM sends it to OpenRouter as the top-level ``reasoning_effort``
            request field (OpenRouter treats it as ``reasoning.effort``). ``None``
            leaves the model's own default (high for the benchmarked model).
    """

    model_id: str
    temperature: float = 0.0
    provider_order: tuple[str, ...] = ()
    timeout_s: float | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        """Reject a reasoning effort OpenRouter would not accept."""
        effort = self.reasoning_effort
        if effort is not None and effort not in REASONING_EFFORTS:
            raise ValueError(
                f"reasoning_effort must be one of {', '.join(REASONING_EFFORTS)}; "
                f"got {effort!r}"
            )

    def to_llm(self) -> LiteLlm:
        """Build the ADK ``LiteLlm`` for this configuration."""
        kwargs: dict[str, object] = {"temperature": self.temperature}
        if self.timeout_s is not None:
            kwargs["timeout"] = self.timeout_s
        if self.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if self.provider_order:
            # LiteLLM merges ``extra_body`` into the OpenRouter request body, so
            # ``provider`` reaches OpenRouter as its provider-routing preferences.
            kwargs["extra_body"] = {
                "provider": {
                    "order": list(self.provider_order),
                    "allow_fallbacks": False,
                }
            }
        return LiteLlm(model=f"openrouter/{self.model_id}", **kwargs)
