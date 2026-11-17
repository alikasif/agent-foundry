"""Tool decorator and ToolRegistry: schemas, validation, errors as observations.

FR-005: invalid args never reach the tool body; unknown tools are rejected.
FR-006: validation, unknown tool, exception and timeout failures come back as
``Observation(ok=False, error=ErrorObservation(...))``; nothing is raised and
the registry keeps serving later calls.
"""

import time
from pathlib import Path
from typing import Annotated, Any, cast

import anyio
import pytest
from pydantic import BaseModel, Field

from harness_builder.core import protocols
from harness_builder.core.types import (
    ErrorObservation,
    Observation,
    ToolCall,
    ToolResult,
    ValidatedCall,
)
from harness_builder.errors import ConfigError
from harness_builder.tools import Tool, ToolRegistry, derive_args_model, tool

pytestmark = pytest.mark.anyio


class StubContext:
    """Minimal object handed to tools as ``ctx`` (identity is all we check)."""

    run_id = "run-test"
    depth = 0


CTX = cast(protocols.RunContextView, StubContext())


@pytest.fixture
def calls() -> list[dict[str, Any]]:
    """Recorder of the kwargs every tool body received."""
    return []


@pytest.fixture
def registry(calls: list[dict[str, Any]]) -> ToolRegistry:
    """Registry with an ``add`` tool whose body records its arguments."""

    @tool
    def add(a: int, b: int = 1) -> int:
        """Add two integers."""
        calls.append({"a": a, "b": b})
        return a + b

    return ToolRegistry([add])


async def run(reg: ToolRegistry, name: str, args: dict[str, Any]) -> Observation:
    """Validate then invoke one call the way the loop does."""
    vcall = reg.validate(ToolCall(id="c1", name=name, args=args))
    if isinstance(vcall, ErrorObservation):
        return Observation(call_id="c1", ok=False, content=vcall.message, error=vcall)
    return await reg.invoke(vcall, CTX)


async def assert_still_serves(reg: ToolRegistry) -> None:
    """After a failure the registry must still run a valid call ("continues")."""
    obs = await run(reg, "add", {"a": 2, "b": 3})
    assert obs.ok
    assert obs.content == "5"


# --- decorator and schemas -----------------------------------------------------


def test_bare_decorator_builds_tool_from_signature() -> None:
    @tool
    def search_docs(query: str, limit: int = 5) -> str:
        """Search the docs."""
        return query

    assert isinstance(search_docs, Tool)
    assert search_docs.name == "search_docs"
    assert search_docs.description == "Search the docs."
    assert search_docs.args_model.__name__ == "SearchDocsArgs"
    schema = search_docs.args_model.model_json_schema()
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["limit"]["default"] == 5
    assert schema["required"] == ["query"]
    assert schema["additionalProperties"] is False


def test_decorator_options_override_defaults() -> None:
    @tool(
        name="renamed",
        description="Explicit.",
        timeout_s=2.0,
        nondeterministic=True,
        risk=["network", "network", "subprocess"],
    )
    def fn(x: int) -> int:
        return x

    assert (fn.name, fn.description, fn.timeout_s) == ("renamed", "Explicit.", 2.0)
    assert fn.nondeterministic is True
    assert fn.risk == frozenset({"network", "subprocess"})


def test_annotated_field_description_reaches_schema() -> None:
    @tool
    def f(path: Annotated[str, Field(description="File to read")]) -> str:
        """Read."""
        return path

    props = f.args_model.model_json_schema()["properties"]
    assert props["path"]["description"] == "File to read"


def test_explicit_args_model_is_used() -> None:
    class Args(BaseModel):
        n: int

    @tool(args_model=Args)
    def f(n: int) -> int:
        """Echo."""
        return n

    assert f.args_model is Args


def test_ctx_parameter_is_not_in_schema() -> None:
    @tool
    def f(x: int, ctx: object) -> int:
        """Uses ctx."""
        return x

    assert set(f.args_model.model_fields) == {"x"}


def test_derive_args_model_forbids_extra() -> None:
    def g(a: str) -> None:
        pass

    model = derive_args_model(g, "my_tool")
    assert model.__name__ == "MyToolArgs"
    with pytest.raises(ValueError):
        model.model_validate({"a": "x", "b": 1})


def _no_doc(x: int) -> int:
    return x


def _untyped(x) -> int:  # type: ignore[no-untyped-def]  # noqa: ANN001
    """Untyped."""
    return 0


def _varargs(*xs: int) -> int:
    """Varargs."""
    return 0


def _kwargs(**kw: int) -> int:
    """Kwargs."""
    return 0


def _posonly(x: int, /) -> int:
    """Positional only."""
    return x


