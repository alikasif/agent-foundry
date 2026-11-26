"""Pure-arithmetic tests for ``summarize`` and ``render_markdown``."""

import pytest

from mcpx.bench.report import render_markdown, summarize
from mcpx.bench.usage import CallUsage, RunRecord
from mcpx.llm.pricing import Prices

MODEL = "deepseek/deepseek-v4.1-flash"


def rec(
    *,
    task: str = "A",
    arm: str = "direct",
    model: str = MODEL,
    run_index: int = 0,
    calls: list[tuple[int | None, int | None, int | None]] | None = None,
    tool_calls: int = 1,
    mcp_calls: int | None = None,
    settings: str = "",
    wall_s: float = 1.0,
    correct: bool = True,
    error: str | None = None,
) -> RunRecord:
    """Build a record by hand; ``calls`` is a list of (prompt, output, cached)."""
    usage = [CallUsage(*c) for c in (calls if calls is not None else [(10, 5, None)])]
    return RunRecord(
        task_id=task,
        arm=arm,
        model=model,
        run_index=run_index,
        calls=usage,
        tool_calls=tool_calls,
        wall_s=wall_s,
        answer_text="",
        correct=correct,
        error=error,
        mcp_calls=mcp_calls,
        settings=settings,
    )


def prompts(task: str, arm: str, values: list[int]) -> list[RunRecord]:
    """One correct record per prompt-token value, each with a single model call."""
    return [
        rec(task=task, arm=arm, run_index=i, calls=[(v, 7, None)])
        for i, v in enumerate(values)
    ]


def test_summarize_medians_and_ranges() -> None:
    rows = summarize(prompts("A", "direct", [100, 300, 200]))
    assert len(rows) == 1
    row = rows[0]
    assert row["prompt_tokens"] == {"median": 200, "min": 100, "max": 300}
    assert row["output_tokens"] == {"median": 7, "min": 7, "max": 7}
    assert (row["task_id"], row["arm"], row["model"]) == ("A", "direct", MODEL)
    assert (row["runs"], row["correct_runs"]) == (3, 3)


def test_summarize_totals_sum_over_calls() -> None:
    records = [
        rec(calls=[(10, 1, 4), (20, 2, 6)], tool_calls=3, wall_s=2.5),
        rec(calls=[(30, 3, 0)], tool_calls=1, wall_s=1.5, run_index=1),
    ]
    row = summarize(records)[0]
    assert row["prompt_tokens"] == {"median": 30, "min": 30, "max": 30}
    assert row["output_tokens"]["median"] == 3
    assert row["cached_tokens"] == {"median": 5.0, "min": 0, "max": 10}
    assert row["model_calls"] == {"median": 1.5, "min": 1, "max": 2}
    assert row["tool_calls"] == {"median": 2.0, "min": 1, "max": 3}
    assert row["wall_s"] == {"median": 2.0, "min": 1.5, "max": 2.5}


def test_summarize_groups_by_task_arm_and_model() -> None:
    records = (
        prompts("A", "direct", [1, 2])
        + prompts("A", "code", [3])
        + prompts("B", "direct", [4])
        + [rec(model="other/model")]
    )
    rows = summarize(records)
    keys = {(r["task_id"], r["arm"], r["model"]) for r in rows}
    assert keys == {
        ("A", "direct", MODEL),
        ("A", "code", MODEL),
        ("B", "direct", MODEL),
        ("A", "direct", "other/model"),
    }


def test_unreported_usage_is_none_and_prints_na_not_zero() -> None:
    record = rec(calls=[(None, None, None)], tool_calls=1)
    row = summarize([record])[0]
    assert row["prompt_tokens"] is None
    assert row["output_tokens"] is None
    assert row["cached_tokens"] is None
    assert row["cost_list"] is None and row["cost_cached"] is None
    md = render_markdown([row])
    assert "n/a" in md
    assert "| 0 |" not in md


def test_one_unreported_call_makes_that_runs_total_none_not_partial() -> None:
    records = [
        rec(calls=[(100, 5, None), (None, 5, None)]),
        rec(calls=[(40, 5, None)], run_index=1),
    ]
    row = summarize(records)[0]
    # The first run is skipped (its total is unknown), not counted as 100.
    assert row["prompt_tokens"] == {"median": 40, "min": 40, "max": 40}


def test_a_run_without_any_model_call_has_no_token_figures() -> None:
    row = summarize([rec(calls=[], error="boom", correct=False)])[0]
    assert row["prompt_tokens"] is None
    assert row["model_calls"] == {"median": 0, "min": 0, "max": 0}


