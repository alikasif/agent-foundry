"""T017: permission rules, defaults, narrowing and approvers (FR-008).

Docker-free and offline. Contracts: designs.md section 5 and the T005 section
of shared/implementation-notes.md.
"""

import sys
from dataclasses import FrozenInstanceError
from io import StringIO
from typing import Any

import pytest
from pydantic import ValidationError
from rich.console import Console

from harness_builder.core.protocols import Approver, PermissionPolicy
from harness_builder.core.types import (
    ApprovalRequest,
    PermissionDecision,
    PermissionValue,
    ToolCall,
)
from harness_builder.permissions.approvers import (
    AutoDenyApprover,
    CliApprover,
    StaticApprover,
    default_approver,
)
from harness_builder.permissions.policy import (
    STRICTNESS,
    NarrowedPolicy,
    Rule,
    RulePermissionPolicy,
)

DECISIONS: list[PermissionValue] = ["allow", "ask", "deny"]


def make_call(name: str = "run_code", **args: Any) -> ToolCall:
    """Return a ToolCall with a fixed id."""
    return ToolCall(id="c1", name=name, args=args)


class FixedPolicy:
    """Policy that always answers ``decision`` with ``rule_id``."""

    def __init__(self, decision: PermissionValue, rule_id: str) -> None:
        """Store the fixed answer."""
        self.decision: PermissionValue = decision
        self.rule_id = rule_id

    def decide(self, call: ToolCall) -> PermissionDecision:
        """Return the fixed decision."""
        return PermissionDecision(decision=self.decision, rule_id=self.rule_id)


# --- protocol conformance (checked by pyrefly) -------------------------------

_POLICY: PermissionPolicy = RulePermissionPolicy([])
_NARROWED: PermissionPolicy = NarrowedPolicy(_POLICY, _POLICY)
_APPROVERS: list[Approver] = [
    AutoDenyApprover(),
    StaticApprover(True),
    CliApprover(console=Console(file=StringIO())),
]


# --- RulePermissionPolicy -----------------------------------------------------


@pytest.mark.parametrize("decision", DECISIONS)
def test_rule_decision_is_returned(decision: PermissionValue) -> None:
    policy = RulePermissionPolicy([Rule("run_code", decision)])
    result = policy.decide(make_call())
    assert result.decision == decision
    assert result.rule_id == "rules[0]"
    assert result.reason is not None and "rules[0]" in result.reason


def test_rule_positional_construction_matches_designs_example() -> None:
    rule = Rule("run_code", "allow")
    assert rule.tool == "run_code"
    assert rule.decision == "allow"
    assert rule.args is None
    assert rule.rule_id is None


def test_unlisted_tool_is_denied_by_default() -> None:
    policy = RulePermissionPolicy([Rule("run_code", "allow")])
    result = policy.decide(make_call("write_file"))
    assert result.decision == "deny"
    assert result.rule_id is None
    assert result.reason is not None and "write_file" in result.reason


def test_empty_rule_list_denies_everything() -> None:
    assert RulePermissionPolicy([]).decide(make_call()).decision == "deny"


@pytest.mark.parametrize("default", DECISIONS)
def test_configured_default_applies_when_no_rule_matches(
    default: PermissionValue,
) -> None:
    policy = RulePermissionPolicy([Rule("other", "deny")], default=default)
    result = policy.decide(make_call())
    assert result.decision == default
    assert result.rule_id is None


def test_first_matching_rule_wins() -> None:
    policy = RulePermissionPolicy(
        [
            Rule("search", "allow"),
            Rule("run_*", "ask", rule_id="ask-run"),
            Rule("run_code", "allow", rule_id="allow-run"),
        ]
    )
    result = policy.decide(make_call("run_code"))
    assert result.decision == "ask"
    assert result.rule_id == "ask-run"


def test_rule_id_falls_back_to_index() -> None:
    policy = RulePermissionPolicy([Rule("a", "allow"), Rule("b", "deny")])
    assert policy.decide(make_call("b")).rule_id == "rules[1]"


def test_explicit_rule_id_is_reported() -> None:
    policy = RulePermissionPolicy([Rule("a", "allow", rule_id="my-rule")])
    assert policy.decide(make_call("a")).rule_id == "my-rule"


