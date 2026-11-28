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


def test_scratch_files_go_in_tmp() -> None:
    prompt = load_system_prompt()

    assert "scratch files in /tmp" in prompt
    assert "Work only in /workspace" not in prompt


def test_the_python_working_directory_is_said_to_persist() -> None:
    kept = "working directory"

    assert kept in load_tool_descriptions()["python"]
    line = next(x for x in load_system_prompt().splitlines() if "`python` keeps" in x)
    assert kept in line


def test_the_first_plan_of_a_message_goes_alone() -> None:
    lines = load_system_prompt().splitlines()
    first = next(line for line in lines if line.startswith("3. "))
    later = next(line for line in lines if line.startswith("4. "))

    assert "on its own" in first
    assert "wait for its result" in first
    assert "Later `plan` calls" in later


def test_a_lost_sandbox_ends_the_turn() -> None:
    prompt = load_system_prompt()

    assert "sandbox is gone" in prompt
    assert "stop and tell the user" in prompt
