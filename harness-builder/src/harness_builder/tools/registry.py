"""Tool model, ``@tool`` decorator and the default ``ToolRegistry`` (FR-005, FR-006).

Tool failures never raise out of the registry: unknown tools, invalid args,
exceptions and timeouts all become ``ErrorObservation`` values.

Calling convention: the tool body is called with one keyword argument per field
of its ``args_model`` (validated values, so ``Path`` or nested models keep their
types). A parameter named ``ctx`` is not part of the args model; the registry
passes the ``RunContextView`` to it.
"""

import inspect
import json
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any, overload

import anyio
import anyio.to_thread
from pydantic import (
    BaseModel,
    ConfigDict,
    PydanticUserError,
    ValidationError,
    create_model,
)
from pydantic_core import to_jsonable_python

from harness_builder.core import protocols
from harness_builder.core.types import (
    ErrorObservation,
    Observation,
    ToolCall,
    ToolResult,
    ToolRisk,
    ValidatedCall,
)
from harness_builder.errors import ConfigError
from harness_builder.llm.types import ToolSchema

CTX_PARAM = "ctx"


class Tool(BaseModel):
    """A callable tool with a pydantic args model; satisfies ``ToolDefinition``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[..., Any]
    timeout_s: float | None = None
    nondeterministic: bool = False
    risk: frozenset[ToolRisk] = frozenset()


@overload
def tool(fn: Callable[..., Any], /) -> Tool: ...


@overload
def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    args_model: type[BaseModel] | None = None,
    timeout_s: float | None = None,
    nondeterministic: bool = False,
    risk: Iterable[ToolRisk] = (),
) -> Callable[[Callable[..., Any]], Tool]: ...


def tool(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    args_model: type[BaseModel] | None = None,
    timeout_s: float | None = None,
    nondeterministic: bool = False,
    risk: Iterable[ToolRisk] = (),
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """Build a ``Tool`` from a typed function; usable bare or with options.

    The name defaults to the function name and the description to its
    docstring. Without ``args_model`` one is derived from the signature.
    Raises ``ConfigError`` for untyped, variadic or positional-only parameters
    and for a missing description.
    """

    def build(func: Callable[..., Any]) -> Tool:
        tool_name = name or func.__name__
        text = description or inspect.getdoc(func)
        if not text:
            raise ConfigError(f"tool {tool_name!r} needs a description or docstring")
        return Tool(
            name=tool_name,
            description=text,
            args_model=args_model or derive_args_model(func, tool_name),
            fn=func,
            timeout_s=timeout_s,
            nondeterministic=nondeterministic,
            risk=frozenset(risk),
        )

    if fn is None:
        return build
    return build(fn)


def derive_args_model(fn: Callable[..., Any], tool_name: str) -> type[BaseModel]:
    """Return a pydantic model (extra args forbidden) from ``fn``'s signature.

    Only the annotations of model parameters are resolved (string annotations
    against ``fn``'s module), so the return and ``ctx`` annotations may name
    ``TYPE_CHECKING``-only types.
    """
    hints = inspect.get_annotations(fn)
    fields: dict[str, Any] = {}
    for param in inspect.signature(fn).parameters.values():
        if param.name == CTX_PARAM:
            continue
        if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            raise ConfigError(
                f"tool {tool_name!r}: parameter {param.name!r} must be a plain "
                "or keyword-only parameter"
            )
        if param.name not in hints:
            raise ConfigError(
                f"tool {tool_name!r}: parameter {param.name!r} needs a type hint"
            )
        default = ... if param.default is param.empty else param.default
        fields[param.name] = (hints[param.name], default)
    model_name = "".join(part.title() for part in tool_name.split("_")) + "Args"
    try:
        model = create_model(
            model_name,
            __config__=ConfigDict(extra="forbid"),
            __module__=fn.__module__,
            **fields,
        )
        model.model_rebuild(raise_errors=True)
    except (NameError, AttributeError, SyntaxError, PydanticUserError) as exc:
        raise ConfigError(f"tool {tool_name!r}: unresolvable type hint: {exc}") from exc
    return model


class ToolRegistry:
    """Default registry: holds tools, validates calls, invokes tool bodies.

    Accepts any ``ToolDefinition`` that also has a callable ``fn`` attribute
    (``Tool`` or an equivalent object such as a container-backed tool).
    """

    def __init__(self, tools: Iterable[protocols.ToolDefinition] = ()) -> None:
        self._tools: dict[str, protocols.ToolDefinition] = {}
        self._fns: dict[str, Callable[..., Any]] = {}
        self._takes_ctx: dict[str, bool] = {}
        for item in tools:
            self.register(item)

    def register(self, tool: protocols.ToolDefinition) -> None:
        """Add ``tool``; raises ``ConfigError`` on a duplicate name or no ``fn``."""
        if tool.name in self._tools:
            raise ConfigError(f"tool {tool.name!r} is already registered")
        fn = getattr(tool, "fn", None)
        if not callable(fn):
            raise ConfigError(f"tool {tool.name!r} has no callable 'fn'")
        self._tools[tool.name] = tool
        self._fns[tool.name] = fn
        self._takes_ctx[tool.name] = CTX_PARAM in inspect.signature(fn).parameters

    def get(self, name: str) -> protocols.ToolDefinition | None:
        """Return the tool called ``name``, if registered."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """Return registered tool names in registration order."""
        return list(self._tools)

    def schemas(self, visible: set[str] | None = None) -> list[ToolSchema]:
        """Return schemas in registration order; unknown ``visible`` names skip."""
        return [
            ToolSchema(
                name=t.name,
                description=t.description,
                parameters=t.args_model.model_json_schema(),
            )
            for t in self._tools.values()
            if visible is None or t.name in visible
        ]

    def validate(self, call: ToolCall) -> ValidatedCall | ErrorObservation:
        """Validate ``call.args``; unknown tools and bad args become observations."""
        found = self._tools.get(call.name)
        if found is None:
            return self._unknown(call.name)
        try:
            model = found.args_model.model_validate(call.args)
        except ValidationError as exc:
            return _validation_error(found, exc)
        return ValidatedCall(call=call, args=model.model_dump(mode="json"))

    async def invoke(
        self, vcall: ValidatedCall, ctx: protocols.RunContextView
    ) -> Observation:
        """Run the tool body under its timeout; failures become observations.

        ``vcall.args`` is re-validated through the args model so the body gets
        typed values (and so replayed or hook-modified args are still checked).
        Sync bodies run in a worker thread that is abandoned on timeout.
        """
        call_id = vcall.call.id
        found = self._tools.get(vcall.call.name)
        if found is None:
            return _failed(call_id, self._unknown(vcall.call.name))
        try:
            model = found.args_model.model_validate(vcall.args)
        except ValidationError as exc:
            return _failed(call_id, _validation_error(found, exc))
        kwargs = {key: getattr(model, key) for key in type(model).model_fields}
        if self._takes_ctx[found.name]:
            kwargs[CTX_PARAM] = ctx
        fn = self._fns[found.name]
        try:
            with anyio.move_on_after(found.timeout_s):
                if inspect.iscoroutinefunction(fn):
                    result = await fn(**kwargs)
                else:
                    result = await anyio.to_thread.run_sync(
                        partial(fn, **kwargs), abandon_on_cancel=True
                    )
                return _to_observation(call_id, result)
        except Exception as exc:
            return _failed(
                call_id,
                ErrorObservation(
                    type="tool_exception",
                    message=f"{type(exc).__name__}: {exc}",
                    hint="The tool failed; check the arguments or try another way.",
                ),
            )
        # Only reached when the timeout scope cancelled the body.
        return _failed(
            call_id,
            ErrorObservation(
                type="timeout",
                message=f"tool {found.name!r} timed out after {found.timeout_s}s",
                hint="Try a smaller input or a different approach.",
            ),
        )

    def _unknown(self, name: str) -> ErrorObservation:
        return ErrorObservation(
            type="unknown_tool",
            message=f"unknown tool {name!r}",
            hint=f"Available tools: {', '.join(self._tools) or '(none)'}.",
        )


def _validation_error(
    found: protocols.ToolDefinition, exc: ValidationError
) -> ErrorObservation:
    problems = "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '(args)'}: {err['msg']}"
        for err in exc.errors()
    )
    schema = found.args_model.model_json_schema()
    fields = ", ".join(schema.get("properties", {})) or "(none)"
    required = ", ".join(schema.get("required", [])) or "(none)"
    return ErrorObservation(
        type="validation",
        message=f"invalid arguments for {found.name!r}: {problems}",
        hint=(
            f"Call {found.name!r} again with valid arguments. "
            f"Fields: {fields}. Required: {required}."
        ),
    )


def _failed(call_id: str, error: ErrorObservation) -> Observation:
    return Observation(call_id=call_id, ok=False, content=error.message, error=error)


def _to_observation(call_id: str, result: Any) -> Observation:
    """Map a tool return value to an ``Observation``.

    ``Observation`` passes through (``call_id`` replaced), ``ToolResult`` maps
    ``content``/``final``, ``str`` and ``dict`` become content, other pydantic
    models are JSON-dumped to a dict, ``None`` is empty content and anything
    else is JSON-encoded to a string.
    """
    if isinstance(result, Observation):
        return result.model_copy(update={"call_id": call_id})
    if isinstance(result, ToolResult):
        content = result.content
        if isinstance(content, dict):
            content = to_jsonable_python(content)
        return Observation(
            call_id=call_id, ok=True, content=content, final=result.final
        )
    if result is None:
        return Observation(call_id=call_id, ok=True)
    if isinstance(result, str):
        return Observation(call_id=call_id, ok=True, content=result)
    if isinstance(result, BaseModel):
        return Observation(
            call_id=call_id, ok=True, content=result.model_dump(mode="json")
        )
    if isinstance(result, dict):
        return Observation(call_id=call_id, ok=True, content=to_jsonable_python(result))
    return Observation(
        call_id=call_id, ok=True, content=json.dumps(to_jsonable_python(result))
    )