@pytest.mark.parametrize(
    "pattern,name,matches",
    [
        ("*", "anything", True),
        ("read_*", "read_file", True),
        ("read_*", "write_file", False),
        ("fs.?", "fs.r", True),
        ("run_code", "run_code", True),
        ("run_code", "Run_Code", False),
        ("RUN_*", "run_code", False),
    ],
)
def test_tool_glob_is_case_sensitive(pattern: str, name: str, matches: bool) -> None:
    policy = RulePermissionPolicy([Rule(pattern, "allow")])
    expected: PermissionValue = "allow" if matches else "deny"
    assert policy.decide(make_call(name)).decision == expected


@pytest.mark.parametrize(
    "args,matches",
    [
        ({"path": "staging/a.py"}, True),
        ({"path": "staging/sub/b.py"}, True),
        ({"path": "src/a.py"}, False),
        ({"path": "STAGING/a.py"}, False),
        ({"other": "staging/a.py"}, False),
        ({}, False),
    ],
)
def test_arg_glob_rule(args: dict[str, Any], matches: bool) -> None:
    policy = RulePermissionPolicy(
        [Rule("write_file", "allow", args={"path": "staging/*"})]
    )
    expected: PermissionValue = "allow" if matches else "deny"
    assert policy.decide(make_call("write_file", **args)).decision == expected


def test_every_listed_arg_must_match() -> None:
    rule = Rule("run_code", "allow", args={"language": "python", "code": "print*"})
    policy = RulePermissionPolicy([rule])
    ok = make_call(language="python", code="print(1)")
    wrong_lang = make_call(language="bash", code="print(1)")
    assert policy.decide(ok).decision == "allow"
    assert policy.decide(wrong_lang).decision == "deny"


def test_arg_rule_falls_through_to_next_rule() -> None:
    policy = RulePermissionPolicy(
        [
            Rule("write_file", "allow", args={"path": "staging/*"}),
            Rule("write_file", "ask"),
        ]
    )
    result = policy.decide(make_call("write_file", path="src/x.py"))
    assert result.decision == "ask"
    assert result.rule_id == "rules[1]"


@pytest.mark.parametrize(
    "value,pattern",
    [
        (3, "3"),
        (True, "true"),
        (None, "null"),
        ([1, 2], "[[]1, 2]"),
        ({"b": 1, "a": 2}, '{"a": 2, "b": 1}'),
    ],
)
def test_non_string_args_match_as_json_text(value: Any, pattern: str) -> None:
    policy = RulePermissionPolicy([Rule("t", "allow", args={"v": pattern})])
    assert policy.decide(make_call("t", v=value)).decision == "allow"


def test_glob_is_not_a_path_guard() -> None:
    """Documented limitation: ``*`` crosses ``/`` and ``..``."""
    policy = RulePermissionPolicy([Rule("w", "allow", args={"path": "staging/*"})])
    assert policy.decide(make_call("w", path="staging/../x")).decision == "allow"


def test_invalid_decision_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Rule("run_code", "maybe")  # pyrefly: ignore[bad-argument-type]


def test_rule_is_frozen() -> None:
    rule = Rule("a", "allow")
    with pytest.raises(FrozenInstanceError):
        rule.tool = "b"  # pyrefly: ignore[read-only]


# --- NarrowedPolicy -----------------------------------------------------------


@pytest.mark.parametrize("parent", DECISIONS)
@pytest.mark.parametrize("child", DECISIONS)
def test_narrowed_policy_returns_strictest(
    parent: PermissionValue, child: PermissionValue
) -> None:
    policy = NarrowedPolicy(FixedPolicy(parent, "p"), FixedPolicy(child, "c"))
    result = policy.decide(make_call())
    assert STRICTNESS[result.decision] == max(STRICTNESS[parent], STRICTNESS[child])
    assert STRICTNESS[result.decision] >= STRICTNESS[parent]
    expected_rule = "c" if STRICTNESS[child] > STRICTNESS[parent] else "p"
    assert result.rule_id == expected_rule


def test_strictness_order() -> None:
    assert STRICTNESS["deny"] > STRICTNESS["ask"] > STRICTNESS["allow"]


