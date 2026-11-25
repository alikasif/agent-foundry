"""Keep API keys out of what a run records.

``RunRecord.error`` holds exception text, which goes into committed JSONL files,
and provider errors may echo request details. Every secret value long enough to
be a real key is replaced before the record is built.
"""

import os
from collections.abc import Iterable

REDACTED = "[redacted]"
# Shorter values are too likely to occur by chance (and are no real keys).
MIN_SECRET_CHARS = 8
SECRET_VARS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY")


def env_secrets() -> list[str]:
    """The values of ``SECRET_VARS`` that are set in the environment."""
    return [value for name in SECRET_VARS if (value := os.environ.get(name))]


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret of at least ``MIN_SECRET_CHARS`` chars in ``text``.

    Longer secrets are replaced first, so a secret containing another is
    removed whole.
    """
    usable = sorted(
        (s for s in secrets if len(s) >= MIN_SECRET_CHARS), key=len, reverse=True
    )
    for secret in usable:
        text = text.replace(secret, REDACTED)
    return text
