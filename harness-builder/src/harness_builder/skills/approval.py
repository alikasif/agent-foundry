"""The mandatory approval gate and atomic promotion (FR-055, D3).

``ApprovalGate.review(cid)`` validates the candidate, renders the review
(description, SKILL.md, script, test output, dedup and safety report) with
rich, asks a ``PromotionApprover`` and, only on an explicit yes, promotes.

Fail-closed rules:

- an invalid candidate is never promoted, whatever the approver says;
- no approver means ``AutoDenyPromotionApprover``; an approver that raises or
  returns anything but ``True`` is a no;
- a name collision with a live skill is refused unless ``replace=True``;
- auto-approve is refused at construction unless the skills root holds the
  ``.hb-sandbox-library`` marker file (OQ-10), and even then it refuses a
  candidate with any safety finding or a dedup match;
- the staged copy must hash to the validated digest, so what is promoted is
  exactly what was reviewed.

Promotion copies ``pending/<cid>/<name>`` to ``.agents/.tmp-<cid>`` and
renames it to ``.agents/skills/<name>`` with ``os.replace`` (same volume, so
atomic). The staging dir is outside ``.agents/skills`` because the catalog
scan does not skip dot-directories: a leftover ``skills/.tmp-<cid>`` after a
crash would be discovered as a live skill. Replace first renames the old
skill to ``skills-trash/<name>-<ts>`` and moves it back if the final rename
fails.
"""

import logging
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import anyio.to_thread
from rich.console import Console
from rich.prompt import Confirm
from rich.rule import Rule
from rich.text import Text

from harness_builder.errors import ConfigError
from harness_builder.skills.store import SkillStore, replace_path
from harness_builder.skills.validation import (
    CandidateValidator,
    ValidationReport,
    tree_digest,
)

logger = logging.getLogger(__name__)

SANDBOX_MARKER = ".hb-sandbox-library"
"""File in the skills root that allows ``auto_approve`` (eval libraries)."""

Decision = Literal["promoted", "denied", "invalid"]


@dataclass(frozen=True)
class CandidateReview:
    """What the approver decides on: the report plus the requested action."""

    report: ValidationReport
    replace: bool
    existing: Path | None
    """The live skill that would be replaced, if any."""


@dataclass(frozen=True)
class ReviewOutcome:
    """Result of one ``ApprovalGate.review`` call."""

    cid: str
    name: str | None
    decision: Decision
    reasons: tuple[str, ...]
    report: ValidationReport
    live_path: Path | None = None
    trashed: Path | None = None

    @property
    def promoted(self) -> bool:
        """True when the candidate is now a live skill."""
        return self.decision == "promoted"


class PromotionApprover(Protocol):
    """Decides on one candidate; only ``True`` approves."""

    async def approve(self, review: CandidateReview) -> bool:
        """Return True to promote ``review.report``'s candidate."""
        ...


class AutoDenyPromotionApprover:
    """Always says no; the default for non-interactive use."""

    async def approve(self, review: CandidateReview) -> bool:
        """Deny."""
        return False


class StaticPromotionApprover:
    """Returns a fixed answer and records every review (tests)."""

    def __init__(self, answer: bool) -> None:
        """Answer every review with ``answer``."""
        self.answer = answer
        self.reviews: list[CandidateReview] = []

    async def approve(self, review: CandidateReview) -> bool:
        """Record ``review`` and return the fixed answer."""
        self.reviews.append(review)
        return self.answer


class CliPromotionApprover:
    """Asks on the terminal (rich ``Confirm``, default no).

    The gate has already printed the review; this only asks. EOF and
    Ctrl-C mean no.
    """

    def __init__(self, console: Console | None = None) -> None:
        """Ask on ``console`` (rich's default console when None)."""
        self.console = console

    async def approve(self, review: CandidateReview) -> bool:
        """Ask the user in a worker thread (``Confirm.ask`` blocks)."""
        return await anyio.to_thread.run_sync(self._ask, review)

    def _ask(self, review: CandidateReview) -> bool:
        action = "Replace" if review.existing is not None else "Promote"
        name = review.report.name or "?"
        try:
            return Confirm.ask(
                Text(f"{action} skill {name!r} into the live library?"),
                console=self.console,
                default=False,
            )
        except (EOFError, KeyboardInterrupt):
            return False


