"""ContainerTool: a generated tool that runs only in Docker (designs.md 9.2, U5).

A generated tool module is untrusted code. The host never imports it: stage
B0 runs ``hb_entry.py schema <module> <func>`` in a container
(``extract_tool_schema``) and stores the result in ``tools.schema.json``
(``ToolSchemaFile``). At runtime a ``ContainerTool`` built from that entry
advertises the stored JSON schema, and each call runs ``hb_entry.py call``
in a fresh container with the harness directory mounted read-only at
``/tools`` and the args as JSON on stdin. The real pydantic model validates
the args inside the container, so invalid args come back as a ``validation``
error and never reach the body (FR-005).

The host-side ``args_model`` only shapes the call: one ``Any`` field per
schema property (required ones required, schema defaults filled), so the
registry passes every argument through; its JSON schema is the stored one.

``ContainerTool`` implements ``ExecutorTool``: ``HarnessBuilder.build()``
binds the harness executor and runs Docker preflight once. FINAL is never
read from a tool container (``final_set`` is ignored).

``host_tool`` is the trusted escape hatch (``runtime: host``, only with
``--trust-tools`` / ``trust_host_tools=True``): it loads the module
in-process with the same ``hb_entry`` functions the container uses.
"""

import copy
import json
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic.json_schema import (
    DEFAULT_REF_TEMPLATE,
    GenerateJsonSchema,
    JsonSchemaMode,
)

from harness_builder.core.protocols import Executor, RunContextView
from harness_builder.core.types import ErrorObservation, Observation, ToolRisk
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.image import hb_entry
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.tools.registry import Tool

TOOLS_MOUNT = "/tools"
"""Container path of the read-only harness directory."""
TOOLS_SCHEMA_FILE = "tools.schema.json"
DEFAULT_TIMEOUT_S = 30.0
"""Per-call container timeout (ASSUMED, same as ``SandboxConfig``)."""
STDERR_TAIL_CHARS = 800
RISK: frozenset[ToolRisk] = frozenset({"code_exec"})


class ToolSchemaEntry(BaseModel):
    """One generated tool as extracted by ``hb_entry.py schema``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    module: str
    function: str
    description: str = ""
    parameters: dict[str, Any]


class ToolSchemaFile(BaseModel):
    """``tools.schema.json``: entries keyed by ``schema_key(module, function)``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tools: dict[str, ToolSchemaEntry] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ToolSchemaFile":
        """Read and validate a ``tools.schema.json``; raises ``ConfigError``."""
        try:
            return cls.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ConfigError(f"{path}: invalid tool schema file: {exc}") from exc

    def entry(self, module: str, function: str) -> ToolSchemaEntry:
        """The entry for ``module:function``; raises ``ConfigError`` if absent."""
        found = self.tools.get(schema_key(module, function))
        if found is None:
            raise ConfigError(
                f"no schema for {schema_key(module, function)} in {TOOLS_SCHEMA_FILE};"
                " run `harness validate` to extract it"
            )
        return found


def schema_key(module: str, function: str) -> str:
    """Key of a tool in ``ToolSchemaFile.tools``: ``"tools/x.py:func"``."""
    return f"{module}:{function}"


def resolve_module(harness_dir: Path, module: str) -> Path:
    """The module file of ``module`` (``tools/x.py``) inside ``harness_dir``.

    ``module`` must be a relative POSIX path to a ``.py`` file without ``..``
    or backslashes, and must resolve (links followed) inside the resolved
    harness directory. Raises ``ConfigError``.
    """
    rel = PurePosixPath(module)
    if (
        "\\" in module
        or ":" in module
        or rel.is_absolute()
        or ".." in rel.parts
        or rel.suffix != ".py"
    ):
        raise ConfigError(f"tool module must be a relative .py path: {module!r}")
    root = harness_dir.resolve(strict=True)
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ConfigError(f"tool module {module!r} is not a file inside {root}")
    return path


