"""Settings loading and AdkLiteLlmClient configuration/mapping (FR-015).

No network: ``google.adk.models.lite_llm`` (which imports litellm) is never
imported here; the LiteLlm instance is replaced by a stub for ``complete``.
Only ``test_live_openrouter_call`` reaches OpenRouter, and it is ``live``-marked.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from harness_builder.config import DEFAULT_TRACE_DIR, Settings
from harness_builder.core.protocols import LLMClient
from harness_builder.core.types import Message, ModelConfig, ToolCall
from harness_builder.errors import ConfigError
from harness_builder.llm.adk_litellm import (
    MISSING_KEY,
    MISSING_MODEL,
    AdkLiteLlmClient,
    LLMCallError,
    from_adk_response,
    openrouter_model,
    to_adk_request,
)
from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema

ENV_NAMES = (
    "OPENROUTER_API_KEY",
    "HARNESS_MODEL",
    "HARNESS_TRACE_DIR",
    "HARNESS_SKILLS_ROOT",
    "HARNESS_DOCKER_IMAGE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run in an empty temp cwd with no harness env vars; return the cwd."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def settings(
    key: str | None = "sk-test", model: str | None = "openai/gpt-4o"
) -> Settings:
    """Return explicit settings (never read from the machine's .env)."""
    return Settings(openrouter_api_key=key, model=model)


# --- Settings.from_env --------------------------------------------------------


def test_from_env_reads_mapping() -> None:
    s = Settings.from_env(
        environ={
            "OPENROUTER_API_KEY": "k",
            "HARNESS_MODEL": "m",
            "HARNESS_TRACE_DIR": "t",
            "HARNESS_SKILLS_ROOT": "s",
            "HARNESS_DOCKER_IMAGE": "i",
        },
        dotenv_path=Path("does-not-exist.env"),
    )
    assert s == Settings(
        openrouter_api_key="k",
        model="m",
        trace_dir=Path("t"),
        skills_root=Path("s"),
        docker_image="i",
    )


def test_from_env_defaults(tmp_path: Path) -> None:
    s = Settings.from_env(environ={}, dotenv_path=tmp_path / "missing.env")
    assert s.openrouter_api_key is None
    assert s.model is None
    assert s.trace_dir == DEFAULT_TRACE_DIR


def test_process_env_wins_over_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENROUTER_API_KEY=from-file\nHARNESS_MODEL=file-model\n", encoding="utf-8"
    )
    s = Settings.from_env(environ={"HARNESS_MODEL": "env-model"}, dotenv_path=dotenv)
    assert s.model == "env-model"
    assert s.openrouter_api_key == "from-file"


def test_empty_values_count_as_unset(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("HARNESS_MODEL=file-model\n", encoding="utf-8")
    s = Settings.from_env(
        environ={"OPENROUTER_API_KEY": "", "HARNESS_MODEL": ""}, dotenv_path=dotenv
    )
    assert s.openrouter_api_key is None
    assert s.model == "file-model"


def test_from_env_does_not_mutate_os_environ(clean_env: Path) -> None:
    (clean_env / ".env").write_text("OPENROUTER_API_KEY=secret\n", encoding="utf-8")
    s = Settings.from_env()
    assert s.openrouter_api_key == "secret"
    assert "OPENROUTER_API_KEY" not in os.environ


# --- client construction -----------------------------------------------------


def test_adk_client_satisfies_llm_client_protocol() -> None:
    client: LLMClient = AdkLiteLlmClient(settings())
    assert client is not None


@pytest.mark.parametrize("key", [None, ""])
def test_missing_key_raises_config_error(key: str | None) -> None:
    with pytest.raises(ConfigError) as info:
        AdkLiteLlmClient(settings(key=key))
    assert str(info.value) == MISSING_KEY
    assert "OPENROUTER_API_KEY" in str(info.value)
    assert ".env" in str(info.value)


def test_missing_model_raises_config_error() -> None:
    with pytest.raises(ConfigError) as info:
        AdkLiteLlmClient(settings(model=None))
    assert str(info.value) == MISSING_MODEL


def test_default_settings_from_empty_env_raise_missing_key(clean_env: Path) -> None:
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        AdkLiteLlmClient()


def test_default_settings_read_dotenv(clean_env: Path) -> None:
    (clean_env / ".env").write_text(
        "OPENROUTER_API_KEY=k\nHARNESS_MODEL=openai/gpt-4o\n", encoding="utf-8"
    )
    assert AdkLiteLlmClient().model == "openrouter/openai/gpt-4o"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("openai/gpt-4o", "openrouter/openai/gpt-4o"),
        ("openrouter/openai/gpt-4o", "openrouter/openai/gpt-4o"),
    ],
)
def test_openrouter_prefix_applied_once(name: str, expected: str) -> None:
    assert openrouter_model(name) == expected
    assert AdkLiteLlmClient(settings(model=name)).model == expected


