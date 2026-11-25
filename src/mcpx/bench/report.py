"""Summarize run records into per-group statistics and render them as Markdown.

Everything here is arithmetic over ``RunRecord`` data; nothing calls a model.
The honesty rules the report follows:

* A figure the provider did not report is ``None``, never ``0``, and renders as
  ``n/a``.
* A group with an incorrect (or errored) run is flagged, and no ratio between
  arms is printed for it.
* Runs made with different settings (``RunRecord.settings``) are never merged:
  each (model, settings) pair gets its own table.
"""

from collections.abc import Callable, Iterable
from statistics import median
from typing import Any

from mcpx.bench.usage import RunRecord
from mcpx.llm.pricing import Prices, price_for

Row = dict[str, Any]
Stat = dict[str, float | int]

_ARM_ORDER = {"direct": 0, "code": 1}
_COLUMNS = (
    "task",
    "arm",
    "correct",
    "prompt tokens",
    "output tokens",
    "cached tokens",
    "model calls",
    "tool calls",
    "MCP calls",
    "wall s",
    "cost (list)",
    "cost (with reported cache reads)",
)
NO_CACHE_NOTE = "provider reported no cached-token counts; cached cost equals list cost"
NOT_RECORDED = "(not recorded)"
GroupKey = tuple[str, str, str, str]


def _total(record: RunRecord, attr: str) -> int | None:
    """Sum ``attr`` over the run's model calls; ``None`` if any call lacks it.

    A run without any model call has no token figures at all, so it is ``None``
    too (nothing was reported), not zero.
    """
    if not record.calls:
        return None
    values = [getattr(call, attr) for call in record.calls]
    if any(v is None for v in values):
        return None
    return sum(values)


def prompt_total(record: RunRecord) -> int | None:
    """Total prompt tokens of a run, or ``None`` when not fully reported."""
    return _total(record, "prompt_tokens")


def output_total(record: RunRecord) -> int | None:
    """Total output tokens of a run, or ``None`` when not fully reported."""
    return _total(record, "output_tokens")


def _price(model: str) -> Prices | None:
    """The model's prices, or ``None`` when ``prices.toml`` lacks the model."""
    try:
        return price_for(model)
    except KeyError:
        return None


def _cost_list(
    prompt: int | None, output: int | None, p: Prices | None
) -> float | None:
    """All prompt tokens at the input price plus output at the output price (USD)."""
    if prompt is None or output is None or p is None:
        return None
    return (prompt * p.input_per_mtok + output * p.output_per_mtok) / 1e6


def _cost_cached(record: RunRecord, p: Prices | None) -> float | None:
    """Cost with cache reads priced separately, when the run reported them.

    Uses the cache-read price for cached tokens only when every call in the run
    reported a cached-token count and the price table has a cache-read price;
    otherwise this equals the list cost.
    """
    prompt, output = prompt_total(record), output_total(record)
    listed = _cost_list(prompt, output, p)
    cached = _total(record, "cached_tokens")
    if listed is None or p is None or cached is None or prompt is None:
        return listed
    if p.cache_read_per_mtok is None or output is None:
        return listed
    return (
        (prompt - cached) * p.input_per_mtok
        + cached * p.cache_read_per_mtok
        + output * p.output_per_mtok
    ) / 1e6


