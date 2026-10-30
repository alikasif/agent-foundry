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
