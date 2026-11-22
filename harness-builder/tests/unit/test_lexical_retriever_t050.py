"""Focused tests for ``skills.retriever.LexicalRetriever`` (T050).

T054 owns the retriever contract/quality suite; this file pins behaviour and
measures top-3 hit rate on a small fixture library and search latency over
1,000 synthetic skills (FR-051, OQ-14 ASSUMED thresholds: >= 90%, < 100 ms).
"""

import os
import random
import statistics
import subprocess
import sys
import time

import pytest

from harness_builder.core.protocols import Retriever
from harness_builder.core.types import ScoredSkill, SkillEntry
from harness_builder.skills.retriever import LexicalRetriever, tokenize


def _entry(
    name: str, description: str, triggers: list[str] | None = None
) -> SkillEntry:
    return SkillEntry(
        name=name,
        description=description,
        triggers=triggers or [],
        path=f"/skills/{name}/SKILL.md",
    )


FIXTURE_SKILLS: list[SkillEntry] = [
    _entry(
        "pdf-extract-text",
        "Extract plain text and tables from PDF documents.",
        ["pdf", "read pdf"],
    ),
    _entry("pdf-merge", "Combine several PDF files into a single document."),
    _entry(
        "csv-summary",
        "Load a CSV file and report column statistics such as mean and median.",
        ["spreadsheet", "tabular data"],
    ),
    _entry("csv-to-json", "Convert CSV rows into a JSON array of objects."),
    _entry(
        "http-get-json",
        "Fetch a URL over HTTP and parse the JSON response body.",
        ["download", "api call", "rest"],
    ),
    _entry(
        "git-changelog",
        "Generate a changelog from git commit history between two tags.",
        ["release notes"],
    ),
    _entry("git-blame-line", "Find which commit and author last changed a line."),
    _entry(
        "image-resize",
        "Resize or scale an image to a target width and height.",
        ["thumbnail", "png", "jpeg"],
    ),
    _entry("image-ocr", "Recognise printed text inside an image with OCR."),
    _entry(
        "sql-query-sqlite",
        "Run a SQL query against a SQLite database file and return rows.",
        ["database"],
    ),
    _entry(
        "regex-tester",
        "Test a regular expression pattern against sample strings.",
        ["regex", "pattern matching"],
    ),
    _entry(
        "date-diff",
        "Compute the number of days between two calendar dates.",
        ["timedelta", "how long until"],
    ),
    _entry(
        "unit-convert",
        "Convert measurements between metric and imperial units.",
        ["miles to km", "celsius fahrenheit"],
    ),
    _entry(
        "word-count",
        "Count words, lines and characters in a text file.",
        ["wc"],
    ),
    _entry(
        "zip-archive",
        "Compress files and folders into a zip archive or extract one.",
        ["unzip", "compress"],
    ),
    _entry(
        "email-validate",
        "Check whether an email address is syntactically valid.",
    ),
    _entry(
        "markdown-to-html",
        "Render Markdown text as HTML.",
        ["md"],
    ),
    _entry(
        "hash-file",
        "Compute a SHA-256 or MD5 checksum of a file.",
        ["checksum", "digest"],
    ),
    _entry(
        "json-schema-validate",
        "Validate a JSON document against a JSON Schema.",
    ),
    _entry(
        "plot-line-chart",
        "Draw a line chart from x and y values and save it as PNG.",
        ["graph", "visualize"],
    ),
]