def _bad_hint(x: "NoSuchType") -> int:  # type: ignore[name-defined]  # noqa: F821
    """Unresolvable."""
    return 0


@pytest.mark.parametrize(
    "fn", [_no_doc, _untyped, _varargs, _kwargs, _posonly, _bad_hint]
)
def test_bad_tool_definitions_raise_config_error(fn: Any) -> None:
    with pytest.raises(ConfigError):
        tool(fn)


def test_schemas_keep_order_and_filter_visible(registry: ToolRegistry) -> None:
    @tool
    def zeta(x: int) -> int:
        """Z."""
        return x

    registry.register(zeta)
    assert [s.name for s in registry.schemas()] == ["add", "zeta"]
    assert [s.name for s in registry.schemas({"zeta", "missing"})] == ["zeta"]
    schema = registry.schemas({"add"})[0]
    assert schema.description == "Add two integers."
    assert schema.parameters["required"] == ["a"]


def test_names_and_get(registry: ToolRegistry) -> None:
    assert registry.names() == ["add"]
    found = registry.get("add")
    assert found is not None
    assert found.name == "add"
    assert registry.get("nope") is None


def test_duplicate_registration_raises(registry: ToolRegistry) -> None:
    existing = registry.get("add")
    assert existing is not None
    with pytest.raises(ConfigError, match="already registered"):
        registry.register(existing)


def test_definition_without_fn_raises() -> None:
    class NoFn:
        name = "x"
        description = "d"
        args_model = BaseModel
        timeout_s = None
        nondeterministic = False
        risk: frozenset[str] = frozenset()

    with pytest.raises(ConfigError, match="fn"):
        ToolRegistry().register(cast(protocols.ToolDefinition, NoFn()))


def test_registry_satisfies_protocol() -> None:
    reg: protocols.ToolRegistry = ToolRegistry()
    assert reg.schemas() == []


# --- validation: invalid args never reach the body ------------------------------


def test_valid_call_is_normalized(registry: ToolRegistry) -> None:
    vcall = registry.validate(ToolCall(id="c", name="add", args={"a": "4"}))
    assert isinstance(vcall, ValidatedCall)
    assert vcall.args == {"a": 4, "b": 1}


@pytest.mark.parametrize(
    "args",
    [{}, {"a": "not-int"}, {"a": 1, "extra": True}, {"a": [1]}],
    ids=["missing", "wrong-type", "extra-arg", "list"],
)
async def test_invalid_args_become_validation_observation(
    registry: ToolRegistry, calls: list[dict[str, Any]], args: dict[str, Any]
) -> None:
    obs = await run(registry, "add", args)
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.type == "validation"
    assert "'add'" in obs.error.message
    assert obs.error.hint is not None
    assert "Required: a" in obs.error.hint
    assert calls == []
    await assert_still_serves(registry)


async def test_invoke_revalidates_args(
    registry: ToolRegistry, calls: list[dict[str, Any]]
) -> None:
    forged = ValidatedCall(call=ToolCall(id="c9", name="add", args={}), args={"a": "x"})
    obs = await registry.invoke(forged, CTX)
    assert obs.call_id == "c9"
    assert obs.error is not None
    assert obs.error.type == "validation"
    assert calls == []


async def test_unknown_tool_becomes_observation(registry: ToolRegistry) -> None:
    obs = await run(registry, "nope", {"a": 1})
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.type == "unknown_tool"
    assert "nope" in obs.error.message
    assert obs.error.hint is not None
    assert "add" in obs.error.hint
    await assert_still_serves(registry)


async def test_invoke_unknown_tool_becomes_observation() -> None:
    vcall = ValidatedCall(call=ToolCall(id="c", name="ghost"), args={})
    obs = await ToolRegistry().invoke(vcall, CTX)
    assert obs.error is not None
    assert obs.error.type == "unknown_tool"
    assert obs.error.hint is not None
    assert "(none)" in obs.error.hint


# --- invocation: exceptions and timeouts -------------------------------------------


async def test_tool_exception_becomes_observation(registry: ToolRegistry) -> None:
    @tool
    def boom(x: int) -> int:
        """Always fails."""
        raise RuntimeError(f"bad {x}")

    registry.register(boom)
    obs = await run(registry, "boom", {"x": 7})
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.type == "tool_exception"
    assert obs.error.message == "RuntimeError: bad 7"
    assert obs.content == obs.error.message
    await assert_still_serves(registry)


async def test_async_tool_exception_becomes_observation() -> None:
    @tool
    async def aboom() -> None:
        """Fails asynchronously."""
        raise ValueError("nope")

    obs = await run(ToolRegistry([aboom]), "aboom", {})
    assert obs.error is not None
    assert obs.error.type == "tool_exception"