class ApprovalGate:
    """Validates, shows and (only when approved) atomically promotes candidates."""

    def __init__(
        self,
        store: SkillStore,
        validator: CandidateValidator,
        approver: PromotionApprover | None = None,
        *,
        console: Console | None = None,
        auto_approve: bool = False,
    ) -> None:
        """Raise ``ConfigError`` for ``auto_approve`` without the sandbox marker."""
        if auto_approve and not (store.root / SANDBOX_MARKER).is_file():
            raise ConfigError(
                f"--auto-approve refused: {store.root} has no {SANDBOX_MARKER} "
                "marker (auto-approve is only for sandboxed eval libraries)"
            )
        self.store = store
        self.validator = validator
        self.approver = approver or AutoDenyPromotionApprover()
        self.console = console or Console()
        self.auto_approve = auto_approve

    async def review(self, cid: str, *, replace: bool = False) -> ReviewOutcome:
        """Validate ``cid``, show it, ask, and promote on approval."""
        report = await self.validator.validate(cid)
        name = report.name
        if not report.ok or name is None:
            render_review(self.console, report, replace=replace, existing=None)
            return ReviewOutcome(cid, name, "invalid", report.reasons, report)
        existing = self.store.skill_dir(name)
        exists = existing.exists()
        render_review(
            self.console, report, replace=replace, existing=existing if exists else None
        )
        if exists and not replace:
            reason = f"a live skill named {name!r} exists; review with replace"
            return ReviewOutcome(cid, name, "invalid", (reason,), report)
        review = CandidateReview(report, replace, existing if exists else None)
        if not await self._approved(review):
            return ReviewOutcome(cid, name, "denied", ("not approved",), report)
        try:
            live, trashed = self._promote(report, replace=replace)
        except (OSError, ValueError, ConfigError) as exc:
            reason = f"promotion failed: {type(exc).__name__}: {exc}"
            return ReviewOutcome(cid, name, "invalid", (reason,), report)
        return ReviewOutcome(cid, name, "promoted", (), report, live, trashed)

    async def _approved(self, review: CandidateReview) -> bool:
        if self.auto_approve:
            return not (review.report.flagged or review.report.dedup)
        try:
            answer = await self.approver.approve(review)
        except Exception:  # fail closed: a broken approver is a no
            logger.exception("promotion approver failed; treating as deny")
            return False
        return answer is True

    def _promote(
        self, report: ValidationReport, *, replace: bool = False
    ) -> tuple[Path, Path | None]:
        """Atomically move the validated candidate into the live library.

        Returns ``(live dir, trashed old dir or None)``. Raises
        ``ValueError`` when the candidate changed since validation (digest
        mismatch) and ``ConfigError`` on a collision without ``replace``.
        The pending candidate is removed after a successful promotion.
        """
        name = report.name
        if not report.ok or name is None or report.digest is None:
            raise ValueError("only a valid report can be promoted")
        source = self.store.candidate_dir(report.cid) / name
        target = self.store.skill_dir(name)
        staging = self.store.agents_dir / f".tmp-{report.cid}"
        self.store.ensure_dirs()
        shutil.rmtree(staging, ignore_errors=True)
        try:
            shutil.copytree(source, staging, symlinks=True)
            if tree_digest(staging) != report.digest:
                raise ValueError("candidate changed since it was validated")
            trashed = self._swap(staging, target, name, replace=replace)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(self.store.candidate_dir(report.cid), ignore_errors=True)
        return target, trashed

    def _swap(
        self, staging: Path, target: Path, name: str, *, replace: bool
    ) -> Path | None:
        if not target.exists():
            replace_path(staging, target)
            return None
        if not replace:
            raise ConfigError(f"a live skill named {name!r} exists")
        trashed = self.store.trash_dir(name)
        replace_path(target, trashed)
        try:
            replace_path(staging, target)
        except BaseException:
            try:
                replace_path(trashed, target)
            except OSError:
                logger.exception(
                    "rollback of %s failed; the old skill is in %s", target, trashed
                )
            raise
        return trashed


def render_review(
    console: Console,
    report: ValidationReport,
    *,
    replace: bool,
    existing: Path | None,
) -> None:
    """Print the approval review. Untrusted text is never parsed as markup."""
    title = f"Skill candidate {report.cid}: {report.name or '?'}"
    console.print(Rule(Text(printable(title))))
    if existing is not None:
        mode = "REPLACE existing skill" if replace else "NAME COLLISION"
        console.print(Text(f"{mode}: {existing}", style="bold yellow"))
    status = "valid" if report.ok else "INVALID"
    console.print(Text(f"Validation: {status}", style="bold"))
    for reason in report.reasons:
        console.print(Text(f"  - {printable(reason)}", style="red"))
    _section(console, "Description", report.description)
    _section(console, "SKILL.md", report.skill_md)
    _section(console, "Script", report.script)
    _section(console, "Test", report.test_source)
    if report.test is not None:
        test = report.test
        verdict = "PASSED" if test.passed else "FAILED"
        _section(
            console,
            f"Test run: {verdict} (exit {test.exit_code})",
            f"{test.stdout}\n{test.stderr}".strip(),
        )
    dedup = [
        f"{m.name}: {m.reason} similarity {m.similarity:.2f} -> {m.proposal}"
        for m in report.dedup
    ]
    _section(console, "Dedup", "\n".join(dedup) or "no similar live skill")
    findings = [
        f"{label} line {f.line}: {f.category}: {f.detail}"
        for label, screen in zip(("script", "test"), report.safety, strict=False)
        for f in screen.findings
    ]
    _section(console, "Safety screen", "\n".join(findings) or "no findings")


def _section(console: Console, title: str, body: str) -> None:
    console.print(Text(printable(title), style="bold cyan"))
    console.print(Text(printable(body) or "(empty)"))


def printable(text: str) -> str:
    """Escape control and format characters (ANSI escapes, bidi overrides).

    Newlines and tabs are kept. This stops candidate content from hiding
    lines from the reviewer in a terminal.
    """
    out: list[str] = []
    for char in text:
        if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf"):
            out.append(char)
        else:
            out.append(f"\\u{ord(char):04x}")
    return "".join(out)
