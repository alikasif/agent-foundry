"""Live needle-in-a-haystack eval for Phase C context-by-reference.

This eval requires a live LLM (makes real API calls).  It is deliberately
excluded from the default pytest run.

Usage
-----
    cd codeact_agent
    CODEACT_LIVE_EVAL=1 uv run python evals/live_needle_eval.py

Environment variables
---------------------
CODEACT_LIVE_EVAL      Must be set to "1" (or any truthy value) to run.
OPENROUTER_API_KEY     Required.  API key for OpenRouter.
CODEACT_MODEL          Optional.  Model identifier (default: see below).
OPENAI_API_BASE        Optional.  API base URL (default: openrouter).

What this eval proves
---------------------
- The agent correctly locates a hidden value in a large corpus (> context
  window) without loading the full text into the LLM history.
- Observations are truncated: no single history message exceeds the
  DEFAULT_OUTPUT_MAX_CHARS limit.
- The full corpus text is absent from every history message.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

# Allow running from the project root or the evals/ dir.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _check_env() -> None:
    """Exit early if CODEACT_LIVE_EVAL is not set."""
    if not os.environ.get("CODEACT_LIVE_EVAL"):
        print(
            "Skipped: set CODEACT_LIVE_EVAL=1 to run the live eval.\n"
            "  Example: CODEACT_LIVE_EVAL=1 uv run python evals/live_needle_eval.py"
        )
        sys.exit(0)


def _build_corpus(needle: str, total_lines: int = 1500) -> tuple[str, int]:
    """Build a synthetic corpus with a hidden needle at a random line.

    Returns:
        (full_corpus_text, needle_line_number_1indexed)
    """
    import random

    needle_line = random.randint(total_lines // 4, (3 * total_lines) // 4)
    lines: list[str] = []
    topics = [
        "machine learning",
        "distributed systems",
        "quantum computing",
        "graph theory",
        "cryptography",
        "natural language processing",
        "computer vision",
        "reinforcement learning",
        "database indexing",
        "network protocols",
    ]
    for i in range(1, total_lines + 1):
        if i == needle_line:
            lines.append(f"line {i}: The answer you are looking for is {needle}\n")
        else:
            topic = topics[i % len(topics)]
            lines.append(
                f"line {i}: Research in {topic} continues to advance "
                f"with new methods being published every year. "
                f"This line contains no special value. (ref: {i * 7})\n"
            )
    return "".join(lines), needle_line


def run_eval() -> None:
    """Run the needle-in-a-haystack live eval."""
    _check_env()

    from dotenv import load_dotenv

    load_dotenv()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("Error: OPENROUTER_API_KEY is not set.")
        sys.exit(1)

    os.environ.setdefault("OPENAI_API_KEY", api_key)

    model = os.environ.get("CODEACT_MODEL", "openrouter/anthropic/claude-3-5-haiku")
    base_url = os.environ.get("OPENAI_API_BASE", "https://openrouter.ai/api/v1")

    import anyio

    from codeact_agent.agent import CodeActAgent
    from codeact_agent.languages.default import build_default_registry
    from codeact_agent.loop import DEFAULT_OUTPUT_MAX_CHARS, AgentLoop
    from codeact_agent.sandbox import PythonSandbox

    # ------------------------------------------------------------------ #
    # Build corpus
    # ------------------------------------------------------------------ #
    needle = f"SECRET_NEEDLE_{uuid.uuid4().hex[:8].upper()}"
    corpus, needle_line = _build_corpus(needle, total_lines=1500)
    corpus_chars = len(corpus)
    corpus_lines = corpus.count("\n")

    print("=" * 60)
    print("Phase C Live Eval — Needle in a Haystack")
    print("=" * 60)
    print(f"Corpus: {corpus_chars:,} chars, {corpus_lines:,} lines")
    print(f"Needle: {needle}")
    print(f"Needle at line: {needle_line}")
    print(f"Truncation limit: {DEFAULT_OUTPUT_MAX_CHARS} chars/stream")
    print()

    # ------------------------------------------------------------------ #
    # Set up agent with the large corpus
    # ------------------------------------------------------------------ #
    agent = CodeActAgent(model=model, api_key=api_key, base_url=base_url)
    sandbox = PythonSandbox()
    # lock=True: the loop won't overwrite the corpus on each turn.
    sandbox.set_context(corpus, lock=True)
    registry = build_default_registry(sandbox)
    loop = AgentLoop(
        agent=agent,
        sandbox=sandbox,
        registry=registry,
        output_max_chars=DEFAULT_OUTPUT_MAX_CHARS,
    )

    history: list[dict[str, str]] = [
        {
            "role": "user",
            "content": (
                "The context variable contains a large document. "
                "Use context.search() or context.grep() to find the line "
                "containing 'SECRET_NEEDLE'. Extract ONLY the needle value "
                "(the token starting with SECRET_NEEDLE_) and call "
                "FINAL(needle_value) with it."
            ),
        }
    ]

    print("Running agent…")

    final_response, final_history = anyio.run(
        loop.run_turn,
        history,
    )

    print(f"Agent answer: {final_response!r}")
    print(f"Expected:     {needle!r}")
    print()

    # ------------------------------------------------------------------ #
    # Assertions
    # ------------------------------------------------------------------ #
    passed = True

    # 1. Correct answer
    if needle in final_response:
        print("[PASS] Needle found in agent answer.")
    else:
        print(f"[FAIL] Expected {needle!r} in answer, got {final_response!r}")
        passed = False

    # 2. Full corpus absent from history
    corpus_in_history = any(
        corpus in str(msg.get("content", "")) for msg in final_history
    )
    if not corpus_in_history:
        print("[PASS] Full corpus text absent from history.")
    else:
        print(
            "[FAIL] Full corpus text FOUND in history "
            "— by-reference invariant violated!"
        )
        passed = False

    # 3. No single history message exceeds the truncation limit by much
    max_msg_len = max(len(str(msg.get("content", ""))) for msg in final_history)
    # Allow some headroom for the XML wrapping
    safe_limit = DEFAULT_OUTPUT_MAX_CHARS + 500
    if max_msg_len <= safe_limit:
        print(
            f"[PASS] Max history message length: {max_msg_len:,} chars "
            f"(<= {safe_limit:,})."
        )
    else:
        print(
            f"[FAIL] History message too long: {max_msg_len:,} chars "
            f"(limit ~{safe_limit:,})."
        )
        passed = False

    print()
    print(f"History messages: {len(final_history)}")
    print(f"Max message length: {max_msg_len:,} chars")
    print()
    print("=" * 60)
    print("Result:", "PASS" if passed else "FAIL")
    print("=" * 60)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    run_eval()
