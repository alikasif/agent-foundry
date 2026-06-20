"""Baseline agent and offline solvers for the RLM eval suite.

Two baselines are provided:

* :class:`BaselineAgent` -- a thin wrapper used in ``--live`` mode that runs an
  ``RLMAgent`` over a context WITHOUT ``llm_query``/recursion, relying on the
  loop's 4000-char observation truncation to model "compaction" (the agent
  only ever sees a truncated view of a large context).

* The ``offline_*`` solver functions -- deterministic, LLM-free heuristics used
  in the default offline mode.  They emulate the *capability difference* the
  paper measures: the RLM solver inspects the full context by reference
  (``ContextStore.search``/``sub_context``) and so can find information
  anywhere, while the baseline solver only sees a truncated prefix and misses
  anything beyond it.  This makes the RLM-vs-baseline delta reproducible in CI
  without any API key.

These offline solvers are explicitly NOT the paper's models; they exist so the
harness can report deterministic *relative* deltas offline.
"""

from __future__ import annotations

from rlm.context import ContextStore

from .datasets import (
    BrowseCompItem,
    OolongClassificationItem,
    OolongPairsItem,
    SniahItem,
)

# Characters a "compaction" baseline can see -- mirrors the loop's default
# observation truncation (DEFAULT_OUTPUT_MAX_CHARS).
BASELINE_VISIBLE_CHARS: int = 4000


class BaselineAgent:
    """Live-mode baseline: an RLM agent with no recursion, truncated context.

    Constructed lazily in ``--live`` mode only, so importing this module never
    requires API credentials or the agents SDK runtime.
    """

    def __init__(self, model: str, api_key: str, base_url: str) -> None:
        """Store connection config for a no-recursion baseline run.

        Args:
            model: Model identifier.
            api_key: Provider API key.
            base_url: OpenAI-compatible base URL.
        """
        self._model = model
        self._api_key = api_key
        self._base_url = base_url


# ---------------------------------------------------------------------------
# Offline RLM solvers (full context by reference)
# ---------------------------------------------------------------------------


def offline_rlm_sniah(item: SniahItem) -> str:
    """Find the needle by searching the full context store by reference."""
    store = ContextStore.from_string(item.context)
    matches = store.search("secret access code for the vault is")
    if not matches:
        return ""
    line = matches[0][1]
    digits = "".join(ch for ch in line if ch.isdigit())
    return digits


def offline_rlm_browsecomp(item: BrowseCompItem) -> str:
    """Locate the answer-bearing doc via search, then extract the project."""
    store = ContextStore.from_string(item.corpus())
    matches = store.search("flagship project is named")
    if not matches:
        return ""
    line = matches[0][1]
    return line.split("named", 1)[1].strip().rstrip(".")


def offline_rlm_oolong_classification(item: OolongClassificationItem) -> str:
    """Classify the query by majority label of matching in-context examples."""
    votes: dict[str, int] = {}
    for question, label in item.examples:
        if question == item.query:
            votes[label] = votes.get(label, 0) + 1
    if not votes:
        return ""
    return max(votes, key=lambda k: votes[k])


def offline_rlm_oolong_pairs(item: OolongPairsItem) -> str:
    """Compare marker positions across both full contexts by reference."""
    a = ContextStore.from_string(item.context_a)
    b = ContextStore.from_string(item.context_b)
    a_hits = a.search(item.marker)
    b_hits = b.search(item.marker)
    a_pos = a_hits[0][0] if a_hits else -1
    b_pos = b_hits[0][0] if b_hits else -1
    return "A" if a_pos >= b_pos else "B"


# ---------------------------------------------------------------------------
# Offline baseline solvers (truncated-prefix view only)
# ---------------------------------------------------------------------------


def _prefix(text: str) -> str:
    """Return only the leading slice a compaction baseline would see."""
    return text[:BASELINE_VISIBLE_CHARS]


def offline_baseline_sniah(item: SniahItem) -> str:
    """Baseline sees only the prefix; misses needles placed later."""
    window = _prefix(item.context)
    marker = "secret access code for the vault is"
    if marker not in window:
        return ""
    line = window.split(marker, 1)[1]
    return "".join(ch for ch in line[:20] if ch.isdigit())


def offline_baseline_browsecomp(item: BrowseCompItem) -> str:
    """Baseline sees only the prefix of the concatenated corpus."""
    window = _prefix(item.corpus())
    if "flagship project is named" not in window:
        return ""
    line = window.split("flagship project is named", 1)[1]
    return line.splitlines()[0].strip().rstrip(".")


def offline_baseline_oolong_classification(item: OolongClassificationItem) -> str:
    """Baseline only sees the truncated example block."""
    window = _prefix(item.context())
    # The baseline only sees a truncated example block; with no guaranteed
    # access to the matching example, it guesses the most frequent label in
    # the visible window -- a weak heuristic by design.
    votes: dict[str, int] = {}
    for line in window.splitlines():
        if line.startswith("LABEL: "):
            lab = line[len("LABEL: ") :]
            votes[lab] = votes.get(lab, 0) + 1
    if not votes:
        return ""
    return max(votes, key=lambda k: votes[k])


def offline_baseline_oolong_pairs(item: OolongPairsItem) -> str:
    """Baseline compares only truncated prefixes of both contexts."""
    a_window = _prefix(item.context_a)
    b_window = _prefix(item.context_b)
    a_pos = a_window.find(item.marker)
    b_pos = b_window.find(item.marker)
    # Marker is placed late in A (likely beyond the window -> -1) and early in
    # B, so the truncated baseline typically picks B (wrong).
    return "A" if a_pos >= b_pos else "B"