async def test_own_timeout_error_is_a_tool_exception() -> None:
    @tool(timeout_s=5.0)
    def slow_api() -> None:
        """Raises its own TimeoutError."""
        raise TimeoutError("upstream")

    obs = await run(ToolRegistry([slow_api]), "slow_api", {})
    assert obs.error is not None
    assert obs.error.type == "tool_exception"


async def test_sync_tool_timeout_becomes_observation(registry: ToolRegistry) -> None:
    @tool(timeout_s=0.05)
    def sleepy() -> str:
        """Sleeps in a thread."""
        time.sleep(0.5)
        return "late"

    registry.register(sleepy)
    start = time.monotonic()
    obs = await run(registry, "sleepy", {})
    assert time.monotonic() - start < 0.45
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.type == "timeout"
    assert "sleepy" in obs.error.message
    await assert_still_serves(registry)


async def test_async_tool_timeout_becomes_observation() -> None:
    @tool(timeout_s=0.05)
    async def asleepy() -> str:
        """Sleeps on the event loop."""
        await anyio.sleep(5)
        return "late"

    start = time.monotonic()
    obs = await run(ToolRegistry([asleepy]), "asleepy", {})
    assert time.monotonic() - start < 1.0
    assert obs.error is not None
    assert obs.error.type == "timeout"


async def test_no_timeout_lets_tool_finish() -> None:
    @tool
    async def quick() -> str:
        """Quick."""
        await anyio.sleep(0.01)
        return "done"

    obs = await run(ToolRegistry([quick]), "quick", {})
    assert obs.ok
    assert obs.content == "done"


# --- invocation: arguments, ctx and return mapping -------------------------------


async def test_body_receives_typed_values() -> None:
    seen: list[Any] = []

    class Point(BaseModel):
        x: int
        y: int

    @tool
    def take(path: Path, point: Point) -> None:
        """Typed args."""
        seen.extend([path, point])

    vcall = ToolRegistry([take]).validate(
        ToolCall(id="c", name="take", args={"path": "a/b", "point": {"x": 1, "y": 2}})
    )
    assert isinstance(vcall, ValidatedCall)
    assert vcall.args["path"] == str(Path("a/b"))
    await ToolRegistry([take]).invoke(vcall, CTX)
    assert seen == [Path("a/b"), Point(x=1, y=2)]


async def test_ctx_is_injected() -> None:
    seen: list[Any] = []

    @tool
    def who(ctx: protocols.RunContextView) -> str:
        """Reports the run id."""
        seen.append(ctx)
        return "ok"

    obs = await run(ToolRegistry([who]), "who", {})
    assert obs.ok
    assert seen == [CTX]


async def test_ctx_cannot_be_passed_by_the_model() -> None:
    @tool
    def who(ctx: protocols.RunContextView) -> str:
        """Reports the run id."""
        return "ok"

    obs = await run(ToolRegistry([who]), "who", {"ctx": "forged"})
    assert obs.error is not None
    assert obs.error.type == "validation"


class Payload(BaseModel):
    """Model return value used in the mapping table."""

    path: Path


@pytest.mark.parametrize(
    ("value", "content", "final"),
    [
        ("text", "text", None),
        ({"p": Path("x")}, {"p": "x"}, None),
        (None, "", None),
        ([1, "a"], '[1, "a"]', None),
        (3, "3", None),
        (Payload(path=Path("y")), {"path": "y"}, None),
        (ToolResult(content="r", final="answer"), "r", "answer"),
        (ToolResult(content={"k": 1}), {"k": 1}, None),
    ],
    ids=["str", "dict", "none", "list", "int", "model", "result-final", "result"],
)
async def test_return_values_map_to_observation(
    value: Any, content: Any, final: str | None
) -> None:
    @tool
    def give() -> Any:
        """Returns a fixed value."""
        return value

    obs = await run(ToolRegistry([give]), "give", {})
    assert obs.ok
    assert obs.call_id == "c1"
    assert obs.content == content
    assert obs.final == final
    assert obs.error is None


async def test_observation_return_passes_through_with_call_id() -> None:
    @tool
    def raw() -> Observation:
        """Returns a ready observation."""
        return Observation(call_id="other", ok=True, content="x", final="f")

    obs = await run(ToolRegistry([raw]), "raw", {})
    assert obs == Observation(call_id="c1", ok=True, content="x", final="f")


async def test_unserializable_return_is_tool_exception() -> None:
    @tool
    def odd() -> Any:
        """Returns something JSON cannot encode."""
        return [object()]

    obs = await run(ToolRegistry([odd]), "odd", {})
    assert obs.error is not None
    assert obs.error.type == "tool_exception"
