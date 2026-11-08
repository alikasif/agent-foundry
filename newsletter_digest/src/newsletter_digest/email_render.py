"""Renders the reading list as a Gmail-safe email: HTML and a plain part (ED-7..ED-11).

Role: builds the email HTML from a ``ReadingList`` through the page's Jinja
environment and a separate template (``reading_list_email.html.j2``: tables, inline
styles, no script, CSP, ``<details>``, form controls or images). The result is
ASCII-only (non-ASCII as numeric character references), every line is at most
``MAX_EMAIL_LINE_CHARS`` characters, and the HTML fits ``EMAIL_HTML_BUDGET`` bytes by
dropping whole lower-priority items (never Must read), then compact mode. A short
ASCII plain-text part goes with it.

Pure: no network, no file access, no MCP. Allowed project imports: ``render``
(``ENVIRONMENT`` and ``safe_url`` only) and ``models``. ``ENVIRONMENT`` is never
mutated. Only ``email_delivery`` imports this module.
"""

import re
import textwrap
import unicodedata
from dataclasses import dataclass

from markupsafe import escape

from newsletter_digest.models import ReadingItem, ReadingList
from newsletter_digest.render import ENVIRONMENT, safe_url

EMAIL_TEMPLATE_NAME = "reading_list_email.html.j2"
EMAIL_HTML_BUDGET = 95_000  # bytes of the final ASCII HTML (Q-E9)
MAX_EMAIL_LINE_CHARS = 900  # hard limit
WRAP_AT = 880  # wrapper target, leaves headroom
MAX_EMAIL_HREF_CHARS = 800  # escaped + ASCII-encoded href length
MAX_TEXT_PART_BYTES = 8_000
TEXT_PART_MAX_ITEMS = 30
MAX_TEXT_TITLE_CHARS = 200
TEXT_WRAP_AT = 76
TEXT_LINK_FALLBACK = "(link in the HTML version)"

# ED-8 sanitising: CR and the C0 controls except TAB/LF, DEL, C1, bidi embeddings,
# overrides and isolates are removed; line/paragraph separators become a space.
_REMOVED_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_LINE_SEPARATORS = re.compile("[  ]")
_SURROGATES = re.compile("[\ud800-\udfff]")
_NON_ASCII_RUN = re.compile("[^\x00-\x7f]+")

# ED-8 tokens: a tag, a character reference, a whitespace run, a text run, or "&".
_TOKEN = re.compile(r"<[^>]*>|&#?[A-Za-z0-9]+;|[ \t\n]+|[^<& \t\n]+|&")
# Pieces of one tag: whitespace between attributes, or a run that keeps quoted
# attribute values whole (so a break never lands inside a value).
_TAG_PIECE = re.compile(r"""[ \t\n]+|(?:[^ \t\n"']+|"[^"]*"|'[^']*')+""")


@dataclass(frozen=True)
class EmailContent:
    """The rendered email parts and what the size budget did to them."""

    html: str  # ASCII, every line <= MAX_EMAIL_LINE_CHARS
    text: str  # ASCII, every line <= MAX_EMAIL_LINE_CHARS, <= MAX_TEXT_PART_BYTES
    shown: int  # items rendered
    omitted: int  # items dropped by the budget
    compact: bool  # stage-3 compaction applied
    over_budget: bool  # still > budget after every stage (ED-10)


