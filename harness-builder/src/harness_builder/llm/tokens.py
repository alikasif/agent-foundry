"""Token counters implementing ``core.protocols.TokenCounter``.

``HeuristicCounter`` is deterministic and dependency-free (chars / 4, rounded
up); it is used by tests, ``FakeLLMClient`` and context policies by default.
``LiteLlmCounter`` uses litellm's tokenizer and imports litellm lazily.
"""

import json
import math

from harness_builder.core.types import Message

CHARS_PER_TOKEN = 4


def message_chars(message: Message) -> int:
    """Return the character size of a message: content plus tool-call JSON."""
    chars = len(message.content)
    for call in message.tool_calls:
        chars += len(call.name) + len(json.dumps(call.args, sort_keys=True))
    return chars


class HeuristicCounter:
    """Deterministic counter: ``ceil(chars / 4)`` per text or message."""

    def count_text(self, text: str) -> int:
        """Return the token estimate of a plain string."""
        return math.ceil(len(text) / CHARS_PER_TOKEN)

    def count(self, messages: list[Message]) -> int:
        """Return the summed token estimate of ``messages``."""
        return sum(math.ceil(message_chars(m) / CHARS_PER_TOKEN) for m in messages)


class LiteLlmCounter:
    """Counter backed by ``litellm.token_counter`` for a given model.

    litellm is imported on the first ``count`` call (its import is slow and may
    fetch the remote cost map), so constructing this counter is cheap.
    """

    def __init__(self, model: str) -> None:
        self.model = model

    def count(self, messages: list[Message]) -> int:
        """Return litellm's token count of ``messages`` for ``self.model``."""
        import litellm

        payload = [
            {
                "role": m.role,
                "content": m.content
                or json.dumps([c.model_dump() for c in m.tool_calls]),
            }
            for m in messages
        ]
        return litellm.token_counter(model=self.model, messages=payload)
