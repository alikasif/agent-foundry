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

_PASS_KW = r"(?:password|passwd|pwd|pass(?!ed\b|ing\b|es\b|age\b))"
_SECRET_KW = r"(?:secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credentials?)"
# Key names are bounded (<=40 chars each side of the keyword) and must start at the
# beginning of a word run, so matching stays linear on long dash/dot-separated input.
_KEY = r"(?<![\w.-])[\w.-]{0,40}?%s[\w.-]{0,40}[\"']?[ \t]*[:=][ \t]*"


def _keyed(kw: str, min_len: int) -> list[re.Pattern[str]]:
    quoted = (
        r"(?i)" + _KEY % kw + r"(?P<q>[\"'])(?P<v>" + _NOT_ALREADY + r"[^\n]{%d,}?)(?P=q)" % min_len
    )
    bare = r"(?i)" + _KEY % kw + r"(?P<v>" + _NOT_ALREADY + r"[^\s\"']{%d,})" % min_len
    return [re.compile(quoted), re.compile(bare)]


# Patterns where only the value (group "v") is replaced, keeping the key/prefix.
_VALUE = [
    re.compile(r"(?i)\bBearer\s+(?P<v>" + _NOT_ALREADY + r"[A-Za-z0-9._~+/=-]{16,})"),
    re.compile(
        r"(?i)\bBasic\s+(?P<v>" + _NOT_ALREADY + r"(?=[A-Za-z0-9+/]*[0-9=+/])[A-Za-z0-9+/]{8,}={0,2})"
    ),
    re.compile(
        r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,20}://[^\s:/@]{1,100}:"
        r"(?P<v>" + _NOT_ALREADY + r"[^\s@/]{1,200})(?=@)"
    ),
    *_keyed(_PASS_KW, 3),
    *_keyed(_SECRET_KW, 8),
]

_CANDIDATE = re.compile(r"[A-Za-z0-9+/_=-]{24,}")
_HEX = re.compile(r"[0-9a-fA-F]+")
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


def _looks_secret(tok: str) -> bool:
    if _HEX.fullmatch(tok) and len(tok) == 40:
        return False  # git SHA
    classes = sum(
        (any(c.islower() for c in tok), any(c.isupper() for c in tok), any(c.isdigit() for c in tok))
    )
    return classes >= 3 and _entropy(tok) >= _ENTROPY_MIN


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
            found += 1
            s = m.group(0)
            a = m.start("v") - m.start()
            b = m.end("v") - m.start()
            return s[:a] + REDACTED + s[b:]

        text = pat.sub(repl, text)
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

    risky = "credential" in reasons or "customer_data" in reasons
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