FIXTURE_QUERIES: list[tuple[str, str]] = [
    # (query, expected skill name). Written before any weight tuning; several
    # are paraphrases with little word overlap on purpose.
    ("get the text out of this pdf", "pdf-extract-text"),
    ("join three PDFs together into one file", "pdf-merge"),
    ("what is the mean of each column in sales.csv", "csv-summary"),
    ("turn my csv into json", "csv-to-json"),
    ("call the REST api and read the json it returns", "http-get-json"),
    ("write release notes from the commits since v1.2", "git-changelog"),
    ("who last changed line 40 of main.py", "git-blame-line"),
    ("make a thumbnail of photo.png", "image-resize"),
    ("read the words in this scanned image", "image-ocr"),
    ("query the sqlite database for all users", "sql-query-sqlite"),
    ("does my regex match these strings", "regex-tester"),
    ("how many days between 2024-01-01 and 2024-03-15", "date-diff"),
    ("convert 10 miles to km", "unit-convert"),
    ("count the lines in notes.txt", "word-count"),
    ("unzip the archive into a folder", "zip-archive"),
    ("is foo@bar a valid email address", "email-validate"),
    ("render README markdown as html", "markdown-to-html"),
    ("compute the sha-256 checksum of a file", "hash-file"),
    ("validate config.json against its schema", "json-schema-validate"),
    ("draw a graph of temperature over time", "plot-line-chart"),
    ("scale the picture down to 200 pixels wide", "image-resize"),
    ("how long until christmas", "date-diff"),
    ("download https://example.com/data and parse the response", "http-get-json"),
    ("compress the logs folder", "zip-archive"),
    ("fahrenheit to celsius for 98.6", "unit-convert"),
    ("recognise text in a screenshot", "image-ocr"),
    ("statistics for a spreadsheet of numbers", "csv-summary"),
    ("visualize these points as a line chart", "plot-line-chart"),
    ("md5 digest of the installer", "hash-file"),
    ("extract tables from a report PDF", "pdf-extract-text"),
]

TOP3_TARGET = 0.90
LATENCY_TARGET_S = 0.100


@pytest.fixture
def retriever() -> LexicalRetriever:
    """A retriever indexed with the fixture library."""
    lexical = LexicalRetriever()
    lexical.index(FIXTURE_SKILLS)
    return lexical


def _names(hits: list[ScoredSkill]) -> list[str]:
    return [hit.entry.name for hit in hits]


# --- protocol and basics ---------------------------------------------------


def test_satisfies_retriever_protocol() -> None:
    proto_check: Retriever = LexicalRetriever()
    assert isinstance(proto_check, LexicalRetriever)


def test_import_has_no_heavy_dependencies() -> None:
    code = (
        "import sys, harness_builder.skills.retriever\n"
        "bad = [m for m in sys.modules if m.startswith("
        "('skillful_agent', 'google.adk', 'google.genai', 'litellm'))]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_search_returns_best_first_with_positive_scores(
    retriever: LexicalRetriever,
) -> None:
    hits = retriever.search("merge pdf files", k=3)
    assert hits[0].entry.name == "pdf-merge"
    assert all(hit.score > 0 for hit in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_empty_catalog_returns_nothing() -> None:
    lexical = LexicalRetriever()
    assert lexical.search("anything") == []
    lexical.index([])
    assert lexical.search("anything") == []


@pytest.mark.parametrize("query", ["", "   ", "the and of to", "!!! ---"])
def test_query_without_terms_returns_nothing(
    retriever: LexicalRetriever, query: str
) -> None:
    assert retriever.search(query) == []


@pytest.mark.parametrize("k", [0, -1])
def test_non_positive_k_returns_nothing(retriever: LexicalRetriever, k: int) -> None:
    assert retriever.search("pdf", k=k) == []


def test_k_larger_than_hits_returns_only_matches(retriever: LexicalRetriever) -> None:
    hits = retriever.search("pdf", k=50)
    assert set(_names(hits)) == {"pdf-extract-text", "pdf-merge"}


def test_no_overlap_returns_nothing(retriever: LexicalRetriever) -> None:
    assert retriever.search("quantum chromodynamics") == []


def test_reindex_replaces_previous_entries(retriever: LexicalRetriever) -> None:
    retriever.index([_entry("only-skill", "Bake sourdough bread.")])
    assert retriever.search("pdf") == []
    assert _names(retriever.search("bread")) == ["only-skill"]


def test_index_copies_input_list() -> None:
    entries = [_entry("alpha", "Parse YAML files.")]
    lexical = LexicalRetriever()
    lexical.index(entries)
    entries.append(_entry("beta", "Parse YAML strings."))
    assert _names(lexical.search("yaml", k=5)) == ["alpha"]


def test_entries_with_empty_fields_do_not_crash() -> None:
    lexical = LexicalRetriever()
    lexical.index([_entry("a", ""), SkillEntry(name="the", description="of")])
    assert lexical.search("a") == []


# --- field weighting and tokenisation --------------------------------------


def test_name_weighs_more_than_description() -> None:
    lexical = LexicalRetriever()
    lexical.index(
        [
            _entry("report-builder", "Builds a summary from yaml input."),
            _entry("yaml-lint", "Checks a report for mistakes."),
        ]
    )
    assert _names(lexical.search("yaml")) == ["yaml-lint", "report-builder"]


def test_triggers_weigh_more_than_description() -> None:
    lexical = LexicalRetriever()
    lexical.index(
        [
            _entry("skill-a", "Works with invoices and receipts."),
            _entry("skill-b", "Works with ledgers.", ["invoice"]),
        ]
    )
    assert _names(lexical.search("invoice")) == ["skill-b", "skill-a"]


def test_triggers_alone_can_match() -> None:
    lexical = LexicalRetriever()
    lexical.index([_entry("skill-a", "Generic helper.", ["kubernetes deploy"])])
    assert _names(lexical.search("deploy to kubernetes")) == ["skill-a"]


def test_name_is_split_on_hyphen() -> None:
    assert tokenize("pdf-extract-text") == ["pdf", "extract", "text"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CAFÉ Menu", ["café", "menu"]),
        ("ﬁle", ["file"]),  # NFKC: the fi ligature
        ("Straße", ["strass"]),  # casefold, then a trailing e is stripped
        ("snake_case-and.dots", ["snak", "case", "dot"]),
        ("files processes class bus", ["file", "process", "class", "bus"]),
        ("uuid4 mp3", ["uuid", "4", "mp", "3"]),  # letter/digit split
        (
            "queries extracting formatted validation",
            ["query", "extract", "format", "validat"],
        ),
        ("create validates installing", ["creat", "validat", "install"]),
        ("データ 解析", ["データ", "解析"]),
    ],
)
def test_tokenize_normalises_unicode_and_case(text: str, expected: list[str]) -> None:
    assert tokenize(text) == expected


