"""T054: retrieval quality and speed on fixtures independent of T050's.

Quality runs the real pipeline: the committed fixture library on disk ->
``SkillStore.catalog_entries`` (frontmatter ``triggers`` included) ->
``LexicalRetriever.search(k=3)``. The queries in ``fixtures/queries.json``
were written as a user would phrase a task and committed before the first
measurement; they were never tuned to the scorer. FR-051 thresholds are
ASSUMED (OQ-14): top-3 hit rate >= 90 %, search over 1,000 skills < 100 ms.
Exact scores are never asserted.
"""

import random
import statistics
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from harness_builder.core.types import SkillEntry
from harness_builder.skills.extension import SkillsExtension
from harness_builder.skills.retriever import LexicalRetriever
from harness_builder.skills.store import SkillStore

HIT_RATE_THRESHOLD = 0.90
"""FR-051 top-3 hit rate (ASSUMED, OQ-14)."""
REGRESSION_FLOOR = 48 / 52 - 1e-9
"""Hit rate measured after the T056 stemmer (48 of 52; it was 46 of 52 at
cc3f78d); a drop below it is a regression."""
CACHED_SELECT_LIMIT_S = 0.5
"""Run-start budget with a warm cache (designs 10.2 asks for 0.1 s of search)."""
LATENCY_THRESHOLD_S = 0.100
"""PRD performance target for one search over 1,000 skills (ASSUMED)."""
TOP_K = 3
LIBRARY_SIZE = 1_000
TIMED_QUERIES = 50

Recorder = Callable[[str, object], None]

_VOCAB = (
    "file data text report table record value list number date time user "
    "account order invoice payment customer product price stock image photo "
    "video audio sound page site link email message chat note document sheet "
    "row column field key token secret config setting log event error alert "
    "metric chart graph map route address location city country language "
    "translate convert format parse validate check clean merge split sort "
    "filter group count sum average compare search index extract render "
    "generate build export import upload download compress archive encrypt "
    "decrypt sign verify hash schedule remind notify sync backup restore "
    "monitor measure score rank classify detect summarize annotate label tag "
    "queue batch stream cache retry limit quota budget forecast trend weekly "
    "daily monthly yearly local remote cloud server client api request "
    "response header body json yaml xml html markdown csv spreadsheet "
    "database query schema migration model template layout style theme color "
    "font icon button form input output command script task job worker"
).split()


def fixture_entries(store: SkillStore) -> list[SkillEntry]:
    """Catalog entries of a store (the path the contributor uses)."""
    return store.catalog_entries(store.manager())


def synthetic_library(rng: random.Random, size: int) -> list[SkillEntry]:
    """``size`` skills with ~200-char descriptions over a shared vocabulary."""
    entries: list[SkillEntry] = []
    for i in range(size):
        words = rng.sample(_VOCAB, 3)
        description = " ".join(rng.choices(_VOCAB, k=40))[:200]
        triggers = [" ".join(rng.sample(_VOCAB, 2)) for _ in range(rng.randint(0, 3))]
        entries.append(
            SkillEntry(
                name=f"{'-'.join(words)}-{i}",
                description=description,
                triggers=triggers,
                path=f"/lib/{i}/SKILL.md",
            )
        )
    return entries


def test_queries_never_name_their_gold_skill_verbatim(
    labelled_queries: list[tuple[str, str]], library_dir: Path
) -> None:
    names = {p.name for p in library_dir.iterdir() if p.is_dir()}
    assert len(labelled_queries) >= 50
    for query, gold in labelled_queries:
        assert gold in names, gold
        assert gold not in query.lower(), query
    assert len({gold for _, gold in labelled_queries}) >= 30


def test_catalog_carries_frontmatter_triggers(fixture_store: SkillStore) -> None:
    by_name = {e.name: e for e in fixture_entries(fixture_store)}
    assert by_name["jwt-decode"].triggers == ["bearer token"]
    assert by_name["uuid-generate"].triggers == []
    assert all(e.path and e.path.endswith("SKILL.md") for e in by_name.values())


def measure_hit_rate(
    store: SkillStore, queries: list[tuple[str, str]]
) -> tuple[float, list[tuple[str, str, list[str]]]]:
    """Top-3 hit rate over ``queries`` and the misses as (query, gold, top-3)."""
    retriever = LexicalRetriever()
    retriever.index(fixture_entries(store))
    misses: list[tuple[str, str, list[str]]] = []
    for query, gold in queries:
        top = [hit.entry.name for hit in retriever.search(query, k=TOP_K)]
        if gold not in top:
            misses.append((query, gold, top))
    return 1 - len(misses) / len(queries), misses