def test_error_runs_count_as_runs_and_as_incorrect() -> None:
    records = [
        rec(run_index=0),
        rec(run_index=1, correct=False, error="TimeoutError: slow"),
    ]
    row = summarize(records)[0]
    assert (row["runs"], row["correct_runs"], row["error_runs"]) == (2, 1, 1)
    md = render_markdown([row])
    assert "1/2 correct" in md and "1 errored" in md


def test_wrong_answers_are_flagged() -> None:
    records = [
        rec(run_index=0),
        rec(run_index=1, correct=False),
        rec(run_index=2, correct=False),
    ]
    md = render_markdown(summarize(records))
    assert "1/3 correct" in md
    assert "⚠" in md
    full = render_markdown(summarize(prompts("A", "direct", [1, 2, 3])))
    assert "3/3 correct" in full and "⚠" not in full


def test_cost_numeric_example_uses_cache_price_when_all_calls_report() -> None:
    # 3000 prompt (2100 cached), 300 output at 0.14 / 0.42 / 0.0042 USD/Mtok.
    row = summarize([rec(calls=[(1000, 100, 600), (2000, 200, 1500)])])[0]
    list_cost = (3000 * 0.14 + 300 * 0.42) / 1e6
    cached_cost = (900 * 0.14 + 2100 * 0.0042 + 300 * 0.42) / 1e6
    assert row["cost_list"]["median"] == pytest.approx(list_cost)
    assert row["cost_cached"]["median"] == pytest.approx(cached_cost)
    assert row["cost_cached"]["median"] < row["cost_list"]["median"]


def test_cost_cached_equals_list_when_a_call_lacks_cached_counts() -> None:
    row = summarize([rec(calls=[(1000, 100, 600), (2000, 200, None)])])[0]
    assert row["cost_cached"] == row["cost_list"]
    assert row["cost_list"]["median"] > 0


def test_cost_cached_equals_list_when_price_has_no_cache_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mcpx.bench.report.price_for", lambda model_id: Prices(1.0, 2.0, None)
    )
    row = summarize([rec(calls=[(1_000_000, 500_000, 400_000)])])[0]
    assert row["cost_list"]["median"] == pytest.approx(1.0 + 1.0)
    assert row["cost_cached"] == row["cost_list"]


def test_cost_is_none_for_a_model_without_prices() -> None:
    row = summarize([rec(model="nobody/home")])[0]
    assert row["prompt_tokens"] is not None
    assert row["cost_list"] is None and row["cost_cached"] is None
    assert "n/a" in render_markdown([row])


def test_render_markdown_snapshot_substrings() -> None:
    records = prompts("A", "direct", [100, 300, 200]) + prompts(
        "A", "code", [10, 30, 20]
    )
    md = render_markdown(summarize(records))
    assert f"### {MODEL}" in md
    header = (
        "| task | arm | correct | prompt tokens | output tokens | cached tokens "
        "| reasoning tokens | model calls | tool calls | MCP calls | wall s | cost (list) "
        "| cost (with reported cache reads) |"
    )
    assert header in md
    assert "| A | direct | 3/3 correct | 200 [100-300] |" in md
    assert "| A | code | 3/3 correct | 20 [10-30] |" in md
    assert "Code arm vs direct arm" in md
    assert "A: code uses 10% of direct's prompt tokens" in md
    assert "prices.toml" in md
    assert "reported by the provider" in md


def test_footnote_explains_that_adk_turns_missing_counts_into_zero() -> None:
    for cached in (None, 4):
        md = render_markdown(summarize([rec(calls=[(10, 5, cached)])]))
        assert "provider reported no cached-token counts" not in md
        assert "a missing prompt, output or cached-token count becomes 0" in md
        assert "may mean the provider did not report it" in md


def test_reasoning_tokens_sum_reported_calls_and_are_na_when_none_reported() -> None:
    with_reasoning = RunRecord(
        task_id="A",
        arm="direct",
        model=MODEL,
        run_index=0,
        calls=[CallUsage(10, 5, 0, 30), CallUsage(10, 5, 0, None)],
        tool_calls=1,
        wall_s=1.0,
        answer_text="",
        correct=True,
        error=None,
    )
    second = RunRecord.from_json(with_reasoning.to_json())
    second.run_index = 1
    second.calls[1].reasoning_tokens = 20
    row = summarize([with_reasoning, second])[0]
    assert row["reasoning_tokens"] == {"median": 40.0, "min": 30, "max": 50}
    assert "| 40 [30-50] |" in render_markdown([row])
    none = summarize([rec(calls=[(10, 5, 0)])])[0]
    assert none["reasoning_tokens"] is None


