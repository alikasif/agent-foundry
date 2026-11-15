"""Seeded corpus for scrubber tests. Every secret here is obviously fake."""
from __future__ import annotations

# (label, text, fragment that must not survive scrubbing)
SECRETS: list[tuple[str, str, str]] = [
    ("aws_key", "deploy uses AKIAFAKEFAKEFAKE1234 for uploads", "AKIAFAKEFAKEFAKE1234"),
    (
        "github_token",
        "clone with ghp_FAKEfakeFAKEfakeFAKEfakeFAKEfake1234 in CI",
        "ghp_FAKEfakeFAKEfakeFAKEfakeFAKEfake1234",
    ),
    ("api_key_assign", 'config: api_key = "sk-fake-0000-not-a-real-key-9999"', "sk-fake-0000-not-a-real-key-9999"),
    (
        "bearer",
        "curl -H 'Authorization: Bearer fake.token.value-abcdefghijklmnop1234' https://x.test",
        "fake.token.value-abcdefghijklmnop1234",
    ),
    (
        "private_key",
        "key:\n-----BEGIN RSA PRIVATE KEY-----\nFAKEFAKEFAKEfakefakefake\nNOTAREALKEYBODY==\n"
        "-----END RSA PRIVATE KEY-----\nend",
        "NOTAREALKEYBODY",
    ),
    ("password_assign", "set password=hunter2-fake-pw before login", "hunter2-fake-pw"),
    ("password_colon", "db_password: fakeP@ssw0rd!", "fakeP@ssw0rd!"),
    ("high_entropy", "blob Zk9xQ2v8Lm3Rt7Yp1Wd5Nb0Hs4Jc6Xa2 stored", "Zk9xQ2v8Lm3Rt7Yp1Wd5Nb0Hs4Jc6Xa2"),
]

DENYLIST = ["Acme Corp", "Globex"]

CUSTOMER_TEXT = [
    ("Acme Corp reported the outage", "Acme Corp"),
    ("ticket from GLOBEX about billing", "GLOBEX"),
]

CLEAN_TEXT = [
    "Use the retry helper in src/shared_context/store/memory.py when the store is busy.",
    "The function getUserAccountPreferencesHandler returns early on empty input.",
    "Fixed in commit a1b2c3d (short sha) and 9fceb02; see PR 42.",
    "Set the password policy to require 12 characters; token budget is 800.",
    r"Run `uv run pytest tests/test_scrub.py -q` then check the C:\Users\dev\project\notes.md file.",
]