async def extract_tool_schema(
    executor: Executor,
    *,
    harness_dir: Path,
    module: str,
    function: str,
    workdir: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ToolSchemaEntry:
    """Run ``hb_entry.py schema`` in the sandbox (stage B0) and parse it.

    Raises ``ConfigError`` when the module cannot be loaded or has no usable
    args model, and ``DockerUnavailableError`` from the executor.
    """
    harness_dir = harness_dir.resolve(strict=True)
    resolve_module(harness_dir, module)
    request = _tool_request(
        "schema", harness_dir, module, function, workdir, timeout_s, None
    )
    result = await executor.run(request)
    envelope = _envelope(result)
    if isinstance(envelope, str):
        raise ConfigError(f"schema extraction for {module}:{function}: {envelope}")
    if not envelope["ok"]:
        raise ConfigError(
            f"schema extraction for {module}:{function} failed: "
            f"{_error_message(envelope)}"
        )
    return ToolSchemaEntry(
        module=module,
        function=function,
        description=str(envelope.get("description") or ""),
        parameters=envelope.get("parameters") or {},
    )


class ContainerTool:
    """A generated tool executed per call in a fresh sandbox container.

    ``name`` defaults to the function name. ``timeout_s`` and ``network``
    go into every ``ExecRequest`` (explicit options, never read from the
    executor). The registry timeout is None: the executor kills and removes
    a container that runs too long.
    """

    timeout_s: float | None = None
    nondeterministic = True
    """Replay must use recorded output: running the tool needs Docker."""
    risk: frozenset[ToolRisk] = RISK

    def __init__(
        self,
        entry: ToolSchemaEntry,
        harness_dir: Path,
        *,
        name: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        network: bool = False,
        executor: Executor | None = None,
    ) -> None:
        """Check the module path and build the pass-through args model.

        Raises ``ConfigError`` for a module outside ``harness_dir``, an
        invalid function name or a non-positive timeout.
        """
        if not entry.function.isidentifier():
            raise ConfigError(f"invalid tool function name: {entry.function!r}")
        if not timeout_s > 0:
            raise ConfigError(f"timeout_s must be > 0, got {timeout_s}")
        self.harness_dir = harness_dir.resolve(strict=True)
        resolve_module(self.harness_dir, entry.module)
        self.entry = entry
        self.name = name or entry.function
        self.description = entry.description or f"Generated tool {self.name}."
        self.exec_timeout_s = timeout_s
        self.network = network
        self._executor = executor
        self.args_model, self._aliases, self._omit_if_none = _pass_through_model(
            self.name, entry.parameters
        )

    @property
    def executor(self) -> Executor | None:
        """The bound executor; None until ``HarnessBuilder.build()`` binds one."""
        return self._executor

    def with_executor(self, executor: Executor) -> "ContainerTool":
        """A copy with the same options bound to ``executor``."""
        return ContainerTool(
            self.entry,
            self.harness_dir,
            name=self.name,
            timeout_s=self.exec_timeout_s,
            network=self.network,
            executor=executor,
        )

    async def fn(self, ctx: RunContextView, **kwargs: Any) -> Observation:
        """Run the tool in a container; never raises executor or tool errors.

        ``kwargs`` are the host model's fields; they are sent under their
        schema names as JSON on stdin.
        """
        if self._executor is None or ctx.workdir is None:
            return _failed(
                _executor_error(
                    f"tool {self.name!r} has no executor or workdir; build the "
                    "harness with HarnessBuilder so one is bound"
                )
            )
        args = {
            self._aliases[key]: value
            for key, value in kwargs.items()
            if not (value is None and key in self._omit_if_none)
        }
        request = _tool_request(
            "call",
            self.harness_dir,
            self.entry.module,
            self.entry.function,
            ctx.workdir,
            self.exec_timeout_s,
            json.dumps(args),
            network=self.network,
            run_id=ctx.run_id,
        )
        try:
            result = await self._executor.run(request)
        except (ConfigError, DockerUnavailableError) as exc:
            return _failed(_executor_error(f"{type(exc).__name__}: {exc}"))
        return self.observation(result)

    def observation(self, result: ExecResult) -> Observation:
        """Map a ``call`` ``ExecResult`` to an ``Observation`` (FINAL ignored).

        Timeout, truncated output (never parsed as partial JSON), OOM and a
        missing or malformed envelope are errors; so is an error envelope
        (``validation`` keeps its type, everything else is
        ``tool_exception``).
        """
        result = result.model_copy(update={"final_set": False, "final_value": None})
        if result.timed_out:
            error = ErrorObservation(
                type="timeout",
                message=f"tool {self.name!r} timed out after {self.exec_timeout_s}s",
                hint="Try a smaller input or a different approach.",
            )
            return _failed(error, result)
        envelope = _envelope(result)
        if isinstance(envelope, str):
            return _failed(_tool_exception(f"tool {self.name!r}: {envelope}"), result)
        if envelope["ok"]:
            value = envelope.get("result")
            content = value if isinstance(value, str | dict) else json.dumps(value)
            return Observation(call_id="", ok=True, content=content, exec=result)
        if envelope["error"].get("type") == "validation":
            return _failed(self._validation_error(envelope), result)
        message = f"tool {self.name!r} failed: {_error_message(envelope)}"
        return _failed(_tool_exception(message), result)

    def _validation_error(self, envelope: dict[str, Any]) -> ErrorObservation:
        """Registry-style ``loc: msg; ...`` from the structured errors, falling
        back to the envelope message (pydantic's multi-line text)."""
        params = self.entry.parameters
        fields = ", ".join(params.get("properties", {})) or "(none)"
        required = ", ".join(params.get("required", [])) or "(none)"
        errors = envelope["error"].get("errors")
        problems = _error_message(envelope)
        if isinstance(errors, list) and errors:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', [])) or '(args)'}: "
                f"{e.get('msg', '')}"
                for e in errors
                if isinstance(e, dict)
            )
        return ErrorObservation(
            type="validation",
            message=f"invalid arguments for {self.name!r}: {problems}",
            hint=(
                f"Call {self.name!r} again with valid arguments. "
                f"Fields: {fields}. Required: {required}."
            ),
        )