def test_ratio_refused_when_any_run_is_incorrect() -> None:
    records = prompts("A", "direct", [100, 100]) + [
        rec(task="A", arm="code", run_index=0, calls=[(10, 1, None)]),
        rec(task="A", arm="code", run_index=1, calls=[(10, 1, None)], correct=False),
    ]
    md = render_markdown(summarize(records))
    assert "not comparable: a run was incorrect" in md
    assert "% of direct's prompt tokens" not in md


def test_ratio_refused_when_a_median_is_missing_or_an_arm_is_absent() -> None:
    missing = prompts("A", "direct", [100]) + [
        rec(task="A", arm="code", calls=[(None, None, None)])
    ]
    md = render_markdown(summarize(missing))
    assert "not comparable" in md
    assert "% of direct's prompt tokens" not in md
    only_one = render_markdown(summarize(prompts("A", "direct", [100])))
    assert "not comparable: only the direct arm was run" in only_one


def test_mcp_calls_column_shows_executed_calls_and_na_for_old_records() -> None:
    records = [
        rec(arm="code", tool_calls=2, mcp_calls=40),
        rec(arm="code", tool_calls=2, mcp_calls=60, run_index=1),
    ]
    row = summarize(records)[0]
    assert row["tool_calls"] == {"median": 2, "min": 2, "max": 2}
    assert row["mcp_calls"] == {"median": 50.0, "min": 40, "max": 60}
    md = render_markdown([row])
    assert "| 2 [2-2] | 50 [40-60] |" in md
    assert "`MCP calls` are the MCP tool calls actually executed" in md
    old = summarize([rec(tool_calls=3)])[0]
    assert old["mcp_calls"] is None
    assert "| 3 [3-3] | n/a |" in render_markdown([old])


MEDIUM = "reasoning=medium;provider=default;max_llm_calls=40;run_timeout_s=600"
LOW = "reasoning=low;provider=default;max_llm_calls=40;run_timeout_s=600"


def test_batches_differing_only_in_settings_never_merge() -> None:
    records = [
        rec(arm="direct", settings=MEDIUM, calls=[(100, 1, None)]),
        rec(arm="code", settings=MEDIUM, calls=[(10, 1, None)]),
        rec(arm="direct", settings=LOW, calls=[(300, 1, None)]),
        rec(arm="code", settings=LOW, calls=[(60, 1, None)]),
    ]
    rows = summarize(records)
    assert len(rows) == 4
    assert {(r["arm"], r["settings"]) for r in rows} == {
        ("direct", MEDIUM),
        ("code", MEDIUM),
        ("direct", LOW),
        ("code", LOW),
    }
    md = render_markdown(rows)
    assert md.count(f"### {MODEL}") == 2
    assert f"settings: `{MEDIUM}`" in md and f"settings: `{LOW}`" in md
    # Each table compares its own arms only.
    assert "A: code uses 10% of direct's prompt tokens" in md
    assert "A: code uses 20% of direct's prompt tokens" in md


def test_old_records_group_under_not_recorded() -> None:
    old = prompts("A", "direct", [100, 200])
    new = [rec(settings=MEDIUM, run_index=5)]
    rows = summarize(old + new)
    assert [r["settings"] for r in rows] == ["", MEDIUM]
    assert rows[0]["runs"] == 2 and rows[1]["runs"] == 1
    md = render_markdown(rows)
    assert "settings: (not recorded)" in md


TWO_ARM_REPORT = """\
### deepseek/deepseek-v4.1-flash

settings: (not recorded)

| task | arm | correct | prompt tokens | output tokens | cached tokens | reasoning tokens | model calls | tool calls | MCP calls | wall s | cost (list) | cost (with reported cache reads) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | direct | 3/3 correct | 200 [100-300] | 7 [7-7] | n/a | n/a | 1 [1-1] | 1 [1-1] | n/a | 1.0 [1.0-1.0] | $0.000031 [$0.000017-$0.000045] | $0.000031 [$0.000017-$0.000045] |
| A | code | 3/3 correct | 20 [10-30] | 7 [7-7] | n/a | n/a | 1 [1-1] | 1 [1-1] | n/a | 1.0 [1.0-1.0] | $0.000006 [$0.000004-$0.000007] | $0.000006 [$0.000004-$0.000007] |

**Code arm vs direct arm** (median total prompt tokens)

- A: code uses 10% of direct's prompt tokens

Tokens are those reported by the provider; costs use `prices.toml` (USD per million tokens). `cost (with reported cache reads)` equals `cost (list)` for any run where a call lacked cached-token counts. `tool calls` are model-level function calls (one per `run_python` call in the code arm); `MCP calls` are the MCP tool calls actually executed (n/a for records written before they were counted).

ADK fills in usage counts the provider omitted: a missing prompt, output or cached-token count becomes 0, and a reasoning count of 0 is dropped. So a 0 in the `cached tokens` column may mean the provider did not report it, and `reasoning tokens` sums only the calls that reported a count (n/a when none did).
"""


