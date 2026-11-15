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
    ("django_secret_key", "SECRET_KEY = 'django-insecure-fakefakefakefake1234'", "django-insecure-fakefakefakefake1234"),
    ("secret_key_bare", "secret_key=fakefakefakefake1234", "fakefakefakefake1234"),
    ("db_pass", "DB_PASS=hunter2fakepw", "hunter2fakepw"),
    ("password_suffix", "PASSWORD_PROD=fakeprodpw-12345", "fakeprodpw-12345"),
    ("password_semicolon", "password=p;ssw0rd-fake-long", "ssw0rd-fake-long"),
    ("password_comma", "password=ab,cdefghijk", "cdefghijk"),
    ("password_quoted_spaces", 'password = "correct horse battery staple"', "horse battery staple"),
    ("url_credentials", "postgres://admin:hunter2fakepw@db.internal:5432/app", "hunter2fakepw"),
    ("basic_auth", "Authorization: Basic dXNlcjpmYWtlcGFzcw==", "dXNlcjpmYWtlcGFzcw"),
    ("hex64", "key material " + "0123456789abcdef" * 4 + " end", "0123456789abcdef" * 4),
    ("hex32_keyword", "token 9f2c4e7a1b3d5f60718293a4b5c6d7e8", "9f2c4e7a1b3d5f60718293a4b5c6d7e8"),
    ("hex40_keyword", "api key 3f786850e387550fdab836ed7e6dc881de23001b", "3f786850e387550fdab836ed7e6dc881de23001b"),
    ("base36_lower", "session 8k2m4x9q1w7z3c5v6b0n2l4j8h1g5f3d", "8k2m4x9q1w7z3c5v6b0n2l4j8h1g5f3d"),
    (
        "pgp_key",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nNOTAREALPGPBODY\n-----END PGP PRIVATE KEY BLOCK-----",
        "NOTAREALPGPBODY",
    ),
    ("redis_empty_user", "redis://:hunter2fakepw@cache:6379", "hunter2fakepw"),
    ("url_raw_at", "postgres://admin:p@ss:w0rd-fake@db/app", "w0rd-fake"),
    ("basic_short_b64", "Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("long_key_name", "MY_VERY_LONG_SERVICE_NAME_FOR_THE_PAYMENTS_BACKEND_PASSWORD=fakepw12345", "fakepw12345"),
    ("passphrase", "passphrase = 'fake horse staple'", "fake horse staple"),
    ("pass_segment", "pass=fakeP4ss", "fakeP4ss"),
    ("pwd_segment", "DB_PWD=fakeP4ssword", "fakeP4ssword"),
]

DENYLIST = ["Acme Corp", "Globex"]

CUSTOMER_TEXT = [
    ("Acme Corp reported the outage", "Acme Corp"),
    ("ticket from GLOBEX about billing", "GLOBEX"),
]

CLEAN_TEXT = [
    "bypass=true", "compass=north", "passport=AB123456", "passthrough=enabled", "keepass=installed",
    "pass=true", "password_policy: strict", "password_hash = bcrypt(pw)", "token_count = len(tokens)",
    "tokenizer=sentencepiece", "secret_name=prod-db-creds", "api_key_env=OPENAI_API_KEY",
    "password = get_password_from_vault()", "Basic configuration of the proxy",
    "see https://example.com:8080/docs and ssh://git@host/repo.git",
    "Basic configuration of the pass-through proxy; tests passed: 43; token budget is 800.",
    "Use the retry helper in src/shared_context/store/memory.py when the store is busy.",
    "The function getUserAccountPreferencesHandler returns early on empty input.",
    "Fixed in commit a1b2c3d (short sha) and 9fceb02; see PR 42.",
    "Set the password policy to require 12 characters; token budget is 800.",
    r"Run `uv run pytest tests/test_scrub.py -q` then check the C:\Users\dev\project\notes.md file.",
]