def host_tool(
    harness_dir: Path,
    module: str,
    function: str,
    *,
    trusted: bool,
    name: str | None = None,
) -> Tool:
    """A ``runtime: host`` tool, loaded in-process (U5 ASSUMED).

    Only for hand-checked fixtures: ``trusted`` must be True (``--trust-tools``
    or ``trust_host_tools=True``), otherwise ``ConfigError``. It uses the same
    ``hb_entry`` loader and args-model convention as the container, so the
    schema matches ``hb_entry.py schema``. Load failures raise ``ConfigError``.
    """
    if not trusted:
        raise ConfigError(
            f"tool {module}:{function} has runtime 'host'; host tools run "
            "generated code outside Docker and need --trust-tools"
        )
    path = resolve_module(harness_dir, module)
    try:
        loaded = hb_entry.load_tool(path, function)
    except hb_entry.ToolLoadError as exc:
        raise ConfigError(f"{module}:{function}: {exc}") from exc
    tool_name = name or function

    def run(**kwargs: Any) -> Any:
        return hb_entry.invoke(loaded, loaded.args_model.model_construct(**kwargs))

    return Tool(
        name=tool_name,
        description=hb_entry.tool_schema(loaded)["description"]
        or f"Host tool {tool_name}.",
        args_model=loaded.args_model,
        fn=run,
        risk=RISK,
    )


class _StoredSchemaModel(BaseModel):
    """Base of the host args models: the JSON schema is the stored one."""

    stored_schema: ClassVar[dict[str, Any]] = {}

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool | None = None,
        ref_template: str = DEFAULT_REF_TEMPLATE,
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        """A deep copy of the extracted schema (``$defs`` and refs kept)."""
        return copy.deepcopy(cls.stored_schema)