def render_email(
    rl: ReadingList, *, html_name: str, budget: int = EMAIL_HTML_BUDGET
) -> EmailContent:
    """Render both parts of the email, keeping the HTML within ``budget`` bytes.

    ED-10: the full list first; else the smallest number ``k`` of items from the
    head of ``drop_order`` (binary search) whose rendering fits; else compact mode
    with every droppable item gone; else that compact rendering with
    ``over_budget=True`` (it is still meant to be sent). ``html_name`` is the saved
    HTML's file name, quoted in the omitted line and the plain part.

    Raises:
        ValueError: if a tag cannot be wrapped under ``WRAP_AT`` (a bug).
    """
    text = render_email_text(rl, html_name=html_name)
    order = drop_order(rl)

    def build(k: int, compact: bool) -> tuple[str, int]:
        """Render without the first ``k`` dropped items; return (html, shown)."""
        dropped = frozenset(order[:k])
        kept = [item for item in rl.items if item.id not in dropped]
        reduced = rl.model_copy(update={"items": kept})
        html = render_email_html(
            reduced,
            html_name=html_name,
            total=rl.article_count,
            omitted=k,
            compact=compact,
        )
        return html, len(kept)

    def fits(html: str) -> bool:
        """True if the final ASCII HTML is within the budget."""
        return len(html.encode("ascii")) <= budget

    html, shown = build(0, False)
    if fits(html):
        return EmailContent(
            html=html,
            text=text,
            shown=shown,
            omitted=0,
            compact=False,
            over_budget=False,
        )
    best: tuple[int, str, int] | None = None
    low, high = 1, len(order)
    while low <= high:
        k = (low + high) // 2
        candidate, kept = build(k, False)
        if fits(candidate):
            best = (k, candidate, kept)
            high = k - 1
        else:
            low = k + 1
    if best is not None:
        k, html, shown = best
        return EmailContent(
            html=html,
            text=text,
            shown=shown,
            omitted=k,
            compact=False,
            over_budget=False,
        )
    html, shown = build(len(order), True)
    return EmailContent(
        html=html,
        text=text,
        shown=shown,
        omitted=len(order),
        compact=True,
        over_budget=not fits(html),
    )


def render_email_html(
    rl: ReadingList, *, html_name: str, total: int, omitted: int, compact: bool
) -> str:
    """Render ``rl`` (the possibly reduced list) to the final ASCII, wrapped HTML.

    ``total`` is the full list's article count (header and omitted line),
    ``omitted`` the number of items left out, ``compact`` drops key points and
    ``Why:`` lines. One template pass, then ``to_email_ascii``, then
    ``wrap_long_lines(..., WRAP_AT)``.
    """
    rendered = ENVIRONMENT.get_template(EMAIL_TEMPLATE_NAME).render(
        reading_list=rl,
        meta=rl.meta,
        must_read=rl.must_read(),
        by_topic=rl.by_topic(),
        total_articles=total,
        omitted=omitted,
        html_name=_file_name(html_name),
        compact=compact,
        linkable=frozenset(item.id for item in rl.items if email_link(item.url)),
    )
    return wrap_long_lines(to_email_ascii(rendered), WRAP_AT)


def render_email_text(rl: ReadingList, *, html_name: str) -> str:
    """Render the ASCII plain-text part (ED-9).

    Counts, then up to ``TEXT_PART_MAX_ITEMS`` Must-read titles with their links
    (entries stop before ``MAX_TEXT_PART_BYTES``), then a pointer to the HTML
    version and the saved file. Unranked and empty lists get their own sentence
    instead of the item list.
    """
    meta = rl.meta
    name = _file_name(html_name)
    head = [
        f"Newsletter digest: last {_plain(meta.window)} "
        f"({meta.start_date.isoformat()} to {meta.end_date.isoformat()})"
    ]
    if not rl.items:
        if meta.emails_found == 0:
            sentence = (
                f"No newsletter emails matched {_plain(meta.query)} in this window "
                f"({meta.start_date.isoformat()} - {meta.end_date.isoformat()})."
            )
        else:
            sentence = (
                f"No articles were found in the {meta.emails_found} matching "
                f"email{_plural(meta.emails_found)}."
            )
        return _join_text([*head, "", *_wrap(sentence)])

    count = rl.article_count
    counts = (
        f"{count} article{_plural(count)} from {meta.emails_processed} of "
        f"{meta.emails_found} email{_plural(meta.emails_found)}; "
    )
    tail = [
        "",
        *_wrap(
            "Open the HTML version of this email for the full list. It is also "
            f"saved as {name} on the computer that ran the digest."
        ),
    ]
    if not rl.ranked:
        return _join_text(
            [
                *head,
                *_wrap(counts + "unranked."),
                "",
                *_wrap(
                    "Ranking was not completed; open the HTML version for the list."
                ),
                *tail,
            ]
        )

    must = rl.must_read()
    head = [*head, *_wrap(f"{counts}{len(must)} must-read.")]
    if not must:
        return _join_text([*head, *tail])
    entries: list[list[str]] = []
    for number, item in enumerate(must[:TEXT_PART_MAX_ITEMS], start=1):
        candidate = [*entries, _text_entry(number, item)]
        if len(_must_read_text(head, candidate, len(must), tail)) > MAX_TEXT_PART_BYTES:
            break
        entries = candidate
    return _must_read_text(head, entries, len(must), tail)


