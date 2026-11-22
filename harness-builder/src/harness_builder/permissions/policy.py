"""Rule-based permission policies (designs.md section 5, FR-008).

``RulePermissionPolicy`` returns the decision of the first rule matching a call
and falls back to ``default`` (deny, ASSUMED OQ-22). ``NarrowedPolicy`` combines
a parent and a child policy so that a sub-agent can only be stricter than its
parent: deny > ask > allow.

Globs use ``fnmatch.fnmatchcase`` (case-sensitive on every platform). ``*``
also matches ``/`` and ``..``, so an arg glob is not a path guard. ``Rule``
is defined in the ``core.types`` leaf (T024 B1) and re-exported here.
"""

from collections.abc import Sequence

from harness_builder.core.protocols import PermissionPolicy
from harness_builder.core.types import (
    PermissionDecision,
    PermissionValue,
    Rule,
    ToolCall,
)

STRICTNESS: dict[PermissionValue, int] = {"allow": 0, "ask": 1, "deny": 2}
"""Rank of a decision; ``NarrowedPolicy`` keeps the higher one."""


class RulePermissionPolicy:
    """First-match rule list with a default for unlisted calls."""

    def __init__(
        self, rules: Sequence[Rule], default: PermissionValue = "deny"
    ) -> None:
        """Store ``rules`` in order; ``default`` applies when none matches."""
        self.rules: tuple[Rule, ...] = tuple(rules)
        self.default: PermissionValue = default

    def decide(self, call: ToolCall) -> PermissionDecision:
        """Return the first matching rule's decision, else the default."""
        for index, rule in enumerate(self.rules):
            if not rule.matches(call):
                continue
            rule_id = rule.rule_id or f"rules[{index}]"
            return PermissionDecision(
                decision=rule.decision,
                rule_id=rule_id,
                reason=f"matched {rule_id} ({rule.tool!r} -> {rule.decision})",
            )
        return PermissionDecision(
            decision=self.default,
            reason=f"no rule matched {call.name!r}; default {self.default}",
        )


class NarrowedPolicy:
    """Strictest of a parent and a child policy (deny > ask > allow).

    The result is never looser than the parent; ties keep the parent's
    decision. Policies nest, so a grandchild can narrow a ``NarrowedPolicy``.
    """

    def __init__(self, parent: PermissionPolicy, child: PermissionPolicy) -> None:
        """Combine ``parent`` with the narrowing ``child`` policy."""
        self.parent = parent
        self.child = child

    def decide(self, call: ToolCall) -> PermissionDecision:
        """Return whichever of the two decisions is stricter."""
        parent = self.parent.decide(call)
        child = self.child.decide(call)
        if STRICTNESS[child.decision] > STRICTNESS[parent.decision]:
            return child
        return parent
