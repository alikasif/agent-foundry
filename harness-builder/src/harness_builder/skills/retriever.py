"""Lexical skill retrieval: a pure-Python BM25F ``Retriever`` (T050, D4).

Each ``SkillEntry`` is one document with three weighted fields: ``name``
(split on ``-``), ``description`` and ``triggers``. A field's term frequency
is multiplied by its weight before BM25 saturation (BM25F), so a name or
trigger hit counts more than a description hit.

Ranking is deterministic: query terms are scored in first-seen order,
postings follow entry order, scores are rounded and ties break on
``(name, path)``. No ``set`` of strings is ever iterated while summing.
"""

import heapq
import math
import re
import unicodedata
from collections import Counter

from harness_builder.core.types import ScoredSkill, SkillEntry

NAME_WEIGHT = 2.0
DESCRIPTION_WEIGHT = 1.0
TRIGGER_WEIGHT = 1.5
K1 = 1.2
B = 0.75
SCORE_DIGITS = 9
"""Scores are rounded so float noise cannot reorder equal documents."""

_TOKEN_RE = re.compile(r"[^\W_]+")
"""Runs of letters/digits in any script; ``-``, ``_`` and punctuation split."""

STOPWORDS: frozenset[str] = frozenset(
    "a an and are as at be by can do for from how i in into is it its me my "
    "of on or that the this to use using want what when which with you your".split()
)


def tokenize(text: str) -> list[str]:
    """Split ``text`` into normalised index terms.

    NFKC normalisation and ``casefold`` make ``CAFÉ``/``café`` and the ``ﬁ``
    ligature/``fi`` equal. Stopwords are dropped and a plural ``s`` is
    stripped from words longer than three letters (not ``ss``/``us``/``is``;
    ``sses`` becomes ``ss``).
    Text in scripts without spaces (for example CJK) becomes one term per run.
    """
    words = _TOKEN_RE.findall(unicodedata.normalize("NFKC", text).casefold())
    return [_stem(word) for word in words if word not in STOPWORDS]


def _stem(word: str) -> str:
    if word.endswith("sses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _weighted_terms(entry: SkillEntry) -> dict[str, float]:
    weighted: dict[str, float] = {}
    fields = (
        (entry.name, NAME_WEIGHT),
        (entry.description, DESCRIPTION_WEIGHT),
        (" ".join(entry.triggers), TRIGGER_WEIGHT),
    )
    for text, weight in fields:
        for term in tokenize(text):
            weighted[term] = weighted.get(term, 0.0) + weight
    return weighted


def _idf(doc_count: int, doc_freq: int) -> float:
    """Non-negative BM25 IDF; an unseen term gets the largest value."""
    return math.log(1.0 + (doc_count - doc_freq + 0.5) / (doc_freq + 0.5))


class LexicalRetriever:
    """BM25F retriever over skill name, description and triggers.

    ``index`` replaces all earlier state with a copy of ``entries``. Entries
    with the same name are all kept; ``path`` breaks their tie. ``search``
    returns at most ``k`` entries with a positive score, best first.
    """

    def __init__(self) -> None:
        """Create an empty retriever; ``search`` returns ``[]`` until indexed."""
        self._entries: tuple[SkillEntry, ...] = ()
        self._postings: dict[str, list[tuple[int, float]]] = {}
        self._norms: list[float] = []

    def index(self, entries: list[SkillEntry]) -> None:
        """Replace the index with ``entries`` (copied; later edits do not leak)."""
        self._entries = tuple(entries)
        docs = [_weighted_terms(entry) for entry in self._entries]
        lengths = [sum(doc.values()) for doc in docs]
        avg_length = sum(lengths) / len(lengths) if lengths else 0.0
        self._norms = [
            K1 * (1.0 - B + B * length / avg_length) if avg_length else K1
            for length in lengths
        ]
        postings: dict[str, list[tuple[int, float]]] = {}
        for doc_id, doc in enumerate(docs):
            for term, tf in doc.items():
                postings.setdefault(term, []).append((doc_id, tf))
        self._postings = postings

    def search(self, query: str, k: int = 3) -> list[ScoredSkill]:
        """Return up to ``k`` best entries for ``query``, best first.

        Returns ``[]`` for ``k <= 0``, an empty index, or a query without
        index terms. Ties are broken by entry name, then path.
        """
        if k <= 0 or not self._entries:
            return []
        doc_count = len(self._entries)
        scores: dict[int, float] = {}
        for term in dict.fromkeys(tokenize(query)):
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = _idf(doc_count, len(postings))
            for doc_id, tf in postings:
                gain = idf * tf * (K1 + 1.0) / (tf + self._norms[doc_id])
                scores[doc_id] = scores.get(doc_id, 0.0) + gain
        ranked = (
            (round(score, SCORE_DIGITS), self._entries[doc_id])
            for doc_id, score in scores.items()
        )
        best = heapq.nsmallest(
            k, ranked, key=lambda hit: (-hit[0], hit[1].name, hit[1].path or "")
        )
        return [ScoredSkill(entry=entry, score=score) for score, entry in best]

    def similarity(self, text_a: str, text_b: str) -> float:
        """Return an IDF-weighted cosine similarity of two texts in ``[0, 1]``.

        Used for dedup (designs.md 10.3, threshold 0.8). Neither text has to
        be indexed: IDF comes from the current index, a term it has not seen
        gets the largest IDF, and with an empty index every term weighs the
        same. Identical texts give 1.0; a text without terms gives 0.0.
        """
        terms_a = Counter(tokenize(text_a))
        terms_b = Counter(tokenize(text_b))
        if not terms_a or not terms_b:
            return 0.0
        doc_count = len(self._entries)

        def weight(term: str) -> float:
            postings = self._postings.get(term)
            return _idf(doc_count, len(postings) if postings else 0)

        weights = {term: weight(term) for term in sorted(terms_a | terms_b)}
        dot = sum(
            terms_a[term] * terms_b[term] * weights[term] ** 2
            for term in sorted(terms_a.keys() & terms_b.keys())
        )
        norm_a = math.sqrt(sum((n * weights[t]) ** 2 for t, n in terms_a.items()))
        norm_b = math.sqrt(sum((n * weights[t]) ** 2 for t, n in terms_b.items()))
        return min(1.0, round(dot / (norm_a * norm_b), SCORE_DIGITS))
