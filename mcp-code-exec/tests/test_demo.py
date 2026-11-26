"""Tests for the first-look demo: hand-written task-B code through MontyRuntime."""

import re

import pytest

from mcpx.demo import run_demo
from mcpx.targets.ticketdesk.seed import get_dataset

pytestmark = pytest.mark.anyio


def _expected_top_ids() -> list[int]:
    data = get_dataset()
    bugs = [t for t in data.tickets if t.status == "open" and "bug" in t.labels]
    bugs.sort(key=lambda t: (-data.comment_count(t.id), t.id))
    return [t.id for t in bugs[:5]]


async def test_demo_reports_top_five_and_both_figures() -> None:
    report = await run_demo()
    ids = [int(m) for m in re.findall(r"^\s*#(\d+)\s", report, flags=re.MULTILINE)]
    assert ids == _expected_top_ids()
    raw = re.search(r"raw tool-result JSON[^\d]*([\d,]+) chars", report)
    printed = re.search(r"printed back[^\d]*([\d,]+) chars", report)
    assert raw and printed, report
    raw_chars = int(raw.group(1).replace(",", ""))
    printed_chars = int(printed.group(1).replace(",", ""))
    assert printed_chars > 0
    assert printed_chars * 20 < raw_chars
