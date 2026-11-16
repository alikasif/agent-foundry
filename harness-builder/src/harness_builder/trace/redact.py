"""Secret redaction applied to trace events at record time (designs.md 4.3).

The values of configured secrets and of every environment variable whose name
matches ``*KEY*|*TOKEN*|*SECRET*|*PASSWORD*`` (case-insensitive) are replaced
with ``[REDACTED:<NAME>]`` wherever they occur in a string, including dict keys
and nested child traces.
"""

import os
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel

from harness_builder.config import ENV_VARS, Settings

SECRET_NAME_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
"""An env var whose upper-cased name contains one of these is a secret."""

MIN_SECRET_LENGTH = 6
"""Shorter values are not redacted; ``X_KEY=1`` must not mask every ``1``."""

M = TypeVar("M", bound=BaseModel)


def is_secret_name(name: str) -> bool:
    """Return True when an env var name matches the secret name patterns."""
    upper = name.upper()
    return any(part in upper for part in SECRET_NAME_PARTS)


class Redactor:
    """Replaces known secret values with ``[REDACTED:<NAME>]`` markers."""

    def __init__(
        self,
        secrets: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Collect secrets from ``secrets`` and ``environ`` (default os.environ).

        Explicit ``secrets`` (name -> value) win over env vars with the same
        value. Values shorter than ``MIN_SECRET_LENGTH`` are ignored.
        """
        env = os.environ if environ is None else environ
        by_value: dict[str, str] = {}
        for name, value in sorted(env.items()):
            if is_secret_name(name):
                by_value.setdefault(value, name.upper())
        for name, value in (secrets or {}).items():
            by_value[value] = name
        self._pairs: list[tuple[str, str]] = sorted(
            (
                (value, f"[REDACTED:{name}]")
                for value, name in by_value.items()
                if len(value) >= MIN_SECRET_LENGTH
            ),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, environ: Mapping[str, str] | None = None
    ) -> "Redactor":
        """Build a redactor that also covers secrets read only from ``.env``.

        ``Settings.from_env`` never copies ``.env`` into ``os.environ``, so the
        API key is registered explicitly under its env var name.
        """
        secrets: dict[str, str] = {}
        if settings.openrouter_api_key:
            secrets[ENV_VARS["openrouter_api_key"]] = settings.openrouter_api_key
        return cls(secrets=secrets, environ=environ)

    def redact_text(self, text: str) -> str:
        """Return ``text`` with every known secret value replaced."""
        for value, marker in self._pairs:
            text = text.replace(value, marker)
        return text

    def redact(self, value: Any) -> Any:
        """Redact strings inside a JSON-compatible value (keys included)."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {
                self.redact_text(str(key)): self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.redact(item) for item in value]
        return value

    def redact_model(self, model: M) -> M:
        """Return a redacted copy of a model such as a trace event or ``Trace``.

        The model is dumped to JSON-compatible data, redacted and re-validated,
        so secrets containing JSON escape characters are still matched.
        """
        if not self._pairs:
            return model
        return model.model_validate(self.redact(model.model_dump(mode="json")))
