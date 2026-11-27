import re

from harness.prompts.load import load_system_prompt, load_tool_descriptions

# Wording that would make the model run code for requests that need none.
FORCED_CODE = re.compile(
    r"every (task|request|message)\b[^.]*\bcode"
    r"|must (use|run|write) (code|a tool)"
    r"|always (use|run|write) code",
    re.IGNORECASE,
)


def test_the_prompt_mentions_the_workspace() -> None:
    assert "/workspace" in load_system_prompt()


def test_every_tool_has_a_description() -> None:
    descriptions = load_tool_descriptions()

    assert set(descriptions) == {"plan", "bash", "python"}
    assert all(descriptions.values())


def test_python_says_replaced_builtins_last_one_call() -> None:
    description = load_tool_descriptions()["python"]

    assert "builtin" in description
    assert "only for the call that" in description


def test_nothing_requires_code_for_every_request() -> None:
    texts = [load_system_prompt(), *load_tool_descriptions().values()]

    assert not [text for text in texts if FORCED_CODE.search(text)]


def test_the_texts_are_ascii() -> None:
    texts = [load_system_prompt(), *load_tool_descriptions().values()]

    assert all(text.isascii() for text in texts)


def test_the_step_that_allows_no_code_says_files_must_be_looked_at() -> None:
    lines = load_system_prompt().splitlines()
    step = next(line for line in lines if line.startswith("2. "))

    assert "user's files" in step
    assert "what a command prints" in step
    assert "until you have looked at it in this chat" in step