def test_top3_hit_rate_meets_the_assumed_threshold(
    fixture_store: SkillStore,
    labelled_queries: list[tuple[str, str]],
    record_property: Recorder,
) -> None:
    rate, misses = measure_hit_rate(fixture_store, labelled_queries)
    record_property("top3_hit_rate", round(rate, 4))
    record_property("top3_misses", misses)
    print(f"top-3 hit rate {rate:.1%} ({len(misses)} misses): {misses}")
    assert rate >= HIT_RATE_THRESHOLD, misses


def test_top3_hit_rate_does_not_regress_below_the_measured_baseline(
    fixture_store: SkillStore, labelled_queries: list[tuple[str, str]]
) -> None:
    rate, misses = measure_hit_rate(fixture_store, labelled_queries)
    assert rate >= REGRESSION_FLOOR, misses


def test_hits_are_positive_best_first_and_at_most_k(
    fixture_store: SkillStore, labelled_queries: list[tuple[str, str]]
) -> None:
    retriever = LexicalRetriever()
    retriever.index(fixture_entries(fixture_store))
    for query, _ in labelled_queries:
        hits = retriever.search(query, k=TOP_K)
        scores = [hit.score for hit in hits]
        assert len(hits) <= TOP_K
        assert all(score > 0 for score in scores)
        assert scores == sorted(scores, reverse=True)


def test_search_latency_over_1000_skills(record_property: Recorder) -> None:
    rng = random.Random(20261120)
    entries = synthetic_library(rng, LIBRARY_SIZE)
    retriever = LexicalRetriever()
    started = time.perf_counter()
    retriever.index(entries)
    index_s = time.perf_counter() - started
    common = _VOCAB[:60]  # every word is frequent: long posting lists
    queries = [" ".join(rng.sample(common, 8)) for _ in range(TIMED_QUERIES)]
    retriever.search(queries[0], k=TOP_K)  # warm-up
    timings: list[float] = []
    for query in queries:
        started = time.perf_counter()
        hits = retriever.search(query, k=TOP_K)
        timings.append(time.perf_counter() - started)
        assert len(hits) == TOP_K
    median = statistics.median(timings)
    p95 = statistics.quantiles(timings, n=20)[-1]
    record_property("search_median_ms", round(median * 1000, 3))
    record_property("search_p95_ms", round(p95 * 1000, 3))
    record_property("search_max_ms", round(max(timings) * 1000, 3))
    record_property("index_ms", round(index_s * 1000, 1))
    print(
        f"1000 skills: median {median * 1000:.2f} ms, p95 {p95 * 1000:.2f} ms, "
        f"max {max(timings) * 1000:.2f} ms, index {index_s * 1000:.1f} ms"
    )
    assert median < LATENCY_THRESHOLD_S
    assert p95 < LATENCY_THRESHOLD_S


@pytest.mark.timeout(300)
def test_per_run_selection_over_1000_skills_on_disk(
    tmp_path: Path, record_property: Recorder
) -> None:
    """T058 MA-2: a cached run start stats the library, it does not re-parse it."""
    rng = random.Random(54)
    store = SkillStore(tmp_path)
    for entry in synthetic_library(rng, LIBRARY_SIZE - 1):
        skill = store.live / entry.name
        skill.mkdir(parents=True)
        text = f"---\nname: {entry.name}\ndescription: {entry.description}\n---\n"
        (skill / "SKILL.md").write_bytes(text.encode("utf-8"))
    planted = store.live / "zebra-stripe-count"
    planted.mkdir()
    (planted / "SKILL.md").write_bytes(
        b"---\nname: zebra-stripe-count\n"
        b"description: Count the stripes on a zebra photograph.\n---\n"
    )
    ext = SkillsExtension(store, top_k=TOP_K)
    ext._select("warm import")  # first discovery imports the sibling and ADK
    cold = SkillsExtension(store, top_k=TOP_K)
    started = time.perf_counter()
    cold._select("how many stripes does this zebra have")
    cold_s = time.perf_counter() - started
    started = time.perf_counter()
    _, chosen = cold._select("how many stripes does this zebra have")
    cached_s = time.perf_counter() - started
    record_property("select_1000_cold_ms", round(cold_s * 1000, 1))
    record_property("select_1000_cached_ms", round(cached_s * 1000, 1))
    print(
        f"per-run selection over 1000 on-disk skills: cold {cold_s * 1000:.0f} ms,"
        f" cached {cached_s * 1000:.0f} ms"
    )
    assert chosen[0].name == "zebra-stripe-count"
    assert cached_s < CACHED_SELECT_LIMIT_S
