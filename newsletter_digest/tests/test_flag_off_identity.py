"""Flag-off byte identity (AF-9, FR-023 acceptance 10).

The goldens in ``tests/fixtures/`` were rendered by the code at commit ``bb16231``
(before the article-fetching amendment) from the builders in
``tests/flag_off_fixture.py``. With ``--fetch-articles`` off, the current code must
produce exactly the same bytes and the same ``safe_summary()`` keys. Never regenerate
the goldens from the current tree; see ``shared/implementation-notes.md`` (T023).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from newsletter_digest.config import Settings
from newsletter_digest.models import ReadingList
from newsletter_digest.render import render_html, write_reading_list
from tests.flag_off_fixture import (
    SAFE_SUMMARY_BUILD_KWARGS,
    build_empty_reading_list,
    build_ranked_reading_list,
    build_unranked_reading_list,
)

FIXTURES = Path(__file__).parent / "fixtures"
RANKED_GOLDEN = FIXTURES / "flag_off_ranked_golden.html"
UNRANKED_GOLDEN = FIXTURES / "flag_off_unranked_golden.html"
EMPTY_GOLDEN = FIXTURES / "flag_off_empty_golden.html"
SAFE_SUMMARY_KEYS_GOLDEN = FIXTURES / "flag_off_safe_summary_keys.json"
CONTEXT_BYTES = 80


def first_difference(actual: bytes, expected: bytes) -> str:
    """Describe where two byte strings first differ, with 80 bytes of context.

    Returns an empty string when they are equal. Only a window around the first
    differing offset is shown, never the whole document.
    """
    if actual == expected:
        return ""
    offset = next(
        (i for i, (a, b) in enumerate(zip(actual, expected, strict=False)) if a != b),
        min(len(actual), len(expected)),
    )
    start = max(0, offset - CONTEXT_BYTES // 2)
    end = start + CONTEXT_BYTES
    return (
        f"first difference at byte {offset} "
        f"(actual {len(actual)} bytes, golden {len(expected)} bytes)\n"
        f"actual: {actual[start:end]!r}\n"
        f"golden: {expected[start:end]!r}"
    )


def assert_same_bytes(actual: bytes, golden: Path) -> None:
    """Assert ``actual`` equals the golden file byte for byte (short failure text)."""
    expected = golden.read_bytes()
    diff = first_difference(actual, expected)
    assert not diff, f"{golden.name}: {diff}"


@pytest.mark.parametrize(
    ("builder", "golden"),
    [
        (build_ranked_reading_list, RANKED_GOLDEN),
        (build_unranked_reading_list, UNRANKED_GOLDEN),
        (build_empty_reading_list, EMPTY_GOLDEN),
    ],
    ids=["ranked", "unranked", "empty"],
)
def test_render_html_matches_bb16231_golden(
    builder: Callable[[], ReadingList], golden: Path
) -> None:
    assert_same_bytes(render_html(builder()).encode("utf-8"), golden)


def test_write_reading_list_matches_ranked_golden(tmp_path: Path) -> None:
    path = write_reading_list(build_ranked_reading_list(), tmp_path)
    assert path == tmp_path / "reading-list-2026-10-31.html"
    assert_same_bytes(path.read_bytes(), RANKED_GOLDEN)


def test_safe_summary_keys_match_bb16231_golden() -> None:
    settings = Settings.build(**SAFE_SUMMARY_BUILD_KWARGS)
    expected = json.loads(SAFE_SUMMARY_KEYS_GOLDEN.read_text(encoding="utf-8"))
    assert sorted(settings.safe_summary()) == expected


def test_safe_summary_never_contains_the_api_key() -> None:
    summary = Settings.build(**SAFE_SUMMARY_BUILD_KWARGS).safe_summary()
    key = SAFE_SUMMARY_BUILD_KWARGS["api_key"]
    assert key is not None
    assert all(key not in str(value) for value in summary.values())
    assert "openrouter_api_key" not in summary


def test_fixture_module_resolves_inside_tests_package() -> None:
    import tests.flag_off_fixture as fixture

    assert Path(fixture.__file__).resolve().parent == Path(__file__).resolve().parent


@pytest.mark.parametrize(
    ("actual", "expected", "offset"),
    [
        (b"abcdef", b"abcXef", 3),
        (b"abc", b"abcdef", 3),
        (b"abcdef", b"abc", 3),
        (b"x", b"y", 0),
    ],
)
def test_first_difference_reports_offset(
    actual: bytes, expected: bytes, offset: int
) -> None:
    assert f"first difference at byte {offset} " in first_difference(actual, expected)


def test_first_difference_is_empty_for_equal_bytes() -> None:
    assert first_difference(b"same", b"same") == ""


def test_first_difference_shows_only_a_window() -> None:
    golden = b"a" * 10_000 + b"Z" + b"a" * 10_000
    actual = b"a" * 20_001
    message = first_difference(actual, golden)
    assert "first difference at byte 10000 " in message
    assert len(message) < 400
