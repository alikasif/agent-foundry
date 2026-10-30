# Learnings

- (T002) Rewriting `shared/task_list.json` with `json.dump(..., ensure_ascii=False)` turns
  the existing `—` escapes into literal em dashes and produces a huge diff. Use the
  default `ensure_ascii=True` (and indent=2) so a status change is a one-line diff.
- (T002) `uv run ruff format .` reformats sibling agents' in-progress files; use
  `uv run ruff format <own files>` and `uv run ruff format --check .` for the project-wide check.
- (T003) Pydantic v2 checks `Field(min_length=...)` during core str validation, BEFORE
  `field_validator` (mode="after") runs, so `Field(min_length=1)` + a `.strip()` after-
  validator still accepts `" "`. Raise `ValueError` for empty text inside the validator.
- (T003) `shared/task_list.json` has no trailing newline; strip it when rewriting so a
  status change stays a one-line diff.
- (T010) pyrefly types `ChatOpenAI.openai_api_key` as `SecretStr | Callable`, so
  `model.openai_api_key.get_secret_value()` fails type checking after an `is not None`
  check. Narrow with `assert isinstance(model.openai_api_key, SecretStr)` first.
- (T010) pytest-cov is not a project dependency; measure coverage without editing
  pyproject via `UV_HTTP_TIMEOUT=120 uv run --with pytest-cov pytest --cov=...` (the first
  fetch timed out at uv's default HTTP timeout).
- (T006) To check that package data ships in the wheel, `uv build --wheel -o <scratch>`,
  extract it with `python -m zipfile -e` and import from it via `PYTHONPATH` using the
  project venv. Do not create a throwaway venv with `uv pip install` (forbidden here).
- (T006) Jinja autoescape escapes `"` inside a `<script>` body, which breaks JS and
  changes the CSP hash; the static script must be emitted with `|safe`. jinja2 3.1 no
  longer exports `Markup`.
