"""The ticketdesk MCP server: a read-only ticket tracker over the seeded dataset.

Run it over stdio with ``python -m mcpx.targets.ticketdesk.server``. The tool
docstrings are deliberately full paragraphs: their size is part of what the
benchmark measures when it loads tool definitions into a model's context.

Two data sets are each reachable through exactly one tool, on purpose, so that
benchmark tasks must chain or page calls instead of finding a shortcut: ticket
reviewers only through ``list_ticket_reviewers`` (one call per ticket, never a
``reviewers`` field on a ticket row) and activity events only through
``list_activity`` (paged, newest first). ``tests/targets/test_server_guards.py``
enforces both.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from mcpx.targets.ticketdesk.seed import (
    LABELS,
    Event,
    Project,
    Release,
    Ticket,
    User,
    get_dataset,
)

MAX_PER_PAGE = 100
SORTS = ("id_desc", "closed_at_desc")

mcp = MCPServer("ticketdesk")
tool = mcp.tool(annotations=ToolAnnotations(read_only_hint=True))

Row = dict[str, Any]
Page = dict[str, Any]


def _ticket(t: Ticket) -> Row:
    """Return a full ticket dict with its body and comment count, never reviewers."""
    row = asdict(t)
    del row["reviewers"]
    return {**row, "comment_count": get_dataset().comment_count(t.id)}


def _brief(t: Ticket) -> Row:
    """Return a ticket dict without the body, for compact listings."""
    row = _ticket(t)
    del row["body"]
    return row


def _page[T](
    items: Sequence[T], page: int, per_page: int, to_row: Callable[[T], Row]
) -> Page:
    """Slice ordered items into one page of at most 100, converting only that page."""
    size = max(1, min(per_page, MAX_PER_PAGE))
    start = (max(page, 1) - 1) * size
    return {
        "items": [to_row(x) for x in items[start : start + size]],
        "page": max(page, 1),
        "total": len(items),
    }


def _ticket_or_raise(ticket_id: int) -> Ticket:
    """Return a ticket or raise a tool error when the id is unknown."""
    try:
        return get_dataset().ticket(ticket_id)
    except KeyError:
        raise ToolError(f"Unknown ticket id: {ticket_id}") from None


def _user_or_raise(user_id: int) -> User:
    """Return a user or raise a tool error when the id is unknown."""
    for user in get_dataset().users:
        if user.id == user_id:
            return user
    raise ToolError(f"Unknown user id: {user_id}")


def _project_or_raise(project_id: int) -> Project:
    """Return a project or raise a tool error when the id is unknown."""
    for project in get_dataset().projects:
        if project.id == project_id:
            return project
    raise ToolError(f"Unknown project id: {project_id}")


def _project_by_key(key: str) -> Project:
    """Return the project whose key matches case-insensitively, or raise."""
    for project in get_dataset().projects:
        if project.key.lower() == key.strip().lower():
            return project
    raise ToolError(f"Unknown project key: {key}")


def _releases(project_id: int | None = None) -> list[Release]:
    """Return releases, optionally restricted to one project, oldest first."""
    rels = [r for r in get_dataset().releases if project_id in (None, r.project_id)]
    return sorted(rels, key=lambda r: r.published_at)


def _events() -> list[Event]:
    """Return every event newest first (id descending)."""
    return sorted(get_dataset().events, key=lambda e: e.id, reverse=True)


def _tickets_where(**want: Any) -> list[Ticket]:
    """Return tickets matching every non-None criterion, newest id first."""
    out = []
    for t in sorted(get_dataset().tickets, key=lambda t: t.id, reverse=True):
        if want.get("status") not in (None, t.status):
            continue
        if want.get("label") not in (None, *t.labels):
            continue
        if want.get("project_id") not in (None, t.project_id):
            continue
        out.append(t)
    return out


@tool
def count_tickets() -> int:
    """Return the total number of tickets in the whole ticketdesk, open and closed.

    Takes no parameters and returns a single integer. Use it to size the dataset
    before paging through ``list_tickets`` or to sanity check aggregate results.
    """
    return len(get_dataset().tickets)


@tool
def list_tickets(
    status: str | None = None,
    label: str | None = None,
    project_id: int | None = None,
    sort: str = "id_desc",
    page: int = 1,
    per_page: int = 100,
) -> Page:
    """List tickets with optional filters, sorting and pagination.

    ``status`` is ``open`` or ``closed``, ``label`` is one of bug, feature, docs
    or infra, and ``project_id`` restricts to one project. ``sort`` is ``id_desc``
    (newest ticket first, the default) or ``closed_at_desc`` (most recently closed
    first, meaningful for closed tickets). ``per_page`` is capped at 100. Returns
    ``{"items", "page", "total"}`` where every item is the full ticket including
    its body, labels, timestamps and ``comment_count``. Reviewers are not part of
    a ticket; fetch them per ticket with ``list_ticket_reviewers``.
    """
    if sort not in SORTS:
        raise ToolError(f"Unknown sort {sort!r}; expected one of {', '.join(SORTS)}")
    rows = _tickets_where(status=status, label=label, project_id=project_id)
    if sort == "closed_at_desc":
        rows = sorted(rows, key=lambda t: t.closed_at or "", reverse=True)
    return _page(rows, page, per_page, _ticket)


@tool
def get_ticket(ticket_id: int) -> Row:
    """Fetch one ticket by its numeric id, including the full body text.

    Returns the ticket's id, project_id, title, body, status, labels, created_at,
    closed_at and ``comment_count``. Raises a tool error when the ticket id does
    not exist. Comments are fetched separately with ``list_comments`` and
    reviewers with ``list_ticket_reviewers``.
    """
    return _ticket(_ticket_or_raise(ticket_id))


@tool
def get_ticket_summary(ticket_id: int) -> Row:
    """Fetch one ticket by id without its long body text, for cheap lookups.

    Returns the same fields as ``get_ticket`` except ``body``: id, project_id,
    title, status, labels, created_at, closed_at and comment_count.
    Raises a tool error when the ticket id does not exist.
    """
    return _brief(_ticket_or_raise(ticket_id))


@tool
def list_comments(ticket_id: int, page: int = 1, per_page: int = 100) -> Page:
    """List the comments on one ticket in the order they were written.

    ``page`` starts at 1 and ``per_page`` is capped at 100. Returns
    ``{"items", "page", "total"}`` where each item has id, ticket_id, author_id
    and body. Raises a tool error when the ticket id does not exist.
    """
    _ticket_or_raise(ticket_id)
    return _page(get_dataset().comments_for(ticket_id), page, per_page, asdict)


@tool
def list_ticket_reviewers(ticket_id: int) -> list[int]:
    """List the user ids that reviewed a ticket.

    This is the only place reviewers are exposed: ticket rows never carry them.
    Only closed tickets have reviewers; open tickets return an empty list. The
    ids are sorted ascending and can be resolved with ``get_user``. Raises a tool
    error when the ticket id does not exist.
    """
    return list(_ticket_or_raise(ticket_id).reviewers)


@tool
def get_latest_release(project: str) -> Row:
    """Return the most recent release of a project, given its key.

    ``project`` is the project key such as ATLAS and is matched
    case-insensitively. The latest release is the one with the greatest
    ``published_at``. Returns tag, published_at and project_id, or raises a tool
    error when no project has that key.
    """
    found = _releases(_project_by_key(project).id)
    return asdict(found[-1])


@tool
def list_releases(project: str | None = None) -> list[Row]:
    """List releases, optionally for one project, oldest first.

    ``project`` is an optional project key matched case-insensitively; without it
    the releases of every project are returned. Each row has project_id, tag and
    published_at. Raises a tool error for an unknown project key.
    """
    pid = _project_by_key(project).id if project else None
    return [asdict(r) for r in _releases(pid)]


@tool
def list_activity(page: int = 1, per_page: int = 100) -> Page:
    """List the global activity log, newest event first.

    Events cover comments, status changes, deploys and ticket creation across all
    projects, ordered by id descending. ``per_page`` is capped at 100 and this is
    the only tool that returns events, so finding one means paging through the
    log. Returns ``{"items", "page", "total"}`` where each item has id, kind,
    project_id, ticket_id (null for deploys) and at.
    """
    return _page(_events(), page, per_page, asdict)


@tool
def list_projects() -> list[Row]:
    """List every project.

    Returns one row per project with id, key (such as ATLAS) and name. The list
    is small and unpaginated.
    """
    return [asdict(p) for p in get_dataset().projects]


@tool
def get_project(project_id: int) -> Row:
    """Fetch one project by its numeric id.

    Returns id, key and name. Raises a tool error when the project id does not
    exist. Use ``get_project_stats`` for ticket counts.
    """
    return asdict(_project_or_raise(project_id))


@tool
def get_project_stats(project_id: int) -> Row:
    """Summarise one project: ticket counts, comments and releases.

    Returns project_id, key, tickets, open, closed, comments, releases and the
    latest release tag. Raises a tool error when the project id does not exist.
    """
    p = _project_or_raise(project_id)
    mine = _tickets_where(project_id=p.id)
    rels = _releases(p.id)
    return {
        "project_id": p.id,
        "key": p.key,
        "tickets": len(mine),
        "open": sum(t.status == "open" for t in mine),
        "closed": sum(t.status == "closed" for t in mine),
        "comments": sum(get_dataset().comment_count(t.id) for t in mine),
        "releases": len(rels),
        "latest_release": rels[-1].tag if rels else None,
    }


@tool
def list_project_tickets(
    project_id: int, status: str | None = None, page: int = 1, per_page: int = 100
) -> Page:
    """List the tickets of one project, newest first, without body text.

    ``status`` optionally restricts to open or closed tickets and ``per_page`` is
    capped at 100. Returns ``{"items", "page", "total"}`` with compact ticket
    rows (no body). Raises a tool error when the project id does not exist.
    """
    _project_or_raise(project_id)
    rows = _tickets_where(project_id=project_id, status=status)
    return _page(rows, page, per_page, _brief)


@tool
def list_users() -> list[Row]:
    """List every user.

    Returns one row per user with id and name. The list is small and
    unpaginated. Use ``get_user_stats`` for a user's activity totals.
    """
    return [asdict(u) for u in get_dataset().users]


@tool
def get_user(user_id: int) -> Row:
    """Fetch one user by their numeric id.

    Returns id and name. Raises a tool error when the user id does not exist.
    """
    return asdict(_user_or_raise(user_id))


@tool
def get_user_stats(user_id: int) -> Row:
    """Summarise one user's comment activity.

    Returns user_id, name, comments (how many comments they authored) and
    tickets_commented (on how many distinct tickets they commented). Raises a
    tool error when the user id does not exist.
    """
    u = _user_or_raise(user_id)
    ds = get_dataset()
    return {
        "user_id": u.id,
        "name": u.name,
        "comments": sum(c.author_id == u.id for c in ds.comments),
        "tickets_commented": len(
            {c.ticket_id for c in ds.comments if c.author_id == u.id}
        ),
    }


@tool
def list_user_comments(user_id: int, page: int = 1, per_page: int = 100) -> Page:
    """List the comments written by one user, oldest first.

    ``per_page`` is capped at 100. Returns ``{"items", "page", "total"}`` with
    comment rows (id, ticket_id, author_id, body). Raises a tool error when the
    user id does not exist.
    """
    _user_or_raise(user_id)
    rows = [c for c in get_dataset().comments if c.author_id == user_id]
    return _page(rows, page, per_page, asdict)


@tool
def list_labels() -> list[str]:
    """List the labels that can be attached to tickets.

    Returns the label names (bug, feature, docs, infra). A ticket carries one or
    two of them; pass one to the ``label`` filter of ``list_tickets``.
    """
    return list(LABELS)


@tool
def count_tickets_by_status() -> dict[str, int]:
    """Count tickets grouped by status.

    Returns a mapping with the number of open and closed tickets across every
    project. The two numbers add up to ``count_tickets``.
    """
    return dict(Counter(t.status for t in get_dataset().tickets))


@tool
def count_tickets_by_label() -> dict[str, int]:
    """Count tickets grouped by label.

    Returns a mapping from each label to the number of tickets carrying it. A
    ticket with two labels is counted once under each, so totals exceed the
    ticket count.
    """
    return dict(Counter(lb for t in get_dataset().tickets for lb in t.labels))


@tool
def count_tickets_by_project() -> dict[str, int]:
    """Count tickets grouped by project key.

    Returns a mapping from project key (ATLAS, BEACON and so on) to how many
    tickets belong to that project, regardless of status.
    """
    keys = {p.id: p.key for p in get_dataset().projects}
    return dict(Counter(keys[t.project_id] for t in get_dataset().tickets))


@tool
def count_comments(ticket_id: int | None = None) -> int:
    """Count comments, either on one ticket or across the whole ticketdesk.

    With ``ticket_id`` it returns the number of comments on that ticket, raising
    a tool error when the id does not exist. Without it, the grand total of
    comments in the system is returned.
    """
    if ticket_id is None:
        return len(get_dataset().comments)
    return get_dataset().comment_count(_ticket_or_raise(ticket_id).id)


@tool
def search_tickets(query: str, page: int = 1, per_page: int = 100) -> Page:
    """Search ticket titles and bodies for a case-insensitive substring.

    Results are newest ticket first and ``per_page`` is capped at 100. Returns
    ``{"items", "page", "total"}`` with compact ticket rows (no body); fetch the
    full text of a hit with ``get_ticket``.
    """
    q = query.lower()
    hits = [t for t in _tickets_where() if q in t.title.lower() or q in t.body.lower()]
    return _page(hits, page, per_page, _brief)


@tool
def search_comments(query: str, page: int = 1, per_page: int = 100) -> Page:
    """Search comment bodies for a case-insensitive substring.

    Results are ordered by comment id and ``per_page`` is capped at 100. Returns
    ``{"items", "page", "total"}`` with comment rows (id, ticket_id, author_id,
    body).
    """
    q = query.lower()
    rows = [c for c in get_dataset().comments if q in c.body.lower()]
    return _page(rows, page, per_page, asdict)


@tool
def list_recently_closed(limit: int = 10) -> list[Row]:
    """List the most recently closed tickets, newest closure first.

    ``limit`` is clamped to between 1 and 100. Each row is a compact ticket (no
    body) including closed_at and comment_count. Reviewers are fetched per ticket
    with ``list_ticket_reviewers``.
    """
    closed = _tickets_where(status="closed")
    closed.sort(key=lambda t: t.closed_at or "", reverse=True)
    return [_brief(t) for t in closed[: max(1, min(limit, MAX_PER_PAGE))]]


@tool
def list_most_commented_tickets(limit: int = 10) -> list[Row]:
    """List the tickets with the most comments, busiest first.

    ``limit`` is clamped to between 1 and 100. Ties are broken by higher ticket
    id. Each row has ticket_id, title and comment_count.
    """
    ds = get_dataset()
    ranked = sorted(
        ds.tickets, key=lambda t: (ds.comment_count(t.id), t.id), reverse=True
    )
    return [
        {"ticket_id": t.id, "title": t.title, "comment_count": ds.comment_count(t.id)}
        for t in ranked[: max(1, min(limit, MAX_PER_PAGE))]
    ]


@tool
def list_oldest_open_tickets(limit: int = 10) -> list[Row]:
    """List the oldest tickets that are still open, lowest id first.

    Useful for spotting stale work. ``limit`` is clamped to between 1 and 100.
    Each row is a compact ticket (no body) with id, project_id, title, status,
    labels, created_at, closed_at (always null here) and comment_count.
    """
    opened = _tickets_where(status="open")
    opened.sort(key=lambda t: t.id)
    return [_brief(t) for t in opened[: max(1, min(limit, MAX_PER_PAGE))]]


@tool
def list_top_commenters(limit: int = 10) -> list[Row]:
    """List the users who wrote the most comments, busiest first.

    ``limit`` is clamped to between 1 and 100. Ties are broken by lower user id.
    Each row has user_id, name and comments (how many comments that user wrote
    across every ticket).
    """
    ds = get_dataset()
    counts = Counter(c.author_id for c in ds.comments)
    names = {u.id: u.name for u in ds.users}
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"user_id": uid, "name": names[uid], "comments": n}
        for uid, n in ranked[: max(1, min(limit, MAX_PER_PAGE))]
    ]


@tool
def get_label_stats(label: str) -> Row:
    """Summarise how one label is used across all tickets.

    ``label`` is one of bug, feature, docs or infra. Returns label, tickets (how
    many carry it), open and closed. Raises a tool error for an unknown label;
    ``list_labels`` shows the valid names.
    """
    if label not in LABELS:
        raise ToolError(f"Unknown label {label!r}; expected one of {', '.join(LABELS)}")
    mine = [t for t in get_dataset().tickets if label in t.labels]
    return {
        "label": label,
        "tickets": len(mine),
        "open": sum(t.status == "open" for t in mine),
        "closed": sum(t.status == "closed" for t in mine),
    }


@tool
def count_releases_by_project() -> dict[str, int]:
    """Count releases grouped by project key.

    Returns a mapping from project key (ATLAS, BEACON and so on) to how many
    releases that project has published. Use ``list_releases`` for the tags and
    publication times.
    """
    keys = {p.id: p.key for p in get_dataset().projects}
    return dict(Counter(keys[r.project_id] for r in get_dataset().releases))


@tool
def get_overview() -> Row:
    """Return headline totals for the whole ticketdesk in one call.

    Includes the number of users, projects, tickets (with open and closed
    split), comments, releases and activity events. Handy for a first look at
    the size of the data.
    """
    ds = get_dataset()
    by_status = Counter(t.status for t in ds.tickets)
    return {
        "users": len(ds.users),
        "projects": len(ds.projects),
        "tickets": len(ds.tickets),
        "open": by_status["open"],
        "closed": by_status["closed"],
        "comments": len(ds.comments),
        "releases": len(ds.releases),
        "events": len(ds.events),
    }


if __name__ == "__main__":
    mcp.run()