@pytest.mark.parametrize("decision", DECISIONS)
def test_narrowed_tie_keeps_parent_decision(decision: PermissionValue) -> None:
    parent = FixedPolicy(decision, "parent-rule")
    policy = NarrowedPolicy(parent, FixedPolicy(decision, "child-rule"))
    assert policy.decide(make_call()).rule_id == "parent-rule"


def test_narrowed_policy_nests() -> None:
    root = FixedPolicy("allow", "root")
    middle = NarrowedPolicy(root, FixedPolicy("ask", "mid"))
    leaf = NarrowedPolicy(middle, FixedPolicy("allow", "leaf"))
    result = leaf.decide(make_call())
    assert result.decision == "ask"
    assert result.rule_id == "mid"
    deepest = NarrowedPolicy(leaf, FixedPolicy("deny", "deep"))
    assert deepest.decide(make_call()).decision == "deny"


def test_child_cannot_loosen_rule_policies() -> None:
    parent = RulePermissionPolicy([Rule("run_code", "ask")])
    child = RulePermissionPolicy([Rule("*", "allow")])
    assert NarrowedPolicy(parent, child).decide(make_call()).decision == "ask"


def test_child_with_default_deny_denies_unlisted() -> None:
    parent = RulePermissionPolicy([Rule("*", "allow")])
    child = RulePermissionPolicy([Rule("search", "allow")])
    narrowed = NarrowedPolicy(parent, child)
    assert narrowed.decide(make_call("search")).decision == "allow"
    assert narrowed.decide(make_call("run_code")).decision == "deny"


# --- Approvers ----------------------------------------------------------------


@pytest.fixture
def approval_request() -> ApprovalRequest:
    """Return an approval request for a run_code call."""
    return ApprovalRequest(
        call=make_call(code="print('[bold]x[/bold]')"),
        rule_id="ask-run",
        reason="needs approval",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("answer", [True, False])
async def test_static_approver_returns_answer_and_records(
    answer: bool, approval_request: ApprovalRequest
) -> None:
    approver = StaticApprover(answer)
    assert await approver.approve(approval_request) is answer
    assert await approver.approve(approval_request) is answer
    assert approver.requests == [approval_request, approval_request]


@pytest.mark.anyio
async def test_auto_deny_approver_denies(approval_request: ApprovalRequest) -> None:
    assert await AutoDenyApprover().approve(approval_request) is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "typed,expected",
    [
        ("y\n", True),
        ("maybe\ny\n", True),  # rich re-prompts on an invalid answer
        ("yes\n", False),  # rich accepts only y/n; then EOF means no
        ("n\n", False),
        ("no\n", False),
        ("\n", False),
        ("", False),
    ],
)
async def test_cli_approver_reads_answer(
    typed: str, expected: bool, approval_request: ApprovalRequest
) -> None:
    out = StringIO()
    approver = CliApprover(console=Console(file=out), stream=StringIO(typed))
    assert await approver.approve(approval_request) is expected


@pytest.mark.anyio
async def test_cli_approver_prompt_shows_call_and_rule(
    approval_request: ApprovalRequest,
) -> None:
    out = StringIO()
    approver = CliApprover(console=Console(file=out), stream=StringIO("n\n"))
    await approver.approve(approval_request)
    shown = out.getvalue()
    assert "run_code" in shown
    assert "ask-run" in shown
    assert "needs approval" in shown
    assert "[bold]x[/bold]" in shown  # markup in args is not interpreted


@pytest.mark.anyio
async def test_cli_approver_treats_keyboard_interrupt_as_no(
    approval_request: ApprovalRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "harness_builder.permissions.approvers.Confirm.ask", interrupted
    )
    approver = CliApprover(console=Console(file=StringIO()))
    assert await approver.approve(approval_request) is False


class _Stdin:
    """Stand-in for ``sys.stdin`` with a fixed ``isatty`` answer."""

    def __init__(self, tty: bool) -> None:
        """Answer ``isatty`` with ``tty``."""
        self.tty = tty

    def isatty(self) -> bool:
        """Return the configured answer."""
        return self.tty


@pytest.mark.parametrize(
    "tty,expected", [(True, CliApprover), (False, AutoDenyApprover)]
)
def test_default_approver_depends_on_tty(
    tty: bool, expected: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(tty))
    assert isinstance(default_approver(), expected)


def test_default_approver_without_stdin_auto_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", None)
    assert isinstance(default_approver(), AutoDenyApprover)
