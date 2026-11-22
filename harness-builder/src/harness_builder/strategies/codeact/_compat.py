"""The only module that imports ``codeact_agent`` (designs.md 7.3, U2 option A).

Importing ``codeact_agent`` runs its package ``__init__``: openai-agents is
imported (about 12 s cold), a global converter monkey-patch is applied and an
in-process language registry is built. Every import therefore happens inside a
function, on first use, so ReAct-only runs and most tests never pay for it.
"""

DEFAULT_OUTPUT_MAX_CHARS = 4000


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Return ``(language, code)`` for each fenced block in ``text``.

    Delegates to ``codeact_agent.loop.extract_code_blocks``: languages are
    lowercased and aliases normalized (``py`` -> ``python``, ``sh`` -> ``bash``);
    an untagged block has language ``"text"``.
    """
    from codeact_agent.loop import extract_code_blocks as sibling_extract

    return sibling_extract(text)


def truncate_output(text: str, max_chars: int = DEFAULT_OUTPUT_MAX_CHARS) -> str:
    """Cap ``text`` at ``max_chars`` keeping head and tail with an omission marker.

    Delegates to ``codeact_agent.loop.truncate_output``.
    """
    from codeact_agent.loop import truncate_output as sibling_truncate

    return sibling_truncate(text, max_chars)


def sibling_system_prompt() -> str:
    """Return ``codeact_agent.prompts.CODEACT_SYSTEM_PROMPT``.

    Read only for provenance and the section-drift test; the harness sends
    ``prompt.CODEACT_DOCKER_PROMPT`` instead.
    """
    from codeact_agent.prompts import CODEACT_SYSTEM_PROMPT

    return CODEACT_SYSTEM_PROMPT