def test_construction_imports_no_adk_or_litellm() -> None:
    import subprocess
    import sys

    code = (
        "import sys\n"
        "from harness_builder.config import Settings\n"
        "from harness_builder.llm.adk_litellm import AdkLiteLlmClient\n"
        "AdkLiteLlmClient(Settings(openrouter_api_key='k', model='m'))\n"
        "print(sorted(m for m in sys.modules"
        " if m.startswith(('google.adk', 'google.genai', 'litellm'))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert out.stdout.strip() == "[]"


# --- request / response mapping (ADK types only, no litellm) ------------------


def _request(model_name: str = "anthropic/claude-haiku-4.5") -> LLMRequest:
    call = ToolCall(id="c1", name="add", args={"a": 1})
    schema = {
        "type": "object",
        "properties": {"p": {"$ref": "#/$defs/P"}},
        "$defs": {"P": {"type": "object"}},
    }
    return LLMRequest(
        model=ModelConfig(name=model_name, temperature=0.1, max_output_tokens=64),
        system="base",
        messages=[
            Message(role="system", content="extra"),
            Message(role="user", content="add"),
            Message(role="assistant", content="calling", tool_calls=[call]),
            Message(role="tool", content='{"sum": 2}', tool_call_id="c1"),
            Message(role="tool", content="plain", tool_call_id="c1"),
        ],
        tools=[ToolSchema(name="add", description="Add.", parameters=schema)],
    )


def test_to_adk_request_maps_system_tools_and_model() -> None:
    adk = to_adk_request(_request(), "openrouter/default")
    assert adk.model == "openrouter/anthropic/claude-haiku-4.5"
    config = adk.config
    assert config is not None
    assert config.system_instruction == "base\n\nextra"
    assert config.temperature == 0.1
    assert config.max_output_tokens == 64
    assert config.tools is not None
    assert len(config.tools) == 1
    tool = config.tools[0]
    decls = getattr(tool, "function_declarations", None)
    assert decls is not None
    assert decls[0].name == "add"
    assert decls[0].parameters_json_schema == _request().tools[0].parameters


def test_to_adk_request_uses_default_model_when_name_empty() -> None:
    adk = to_adk_request(_request(model_name=""), "openrouter/default")
    assert adk.model == "openrouter/default"


def test_to_adk_request_maps_history() -> None:
    contents = to_adk_request(_request(), "d").contents
    assert [c.role for c in contents] == ["user", "model", "user", "user"]
    parts = contents[1].parts
    assert parts is not None
    assert parts[0].text == "calling"
    fc = parts[1].function_call
    assert fc is not None
    assert (fc.id, fc.name, fc.args) == ("c1", "add", {"a": 1})
    responses = []
    for content in contents[2:]:
        assert content.parts is not None
        fr = content.parts[0].function_response
        assert fr is not None
        assert (fr.id, fr.name) == ("c1", "add")
        responses.append(fr.response)
    assert responses == [{"sum": 2}, {"content": "plain"}]


def _adk_response(**kwargs: Any) -> Any:
    from google.adk.models.llm_response import LlmResponse

    return LlmResponse(**kwargs)


def test_from_adk_response_maps_text_calls_and_usage() -> None:
    from google.genai import types

    resp = _adk_response(
        content=types.Content(
            role="model",
            parts=[
                types.Part(text="thinking", thought=True),
                types.Part(text="hello "),
                types.Part(function_call=types.FunctionCall(name="add", args={"a": 1})),
                types.Part(text="world"),
            ],
        ),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=11, candidates_token_count=4
        ),
        finish_reason=types.FinishReason.STOP,
        model_version="m-1",
    )
    out = from_adk_response(resp)
    assert out.text == "hello world"
    assert out.tool_calls == [ToolCall(id="call_2", name="add", args={"a": 1})]
    assert (out.usage.input_tokens, out.usage.output_tokens) == (11, 4)
    assert out.usage.llm_calls == 1
    assert out.usage.cost_usd is None
    assert out.finish_reason == "stop"
    assert out.model == "m-1"


def test_from_adk_response_error_without_content_raises() -> None:
    with pytest.raises(LLMCallError, match="429"):
        from_adk_response(_adk_response(error_code="429", error_message="slow"))


class _StubLiteLlm:
    """Stands in for ADK LiteLlm: yields scripted chunks, records requests."""

    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks
        self.requests: list[Any] = []

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncIterator[Any]:
        self.requests.append((llm_request, stream))
        for chunk in self.chunks:
            yield chunk


@pytest.mark.anyio
async def test_complete_uses_last_non_partial_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.genai import types

    def chunk(text: str, partial: bool) -> Any:
        content = types.Content(role="model", parts=[types.Part(text=text)])
        return _adk_response(content=content, partial=partial)

    stub = _StubLiteLlm([chunk("par", True), chunk("final", False)])
    client = AdkLiteLlmClient(settings())
    monkeypatch.setattr(client, "_llm", stub)
    out = await client.complete(_request())
    assert out == LLMResponse(text="final", usage=out.usage)
    assert stub.requests[0][1] is False
    assert stub.requests[0][0].model == "openrouter/anthropic/claude-haiku-4.5"


@pytest.mark.anyio
async def test_complete_without_response_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AdkLiteLlmClient(settings())
    monkeypatch.setattr(client, "_llm", _StubLiteLlm([]))
    with pytest.raises(LLMCallError):
        await client.complete(_request())


# --- live -------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.anyio
async def test_live_openrouter_call() -> None:
    """Real OpenRouter call; runs only with HARNESS_LIVE=1 and a configured key."""
    current = Settings.from_env()
    if not current.openrouter_api_key or not current.model:
        pytest.skip("OPENROUTER_API_KEY / HARNESS_MODEL not configured")
    client = AdkLiteLlmClient(current)
    req = LLMRequest(
        model=ModelConfig(name=current.model, max_output_tokens=16),
        messages=[Message(role="user", content="Reply with the word: pong")],
    )
    out = await client.complete(req)
    assert out.text.strip()
    assert out.usage.llm_calls == 1
