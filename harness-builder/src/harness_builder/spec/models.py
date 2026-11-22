"""HarnessSpec v1 (designs.md 4.4; FR-016, FR-030).

Leaf module: imports stdlib, pydantic and the core leaves only, so permission
rules, sub-agent profiles and sandbox limits are mirrored here (``RuleSpec``,
``ChildProfileSpec``, ``SandboxSpec``) and converted by ``spec.loader``; drift
tests pin each mirror to its runtime model (T024 MJ2). Every model forbids
unknown keys, so a typo is reported as a located error. M1 specs load
unchanged: every T040 field is optional and the schema version stays 1.

File references (``prompts.system.file``, ``tools[i].module``,
``hooks[i].module``, ``evals.file``, ``skills.root``) are checked lexically
here and resolved inside the harness directory by ``spec.loader``.
"""

from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
)

from harness_builder.core.types import BudgetLimits, HookEvent, ModelConfig

SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (1,)
SPEC_FILE_NAMES: tuple[str, ...] = ("harness.yaml", "harness.yml", "harness.json")
"""File names looked up, in order, when a spec directory is given."""

ContextPolicyName = Literal["truncate", "summarize", "by_reference"]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategySpec(_Spec):
    """A registered strategy name with its constructor options."""

    name: str = "react"
    options: dict[str, Any] = Field(default_factory=dict)


class PromptFile(_Spec):
    """A prompt read from a file relative to the spec directory."""

    file: str


class PromptsSpec(_Spec):
    """Prompts; ``system`` is inline text or ``{file: "prompts/system.md"}``."""

    system: str | PromptFile = ""


class BuiltinToolRef(_Spec):
    """A built-in tool by name, built with ``options`` as keyword arguments."""

    builtin: str
    options: dict[str, Any] = Field(default_factory=dict)


def check_module_path(value: str) -> str:
    """A relative POSIX path to a ``.py`` file without ``..``, ``\\`` or ``:``."""
    rel = PurePosixPath(value)
    if (
        "\\" in value
        or ":" in value
        or rel.is_absolute()
        or ".." in rel.parts
        or rel.suffix != ".py"
    ):
        raise ValueError(
            "must be a relative .py path inside the harness directory "
            "(no absolute path, drive, '..' or backslash)"
        )
    return value


class ModuleToolRef(_Spec):
    """A tool function in a harness module (designs.md 9.2, U5).

    ``runtime: docker`` (default) runs every call in a sandbox container
    with the schema from ``tools.schema.json``; ``runtime: host`` loads the
    module in-process and needs ``trust_host_tools`` / ``--trust-tools``.
    ``examples`` are argument sets; validation smoke-runs the first one.
    """

    module: str
    function: str
    examples: list[dict[str, Any]] = Field(default_factory=list)
    runtime: Literal["docker", "host"] = "docker"

    @field_validator("module")
    @classmethod
    def _module_path(cls, value: str) -> str:
        return check_module_path(value)

    @field_validator("function")
    @classmethod
    def _identifier(cls, value: str) -> str:
        return check_identifier(value)


def check_identifier(value: str) -> str:
    """A Python identifier (a function name in a harness module)."""
    if not value.isidentifier():
        raise ValueError("must be a Python identifier")
    return value


def _ref_kind(value: Any) -> str:
    """Builtin vs module ref, by key; tags are not data keys (located paths)."""
    if isinstance(value, dict):
        is_module = "module" in value and "builtin" not in value
        return "module_ref" if is_module else "builtin_ref"
    is_module = isinstance(value, ModuleToolRef | ModuleHookRef)
    return "module_ref" if is_module else "builtin_ref"


ToolRef = Annotated[
    Annotated[BuiltinToolRef, Tag("builtin_ref")]
    | Annotated[ModuleToolRef, Tag("module_ref")],
    Discriminator(_ref_kind),
]


class ContextSpec(_Spec):
    """A named context policy; unset options keep the policy's own defaults.

    ``ref_threshold_chars`` and ``preview_chars`` apply to ``by_reference``
    only, ``model`` (summarizer model name) to ``summarize`` only.
    """

    policy: ContextPolicyName
    max_tokens: int = Field(ge=1)
    keep_last: int | None = Field(default=None, ge=0)
    ref_threshold_chars: int | None = Field(default=None, ge=1)
    preview_chars: int | None = Field(default=None, ge=0)
    model: str | None = None


class RuleSpec(_Spec):
    """One first-match permission rule (``permissions.policy.Rule``)."""

    tool: str
    decision: Literal["allow", "deny", "ask"]
    args: dict[str, str] | None = None
    rule_id: str | None = None


class PermissionsSpec(_Spec):
    """Rules and the decision for unlisted calls (deny-unlisted, OQ-22)."""

    default: Literal["allow", "deny", "ask"] = "deny"
    rules: list[RuleSpec] = Field(default_factory=list)


class BudgetSpec(_Spec):
    """Run limits (``core.types.BudgetLimits``) with spec-level bounds.

    ``max_steps`` must be at least 1 (a spec with 0 or less would stop
    before any step); unset limits are unlimited.
    """

    max_steps: int = Field(default=BudgetLimits().max_steps, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0)


class BuiltinHookRef(_Spec):
    """A built-in hook by name, registered for ``event`` at ``priority``."""

    builtin: str
    event: HookEvent
    priority: int = 0
    options: dict[str, Any] = Field(default_factory=dict)


