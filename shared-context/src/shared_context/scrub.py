"""Scrubber: redact credentials, customer terms and custom patterns."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REDACTED = "[REDACTED]"
_NOT_ALREADY = r"(?!\[REDACTED\])"

# Whole-match credential patterns.
_WHOLE = [
    re.compile(
        r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z ]{0,30}PRIVATE KEY(?: BLOCK)?-----|\Z)",
        re.S,
    ),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
]

# Long keywords may sit anywhere in a key; the short ones (`pass`, `pwd`) must start a key
# segment and not be followed by a letter (so `bypass`, `passport`, `passthrough` are clean).
_PASS_KW = (
    r"(?:[\w.-]{0,64}?(?:password|passwd|passphrase)"
    r"|(?:[\w.-]{0,64}?[_.-])?(?:pass|pwd)(?![A-Za-z]))"
)
_SECRET_KW = r"[\w.-]{0,64}?(?:secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credentials?)"
# Keys start at the beginning of a word run and are bounded, so matching stays linear on
# long dash/dot-separated input. `suf` is the rest of the key after the keyword.
_KEY = r"(?<![\w.-])%s(?P<suf>[\w.-]{0,40})[\"']?[ \t]*[:=][ \t]*"

# Key suffixes that mark config/metadata rather than a secret value (`password_policy`).
_BENIGN_SUFFIX = re.compile(
    r"(?i)(?:^|[_.-])(?:policy|hash|hashed|count|counts|name|names|env|file|path|length|len"
    r"|min|max|field|prompt|hint|rules|regex|pattern|type|header|budget|limit|ttl|expiry)(?:$|[_.-])"
    r"|izers?$"
)
_BENIGN_VALUE = re.compile(
    r"(?i)(?:true|false|null|none|nil|yes|no|on|off)"  # literals
    r"|[A-Za-z_][\w.]{0,60}\([\w., ]{0,60}\)"  # simple call: get_password_from_vault()
    # env reference, UPPER_SNAKE only (case-sensitive): $DB_PASS, ${DB_PASS}; $Hunter2 is a password
    r"|(?-i:\$\{?[A-Z_][A-Z0-9_]{0,60}\}?)"
)


def _keyed(kw: str, min_len: int) -> list[re.Pattern[str]]:
    quoted = (
        r"(?i)" + _KEY % kw + r"(?P<q>[\"'])(?P<v>" + _NOT_ALREADY + r"[^\n]{%d,}?)(?P=q)" % min_len
    )
    bare = r"(?i)" + _KEY % kw + r"(?P<v>" + _NOT_ALREADY + r"[^\s\"']{%d,})" % min_len
    return [re.compile(quoted), re.compile(bare)]


def _benign(m: re.Match[str]) -> bool:
    suf = m.groupdict().get("suf")
    if suf is None:
        return False
    return bool(_BENIGN_SUFFIX.search(suf) or _BENIGN_VALUE.fullmatch(m.group("v")))


# Patterns where only the value (group "v") is replaced, keeping the key/prefix.
_VALUE = [
    re.compile(r"(?i)\bBearer\s+(?P<v>" + _NOT_ALREADY + r"[A-Za-z0-9._~+/=-]{16,})"),
    re.compile(
        r"(?i)\bBasic\s+(?P<v>"
        + _NOT_ALREADY
        # digit/=/+// or an interior lower->UPPER step marks base64 rather than an English word
        + r"(?=[A-Za-z0-9+/]{0,200}(?:[0-9=+/]|(?-i:[a-z][A-Z])))[A-Za-z0-9+/]{8,200}={0,2})"
    ),
    # scheme://[user]:password@host ; the password may hold raw '@' (ends at the last '@' of the authority)
    re.compile(
        r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,20}://[^\s:/@]{0,100}:"
        r"(?P<v>" + _NOT_ALREADY + r"[^\s/]{1,200})(?=@[^\s@/]{0,255}(?:[/\s?#]|$))"
    ),
    *_keyed(_PASS_KW, 3),
    *_keyed(_SECRET_KW, 8),
]

_CANDIDATE = re.compile(r"[A-Za-z0-9+/_=-]{24,}")
_HEX = re.compile(r"[0-9a-fA-F]+")
# Hex runs are checked on their own, whatever they are glued to (`key=<hex>`, `note-<hex>`):
# inside one `_CANDIDATE` token they would fail the pure-hex test, and hex entropy (<= 4.0)
# never reaches the 2-class threshold. Linear: a start inside a run fails the lookbehind.
_HEX_RUN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])")
_HEX_ALWAYS = 48  # 32-47 hex chars need a key word just before (a 40-char git SHA alone is clean)
_KEY_NEAR = re.compile(r"(?i)secret|token|key|passw|pwd|credential|auth")
_ENTROPY_MIN = 4.0
_ENTROPY_MIN_2CLASS = 4.2
_SPLIT = re.compile(r"(\[REDACTED\])")


@dataclass
class ScrubRules:
    denylist: list[str] = field(default_factory=list)
    extra_patterns: list[str] = field(default_factory=list)


@dataclass
class ScrubResult:
    text: str
    redactions: int
    risky: bool
    reasons: list[str]


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _looks_secret(tok: str, before: str) -> bool:
    if _HEX.fullmatch(tok):
        if len(tok) >= 48:
            return True
        if len(tok) >= 32:
            return bool(_KEY_NEAR.search(before))
        return False
    classes = sum(
        (any(c.islower() for c in tok), any(c.isupper() for c in tok), any(c.isdigit() for c in tok))
    )
    ent = _entropy(tok)
    if classes >= 3:
        return ent >= _ENTROPY_MIN
    if classes == 2 and len(tok) >= 32 and "/" not in tok and "_" not in tok:
        return ent >= _ENTROPY_MIN_2CLASS
    return False


def _apply_user(text: str, pattern: re.Pattern[str]) -> tuple[str, int]:
    """Apply a user pattern outside existing markers; ignore empty matches."""
    n = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal n
        if not m.group(0):
            return m.group(0)
        n += 1
        return REDACTED

    parts = _SPLIT.split(text)
    for i in range(0, len(parts), 2):  # odd indexes are markers
        parts[i] = pattern.sub(repl, parts[i])
    return "".join(parts), n


def scrub(text: str, rules: ScrubRules) -> ScrubResult:
    count = 0
    reasons: list[str] = []

    def note(reason: str, n: int) -> None:
        nonlocal count
        if n:
            count += n
            if reason not in reasons:
                reasons.append(reason)

    for pat in _WHOLE:
        text, n = pat.subn(REDACTED, text)
        note("credential", n)

    for pat in _VALUE:
        found = 0

        def repl(m: re.Match[str]) -> str:
            nonlocal found
            if _benign(m):
                return m.group(0)
            found += 1
            s = m.group(0)
            a = m.start("v") - m.start()
            b = m.end("v") - m.start()
            return s[:a] + REDACTED + s[b:]

        text = pat.sub(repl, text)
        note("credential", found)

    found = 0

    def hex_repl(m: re.Match[str]) -> str:
        nonlocal found
        tok = m.group(0)
        before = m.string[max(0, m.start() - 30) : m.start()]
        if len(tok) >= _HEX_ALWAYS or _KEY_NEAR.search(before):
            found += 1
            return REDACTED
        return tok

    text = _HEX_RUN.sub(hex_repl, text)
    note("credential", found)

    found = 0

    def entropy_repl(m: re.Match[str]) -> str:
        nonlocal found
        before = m.string[max(0, m.start() - 30) : m.start()]
        if _looks_secret(m.group(0), before):
            found += 1
            return REDACTED
        return m.group(0)

    text = _CANDIDATE.sub(entropy_repl, text)
    note("credential", found)

    for term in rules.denylist:
        if term.strip():
            text, n = _apply_user(text, re.compile(re.escape(term), re.I))
            note("customer_data", n)

    for pattern in rules.extra_patterns:
        text, n = _apply_user(text, re.compile(pattern))
        note("custom_pattern", n)

    risky = bool(reasons)  # credentials, denylisted terms and user patterns all hold the entry
    return ScrubResult(text=text, redactions=count, risky=risky, reasons=reasons)


def load_rules(path: Path) -> ScrubRules:
    """Read `denylist` and `extra_patterns` from the store repo's config.yaml."""
    if not path.exists():
        return ScrubRules()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    out: dict[str, list[str]] = {}
    for key in ("denylist", "extra_patterns"):
        val = data.get(key) or []
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            raise ValueError(f"{key} in {path} must be a list of strings")
        out[key] = list(val)
    for pattern in out["extra_patterns"]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex in extra_patterns: {pattern!r}: {exc}") from exc
        if re.compile(pattern).search(""):
            raise ValueError(f"extra_patterns entry {pattern!r} matches the empty string")
    return ScrubRules(denylist=out["denylist"], extra_patterns=out["extra_patterns"])
