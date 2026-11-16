
- (T002) ruff F401 flags `TYPE_CHECKING` re-exports when `__all__` is computed (e.g. `sorted(dict)`); keep `__all__` a literal list. Ruff N818 (Error suffix) is enabled: design-fixed exception names such as `ReplayDivergence` need `# noqa: N818`.
- (T006) `requires-python = ">=3.11"`: PEP 695 generics (`def f[T](...)`) are a syntax error for ruff's target; use `typing.TypeVar`. Verify a pyrefly structural check against a protocol with a negative control; `pyrefly check -c pyproject.toml <file>` checks a scratchpad script with project imports.
