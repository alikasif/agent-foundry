"""HarnessSpec v1, M1 fields (designs.md 4.4; FR-016, FR-030).

Leaf module: imports stdlib, pydantic and the core leaves only, so permission
rules and sub-agent profiles are mirrored here (``RuleSpec``,
``ChildProfileSpec``) and converted by ``spec.loader``. Every model forbids
unknown keys, so a typo is reported as a located error. T040 (M3) adds the
remaining fields (module tools and hooks, sandbox, skills, evals); the schema
version stays 1.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class HarnessSpec(_Spec):
    """A declarative harness (HarnessSpec v1, M1 fields).

    Absent optional sections keep the ``HarnessBuilder`` defaults: the model
    from ``HARNESS_MODEL``, no context policy, deny-unlisted permissions,
    ``BudgetLimits()``, no sub-agents.
    """

    schema_version: Literal[1]
    name: str = Field(min_length=1)
    description: str = ""
    model: ModelConfig | None = None
    strategy: StrategySpec = Field(default_factory=StrategySpec)
    prompts: PromptsSpec = Field(default_factory=PromptsSpec)
    tools: list[BuiltinToolRef] = Field(default_factory=list)
    context: ContextSpec | None = None
    permissions: PermissionsSpec = Field(default_factory=PermissionsSpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    hooks: list[BuiltinHookRef] = Field(default_factory=list)
    subagents: SubagentsSpec | None = None
