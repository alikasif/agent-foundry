"""Model configuration: builds ADK's LiteLlm for an OpenRouter model."""

from dataclasses import dataclass

from google.adk.models.lite_llm import LiteLlm


@dataclass(frozen=True)
class ModelConfig:
    """Which OpenRouter model to benchmark and how to call it.

    Attributes:
        model_id: OpenRouter model id, for example ``deepseek/deepseek-v4.1-flash``.
        temperature: Sampling temperature sent with every request.
        provider_order: OpenRouter providers to try, in order. When non-empty the
            request pins routing to them and disables fallbacks, so a run cannot
            silently move to another provider.
    """

    model_id: str
    temperature: float = 0.0
    provider_order: tuple[str, ...] = ()

    def to_llm(self) -> LiteLlm:
        """Build the ADK ``LiteLlm`` for this configuration."""
        kwargs: dict[str, object] = {"temperature": self.temperature}
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
