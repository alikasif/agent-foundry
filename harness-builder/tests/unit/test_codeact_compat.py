"""Pins the ``codeact_agent`` behaviour CodeAct relies on (designs.md 7.3, U2).

``strategies.codeact._compat`` delegates to the sibling package; these tests
fail if a sibling upgrade changes block extraction, truncation or the prompt
sections ``prompt.REUSED_SECTIONS`` copies verbatim (drift test).
"""

import subprocess
import sys

import pytest

from harness_builder.strategies.codeact import _compat, prompt
from harness_builder.strategies.codeact.strategy import (
    BLOCK_ID_PREFIX,
    PROSE_LANGUAGES,
    SUPPORTED_LANGUAGES,
    CodeActStrategy,
)

# --- extract_code_blocks -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("no code here", [], id="none"),
        pytest.param("```python\nx = 1\n```", [("python", "x = 1")], id="python"),
        pytest.param("```py\nx\n```", [("python", "x")], id="py-alias"),
        pytest.param("```sh\necho\n```", [("bash", "echo")], id="sh-alias"),
        pytest.param("```PYTHON\nx\n```", [("python", "x")], id="lowercased"),
        pytest.param("```\nplain\n```", [("text", "plain")], id="untagged"),
        pytest.param("```js\n1\n```", [("javascript", "1")], id="js-alias"),
        pytest.param(
            "```python3\nx\n```", [("python3", "x")], id="python3-not-normalized"
        ),
        pytest.param(
            "```python\n\n  x = 1  \n\n```", [("python", "x = 1")], id="stripped"
        ),
    ],
)
def test_extract_code_blocks(text: str, expected: list[tuple[str, str]]) -> None:
    assert _compat.extract_code_blocks(text) == expected


def test_extract_code_blocks_keeps_response_order() -> None:
    text = "a\n```bash\necho 1\n```\nb\n```python\nprint(2)\n```\nc\n```\n3\n```"

    assert _compat.extract_code_blocks(text) == [
        ("bash", "echo 1"),
        ("python", "print(2)"),
        ("text", "3"),
    ]


def test_untagged_default_is_treated_as_prose() -> None:
    [(lang, _)] = _compat.extract_code_blocks("```\nx\n```")

    assert lang in PROSE_LANGUAGES
    assert not PROSE_LANGUAGES & set(SUPPORTED_LANGUAGES)


# --- truncate_output -----------------------------------------------------------


@pytest.mark.parametrize("text", ["", "abc", "a" * 20])
def test_truncate_output_leaves_short_text(text: str) -> None:
    assert _compat.truncate_output(text, 20) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a" * 21, "a" * 10 + "\n… [1 chars omitted] …\n" + "a" * 10),
        (
            "HEAD" + "x" * 92 + "TAIL",
            "HEADxxxxxx\n… [80 chars omitted] …\nxxxxxxTAIL",
        ),
    ],
)
def test_truncate_output_keeps_head_and_tail(text: str, expected: str) -> None:
    assert _compat.truncate_output(text, 20) == expected


def test_truncate_output_default_matches_sibling() -> None:
    from codeact_agent.loop import DEFAULT_OUTPUT_MAX_CHARS

    assert _compat.DEFAULT_OUTPUT_MAX_CHARS == DEFAULT_OUTPUT_MAX_CHARS == 4000
    long = "z" * 5000
    assert _compat.truncate_output(long) == _compat.truncate_output(long, 4000)
    assert CodeActStrategy().output_max_chars == 4000


# --- prompt drift ----------------------------------------------------------------


@pytest.mark.parametrize(
    "section",
    prompt.REUSED_SECTIONS,
    ids=[s.splitlines()[0][:30] for s in prompt.REUSED_SECTIONS],
)
def test_reused_section_is_verbatim_in_sibling_prompt(section: str) -> None:
    assert section in _compat.sibling_system_prompt()


@pytest.mark.parametrize(
    "section",
    prompt.REUSED_SECTIONS,
    ids=[s.splitlines()[0][:30] for s in prompt.REUSED_SECTIONS],
)
def test_reused_section_is_in_docker_prompt(section: str) -> None:
    assert section in prompt.CODEACT_DOCKER_PROMPT


def test_reused_sections_are_the_eight_documented_ones() -> None:
    assert len(prompt.REUSED_SECTIONS) == 8
    assert len(set(prompt.REUSED_SECTIONS)) == 8


@pytest.mark.parametrize(
    "dropped", ["llm_query", "register(", "ContextStore", "Git Bash"]
)
def test_sandbox_prompt_drops_host_only_features(dropped: str) -> None:
    assert dropped in _compat.sibling_system_prompt()  # negative control
    assert dropped not in prompt.CODEACT_DOCKER_PROMPT


def test_strategy_sends_docker_prompt_not_sibling_prompt() -> None:
    sent = CodeActStrategy().system_prompt("Base.", [])

    assert sent.startswith("Base.")
    assert prompt.CODEACT_DOCKER_PROMPT.strip() in sent
    assert _compat.sibling_system_prompt().strip() not in sent


def test_block_id_prefix_is_stable() -> None:
    assert BLOCK_ID_PREFIX == "codeact_block_"
    assert SUPPORTED_LANGUAGES == ("python", "bash")


# --- lazy import (fresh interpreter: other tests already loaded the sibling) --

LAZY_PROBE = """
import sys
from harness_builder.core.types import ModelConfig
from harness_builder.llm.types import LLMResponse
from harness_builder.strategies.registry import get_strategy
s = get_strategy("codeact", {"output_max_chars": 100})
s.system_prompt("base", [])
s.build_request([], [], ModelConfig(name="fake/model"))
before = sorted(m for m in ("codeact_agent", "agents") if m in sys.modules)
s.parse(LLMResponse(text="hi"))
after = "codeact_agent" in sys.modules
print(before, after)
"""


def test_codeact_agent_is_imported_only_on_first_parse() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", LAZY_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[] True"
