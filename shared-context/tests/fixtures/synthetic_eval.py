"""Synthetic past fixes for the capture-quality check (criterion 1).

Authored by hand, no real transcript content. Five fixes, each with noise (tangents,
guesses, a dead end, an embedded FAKE secret) and a labeled list of the learnings a
teammate would have wanted. RESPONSES is what the scripted StubModelClient returns
per transcript, so grading against LABELS is self-graded: it proves the pipeline
(parse, scrub, hold rules, store, scoring), not model quality.
Deliberately imperfect: one labeled learning is missed and two junk entries exist.
"""
from __future__ import annotations

import json

FAKE_DB_URL = "postgres://admin:hunter2fakepw@db.internal:5432/app"
FAKE_GH_TOKEN = "ghp_FAKEfakeFAKEfakeFAKEfakeFAKEfake1234"

TRANSCRIPTS: dict[str, list[tuple[str, str]]] = {
    "exporter_fd_leak.jsonl": [
        ("user", "The nightly exporter dies with too many open files after about an hour. Please find out why."),
        ("assistant", "Looking at exporter/writer.py: every batch opens a new file handle and never closes it."),
        ("user", "Side question, what is a good lunch place near the office? Never mind."),
        ("assistant", "I cannot judge lunch. Back to the bug: I tried raising ulimit to 65536 and the crash only "
                      "moved later, it did not go away."),
        ("user", "OK so ulimit is a dead end. Fix the handle leak properly."),
        ("assistant", "Wrapped the writer in a context manager so each handle closes per batch. Export ran six "
                      "hours with a flat handle count."),
    ],
    "flaky_timezone_test.jsonl": [
        ("user", "test_invoice_due_date fails on CI but passes locally. Flaky?"),
        ("assistant", "Maybe the cache is stale. I cleared it and it still fails, so that was a wrong guess."),
        ("assistant", "The test calls datetime.now(); CI runs in UTC while local machines use local time, so the "
                      "due date flips across midnight."),
        ("user", "Fix it for good."),
        ("assistant", "Added a fixed clock fixture and replaced datetime.now() in the billing module with an "
                      "injected clock. Tests pass in every timezone."),
    ],
    "migration_index_lock.jsonl": [
        ("user", "The migration adding an index to orders locked the table in production for ten minutes."),
        ("assistant", "CREATE INDEX takes a lock that blocks writes. Use CREATE INDEX CONCURRENTLY instead."),
        ("assistant", "CONCURRENTLY cannot run inside a transaction, so the migration must be marked "
                      f"non-transactional. Staging connection used {FAKE_DB_URL} for the dry run."),
        ("user", "Also rename the ticket template, I dislike its name."),
        ("assistant", "Renamed. The migration now runs non transactional with CONCURRENTLY and finished in "
                      "seconds on the staging copy."),
    ],
    "webhook_retry_storm.jsonl": [
        ("user", "After the outage every webhook consumer was hammered at the same second."),
        ("assistant", "All retries used the same fixed backoff, so clients retried in lockstep: a thundering "
                      "herd. Add jitter to the backoff."),
        ("assistant", "Retries also need an idempotency key, otherwise a retried delivery creates duplicate "
                      "orders."),
        ("user", "Is there a nicer name than WebhookThing for the class?"),
        ("assistant", "Sure, maybe WebhookDispatcher. Implemented jitter and idempotency keys; replay is safe now."),
    ],
    "tenant_cache_leak.jsonl": [
        ("user", "Customers saw each other's settings for a few minutes yesterday."),
        ("assistant", "I first shortened the cache TTL to 5 seconds. That only narrowed the window; the leak "
                      "stayed, so a short TTL is a dead end."),
        ("assistant", "The settings cache key omitted the tenant id, so tenant B read tenant A's entry. The key "
                      "must include the tenant."),
        ("user", "Ship it."),
        ("assistant", f"Cache key now includes tenant id; added a regression test. CI token {FAKE_GH_TOKEN} "
                      "was visible in the log, rotate it."),
    ],
}

