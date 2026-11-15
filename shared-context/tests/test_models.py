import pytest

from shared_context.models import Entry, Kind, Status, dump_entry, load_entry, new_entry_id


def make_entry() -> Entry:
    return Entry(
        id=new_entry_id(),
        title="Use anyio for async tests",
        kind=Kind.GOTCHA,
        status=Status.UNVERIFIED,
        author="alice",
        created="2026-11-15T10:00:00Z",
        source_session="sess-1",
        topic_key="testing-async",
        applies_to=["src/a.py", "tests/"],
        evidence=["commit abc123"],
        body="Line one.\n\n---\nLine with: colon and ---\n",
        hold_reason=None,
        stale_reason="gone",
    )


def test_entry_roundtrip():
    e = make_entry()
    assert load_entry(dump_entry(e)) == e


def test_new_entry_id_is_sortable_ulid():
    a = new_entry_id()
    b = new_entry_id()
    assert len(a) == 26 and len(b) == 26
    assert b > a


def test_load_entry_rejects_unknown_status():
    text = dump_entry(make_entry()).replace("unverified", "bogus")
    with pytest.raises(ValueError):
        load_entry(text)


def test_enum_values():
    assert {s.value for s in Status} == {"unverified", "confirmed", "stale", "held"}
    assert {k.value for k in Kind} == {
        "decision", "gotcha", "convention", "dead_end", "runbook_note", "other"
    }


@pytest.mark.parametrize(
    "front",
    ["<<<<<<< HEAD\ntitle: a\n=======\ntitle: b\n>>>>>>> other\n", "title: [unclosed\n"],
)
def test_load_entry_wraps_yaml_errors_as_valueerror(front):
    with pytest.raises(ValueError):
        load_entry(f"---\n{front}---\nbody")