def _must_read_text(
    head: list[str], entries: list[list[str]], must_count: int, tail: list[str]
) -> str:
    """Assemble the ranked plain part, with '... and N more' when entries are cut."""
    more = must_count - len(entries)
    more_line = [f"... and {more} more in the HTML version."] if more else []
    lines = [line for entry in entries for line in entry]
    return _join_text([*head, "", "Must read:", *lines, *more_line, *tail])


def _text_entry(number: int, item: ReadingItem) -> list[str]:
    """One numbered Must-read entry: the wrapped ASCII title, then its link line."""
    lines = textwrap.wrap(
        f"{number}. {_plain_title(item.title)}",
        width=TEXT_WRAP_AT,
        subsequent_indent="   ",
        break_on_hyphens=False,
    )
    href = safe_url(item.url)
    if href is None:
        return lines
    usable = href.isascii() and len(href) <= MAX_EMAIL_HREF_CHARS
    return [*lines, f"   {href if usable else TEXT_LINK_FALLBACK}"]


def _plain(text: str) -> str:
    """ASCII transliteration (ED-9): NFKD, no combining marks, '?' for the rest."""
    text = " ".join(_REMOVED_CHARS.sub("", text).split())
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _NON_ASCII_RUN.sub("?", stripped)


def _plain_title(title: str) -> str:
    """ASCII title cut to ``MAX_TEXT_TITLE_CHARS`` (ending in '...' when cut)."""
    text = _plain(title)
    if len(text) <= MAX_TEXT_TITLE_CHARS:
        return text
    return text[: MAX_TEXT_TITLE_CHARS - 3].rstrip() + "..."


def _wrap(prose: str) -> list[str]:
    """Wrap one paragraph of prose at ``TEXT_WRAP_AT`` columns."""
    return textwrap.wrap(prose, width=TEXT_WRAP_AT, break_on_hyphens=False)


def _join_text(lines: list[str]) -> str:
    """Join plain-part lines with LF and end with a newline."""
    return "\n".join(lines) + "\n"


def _plural(count: int) -> str:
    """'s' unless ``count`` is 1."""
    return "" if count == 1 else "s"


def _file_name(html_name: str) -> str:
    """The last path component only: the omitted line never shows a directory."""
    return html_name.replace("\\", "/").rsplit("/", 1)[-1]


def to_email_ascii(text: str) -> str:
    """Sanitise ``text`` and encode it as ASCII with numeric character references.

    ED-8: removes CR, the C0 controls except TAB and LF, DEL, the C1 controls and
    the bidi controls U+202A-U+202E and U+2066-U+2069; maps U+2028/U+2029 to a
    space and lone surrogates to U+FFFD; then every non-ASCII code point becomes
    one decimal reference (an astral code point is one reference, never a pair).
    """
    text = _REMOVED_CHARS.sub("", text)
    text = _LINE_SEPARATORS.sub(" ", text)
    text = _SURROGATES.sub("�", text)
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


