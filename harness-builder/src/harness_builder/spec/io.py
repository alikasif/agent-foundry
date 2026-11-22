"""HarnessSpec YAML/JSON reading and writing with located errors.

Every problem is a ``SpecError`` whose lines read ``<source>: <path>: <msg>``,
for example ``harness.yaml: tools[2].builtin: Field required``. Parse errors
carry the line and column instead: ``harness.yaml:3:5: <problem>``. Files
ending in ``.json`` are JSON; everything else is YAML.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml  # pyrefly: ignore[untyped-import]
from pydantic import ValidationError

from harness_builder.errors import SpecError
from harness_builder.spec.models import (
    SPEC_FILE_NAMES,
    SUPPORTED_SCHEMA_VERSIONS,
    HarnessSpec,
)


def spec_error(source: str, path: str, message: str) -> SpecError:
    """A ``SpecError`` located at ``path`` (``tools[2].builtin``) in ``source``."""
    return SpecError(f"{source}: {path}: {message}")


def find_spec_file(path: Path) -> Path:
    """Return ``path`` itself, or the first ``SPEC_FILE_NAMES`` file inside it."""
    if path.is_file():
        return path
    if path.is_dir():
        for name in SPEC_FILE_NAMES:
            if (path / name).is_file():
                return path / name
        raise SpecError(
            f"{path}: no spec file found; expected one of {SPEC_FILE_NAMES}"
        )
    raise SpecError(f"{path}: spec file or directory does not exist")


def load_spec(path: Path | str) -> HarnessSpec:
    """Read and validate the spec at ``path`` (a file or a harness directory)."""
    file = find_spec_file(Path(path))
    return parse_spec(read_spec_data(file), file.name)


def read_spec_data(file: Path) -> Any:
    """Parse ``file`` as JSON or YAML; syntax errors give line and column."""
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecError(f"{file.name}: cannot read spec: {exc}") from exc
    if file.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecError(f"{file.name}:{exc.lineno}:{exc.colno}: {exc.msg}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = "" if mark is None else f":{mark.line + 1}:{mark.column + 1}"
        raise SpecError(f"{file.name}{where}: {exc.problem or exc}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"{file.name}: {exc}") from exc


def parse_spec(data: Any, source: str = "<spec>") -> HarnessSpec:
    """Validate parsed ``data`` as a ``HarnessSpec``; ``source`` names it.

    The schema version is checked first, so a future version is reported as
    unsupported rather than as a field mismatch.
    """
    if not isinstance(data, dict):
        raise spec_error(source, "<root>", "a spec must be a mapping")
    version = data.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        found = "missing" if version is None else f"unsupported {version!r}"
        raise spec_error(
            source,
            "schema_version",
            f"{found}; expected one of {list(SUPPORTED_SCHEMA_VERSIONS)}",
        )
    try:
        return HarnessSpec.model_validate(data)
    except ValidationError as exc:
        lines: list[str] = []
        for err in exc.errors():
            path = located_path(data, err["loc"], missing=err["type"] == "missing")
            line = f"{source}: {path}: {err['msg']}"
            if line not in lines:
                lines.append(line)
        raise SpecError("\n".join(lines)) from exc


def located_path(data: Any, loc: Sequence[int | str], *, missing: bool) -> str:
    """Render a pydantic ``loc`` as ``tools[2].builtin`` over the input data.

    Segments absent from the data are union tags (``str``, ``PromptFile``) and
    are skipped, except the last one of a ``missing`` error (the absent key).
    """
    parts: list[str] = []
    node = data
    for index, part in enumerate(loc):
        if isinstance(part, int) and isinstance(node, list) and part < len(node):
            parts.append(f"[{part}]")
            node = node[part]
        elif isinstance(node, dict) and part in node:
            parts.append(f".{part}")
            node = node[part]
        elif missing and index == len(loc) - 1:
            parts.append(f".{part}")
    return "".join(parts).lstrip(".") or "<root>"


def dump_spec(spec: HarnessSpec) -> dict[str, Any]:
    """The JSON-compatible form of ``spec`` without default values."""
    return spec.model_dump(mode="json", exclude_defaults=True)


def write_spec(spec: HarnessSpec, path: Path | str) -> Path:
    """Write ``spec`` as JSON (``.json``) or YAML (UTF-8); returns the path."""
    file = Path(path)
    data = dump_spec(spec)
    if file.suffix.lower() == ".json":
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    else:
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    return file