def _stat(values: Iterable[float | None]) -> Stat | None:
    """Median, min and max over the non-``None`` values; ``None`` if none."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return {"median": median(present), "min": min(present), "max": max(present)}


def _group_row(key: GroupKey, group: list[RunRecord]) -> Row:
    """Build one summary row from the records of one group."""
    task_id, arm, model, settings = key
    prices = _price(model)
    figures: dict[str, Callable[[RunRecord], float | None]] = {
        "prompt_tokens": prompt_total,
        "output_tokens": output_total,
        "cached_tokens": lambda r: _total(r, "cached_tokens"),
        "model_calls": lambda r: len(r.calls),
        "tool_calls": lambda r: r.tool_calls,
        "mcp_calls": lambda r: r.mcp_calls,
        "wall_s": lambda r: r.wall_s,
        "cost_list": lambda r: _cost_list(prompt_total(r), output_total(r), prices),
        "cost_cached": lambda r: _cost_cached(r, prices),
    }
    row: Row = {"task_id": task_id, "arm": arm, "model": model, "settings": settings}
    row["runs"] = len(group)
    row["correct_runs"] = sum(1 for r in group if r.correct)
    row["error_runs"] = sum(1 for r in group if r.error)
    row["cached_reported"] = any(
        c.cached_tokens is not None for r in group for c in r.calls
    )
    for name, figure in figures.items():
        row[name] = _stat(figure(r) for r in group)
    return row


def summarize(records: list[RunRecord]) -> list[Row]:
    """Group records by ``(task_id, arm, model, settings)``; summarize each group.

    Token counts are the provider's: ``prompt_tokens`` is assumed OpenAI-style,
    that is, it INCLUDES the cached tokens. Rows are sorted by model, settings,
    task id, then arm (``direct`` before ``code``).

    Each row has these keys:

    * ``task_id``, ``arm``, ``model``, ``settings``: the group key
      (``settings`` is ``""`` for records written before it was recorded).
    * ``runs``: records in the group; ``correct_runs``: those with
      ``correct``; ``error_runs``: those with ``error`` set. Errored runs
      count as runs and as not correct.
    * ``cached_reported``: ``True`` when any call in the group reported a
      cached-token count.
    * ``prompt_tokens``, ``output_tokens``, ``cached_tokens``: per-run totals
      over the run's model calls.
    * ``model_calls``: number of model calls per run; ``tool_calls`` (model-level
      function calls), ``mcp_calls`` (MCP tool calls actually executed;
      ``None`` for records written before it was recorded) and ``wall_s``: the
      record's values.
    * ``cost_list``: USD per run, all prompt tokens at the input price plus
      output tokens at the output price.
    * ``cost_cached``: USD per run, ``(prompt - cached)`` tokens at the input
      price, cached tokens at the cache-read price and output tokens at the
      output price, only when every call in the run reported cached tokens
      and the price table has a cache-read price; otherwise equal to
      ``cost_list``.

    Every figure key (``prompt_tokens`` to ``cost_cached``) holds
    ``{"median": ..., "min": ..., "max": ...}`` over the group's runs, or
    ``None`` when no run has a value. A run's figure is ``None`` when any
    contributing value was ``None`` (a model without a price makes both cost
    figures ``None``); such runs are skipped in the aggregate, never counted as
    zero.
    """
    groups: dict[GroupKey, list[RunRecord]] = {}
    for r in records:
        groups.setdefault((r.task_id, r.arm, r.model, r.settings), []).append(r)
    keys = sorted(
        groups,
        key=lambda k: (k[2], k[3], k[0], _ARM_ORDER.get(k[1], len(_ARM_ORDER)), k[1]),
    )
    return [_group_row(key, groups[key]) for key in keys]


def _num(value: float, decimals: int) -> str:
    """Format a number without a trailing ``.0`` for whole values."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.{decimals}f}"


def _cell(stat: Stat | None, fmt: Callable[[float], str]) -> str:
    """Render a stat as ``median [min-max]``; ``n/a`` when it is ``None``."""
    if stat is None:
        return "n/a"
    return f"{fmt(stat['median'])} [{fmt(stat['min'])}-{fmt(stat['max'])}]"


def _money(value: float) -> str:
    return f"${value:.6f}"


def _correct_cell(row: Row) -> str:
    text = f"{row['correct_runs']}/{row['runs']} correct"
    if row["error_runs"]:
        text += f" ({row['error_runs']} errored)"
    if row["correct_runs"] != row["runs"]:
        text = f"⚠ {text}"
    return text


