"""Lexical relevance shared by every store: tokens, stop words and a minimum-match floor.

A query matches an entry only when enough CONTENT tokens (3+ characters, not a stop
word) appear in the entry: at least 2, or all of them when the query has 2 or fewer.
So a prompt sharing only common words ("what is the ... for the ...") injects nothing.
"""
from __future__ import annotations

import re

from shared_context.models import Entry

REPO_BOOST = 1.0
MIN_TOKEN_CHARS = 3
MIN_MATCHES = 2
_TOKEN = re.compile(r"\w+")

STOP_WORDS = frozenset("""
a about above after again against all also am an and any are aren as at be because been before
being below between both but by can cannot could did didn do does doesn doing don down during each
few for from further get gets got had hadn has hasn have haven having he her here hers herself him
himself his how i if in into is isn it its itself just let lets like make me might more most much
must my myself need no nor not now of off on once only or other our ours ourselves out over own
please same she should shouldn so some still such than that the their theirs them themselves then
there these they this those through to too under until up us use used using very via want was wasn
way we were weren what when where which while who whom why will with won would wouldn yes yet you
your yours yourself yourselves thing things something anything everything one two new good best
know think tell show give help work works working really able
""".split())


def tokens(text: str) -> set[str]:
    """All word tokens, lower-cased (used for the FTS candidate query and for scoring)."""
    return set(_TOKEN.findall(text.lower()))


def content_tokens(text: str) -> set[str]:
    return {t for t in tokens(text) if len(t) >= MIN_TOKEN_CHARS and t not in STOP_WORDS}


def doc_text(e: Entry) -> str:
    return " ".join([e.title, e.topic_key, e.body, *e.applies_to])


def score(query: set[str], e: Entry, repo: str | None) -> float | None:
    """Score of `e` for the content tokens `query`, or None below the relevance floor."""
    if not query:
        return None
    overlap = len(query & tokens(doc_text(e)))
    if overlap < min(MIN_MATCHES, len(query)) or (len(query) <= MIN_MATCHES and overlap < len(query)):
        return None
    s = overlap / len(query)
    if repo is not None and repo in e.applies_to:
        s += REPO_BOOST
    return s