def test_unicode_query_matches_unicode_entry() -> None:
    lexical = LexicalRetriever()
    lexical.index(
        [
            _entry("menu-fr", "Traduire le MENU du CAFÉ."),
            _entry("data-jp", "データ 解析 ツール"),
            _entry("plain", "Translate a menu."),
        ]
    )
    assert _names(lexical.search("café"))[0] == "menu-fr"
    assert _names(lexical.search("データ")) == ["data-jp"]


# --- determinism ------------------------------------------------------------


def test_ties_break_on_name_then_path() -> None:
    entries = [
        SkillEntry(name="zeta", description="Same text here.", path="/b"),
        SkillEntry(name="alpha", description="Same text here.", path="/z"),
        SkillEntry(name="alpha", description="Same text here.", path="/a"),
    ]
    lexical = LexicalRetriever()
    lexical.index(entries)
    hits = lexical.search("same text", k=3)
    assert [(h.entry.name, h.entry.path) for h in hits] == [
        ("alpha", "/a"),
        ("alpha", "/z"),
        ("zeta", "/b"),
    ]
    assert len({h.score for h in hits}) == 1


def test_ranking_independent_of_input_order() -> None:
    shuffled = list(FIXTURE_SKILLS)
    random.Random(7).shuffle(shuffled)
    first, second = LexicalRetriever(), LexicalRetriever()
    first.index(FIXTURE_SKILLS)
    second.index(shuffled)
    for query, _expected in FIXTURE_QUERIES:
        assert first.search(query, k=5) == second.search(query, k=5)


def test_ranking_stable_across_hash_seeds() -> None:
    code = (
        "from harness_builder.skills.retriever import LexicalRetriever\n"
        "from tests.unit.test_lexical_retriever_t050 import FIXTURE_SKILLS\n"
        "r = LexicalRetriever(); r.index(FIXTURE_SKILLS)\n"
        "print([(h.entry.name, h.score) for h in r.search("
        "'convert csv json data file', k=5)])\n"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("0", "1", "12345")
    }
    assert len(outputs) == 1


