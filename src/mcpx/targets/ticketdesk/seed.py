"""Deterministic seeded dataset for the ticketdesk target.

Everything is derived from a single ``random.Random(seed)`` instance, so the same
seed always yields an identical :class:`Dataset`. Later tasks assert exact values
from the default dataset (``seed=7``), so changing the generation order or the
random draws here changes those answers.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache

DEFAULT_SEED = 7

N_USERS = 40
N_TICKETS = 3000
N_EVENTS = 5000
RELEASES_PER_PROJECT = 3
CLOSED_RATIO = 0.35
RECENT_WINDOW = 30
# Reviewer counts forced onto the most recently closed tickets so that ranking
# them by reviewer count has a strict, tie-free top of the list.
TOP_REVIEWER_COUNTS = (10, 9, 8, 7, 6, 5)
# The newest ``DEPLOY_FREE_TAIL`` events never contain an Atlas deploy.
DEPLOY_FREE_TAIL = 500
ATLAS_DEPLOY_NEWEST_FIRST_INDEX = 650

STATUSES = ("open", "closed")
LABELS = ("bug", "feature", "docs", "infra")
EVENT_KINDS = ("comment", "status_change", "deploy", "ticket_created")
EVENT_WEIGHTS = (45, 30, 10, 15)

_FIRST_NAMES = (  # noqa: SIM905
    "Ada Ben Cleo Dev Eli Fay Gus Hana Ivan Jade Kai Lena Milo Nia Omar Pia "
    "Quinn Rhea Sam Tess Uma Vik Wren Xavi Yara Zane Abel Bree Cyrus Dana Emil "
    "Flor Gil Hugo Iris Joel Kira Leo Maya Noel"
).split()
_PROJECTS = (
    ("ATLAS", "Atlas"),
    ("BEACON", "Beacon"),
    ("CINDER", "Cinder"),
    ("DELTA", "Delta"),
    ("EMBER", "Ember"),
    ("FALCON", "Falcon"),
    ("GLACIER", "Glacier"),
    ("HARBOR", "Harbor"),
)
_WORDS = (  # noqa: SIM905
    "the service request queue worker cache index schema migration deploy build "
    "pipeline retry timeout latency memory leak crash login session token user "
    "payment invoice report export import dashboard search filter sort page "
    "render layout mobile desktop browser config setting flag rollout rollback "
    "metric alert log trace error warning failure regression upgrade dependency "
    "module test fixture mock stub release branch commit review approve merge "
    "conflict rebase patch hotfix backlog sprint owner label status comment "
    "thread notify email webhook callback endpoint payload response header "
    "cookie proxy gateway balancer cluster node shard replica backup restore"
).split()
_BASE_TIME = datetime(2025, 1, 1, 8, 0, 0)


@dataclass
class User:
    """A ticketdesk user."""

    id: int
    name: str


@dataclass
class Project:
    """A project that owns tickets and releases."""

    id: int
    key: str
    name: str


@dataclass
class Comment:
    """A comment left on a ticket."""

    id: int
    ticket_id: int
    author_id: int
    body: str


@dataclass
class Ticket:
    """A ticket; closed tickets carry reviewers and a ``closed_at`` timestamp."""

    id: int
    project_id: int
    title: str
    body: str
    status: str
    labels: list[str]
    reviewers: list[int]
    created_at: str
    closed_at: str | None


@dataclass
class Release:
    """A published release of a project."""

    project_id: int
    tag: str
    published_at: str


@dataclass
class Event:
    """An activity-log entry; ``ticket_id`` is None for deploy events."""

    id: int
    kind: str
    project_id: int
    ticket_id: int | None
    at: str


@dataclass
class Dataset:
    """The whole fake ticketdesk database plus convenient lookup helpers."""

    users: list[User]
    projects: list[Project]
    tickets: list[Ticket]
    comments: list[Comment]
    releases: list[Release]
    events: list[Event]
    _tickets_by_id: dict[int, Ticket] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )
    _comments_by_ticket: dict[int, list[Comment]] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        """Build the by-id and by-ticket indexes."""
        self._tickets_by_id = {t.id: t for t in self.tickets}
        self._comments_by_ticket = {}
        for c in self.comments:
            self._comments_by_ticket.setdefault(c.ticket_id, []).append(c)

    def ticket(self, ticket_id: int) -> Ticket:
        """Return the ticket with ``ticket_id`` (KeyError if absent)."""
        return self._tickets_by_id[ticket_id]

    def comments_for(self, ticket_id: int) -> list[Comment]:
        """Return the comments on a ticket, in id order."""
        return self._comments_by_ticket.get(ticket_id, [])

    def comment_count(self, ticket_id: int) -> int:
        """Return how many comments a ticket has."""
        return len(self.comments_for(ticket_id))


def _stamp(moment: datetime) -> str:
    """Format a datetime as a second-resolution ISO-8601 string."""
    return moment.isoformat(timespec="seconds")


def _text(rng: random.Random, target: int) -> str:
    """Build pseudo-prose whose length is within a few characters of ``target``."""
    words: list[str] = []
    length = 0
    while length < target:
        word = rng.choice(_WORDS)
        words.append(word)
        length += len(word) + 1
    text = " ".join(words)
    return text[:target].rstrip() + "."


def _make_tickets(
    rng: random.Random, project_ids: list[int], user_ids: list[int]
) -> list[Ticket]:
    """Create tickets with strictly increasing ids and creation times."""
    tickets: list[Ticket] = []
    created = _BASE_TIME
    closed_stamps: set[str] = set()
    for ticket_id in range(1, N_TICKETS + 1):
        created += timedelta(seconds=rng.randint(3600, 14400))
        status = "closed" if rng.random() < CLOSED_RATIO else "open"
        reviewers: list[int] = []
        closed_at: str | None = None
        if status == "closed":
            reviewers = sorted(rng.sample(user_ids, rng.randint(1, 4)))
            while True:
                closed = created + timedelta(seconds=rng.randint(3600, 90 * 86400))
                if _stamp(closed) not in closed_stamps:
                    break
            closed_at = _stamp(closed)
            closed_stamps.add(closed_at)
        tickets.append(
            Ticket(
                id=ticket_id,
                project_id=rng.choice(project_ids),
                title=_text(rng, rng.randint(25, 50)),
                body=_text(rng, rng.randint(350, 450)),
                status=status,
                labels=sorted(rng.sample(LABELS, rng.randint(1, 2))),
                reviewers=reviewers,
                created_at=_stamp(created),
                closed_at=closed_at,
            )
        )
    return tickets


def _force_top_reviewer_counts(
    rng: random.Random, tickets: list[Ticket], user_ids: list[int]
) -> None:
    """Give the most recently closed tickets a tie-free reviewer-count ranking."""
    closed = [t for t in tickets if t.status == "closed"]
    closed.sort(key=lambda t: str(t.closed_at), reverse=True)
    chosen = rng.sample(closed[:RECENT_WINDOW], len(TOP_REVIEWER_COUNTS))
    for ticket, count in zip(chosen, TOP_REVIEWER_COUNTS):
        ticket.reviewers = sorted(rng.sample(user_ids, count))


def _make_comments(
    rng: random.Random, tickets: list[Ticket], user_ids: list[int]
) -> list[Comment]:
    """Create about six comments per ticket."""
    comments: list[Comment] = []
    for ticket in tickets:
        for _ in range(rng.randint(4, 8)):
            comments.append(
                Comment(
                    id=len(comments) + 1,
                    ticket_id=ticket.id,
                    author_id=rng.choice(user_ids),
                    body=_text(rng, rng.randint(250, 350)),
                )
            )
    return comments


def _make_releases(rng: random.Random, projects: list[Project]) -> list[Release]:
    """Create three releases per project."""
    releases: list[Release] = []
    for project in projects:
        published = _BASE_TIME
        for minor in range(RELEASES_PER_PROJECT):
            published += timedelta(days=rng.randint(30, 120))
            releases.append(Release(project.id, f"v1.{minor}.0", _stamp(published)))
    return releases


def _make_events(
    rng: random.Random, tickets: list[Ticket], projects: list[Project]
) -> list[Event]:
    """Create the event log ordered by id, with Atlas deploys buried deep."""
    atlas_id = next(p.id for p in projects if p.key == "ATLAS")
    project_ids = [p.id for p in projects]
    forced_index = N_EVENTS - 1 - ATLAS_DEPLOY_NEWEST_FIRST_INDEX
    events: list[Event] = []
    at = _BASE_TIME
    for event_id in range(1, N_EVENTS + 1):
        at += timedelta(seconds=rng.randint(60, 3600))
        kind = rng.choices(EVENT_KINDS, EVENT_WEIGHTS)[0]
        ticket_id: int | None = None
        if kind == "deploy":
            project_id = rng.choice(project_ids)
            in_tail = event_id - 1 > N_EVENTS - 1 - DEPLOY_FREE_TAIL
            while in_tail and project_id == atlas_id:
                project_id = rng.choice(project_ids)
        else:
            ticket = rng.choice(tickets)
            ticket_id, project_id = ticket.id, ticket.project_id
        if event_id - 1 == forced_index:
            kind, ticket_id, project_id = "deploy", None, atlas_id
        events.append(Event(event_id, kind, project_id, ticket_id, _stamp(at)))
    return events


def build_dataset(seed: int = DEFAULT_SEED) -> Dataset:
    """Build the full dataset deterministically from ``seed``."""
    rng = random.Random(seed)
    users = [User(i + 1, _FIRST_NAMES[i]) for i in range(N_USERS)]
    projects = [Project(i + 1, key, name) for i, (key, name) in enumerate(_PROJECTS)]
    user_ids = [u.id for u in users]
    tickets = _make_tickets(rng, [p.id for p in projects], user_ids)
    _force_top_reviewer_counts(rng, tickets, user_ids)
    comments = _make_comments(rng, tickets, user_ids)
    releases = _make_releases(rng, projects)
    events = _make_events(rng, tickets, projects)
    return Dataset(users, projects, tickets, comments, releases, events)


@cache
def get_dataset() -> Dataset:
    """Return the cached default dataset (``build_dataset(7)``)."""
    return build_dataset(DEFAULT_SEED)