def test_a_two_arm_report_is_byte_identical_to_before() -> None:
    records = prompts("A", "direct", [100, 300, 200]) + prompts(
        "A", "code", [10, 30, 20]
    )
    assert render_markdown(summarize(records)) == TWO_ARM_REPORT
    assert "Code arm vs direct arm" in TWO_ARM_REPORT
    assert "fastmcp" not in TWO_ARM_REPORT


def three_arms(fastmcp_ok: bool = True, code_ok: bool = True) -> list[RunRecord]:
    """Two direct runs at 100, two code at 10, two fastmcp at 50 prompt tokens."""
    records = prompts("A", "direct", [100, 100])
    for arm, ok in (("code", code_ok), ("fastmcp", fastmcp_ok)):
        value = 10 if arm == "code" else 50
        records.append(rec(task="A", arm=arm, run_index=0, calls=[(value, 7, None)]))
        records.append(
            rec(task="A", arm=arm, run_index=1, calls=[(value, 7, None)], correct=ok)
        )
    return records


def test_three_arms_render_in_order_direct_code_fastmcp() -> None:
    rows = summarize(three_arms()[::-1])
    assert [r["arm"] for r in rows] == ["direct", "code", "fastmcp"]
    md = render_markdown(rows)
    at = [md.index(f"| A | {arm} |") for arm in ("direct", "code", "fastmcp")]
    assert at == sorted(at)


def test_three_arms_give_a_ratio_line_each() -> None:
    md = render_markdown(summarize(three_arms()))
    assert "**Code arms vs direct arm** (median total prompt tokens)" in md
    assert "**Code arm vs direct arm**" not in md
    code_at = md.index("- A: code uses 10% of direct's prompt tokens")
    fast_at = md.index("- A: fastmcp uses 50% of direct's prompt tokens")
    assert code_at < fast_at


def test_ratio_withheld_when_fastmcp_has_an_incorrect_run() -> None:
    md = render_markdown(summarize(three_arms(fastmcp_ok=False)))
    assert "A: not comparable: a run was incorrect" in md
    assert "fastmcp uses" not in md
    # The other pair is judged on its own.
    assert "A: code uses 10% of direct's prompt tokens" in md


def test_ratio_withheld_for_code_does_not_suppress_fastmcp() -> None:
    md = render_markdown(summarize(three_arms(code_ok=False)))
    assert "code uses" not in md
    assert "A: fastmcp uses 50% of direct's prompt tokens" in md
    assert md.count("not comparable: a run was incorrect") == 1


def test_fastmcp_with_direct_but_no_code_names_only_fastmcp() -> None:
    records = prompts("A", "direct", [100]) + prompts("A", "fastmcp", [25])
    md = render_markdown(summarize(records))
    assert "**Code arms vs direct arm**" in md
    assert "A: fastmcp uses 25% of direct's prompt tokens" in md
    assert "code uses" not in md and "not comparable" not in md


def test_fastmcp_without_direct_is_not_comparable() -> None:
    md = render_markdown(summarize(prompts("A", "fastmcp", [25])))
    assert "A: not comparable: only the fastmcp arm was run" in md
    both = render_markdown(
        summarize(prompts("A", "fastmcp", [25]) + prompts("A", "code", [5]))
    )
    assert "% of direct's prompt tokens" not in both
    assert "A: not comparable: the direct arm was not run" in both


def test_only_code_or_only_direct_keep_their_old_lines() -> None:
    only_code = render_markdown(summarize(prompts("A", "code", [5])))
    assert "- A: not comparable: only the code arm was run" in only_code
    assert "**Code arm vs direct arm**" in only_code
    only_direct = render_markdown(summarize(prompts("A", "direct", [5])))
    assert "- A: not comparable: only the direct arm was run" in only_direct


def test_each_task_is_compared_on_the_arms_it_has() -> None:
    records = (
        prompts("A", "direct", [100])
        + prompts("A", "code", [10])
        + prompts("A", "fastmcp", [20])
        + prompts("B", "direct", [200])
        + prompts("B", "code", [40])
    )
    md = render_markdown(summarize(records))
    assert "- A: fastmcp uses 20% of direct's prompt tokens" in md
    assert "B: fastmcp" not in md
    assert "- B: code uses 20% of direct's prompt tokens" in md


def test_footnote_explains_fastmcp_columns_only_when_the_arm_ran() -> None:
    with_fast = render_markdown(summarize(three_arms()))
    assert "fastmcp arm" in with_fast and "search, get_schema, execute" in with_fast
    assert "`MCP calls` are the MCP tool calls actually executed" in with_fast
