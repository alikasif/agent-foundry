"""`shared-context eval` must grade the WHOLE transcript, like production's incremental passes (I8)."""
from __future__ import annotations

import json

from sim_support import learning, reply, write_transcript

from shared_context import evalcapture
from shared_context.config import Config


class KeywordClient:
    """Answers with a learning for every marker it sees in the prompt; records prompts."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, timeout_s: float) -> str:
        self.prompts.append(prompt)
        items = []
        if "EARLYMARKER" in prompt:
            items.append(learning("early-fd-leak", "Exporter leaks file handles",
                                  "The exporter opened a handle per batch; close it."))
        if "LATEMARKER" in prompt:
            items.append(learning("late-lease", "Release the scheduler lease first",
                                  "Release the scheduler lease before restarting the exporter."))
        return reply(*items)


def test_eval_covers_text_older_than_the_per_pass_cap(tmp_path):
    cap = Config(store_path=tmp_path).max_transcript_chars
    filler = "filler words about nothing in particular " * 100  # ~4,300 chars per turn
    turns = [("user", "EARLYMARKER the exporter crashes with too many open files")]
    turns += [("assistant" if i % 2 else "user", f"{filler} {i}") for i in range(40)]  # far over the cap
    turns += [("user", "LATEMARKER the scheduler lease deadlocks the restart")]
    tdir = tmp_path / "t"
    tdir.mkdir()
    write_transcript(tdir / "long.jsonl", turns)
    total = sum(len(t) for _r, t in turns)
    assert total > 2 * cap
    labels = {"long.jsonl": [{"id": "early", "keywords": ["exporter", "handle"]},
                             {"id": "late", "keywords": ["scheduler", "lease"]}]}
    client = KeywordClient()
    report = evalcapture.evaluate(tdir, labels, lambda _n: client)
    [score] = report.scores
    assert sorted(score.found) == ["early", "late"], (score.missed, len(client.prompts))
    assert len(client.prompts) >= 2  # several passes, each within the cap
    assert score.skipped_reason is None


def test_eval_short_transcript_single_final_pass(tmp_path):
    tdir = tmp_path / "t"
    tdir.mkdir()
    write_transcript(tdir / "s.jsonl", [("user", "EARLYMARKER exporter handle problem")])
    client = KeywordClient()
    report = evalcapture.evaluate(tdir, {"s.jsonl": [{"id": "early", "keywords": ["exporter", "handle"]}]},
                                  lambda _n: client)
    assert report.scores[0].found == ["early"] and len(client.prompts) == 1
    assert json.loads(json.dumps(report.recall)) == 1.0