def wrap_long_lines(text: str, limit: int) -> str:
    """Insert line breaks so that no line of the HTML ``text`` exceeds ``limit``.

    ED-8 tokeniser over the whole document: tags (split into attribute pieces at
    their whitespace, quoted values kept whole), character references, whitespace
    runs, text runs and single ``&``. A token that would pass ``limit`` breaks the
    line at the last whitespace of the line (in text or between attributes),
    otherwise just before the token when that is text context. Text runs longer
    than ``limit`` are split into ``limit``-sized chunks. A whitespace run that
    holds a LF keeps every LF but drops the blanks of any of its lines that would
    pass ``limit``.

    Raises:
        ValueError: if a piece of a tag is longer than ``limit`` or cannot be
            placed under it (attribute values are never split).
    """
    out: list[str] = []
    column = 0
    last_space: tuple[int, int] | None = None  # (index in out, column after it)
    for piece, space, breakable in _wrap_tokens(text, limit):
        if space:
            if "\n" in piece:
                # Each line of the run keeps its blanks only where they fit; the
                # LFs themselves are the breaks, so dropped blanks change nothing.
                head, *rest = piece.split("\n")
                lines = [head if column + len(head) <= limit else ""]
                lines += [line if len(line) <= limit else "" for line in rest]
                out.append("\n".join(lines))
                column = len(lines[-1])
                last_space = None
            elif column + len(piece) > limit:
                out.append("\n")
                column = 0
                last_space = None
            else:
                out.append(piece)
                column += len(piece)
                last_space = (len(out) - 1, column)
            continue
        if column + len(piece) > limit:
            if last_space and column - last_space[1] + len(piece) <= limit:
                out[last_space[0]] = "\n"
                column -= last_space[1]
            elif breakable and len(piece) <= limit:
                out.append("\n")
                column = 0
            else:
                raise ValueError(
                    f"cannot wrap a {len(piece)}-character tag piece under {limit}"
                )
            last_space = None
        out.append(piece)
        column += len(piece)
    return "".join(out)


def _wrap_tokens(text: str, limit: int) -> list[tuple[str, bool, bool]]:
    """Tokens for ``wrap_long_lines`` as (text, is_whitespace, break_allowed_before).

    A break may be inserted before text runs, character references, ``&`` and the
    first piece of a tag (text context), never before a later piece of a tag.
    """
    tokens: list[tuple[str, bool, bool]] = []
    for token in _TOKEN.findall(text):
        if token.startswith("<"):
            pieces = _TAG_PIECE.findall(token)
            if "".join(pieces) != token:
                pieces = [token]
            tokens.extend(
                (piece, piece.isspace(), index == 0)
                for index, piece in enumerate(pieces)
            )
        elif token.isspace():
            tokens.append((token, True, False))
        elif len(token) > limit and not token.startswith("&"):
            tokens.extend(
                (token[start : start + limit], False, True)
                for start in range(0, len(token), limit)
            )
        else:
            tokens.append((token, False, True))
    return tokens


def drop_order(rl: ReadingList) -> list[str]:
    """Item ids in the order the budget drops them, first dropped first (ED-10).

    Ranked: skim and unprioritised items, then worth-reading items, each in reverse
    display order (Must read, then ``by_topic`` flattened); Must-read items never
    appear. Unranked: every item in reverse display order.
    """
    display = [*rl.must_read(), *(item for _, items in rl.by_topic() for item in items)]
    newest_last = list(reversed(display))
    if not rl.ranked:
        return [item.id for item in newest_last]
    skim = [item.id for item in newest_last if item.priority in ("skim", None)]
    worth = [item.id for item in newest_last if item.priority == "worth-reading"]
    return [*skim, *worth]


def email_link(url: str | None) -> str | None:
    """``safe_url(url)``, kept only if its escaped ASCII form is short enough.

    Returns ``None`` for unsafe or missing URLs and for hrefs longer than
    ``MAX_EMAIL_HREF_CHARS`` after HTML escaping and ``to_email_ascii`` (those
    render unlinked, so no line or tag can outgrow the wrapper).
    """
    href = safe_url(url)
    if href is None:
        return None
    if len(to_email_ascii(str(escape(href)))) > MAX_EMAIL_HREF_CHARS:
        return None
    return href
