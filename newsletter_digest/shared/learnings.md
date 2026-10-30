# Learnings

- (T002) Rewriting `shared/task_list.json` with `json.dump(..., ensure_ascii=False)` turns
  the existing `—` escapes into literal em dashes and produces a huge diff. Use the
  default `ensure_ascii=True` (and indent=2) so a status change is a one-line diff.
- (T002) `uv run ruff format .` reformats sibling agents' in-progress files; use
  `uv run ruff format <own files>` and `uv run ruff format --check .` for the project-wide check.
