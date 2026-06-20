"""Synthetic dataset generators for the RLM long-context eval suite.

These four generators are **inspired by**, not reproducing, the benchmarks in
the Recursive Language Models paper (arXiv:2512.24601): S-NIAH,
BrowseComp-Plus, OOLONG (trec_coarse), and OOLONG-Pairs.  They produce
deterministic synthetic data (seeded via a local ``random.Random``) so the
eval harness can run offline and CI-safe, with scores reported as
RLM-vs-baseline *relative* deltas rather than paper-comparable absolutes.

This module is pure data generation: it performs no LLM calls, imports no
agent code, and has no import side effects.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Shared filler-text generation
# ---------------------------------------------------------------------------

_WORDS: tuple[str, ...] = (
    "system",
    "process",
    "value",
    "memory",
    "network",
    "signal",
    "vector",
    "matrix",
    "cluster",
    "kernel",
    "buffer",
    "thread",
    "module",
    "pipeline",
    "gradient",
    "tensor",
    "policy",
    "context",
    "token",
    "weight",
)


def _sentence(rng: random.Random) -> str:
    """Build one filler sentence from the word pool."""
    n = rng.randint(8, 16)
    words = [rng.choice(_WORDS) for _ in range(n)]
    words[0] = words[0].capitalize()
    return " ".join(words) + "."


def _filler(rng: random.Random, n_chars: int) -> str:
    """Generate at least *n_chars* of filler text (sentence-aligned)."""
    parts: list[str] = []
    total = 0
    while total < n_chars:
        s = _sentence(rng)
        parts.append(s)
        total += len(s) + 1
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 1. S-NIAH — Single Needle in a Haystack
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SniahItem:
    """A single needle-in-a-haystack item."""

    context: str
    question: str
    expected_answer: str
    needle_sentence: str
    needle_char_offset: int
    n_chars: int


def generate_sniah(
    seed: int = 42,
    *,
    n_chars: int = 8000,
    needle_position: float = 0.75,
) -> SniahItem:
    """Generate an S-NIAH-style item.

    A filler haystack with one short "needle" fact inserted deep in the
    document; the question's answer is only in the needle.

    Args:
        seed: RNG seed for deterministic generation.
        n_chars: Approximate haystack size in characters.
        needle_position: Fractional position (0..1) to insert the needle.

    Returns:
        A :class:`SniahItem`.
    """
    rng = random.Random(seed)
    magic = rng.randint(100_000, 999_999)
    needle = f"The secret access code for the vault is {magic}."
    question = "What is the secret access code for the vault?"

    head = _filler(rng, int(n_chars * needle_position))
    tail = _filler(rng, n_chars - len(head))
    offset = len(head) + 1
    context = f"{head} {needle} {tail}"
    return SniahItem(
        context=context,
        question=question,
        expected_answer=str(magic),
        needle_sentence=needle,
        needle_char_offset=offset,
        n_chars=len(context),
    )


# ---------------------------------------------------------------------------
# 2. BrowseComp-Plus — multi-document corpus QA
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrowseDoc:
    """One document in a multi-doc corpus."""

    doc_id: str
    title: str
    body: str


@dataclass(frozen=True)
class BrowseCompItem:
    """A multi-doc corpus QA item."""

    docs: tuple[BrowseDoc, ...]
    question: str
    expected_answer: str
    answer_doc_id: str

    def corpus(self) -> str:
        """Assemble all docs into a single corpus blob."""
        return "\n\n".join(f"### {d.doc_id}: {d.title}\n{d.body}" for d in self.docs)


def generate_browsecomp(
    seed: int = 42,
    *,
    k_docs: int = 8,
    paragraphs_per_doc: int = 3,
    para_chars: int = 400,
) -> BrowseCompItem:
    """Generate a BrowseComp-Plus-style multi-doc QA item.

    K synthetic documents, exactly one carrying the answer; the question
    requires locating the right doc and extracting the answer.

    Args:
        seed: RNG seed.
        k_docs: Number of documents in the corpus.
        paragraphs_per_doc: Paragraphs per document.
        para_chars: Approx characters per paragraph.

    Returns:
        A :class:`BrowseCompItem`.
    """
    rng = random.Random(seed)
    topics = [rng.choice(_WORDS).capitalize() for _ in range(k_docs)]
    answer_idx = rng.randrange(k_docs)
    code = rng.randint(1000, 9999)
    project = f"Project-{rng.choice(_WORDS).capitalize()}-{code}"
    question = "What is the name of the flagship project?"

    docs: list[BrowseDoc] = []
    for i in range(k_docs):
        body_parts = [_filler(rng, para_chars) for _ in range(paragraphs_per_doc)]
        if i == answer_idx:
            body_parts.insert(1, f"The flagship project is named {project}.")
        docs.append(
            BrowseDoc(
                doc_id=f"doc{i:02d}",
                title=f"{topics[i]} Report",
                body="\n".join(body_parts),
            )
        )
    return BrowseCompItem(
        docs=tuple(docs),
        question=question,
        expected_answer=project,
        answer_doc_id=f"doc{answer_idx:02d}",
    )


# ---------------------------------------------------------------------------
# 3. OOLONG / trec_coarse — classification over long context
# ---------------------------------------------------------------------------

_TREC_LABELS: tuple[str, ...] = ("ABBR", "ENTY", "DESC", "HUM", "LOC", "NUM")

_TREC_EXAMPLES: dict[str, tuple[str, ...]] = {
    "ABBR": ("What does NASA stand for?", "What is the abbreviation for kilogram?"),
    "ENTY": ("What kind of animal is a koala?", "What flower symbolizes love?"),
    "DESC": ("How does photosynthesis work?", "Why is the sky blue?"),
    "HUM": ("Who wrote Hamlet?", "Which scientist proposed relativity?"),
    "LOC": ("Where is the Eiffel Tower?", "What country borders Spain?"),
    "NUM": ("How many planets orbit the sun?", "What year did WWII end?"),
}


@dataclass(frozen=True)
class OolongClassificationItem:
    """A classification-over-long-context item."""

    examples: tuple[tuple[str, str], ...]  # (question, label)
    query: str
    expected_answer: str

    def context(self) -> str:
        """Render the labeled in-context examples as a long context block."""
        return "\n".join(f"Q: {q}\nLABEL: {lab}" for q, lab in self.examples)


def generate_oolong_classification(
    seed: int = 42,
    *,
    n_items: int = 20,
    examples_per_item: int = 30,
) -> tuple[OolongClassificationItem, ...]:
    """Generate OOLONG/trec_coarse-style classification items.

    Each item has many labeled question/category examples plus a final
    unlabeled query to classify by analogy.

    Args:
        seed: RNG seed.
        n_items: Number of eval items to produce.
        examples_per_item: Labeled examples per item's context.

    Returns:
        A tuple of :class:`OolongClassificationItem`.
    """
    rng = random.Random(seed)
    items: list[OolongClassificationItem] = []
    for _ in range(n_items):
        examples: list[tuple[str, str]] = []
        for _ in range(examples_per_item):
            label = rng.choice(_TREC_LABELS)
            q = rng.choice(_TREC_EXAMPLES[label])
            examples.append((q, label))
        target_label = rng.choice(_TREC_LABELS)
        query = rng.choice(_TREC_EXAMPLES[target_label])
        items.append(
            OolongClassificationItem(
                examples=tuple(examples),
                query=query,
                expected_answer=target_label,
            )
        )
    return tuple(items)


# ---------------------------------------------------------------------------
# 4. OOLONG-Pairs — pairwise long-context comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OolongPairsItem:
    """A pairwise long-context comparison item."""

    context_a: str
    context_b: str
    question: str
    expected_answer: str  # "A" or "B"
    marker: str = field(default="")


def generate_oolong_pairs(
    seed: int = 42,
    *,
    n_chars: int = 6000,
) -> OolongPairsItem:
    """Generate an OOLONG-Pairs-style comparison item.

    Two long synthetic contexts A and B; the question asks which context
    mentions a marker phrase later (more recently) in its text.

    Args:
        seed: RNG seed.
        n_chars: Approx characters per context.

    Returns:
        An :class:`OolongPairsItem`.
    """
    rng = random.Random(seed)
    marker = f"checkpoint-{rng.randint(1000, 9999)}"
    sentence = f"The {marker} milestone was reached here."

    # Place the marker near the END of A and near the START of B, so A
    # mentions it "more recently" (later position) -> expected answer "A".
    a_head = _filler(rng, int(n_chars * 0.9))
    a_tail = _filler(rng, n_chars - len(a_head))
    context_a = f"{a_head} {sentence} {a_tail}"

    b_head = _filler(rng, int(n_chars * 0.1))
    b_tail = _filler(rng, n_chars - len(b_head))
    context_b = f"{b_head} {sentence} {b_tail}"

    question = (
        f"Which context mentions '{marker}' later in its text "
        f"(closer to the end), A or B?"
    )
    return OolongPairsItem(
        context_a=context_a,
        context_b=context_b,
        question=question,
        expected_answer="A",
        marker=marker,
    )
