"""Secret redaction applied to trace events at record time (designs.md 4.3).

The values of configured secrets and of every environment variable whose name
matches ``*KEY*|*TOKEN*|*SECRET*|*PASSWORD*`` (case-insensitive) are replaced
with ``[REDACTED:<NAME>]`` wherever they occur in a string, including dict keys
and nested child traces.

Literal fields (``kind``, ``role``, ``decision``, ...) are never rewritten when
their value is one of the schema's constants: a public enum word is not a
secret, and rewriting it would make the event invalid (review T021 M2).
"""

import functools
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

from dotenv import dotenv_values
from pydantic import BaseModel, ValidationError
from pydantic.errors import PydanticUserError

from harness_builder.config import ENV_VARS, Settings, default_dotenv_path
from harness_builder.errors import HarnessError

SECRET_NAME_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
"""An env var whose upper-cased name contains one of these is a secret."""

MIN_SECRET_LENGTH = 6
"""Shorter values are not redacted; ``X_KEY=1`` must not mask every ``1``."""

M = TypeVar("M", bound=BaseModel)
LiteralFields = frozenset[tuple[str, str]]


class RedactionError(HarnessError):
    """A redacted model could not be rebuilt without keeping a secret."""


def is_secret_name(name: str) -> bool:
    """Return True when an env var name matches the secret name patterns."""
    upper = name.upper()
    return any(part in upper for part in SECRET_NAME_PARTS)


def literal_fields(model_type: type[BaseModel]) -> LiteralFields:
    """``(field name, constant)`` pairs of every Literal/enum field in the schema.

    Collected from ``model_json_schema()`` at any depth (nested models, unions,
    ``$defs``). A model without a JSON schema yields no pairs.
    """
    return _schema_info(model_type)[0]


def field_names(model_type: type[BaseModel]) -> frozenset[str]:
    """Property names of the model and every nested model in its schema.

    Dict keys equal to one of these are public field names, never secrets, so
    ``redact_model`` leaves them unchanged (review T021 R2-M1).
    """
    return _schema_info(model_type)[1]


@functools.cache
def _schema_info(
    model_type: type[BaseModel],
) -> tuple[LiteralFields, frozenset[str]]:
    """One cached schema walk: Literal ``(field, constant)`` pairs and names."""
    try:
        schema = model_type.model_json_schema()
    except PydanticUserError:
        return frozenset(), frozenset()
    defs = schema.get("$defs", {})
    pairs: set[tuple[str, str]] = set()
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
            for name, prop in properties.items():
                pairs.update((name, value) for value in _constants(prop, defs))
        for item in node.values():
            walk(item)

    walk(schema)
    return frozenset(pairs), frozenset(names)


def _constants(prop: Any, defs: dict[str, Any]) -> list[str]:
    """String ``const``/``enum`` values a property schema allows."""
    if not isinstance(prop, dict):
        return []
    ref = prop.get("$ref")
    if isinstance(ref, str):
        return _constants(defs.get(ref.rsplit("/", 1)[-1]), defs)
    found = [prop["const"]] if "const" in prop else []
    found += prop.get("enum", [])
    for key in ("anyOf", "oneOf", "allOf"):
        for option in prop.get(key, []):
            found += _constants(option, defs)
    return [value for value in found if isinstance(value, str)]


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
        cls,
        settings: Settings,
        environ: Mapping[str, str] | None = None,
        dotenv_path: Path | None = None,
    ) -> "Redactor":
        """Build a redactor that also covers secrets read only from ``.env``.

        ``Settings.from_env`` never copies ``.env`` into ``os.environ``, so the
        API key and every secret-named key of the ``.env`` file (default
        ``./.env``, as in ``Settings.from_env``) are registered explicitly.
        """
        path = dotenv_path if dotenv_path is not None else default_dotenv_path()
        file_values = dotenv_values(path) if path.is_file() else {}
        secrets = {
            name.upper(): value
            for name, value in sorted(file_values.items())
            if value and is_secret_name(name)
        }
        if settings.openrouter_api_key is not None:
            key = settings.openrouter_api_key.get_secret_value()
            if key:
                secrets[ENV_VARS["openrouter_api_key"]] = key
        return cls(secrets=secrets, environ=environ)

    def redact_text(self, text: str) -> str:
        """Return ``text`` with every known secret value replaced."""
        for value, marker in self._pairs:
            text = text.replace(value, marker)
        return text

    def redact(
        self,
        value: Any,
        keep: LiteralFields = frozenset(),
        names: frozenset[str] = frozenset(),
    ) -> Any:
        """Redact strings inside a JSON-compatible value (keys included).

        A string stored under key ``k`` is left as is when ``(k, string)`` is
        in ``keep`` (see ``literal_fields``); a key in ``names`` (see
        ``field_names``) is left as is. Other keys are redacted.
        """
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {
                (key if key in names else self.redact_text(str(key))): (
                    item
                    if isinstance(item, str) and (key, item) in keep
                    else self.redact(item, keep, names)
                )
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.redact(item, keep, names) for item in value]
        return value

    def redact_model(self, model: M) -> M:
        """Return a redacted copy of a model such as a trace event or ``Trace``.

        The model is dumped to JSON-compatible data, redacted and re-validated,
        so secrets containing JSON escape characters are still matched. Literal
        fields keep their schema constants, and keys that are schema field
        names are not rewritten; keys of free-form dicts are. If a typed scalar
        field (such as a datetime) no longer validates because a secret occurred
        inside it, that one field keeps its original value; it cannot hold free
        text. Anything
        else raises ``RedactionError``: the unredacted model is never returned.
        """
        if not self._pairs:
            return model
        data = model.model_dump(mode="json")
        model_type = type(model)
        redacted = self.redact(
            data, literal_fields(model_type), field_names(model_type)
        )
        try:
            return model.model_validate(redacted)
        except ValidationError as exc:
            for error in exc.errors():
                original = _get_at(data, error["loc"])
                if original is None or isinstance(original, dict | list):
                    raise RedactionError(
                        f"cannot redact {type(model).__name__} at {error['loc']}"
                    ) from exc
                _set_at(redacted, error["loc"], original)
        try:
            return model.model_validate(redacted)
        except ValidationError as exc:
            raise RedactionError(f"cannot redact {type(model).__name__}") from exc


def _get_at(data: Any, loc: tuple[int | str, ...]) -> Any:
    """Value at a pydantic error ``loc``; union tags absent from data are skipped."""
    for part in loc:
        if isinstance(data, list) and isinstance(part, int) and part < len(data):
            data = data[part]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        elif not isinstance(data, dict):
            return None
    return data


def _set_at(data: Any, loc: tuple[int | str, ...], value: Any) -> None:
    """Set the value at ``loc`` (as resolved by ``_get_at``)."""
    path: list[int | str] = []
    node = data
    for part in loc:
        if isinstance(node, list) and isinstance(part, int):
            path.append(part)
            node = node[part]
        elif isinstance(node, dict) and part in node:
            path.append(part)
            node = node[part]
    parent = _get_at(data, tuple(path[:-1]))
    parent[path[-1]] = value