# --- dedup similarity helper ------------------------------------------------


def test_similarity_identical_text_is_one(retriever: LexicalRetriever) -> None:
    text = "Combine several PDF files into a single document."
    assert retriever.similarity(text, text) == 1.0


def test_similarity_works_on_empty_index() -> None:
    lexical = LexicalRetriever()
    assert lexical.similarity("parse yaml files", "parse yaml files") == 1.0
    assert lexical.similarity("parse yaml files", "bake bread") == 0.0


def test_similarity_calibration(retriever: LexicalRetriever) -> None:
    original = "Combine several PDF files into a single document."
    # Short texts are sensitive: one substituted word out of six content
    # terms ("several" -> "multiple") scores about 0.77, below 0.8.
    near_dups = [
        "Combine several PDF files together into a single document.",
        "Combine PDF files into a single document.",
    ]
    same_domain = "Extract plain text and tables from PDF documents."
    unrelated = "Compute the number of days between two calendar dates."
    assert all(retriever.similarity(original, dup) >= 0.8 for dup in near_dups)
    assert retriever.similarity(original, same_domain) < 0.8
    assert retriever.similarity(original, unrelated) == 0.0


def test_similarity_is_symmetric_and_bounded(retriever: LexicalRetriever) -> None:
    a = "Convert CSV rows into a JSON array."
    b = "Validate a JSON document against a schema."
    assert retriever.similarity(a, b) == retriever.similarity(b, a)
    assert 0.0 < retriever.similarity(a, b) < 1.0


@pytest.mark.parametrize(("a", "b"), [("", "pdf"), ("pdf", "the of"), ("", "")])
def test_similarity_without_terms_is_zero(
    retriever: LexicalRetriever, a: str, b: str
) -> None:
    assert retriever.similarity(a, b) == 0.0


# --- measured quality and latency ------------------------------------------


def test_top3_hit_rate_on_fixture_library(retriever: LexicalRetriever) -> None:
    misses = [
        (query, expected, _names(retriever.search(query, k=3)))
        for query, expected in FIXTURE_QUERIES
        if expected not in _names(retriever.search(query, k=3))
    ]
    hit_rate = 1 - len(misses) / len(FIXTURE_QUERIES)
    print(f"top-3 hit rate {hit_rate:.1%} ({len(FIXTURE_QUERIES)} queries)")
    assert hit_rate >= TOP3_TARGET, misses


_VOCAB = (
    "parse convert extract render validate compute fetch merge split compress "
    "resize count query plot schedule deploy encrypt decode format lint test "
    "json yaml csv xml pdf image audio video email date time file folder "
    "database table chart report invoice log metric token secret archive "
    "cloud bucket queue cache index graph tree list string number matrix"
).split()


def _synthetic_catalog(size: int) -> list[SkillEntry]:
    rng = random.Random(1234)
    return [
        SkillEntry(
            name=f"{rng.choice(_VOCAB)}-{rng.choice(_VOCAB)}-{i}",
            description=" ".join(rng.choices(_VOCAB, k=18)),
            triggers=[" ".join(rng.choices(_VOCAB, k=2)) for _ in range(2)],
            path=f"/skills/s{i}/SKILL.md",
        )
        for i in range(size)
    ]


def test_search_latency_over_1000_skills() -> None:
    catalog = _synthetic_catalog(1000)
    lexical = LexicalRetriever()
    started = time.perf_counter()
    lexical.index(catalog)
    index_s = time.perf_counter() - started
    rng = random.Random(99)
    queries = [" ".join(rng.choices(_VOCAB, k=8)) for _ in range(20)]
    timings = []
    for query in queries:
        started = time.perf_counter()
        hits = lexical.search(query, k=3)
        timings.append(time.perf_counter() - started)
        assert len(hits) == 3
    median_s = statistics.median(timings)
    print(
        f"index {index_s * 1000:.1f} ms; search median {median_s * 1000:.2f} ms, "
        f"max {max(timings) * 1000:.2f} ms over {len(queries)} queries"
    )
    assert median_s < LATENCY_TARGET_S
    assert index_s < 5.0
