"""Tests for the deterministic ticketdesk dataset."""

from statistics import mean

from mcpx.targets.ticketdesk.seed import build_dataset, get_dataset


def test_dataset_is_deterministic() -> None:
    assert build_dataset(7) == build_dataset(7)


def test_different_seed_differs() -> None:
    assert build_dataset(8).tickets != get_dataset().tickets


def test_get_dataset_is_cached_default_seed() -> None:
    assert get_dataset() is get_dataset()
    assert get_dataset() == build_dataset(7)


def test_dataset_scale() -> None:
    d = get_dataset()
    assert len(d.tickets) == 3000 and len(d.users) == 40 and len(d.projects) == 8
    assert any(p.key == "ATLAS" and p.name == "Atlas" for p in d.projects)
    assert len(d.events) == 5000
    assert len(d.releases) == 24
    for p in d.projects:
        assert sum(r.project_id == p.id for r in d.releases) == 3


def test_closed_tickets_have_reviewers_and_closed_at() -> None:
    d = get_dataset()
    closed = [t for t in d.tickets if t.status == "closed"]
    assert len(closed) >= 60 and all(t.reviewers and t.closed_at for t in closed)
    opened = [t for t in d.tickets if t.status == "open"]
    assert opened and all(not t.reviewers and t.closed_at is None for t in opened)


def test_ticket_shape() -> None:
    d = get_dataset()
    user_ids = {u.id for u in d.users}
    project_ids = {p.id for p in d.projects}
    assert [t.id for t in d.tickets] == list(range(1, 3001))
    for t in d.tickets:
        assert t.project_id in project_ids
        assert t.status in {"open", "closed"}
        assert t.labels and set(t.labels) <= {"bug", "feature", "docs", "infra"}
        assert set(t.reviewers) <= user_ids
    assert 350 <= mean(len(t.body) for t in d.tickets) <= 450


def test_newest_ticket_has_highest_id() -> None:
    d = get_dataset()
    created = [t.created_at for t in d.tickets]
    assert created == sorted(created) and len(set(created)) == len(created)
    assert max(d.tickets, key=lambda t: t.created_at).id == 3000


def test_closed_at_is_independent_of_id_with_clean_recent_ranking() -> None:
    d = get_dataset()
    closed = [t for t in d.tickets if t.status == "closed"]
    by_closed = sorted(closed, key=lambda t: str(t.closed_at), reverse=True)
    assert len({t.closed_at for t in closed}) == len(closed)
    assert [t.id for t in by_closed] != sorted((t.id for t in closed), reverse=True)
    assert all(str(t.closed_at) > t.created_at for t in closed)
    recent = by_closed[:30]
    counts = sorted((len(t.reviewers) for t in recent), reverse=True)
    assert counts[0] > counts[1] > counts[2] > counts[3] > counts[4] > counts[5]


def test_comments() -> None:
    d = get_dataset()
    assert 5 <= len(d.comments) / len(d.tickets) <= 7
    assert 250 <= mean(len(c.body) for c in d.comments) <= 350
    user_ids = {u.id for u in d.users}
    assert all(c.author_id in user_ids for c in d.comments)
    assert sum(d.comment_count(t.id) for t in d.tickets) == len(d.comments)
    assert len(d.comments_for(1)) == d.comment_count(1)


def test_events() -> None:
    d = get_dataset()
    assert [e.id for e in d.events] == list(range(1, 5001))
    ats = [e.at for e in d.events]
    assert ats == sorted(ats)
    assert {"deploy", "comment", "status_change"} <= {e.kind for e in d.events}
    atlas = next(p for p in d.projects if p.key == "ATLAS")
    newest_first = list(reversed(d.events))
    idx = [
        i
        for i, e in enumerate(newest_first)
        if e.kind == "deploy" and e.project_id == atlas.id
    ]
    assert len(idx) >= 5 and idx[0] >= 500
    for e in d.events:
        if e.ticket_id is not None:
            assert d.ticket(e.ticket_id).project_id == e.project_id
