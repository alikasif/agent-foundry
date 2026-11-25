"""Verify the third-party stack installs and imports."""


def test_stack_imports() -> None:
    """The ADK, LiteLLM, MCP and psutil imports all resolve."""
    import google.adk  # noqa: F401
    import litellm  # noqa: F401
    import mcp  # noqa: F401
    import psutil  # noqa: F401
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.tools.mcp_tool import (  # noqa: F401
        McpToolset,
        StdioConnectionParams,
    )

    llm = LiteLlm(model="openrouter/anthropic/claude-sonnet-4.5")
    assert "openrouter" in llm.model