LABELS: dict[str, list[dict]] = {
    "exporter_fd_leak.jsonl": [
        {"id": "handle-leak", "keywords": ["exporter", "handle"]},
        {"id": "ulimit-dead-end", "keywords": ["ulimit"]},
    ],
    "flaky_timezone_test.jsonl": [
        {"id": "ci-utc-vs-local", "keywords": ["utc", "datetime.now"]},
        {"id": "inject-clock", "keywords": ["clock", "fixture"]},
    ],
    "migration_index_lock.jsonl": [
        {"id": "index-concurrently", "keywords": ["concurrently", "index"]},
        {"id": "no-transaction", "keywords": ["transaction"]},
    ],
    "webhook_retry_storm.jsonl": [
        {"id": "jitter", "keywords": ["jitter", "backoff"]},
        {"id": "idempotency", "keywords": ["idempotency", "key"]},
        {"id": "replay-safe", "keywords": ["replay", "duplicate"]},  # the stub misses this one
    ],
    "tenant_cache_leak.jsonl": [
        {"id": "tenant-in-key", "keywords": ["cache key", "tenant"]},
        {"id": "ttl-dead-end", "keywords": ["ttl"]},
    ],
}


def _item(topic: str, title: str, body: str, kind: str = "gotcha", risk: str = "ok", **extra) -> dict:
    return {"title": title, "kind": kind, "body": body, "applies_to": [], "evidence": [],
            "topic_key": topic, "risk": risk, "hold_reason": extra.get("hold_reason")}


RESPONSES: dict[str, str] = {
    "exporter_fd_leak.jsonl": json.dumps({"learnings": [
        _item("exporter-handle-leak", "Exporter leaked a file handle per batch",
              "The exporter writer opened a handle per batch and never closed it; use a context manager."),
        _item("exporter-ulimit", "Raising ulimit does not fix the exporter crash", "Raising ulimit only delayed "
              "the too many open files crash; fix the handle leak instead.", kind="dead_end"),
        _item("lunch-places", "Lunch places near the office", "The team discussed where to eat lunch."),  # junk
    ]}),
    "flaky_timezone_test.jsonl": json.dumps({"learnings": [
        _item("ci-utc-datetime-now", "CI runs in UTC so datetime.now() tests flip across midnight",
              "Tests calling datetime.now() behave differently on CI (UTC) than on local machines."),
        _item("inject-clock-fixture", "Use an injected clock fixture instead of datetime.now()",
              "Replace datetime.now() in the billing module with an injected clock and a fixed clock fixture.",
              kind="convention"),
    ]}),
    "migration_index_lock.jsonl": json.dumps({"learnings": [
        _item("create-index-concurrently", "Use CREATE INDEX CONCURRENTLY on large tables",
              "A plain index build blocks writes; CREATE INDEX CONCURRENTLY avoids the lock on the orders table."),
        _item("migration-non-transactional", "CONCURRENTLY migrations must not run in a transaction",
              f"Mark the migration non transactional. A dry run used {FAKE_DB_URL} on staging."),
    ]}),
    "webhook_retry_storm.jsonl": json.dumps({"learnings": [
        _item("webhook-jitter", "Add jitter to webhook retry backoff",
              "A fixed backoff makes every client retry in lockstep after an outage; add jitter."),
        _item("webhook-idempotency-key", "Webhook retries need an idempotency key",
              "Without an idempotency key a retried delivery can create a second order."),
        _item("webhook-class-name", "Webhook class naming", "Someone suggested another class name."),  # junk
    ]}),
    "tenant_cache_leak.jsonl": json.dumps({"learnings": [
        _item("tenant-cache-key", "Settings cache key must include the tenant id",
              "The cache key omitted the tenant id so one tenant read another tenant's settings."),
        _item("cache-ttl-dead-end", "Shortening the cache TTL did not stop the tenant leak",
              f"A 5 second TTL only narrowed the window. CI token {FAKE_GH_TOKEN} showed up in a log.",
              kind="dead_end"),
    ]}),
}
