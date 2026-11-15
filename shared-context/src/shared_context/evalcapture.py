"""Capture-quality check (design success criterion 1): `shared-context eval`.

Runs the real capture pipeline (one `final` pass per transcript, scrubber included,
in-memory store, throwaway state dir) over a directory of transcript JSONL files and
grades the learnings against labels you wrote by hand.

Labels file (YAML or JSON): a mapping from transcript file name to the learnings a
teammate would have wanted, each with an id and keywords that must ALL appear
(case-insensitive) in the title, topic key or body of a captured entry::

    exporter.jsonl:
      - id: fd-leak
        keywords: [exporter, handle]

Recall = labeled learnings matched by at least one entry / all labeled learnings.
Junk = captured entries that match no labeled learning (a coarse proxy: a good but
unlabeled learning counts as junk, so label generously). The model is whatever
`client_for(transcript_name)` returns; the CLI uses the real headless `claude`.
"""
from __future__ import annotations

import argparse
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from shared_context.capture import run_capture
from shared_context.config import Config
from shared_context.model import ModelClient
from shared_context.models import Entry, Status
from shared_context.scrub import ScrubRules, load_rules
from shared_context.store.memory import InMemoryStore

DEFAULT_MIN_RECALL = 0.70
DEFAULT_MAX_JUNK_RATIO = 0.25
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass
class TranscriptScore:
    name: str
    expected: int
    found: list[str]
    missed: list[str]
    entries: list[Entry] = field(default_factory=list)
    junk: list[Entry] = field(default_factory=list)
    skipped_reason: str | None = None


@dataclass
class EvalReport:
    scores: list[TranscriptScore]

    @property
    def expected(self) -> int:
        return sum(s.expected for s in self.scores)

    @property
    def found(self) -> int:
        return sum(len(s.found) for s in self.scores)

    @property
    def entries(self) -> int:
        return sum(len(s.entries) for s in self.scores)

    @property
    def junk(self) -> int:
        return sum(len(s.junk) for s in self.scores)

    @property
    def recall(self) -> float:
        return self.found / self.expected if self.expected else 0.0

    @property
    def junk_ratio(self) -> float:
        return self.junk / self.entries if self.entries else 0.0

    def passed(self, min_recall: float = DEFAULT_MIN_RECALL, max_junk_ratio: float = DEFAULT_MAX_JUNK_RATIO) -> bool:
        return self.expected > 0 and self.recall >= min_recall and self.junk_ratio <= max_junk_ratio


def load_labels(path: Path) -> dict[str, list[dict]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("labels file must map transcript names to lists of learnings")
    labels: dict[str, list[dict]] = {}
    for name, items in data.items():
        if not isinstance(items, list):
            raise ValueError(f"labels for {name} must be a list")
        clean = []
        for item in items:
            kws = item.get("keywords") if isinstance(item, dict) else None
            if not (isinstance(kws, list) and kws and all(isinstance(k, str) and k.strip() for k in kws)):
                raise ValueError(f"each label for {name} needs a non-empty keywords list")
            clean.append({"id": str(item.get("id") or "|".join(kws)), "keywords": [k.lower() for k in kws]})
        labels[str(name)] = clean
    return labels


def _text(entry: Entry) -> str:
    return " ".join([entry.title, entry.topic_key, entry.body]).lower()


def evaluate(
    transcripts_dir: Path,
    labels: dict[str, list[dict]],
    client_for: Callable[[str], ModelClient],
    rules: ScrubRules | None = None,
) -> EvalReport:
    """Run one final capture pass per labeled transcript and score it."""
    rules = rules if rules is not None else ScrubRules()
    scores: list[TranscriptScore] = []
    for name in sorted(labels):
        path = Path(transcripts_dir) / name
        expected = labels[name]
        if not path.is_file():
            scores.append(TranscriptScore(name, len(expected), [], [e["id"] for e in expected],
                                          skipped_reason="transcript_missing"))
            continue
        store = InMemoryStore()
        with tempfile.TemporaryDirectory(prefix="shared-context-eval-") as tmp:
            cfg = Config(store_path=Path(tmp))
            result = run_capture(_SAFE.sub("-", path.stem) or "eval", path, "final", store,
                                 client_for(name), rules, Path(tmp), cfg, time.time())
        # held entries count too: they are produced (and need review), just not shared
        entries = [*store.iter_entries(), *store.iter_entries(Status.HELD)]
        texts = [(_text(e), e) for e in entries]
        found, missed, matched = [], [], set()
        for exp in expected:
            hits = [e for t, e in texts if all(k in t for k in exp["keywords"])]
            if hits:
                found.append(exp["id"])
                matched.update(e.id for e in hits)
            else:
                missed.append(exp["id"])
        junk = [e for _, e in texts if e.id not in matched]
        scores.append(TranscriptScore(name, len(expected), found, missed, entries, junk, result.skipped_reason))
    return EvalReport(scores)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--transcripts", type=Path, required=True, help="directory of transcript .jsonl files")
    parser.add_argument("--labels", type=Path, required=True, help="YAML/JSON labels file (see module docs)")
    parser.add_argument("--min-recall", type=float, default=DEFAULT_MIN_RECALL)
    parser.add_argument("--max-junk-ratio", type=float, default=DEFAULT_MAX_JUNK_RATIO)
    parser.add_argument("--rules", type=Path, help="scrub rules YAML (default: only the built-in rules)")


def _headless(_name: str) -> ModelClient:
    from shared_context.model import ClaudeHeadlessClient

    return ClaudeHeadlessClient()


def run(args: argparse.Namespace, client_for: Callable[[str], ModelClient] | None = None) -> int:
    """Print the report; exit 0 when both thresholds are met, 1 otherwise."""
    from shared_context.cli import clean

    try:
        labels = load_labels(args.labels)
        rules = load_rules(args.rules) if args.rules else None
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"shared-context: eval: cannot read inputs ({type(exc).__name__})")
        return 1
    report = evaluate(args.transcripts, labels, client_for or _headless, rules)
    for s in report.scores:
        note = f"  [{s.skipped_reason}]" if s.skipped_reason else ""
        print(f"{clean(s.name, 60)}: found {len(s.found)}/{s.expected}, {len(s.entries)} entries, "
              f"{len(s.junk)} junk{note}")
        for missed in s.missed:
            print(f"    missed: {clean(missed, 60)}")
        for e in s.junk:
            print(f"    junk:   {clean(e.title, 80)}")
    print(f"recall {report.recall:.0%} (need {args.min_recall:.0%}); "
          f"junk {report.junk}/{report.entries} = {report.junk_ratio:.0%} (max {args.max_junk_ratio:.0%})")
    ok = report.passed(args.min_recall, args.max_junk_ratio)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None, client_for: Callable[[str], ModelClient] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shared-context eval")
    add_arguments(parser)
    return run(parser.parse_args(argv), client_for)