class ModuleHookRef(_Spec):
    """A hook function in a harness module, registered for ``event``.

    Hooks are host callables (there is no container mode), so a module hook
    loads only with ``trust_host_tools`` / ``--trust-tools``.
    """

    module: str
    function: str
    event: HookEvent
    priority: int = 0

    @field_validator("module")
    @classmethod
    def _module_path(cls, value: str) -> str:
        return check_module_path(value)

    @field_validator("function")
    @classmethod
    def _identifier(cls, value: str) -> str:
        return check_identifier(value)


HookRef = Annotated[
    Annotated[BuiltinHookRef, Tag("builtin_ref")]
    | Annotated[ModuleHookRef, Tag("module_ref")],
    Discriminator(_ref_kind),
]


class ChildProfileSpec(_Spec):
    """A sub-agent profile (``core.builder.ChildProfile``); unset inherits."""

    description: str = ""
    model: str | None = None
    strategy: str | None = None
    strategy_options: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str | None = None
    tools: list[str] | None = None
    budget_fraction: float = Field(default=0.5, gt=0, le=1)
    permissions: list[RuleSpec] = Field(default_factory=list)


class SubagentsSpec(_Spec):
    """``spawn_agent`` settings; no profiles means one inheriting default."""

    max_depth: int = Field(default=2, ge=1)
    profiles: dict[str, ChildProfileSpec] = Field(default_factory=dict)


class SandboxSpec(_Spec):
    """Sandbox limits (``executor.models.SandboxConfig``) as a spec may set them.

    No ``image``: the image is an environment choice (``HARNESS_DOCKER_IMAGE``
    or the content-hash tag), never a harness one. ``state_mode`` accepts only
    ``stateless`` (T036 C4: ``session`` is not implemented). Upper bounds are
    ASSUMED sanity caps so a downloaded spec cannot ask for unbounded limits.
    """

    timeout_s: float = Field(default=30.0, gt=0, le=600, allow_inf_nan=False)
    output_cap_bytes: int = Field(default=65536, gt=0, le=16 * 1024 * 1024)
    memory_mb: int = Field(default=512, gt=0, le=16384)
    cpus: float = Field(default=1.0, gt=0, le=16, allow_inf_nan=False)
    pids: int = Field(default=128, gt=0, le=4096)
    network: bool = False
    state_mode: Literal["stateless"] = "stateless"


class PromotionSpec(_Spec):
    """Skill promotion of a harness (FR-053, D3).

    ``deferred`` writes ``skills-pending/<cid>/source.json`` after a verified
    successful run, and ``harness skills propose`` generates the candidate
    later, so a run never waits on the extra LLM call. ``on_stop`` (generate
    at the end of the run) needs an LLM client owned by the caller, so it is
    available through the builder API only and is not accepted in a spec.
    """

    mode: Literal["deferred"] = "deferred"


class SkillsConfig(_Spec):
    """The skills extension of a harness (designs.md 10, FR-050..052).

    ``root`` is the skills root (``<root>/.agents/skills``), relative to the
    harness directory and confined to it; None resolves it at run time
    (``--skills-root``, ``HARNESS_SKILLS_ROOT``, nearest ``pyproject.toml``
    above the CWD; U4). ``top_k`` entries are retrieved per run. Bundled
    skills are never included (designs.md 10.1), so ``include_bundled`` must
    stay false. ``enabled: false`` keeps the section but installs nothing.
    ``promotion`` (default None: no promotion) enables deferred candidate
    capture after verified successful runs.
    """

    enabled: bool = True
    root: str | None = None
    top_k: int = Field(default=3, ge=1, le=50)
    include_user_skills: bool = False
    include_bundled: bool = False
    promotion: PromotionSpec | None = None

    @field_validator("include_bundled")
    @classmethod
    def _no_bundled(cls, value: bool) -> bool:
        if value:
            raise ValueError("bundled skills are never included (designs.md 10.1)")
        return value


class EvalsFile(_Spec):
    """Eval cases in a YAML/JSON file inside the harness directory."""

    file: str


class HarnessSpec(_Spec):
    """A declarative harness (HarnessSpec v1).

    Absent optional sections keep the ``HarnessBuilder`` defaults: the model
    from ``HARNESS_MODEL``, no context policy, deny-unlisted permissions,
    ``BudgetLimits()``, no sub-agents, the default sandbox, no skills, no
    evals. Inline ``evals`` cases are validated by the loader against
    ``evals.models.EvalCase`` (that module is not a leaf).
    """

    schema_version: Literal[1]
    name: str = Field(min_length=1)
    description: str = ""
    model: ModelConfig | None = None
    strategy: StrategySpec = Field(default_factory=StrategySpec)
    prompts: PromptsSpec = Field(default_factory=PromptsSpec)
    tools: list[ToolRef] = Field(default_factory=list)
    context: ContextSpec | None = None
    permissions: PermissionsSpec = Field(default_factory=PermissionsSpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    hooks: list[HookRef] = Field(default_factory=list)
    subagents: SubagentsSpec | None = None
    sandbox: SandboxSpec | None = None
    skills: SkillsConfig | None = None
    evals: EvalsFile | list[dict[str, Any]] | None = None