class _StoredSchemaForbidExtra(_StoredSchemaModel):
    """Host args base for schemas with ``additionalProperties: false``."""

    model_config = ConfigDict(extra="forbid")


def _pass_through_model(
    name: str, parameters: dict[str, Any]
) -> tuple[type[BaseModel], dict[str, str], frozenset[str]]:
    """``(host args model, field -> schema name, fields omitted when None)``.

    Fields are named ``f0, f1, ...`` with the schema names as aliases, so any
    property name works (even ``ctx`` or ``model_config``). Optional
    properties without a schema default get None and are left out of the
    payload when still None, so the container applies its own default.
    The model's JSON schema is ``parameters``, verbatim.
    """
    properties: dict[str, Any] = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])
    fields: dict[str, Any] = {}
    aliases: dict[str, str] = {}
    omit: set[str] = set()
    for index, (prop, sub) in enumerate(properties.items()):
        field = f"f{index}"
        aliases[field] = prop
        if prop in required:
            default: Any = ...
        elif isinstance(sub, dict) and "default" in sub:
            default = sub["default"]
        else:
            default = None
            omit.add(field)
        fields[field] = (Any, Field(default, alias=prop))
    forbid = parameters.get("additionalProperties") is False
    model_name = "".join(part.title() for part in name.split("_")) + "HostArgs"
    model = create_model(
        model_name,
        __base__=_StoredSchemaForbidExtra if forbid else _StoredSchemaModel,
        **fields,
    )
    model.stored_schema = copy.deepcopy(parameters)
    return model, aliases, frozenset(omit)


def _tool_request(
    entry: Literal["schema", "call"],
    harness_dir: Path,
    module: str,
    function: str,
    workdir: Path,
    timeout_s: float,
    stdin: str | None,
    *,
    network: bool = False,
    run_id: str | None = None,
) -> ExecRequest:
    container_module = f"{TOOLS_MOUNT}/{PurePosixPath(module)}"
    return ExecRequest(
        language="python",
        code="",
        workdir=workdir,
        timeout_s=timeout_s,
        network=network,
        ro_mounts={harness_dir: TOOLS_MOUNT},
        stdin=stdin,
        run_id=run_id,
        entry=entry,
        entry_args=(container_module, function),
    )


def _envelope(result: ExecResult) -> dict[str, Any] | str:
    """The parsed hb_entry envelope, or a description of why there is none."""
    if result.truncated:
        return "output exceeded the sandbox output cap; the result was discarded"
    if result.oom_killed:
        return "the container ran out of memory"
    if result.timed_out:
        return "the container timed out"
    try:
        value: object = json.loads(result.stdout)
    except ValueError:
        value = None
    if isinstance(value, dict):
        ok = value.get("ok")
        if ok is True or (ok is False and isinstance(value.get("error"), dict)):
            return value
    tail = result.stderr[-STDERR_TAIL_CHARS:].strip() or "no stderr"
    return f"no result from the tool container (exit {result.exit_code}): {tail}"


def _error_message(envelope: dict[str, Any]) -> str:
    error = envelope["error"]
    return str(error.get("message") or error.get("type") or "unknown error")


def _tool_exception(message: str) -> ErrorObservation:
    return ErrorObservation(
        type="tool_exception",
        message=message,
        hint="The tool failed; check the arguments or try another way.",
    )


def _executor_error(message: str) -> ErrorObservation:
    return ErrorObservation(
        type="executor_error",
        message=message,
        hint="The sandbox is unavailable; run `harness doctor` or answer without "
        "this tool.",
    )


def _failed(error: ErrorObservation, result: ExecResult | None = None) -> Observation:
    return Observation(
        call_id="", ok=False, content=error.message, error=error, exec=result
    )