def _table_row(row: Row) -> str:
    def tokens(v: float) -> str:
        return _num(v, 1)

    cells = [
        str(row["task_id"]),
        str(row["arm"]),
        _correct_cell(row),
        _cell(row["prompt_tokens"], tokens),
        _cell(row["output_tokens"], tokens),
        _cell(row["cached_tokens"], tokens),
        _cell(row["model_calls"], tokens),
        _cell(row["tool_calls"], tokens),
        _cell(row["mcp_calls"], tokens),
        _cell(row["wall_s"], lambda v: f"{v:.1f}"),
        _cell(row["cost_list"], _money),
        _cell(row["cost_cached"], _money),
    ]
    return "| " + " | ".join(cells) + " |"


def _ratio_line(task_id: str, direct: Row | None, code: Row | None) -> str:
    """One 'code vs direct' line for a task, or the reason it is refused."""
    if direct is None or code is None:
        ran = "direct" if direct is not None else "code"
        return f"{task_id}: not comparable: only the {ran} arm was run"
    if any(r["correct_runs"] != r["runs"] for r in (direct, code)):
        return f"{task_id}: not comparable: a run was incorrect"
    d, c = direct["prompt_tokens"], code["prompt_tokens"]
    if d is None or c is None:
        return f"{task_id}: not comparable: prompt tokens were not reported"
    if d["median"] == 0:
        return f"{task_id}: not comparable: direct median prompt tokens is zero"
    pct = c["median"] / d["median"] * 100
    shown = f"{pct:.0f}" if pct >= 10 else f"{pct:.1f}"
    return f"{task_id}: code uses {shown}% of direct's prompt tokens"


def _settings_label(settings: str) -> str:
    """How a group's settings are shown above its table."""
    return f"settings: `{settings}`" if settings else f"settings: {NOT_RECORDED}"


def _section(model: str, settings: str, rows: list[Row]) -> list[str]:
    """One table (plus its ratio lines) for one (model, settings) pair."""
    lines = [f"### {model}", "", _settings_label(settings), ""]
    lines.append("| " + " | ".join(_COLUMNS) + " |")
    lines.append("|" + "|".join(" --- " for _ in _COLUMNS) + "|")
    lines.extend(_table_row(r) for r in rows)
    lines += ["", "**Code arm vs direct arm** (median total prompt tokens)", ""]
    for task_id in dict.fromkeys(r["task_id"] for r in rows):
        by_arm = {r["arm"]: r for r in rows if r["task_id"] == task_id}
        lines.append(
            "- " + _ratio_line(task_id, by_arm.get("direct"), by_arm.get("code"))
        )
    lines.append("")
    return lines


def render_markdown(rows: list[Row]) -> str:
    """Render summary rows as one Markdown table per (model, settings) pair.

    Each table is headed by its model and its settings (``settings: (not
    recorded)`` for older records), and has a row per (task, arm); a group that is not fully correct is
    marked with a warning sign. Below each table, a per-task line compares the
    code arm's median prompt tokens with the direct arm's, only when both arms
    have a value and every run in both was correct. Footnotes say where the
    numbers come from and which models reported no cached-token counts.
    """
    if not rows:
        return "No runs.\n"
    models = list(dict.fromkeys(r["model"] for r in rows))
    lines: list[str] = []
    for model, settings in dict.fromkeys((r["model"], r["settings"]) for r in rows):
        mine = [r for r in rows if (r["model"], r["settings"]) == (model, settings)]
        lines.extend(_section(model, settings, mine))
    uncached = [
        m
        for m in models
        if not any(r["cached_reported"] for r in rows if r["model"] == m)
    ]
    lines.append(
        "Tokens are those reported by the provider; costs use `prices.toml` "
        "(USD per million tokens). `cost (with reported cache reads)` equals "
        "`cost (list)` for any run where a call lacked cached-token counts. "
        "`tool calls` are model-level function calls (one per `run_python` call "
        "in the code arm); `MCP calls` are the MCP tool calls actually executed "
        "(n/a for records written before they were counted)."
    )
    if uncached:
        lines += ["", f"Note ({', '.join(uncached)}): {NO_CACHE_NOTE}."]
    return "\n".join(lines) + "\n"
