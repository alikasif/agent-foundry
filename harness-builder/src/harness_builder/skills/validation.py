"""Candidate validation before the approval gate (FR-054).

``CandidateValidator.validate(cid)`` checks one pending candidate and returns
a ``ValidationReport``; it never raises for a bad candidate. Every failed
check adds a reason, and ``ok`` is True only with no reasons. Order:

1. layout and confinement: ``candidate.json`` names one skill dir holding
   exactly ``SKILL.md``, ``scripts/<name>.py`` and ``tests/test_<name>.py``;
   no symlink or junction, nothing resolving outside the candidate dir;
2. a throwaway ``ScopedSkillManager([pending/<cid>])`` discovers exactly this
   skill and records zero warnings;
3. frontmatter as the catalog parses it: ``name == dir``, kebab-case,
   description of 1-200 characters (OQ-18), no ``---`` in values;
4. ``ast.parse`` of the script and the test;
5. ``safety.screen`` of script and test: a ``secret`` finding rejects, other
   findings are shown to the approver (and block auto-approve);
6. dedup against the live library (informational, proposes an action);
7. the test, run in the sandbox ``Executor`` (Docker) only when 1-5 passed.

Steps 1-5 run before the test (designs.md lists the Docker test third) so a
malformed or unsafe candidate never costs a container run.
"""

import ast
import hashlib
import json
import os
import re
import stat
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml  # pyrefly: ignore[untyped-import]

from harness_builder.core.protocols import Executor
from harness_builder.core.types import SkillEntry
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.safety.screen import ScreenReport, screen
from harness_builder.skills.promotion import (
    CANDIDATE_FILE,
    SCRIPT_ALIAS,
    CandidateMeta,
)
from harness_builder.skills.retriever import LexicalRetriever
from harness_builder.skills.store import SKILL_NAME_RE, SkillStore

MAX_DESCRIPTION = 200
"""Frontmatter description limit (OQ-18)."""
DEDUP_THRESHOLD = 0.7
"""Description similarity that counts as a near-duplicate. designs.md says
0.8 (ASSUMED, OQ-12); T050 measured one substituted word in a short
description at 0.77, so 0.7 plus an exact-name check is used."""
SKIP_THRESHOLD = 0.9
"""At or above this similarity the proposal is ``skip`` instead of ``merge``."""
TEST_TIMEOUT_S = 60.0
OUTPUT_PREVIEW = 4000
SKILL_MOUNT = "/skill"
REJECTING_CATEGORIES = frozenset({"secret"})

DedupAction = Literal["merge", "skip", "replace"]

RUNNER = """\
import importlib.util
import sys
import traceback

sys.dont_write_bytecode = True


def load(alias, path):
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


try:
    load({alias!r}, {script!r})
    tests = load("skill_test", {test!r})
except BaseException:  # SystemExit at import time must not look like a pass
    traceback.print_exc()
    print("HB-SKILL-TESTS load-error")
    sys.exit(1)
ran = failed = 0
for name in sorted(vars(tests)):
    fn = getattr(tests, name)
    if not name.startswith("test_") or not callable(fn):
        continue
    ran += 1
    try:
        fn()
        print("PASS", name)
    except BaseException:
        failed += 1
        print("FAIL", name)
        traceback.print_exc()
sys.stdout.flush()
print(f"HB-SKILL-TESTS ran={{ran}} failed={{failed}}", flush=True)
sys.exit(1 if failed or not ran else 0)
"""
"""Stdlib test runner (the sandbox image has no pytest).

A pass needs exit code 0 *and* the sentinel as the last stdout line, so a
``sys.exit(0)`` or ``os._exit(0)`` before the runner prints it is a failure.
A forged pass (print the sentinel, then ``os._exit(0)``) is not detected: the
sandbox is the boundary and the approver reads the code, which the safety
screen flags (``os._exit`` is not a screen finding, so review the script)."""
PASS_SENTINEL = re.compile(r"HB-SKILL-TESTS ran=[1-9][0-9]* failed=0")


@dataclass(frozen=True)
class DedupMatch:
    """A live skill the candidate may duplicate, with a proposed action."""

    name: str
    similarity: float
    reason: Literal["name", "description"]
    proposal: DedupAction


@dataclass(frozen=True)
class TestRun:
    """Outcome of the candidate's test in the sandbox."""

    __test__ = False  # not a pytest class

    passed: bool
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    oom_killed: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class ValidationReport:
    """Everything the approval gate shows; ``ok`` means no reasons.

    ``digest`` is a SHA-256 over the validated skill dir (relative paths and
    bytes), so the gate can prove it promotes exactly what was validated.
    """

    cid: str
    name: str | None
    reasons: tuple[str, ...]
    description: str = ""
    skill_dir: Path | None = None
    skill_md: str = ""
    script: str = ""
    test_source: str = ""
    test: TestRun | None = None
    dedup: tuple[DedupMatch, ...] = ()
    safety: tuple[ScreenReport, ...] = ()
    digest: str | None = None
    warnings: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """True when every check passed."""
        return not self.reasons

    @property
    def flagged(self) -> bool:
        """True when the safety screen reported any finding."""
        return any(report.flagged for report in self.safety)


class CandidateValidator:
    """Runs the FR-054 checks on ``store.candidate_dir(cid)``.

    ``executor`` runs the test (the Docker executor in production, a
    ``FakeExecutor`` in tests). Without one the test check fails closed.
    """

    def __init__(
        self,
        store: SkillStore,
        executor: Executor | None,
        *,
        timeout_s: float = TEST_TIMEOUT_S,
        dedup_threshold: float = DEDUP_THRESHOLD,
    ) -> None:
        """Bind the store and the sandbox executor (None fails the test check)."""
        self.store = store
        self.executor = executor
        self.timeout_s = timeout_s
        self.dedup_threshold = dedup_threshold

    async def validate(self, cid: str) -> ValidationReport:
        """Validate candidate ``cid``; never raises for a bad candidate."""
        try:
            candidate_dir = self.store.candidate_dir(cid)
            meta = CandidateMeta.model_validate_json(
                (candidate_dir / CANDIDATE_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, ConfigError) as exc:
            return ValidationReport(cid, None, (f"not a complete candidate: {exc}",))
        name = meta.name
        if meta.cid != cid or not SKILL_NAME_RE.fullmatch(name):
            return ValidationReport(cid, name, ("candidate.json does not match",))
        skill_dir = candidate_dir / name
        reasons = confinement_problems(candidate_dir, name)
        if reasons:
            return ValidationReport(cid, name, tuple(reasons), skill_dir=skill_dir)

        script_path = skill_dir / "scripts" / f"{name}.py"
        test_path = skill_dir / "tests" / f"test_{name}.py"
        try:
            digest = tree_digest(skill_dir)
            skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            script = script_path.read_text(encoding="utf-8")
            test_source = test_path.read_text(encoding="utf-8")
            if tree_digest(skill_dir) != digest:
                raise ValueError("candidate changed while being read")
        except (OSError, ValueError) as exc:
            return ValidationReport(cid, name, (f"unreadable file: {exc}",))

        manager_reasons, frontmatter, caught = _manager_check(candidate_dir, name)
        reasons.extend(manager_reasons)
        description = str(frontmatter.get("description", "")).strip()
        reasons.extend(_frontmatter_problems(name, skill_md, frontmatter))
        reasons.extend(_parse_problems({"script": script, "test": test_source}))
        safety: list[ScreenReport] = []
        if not any(r.startswith("syntax error") for r in reasons):
            safety = [screen(script), screen(test_source)]
            for label, report in zip(("script", "test"), safety, strict=True):
                for finding in report.findings:
                    if finding.category in REJECTING_CATEGORIES:
                        reasons.append(
                            f"safety: {label} line {finding.line}: "
                            f"{finding.category} ({finding.detail})"
                        )
        dedup = self._dedup(name, description)
        test = None
        if not reasons:
            test = await self._run_test(skill_dir, name)
            if not test.passed:
                reasons.append(_test_reason(test))
            if tree_digest(skill_dir) != digest:  # T056 m10
                reasons.append("candidate changed while its test ran")
        return ValidationReport(
            cid=cid,
            name=name,
            reasons=tuple(reasons),
            description=description,
            skill_dir=skill_dir,
            skill_md=skill_md,
            script=script,
            test_source=test_source,
            test=test,
            dedup=dedup,
            safety=tuple(safety),
            digest=digest,
            warnings=caught,
        )

    def _dedup(self, name: str, description: str) -> tuple[DedupMatch, ...]:
        """Exact name collision first, then description similarity."""
        live = self.store.catalog_entries()
        retriever = LexicalRetriever()
        retriever.index(live)
        matches: list[DedupMatch] = []
        for entry in live:
            if entry.name == name:
                matches.append(DedupMatch(entry.name, 1.0, "name", "replace"))
                continue
            match = self._similar(retriever, entry, description)
            if match is not None:
                matches.append(match)
        return tuple(sorted(matches, key=lambda m: (-m.similarity, m.name)))

    def _similar(
        self, retriever: LexicalRetriever, entry: SkillEntry, description: str
    ) -> DedupMatch | None:
        score = retriever.similarity(description, entry.description)
        if score < self.dedup_threshold:
            return None
        proposal: DedupAction = "skip" if score >= SKIP_THRESHOLD else "merge"
        return DedupMatch(entry.name, score, "description", proposal)

    async def _run_test(self, skill_dir: Path, name: str) -> TestRun:
        if self.executor is None:
            return TestRun(False, None, stderr="no executor configured")
        code = RUNNER.format(
            alias=SCRIPT_ALIAS,
            script=f"{SKILL_MOUNT}/scripts/{name}.py",
            test=f"{SKILL_MOUNT}/tests/test_{name}.py",
        )
        with tempfile.TemporaryDirectory(prefix="hb-skilltest-") as workdir:
            request = ExecRequest(
                language="python",
                code=code,
                workdir=Path(workdir),
                timeout_s=self.timeout_s,
                network=False,
                ro_mounts={skill_dir.resolve(): SKILL_MOUNT},
            )
            try:
                result = await self.executor.run(request)
            except (ConfigError, DockerUnavailableError) as exc:
                return TestRun(False, None, stderr=f"{type(exc).__name__}: {exc}")
        return _test_run(result)


def _test_run(result: ExecResult) -> TestRun:
    lines = result.stdout.strip().splitlines()
    sentinel = bool(lines) and PASS_SENTINEL.fullmatch(lines[-1].strip()) is not None
    passed = (
        result.exit_code == 0
        and sentinel
        and not result.timed_out
        and not result.oom_killed
        and not result.truncated
    )
    return TestRun(
        passed=passed,
        exit_code=result.exit_code,
        stdout=result.stdout[-OUTPUT_PREVIEW:],
        stderr=result.stderr[-OUTPUT_PREVIEW:],
        timed_out=result.timed_out,
        oom_killed=result.oom_killed,
        truncated=result.truncated,
    )


def _test_reason(test: TestRun) -> str:
    if test.timed_out:
        return "test timed out"
    if test.oom_killed:
        return "test was killed (out of memory)"
    if test.exit_code is None:
        return f"test did not run: {test.stderr}"
    if test.exit_code != 0:
        return f"test failed (exit code {test.exit_code})"
    if test.truncated:
        return "test output was truncated"
    return "test exited 0 without the runner's pass line (early exit)"


def confinement_problems(candidate_dir: Path, name: str) -> list[str]:
    """Layout and confinement problems of ``candidate_dir`` (empty when fine).

    Allowed entries: ``candidate.json``, ``source.json`` and ``<name>/``
    holding exactly ``SKILL.md``, ``scripts/<name>.py`` and
    ``tests/test_<name>.py``. Any symlink or junction, and any path resolving
    outside ``candidate_dir``, is a problem.
    """
    root = candidate_dir.resolve()
    expected_dirs = {name, f"{name}/scripts", f"{name}/tests"}
    expected_files = {
        CANDIDATE_FILE,
        "source.json",
        f"{name}/SKILL.md",
        f"{name}/scripts/{name}.py",
        f"{name}/tests/test_{name}.py",
    }
    problems: list[str] = []
    seen: set[str] = set()
    for path in sorted(candidate_dir.rglob("*")):
        rel = path.relative_to(candidate_dir).as_posix()
        if _is_link(path):
            problems.append(f"link not allowed: {rel}")
            continue
        if not path.resolve().is_relative_to(root):
            problems.append(f"path escapes the candidate: {rel}")
            continue
        wanted = expected_dirs if path.is_dir() else expected_files
        if rel not in wanted:
            problems.append(f"unexpected entry: {rel}")
        seen.add(rel)
    missing = sorted((expected_files - {"source.json"}) - seen)
    problems.extend(f"missing file: {rel}" for rel in missing)
    return problems


def _is_link(path: Path) -> bool:
    """A symlink, or any Windows reparse point (junctions included)."""
    try:
        info = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) != 0


def tree_digest(directory: Path) -> str:
    """SHA-256 over the relative paths and bytes of every file under ``directory``.

    Raises ``ValueError`` on a symlink or junction, so a digest is only ever
    computed over a plain tree.
    """
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if _is_link(path):
            raise ValueError(f"link in tree: {path}")
        if not path.is_file():
            continue
        rel = path.relative_to(directory).as_posix()
        digest.update(json.dumps(rel).encode("utf-8") + b"\0")
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def _manager_check(
    candidate_dir: Path, name: str
) -> tuple[list[str], dict[str, object], tuple[str, ...]]:
    """Load the candidate with a throwaway manager; zero warnings required.

    Returns (problems, frontmatter as the catalog parses it, warnings).
    """
    from harness_builder.skills._compat import new_scoped_manager

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")  # the per-location registry hides repeats
        manager = new_scoped_manager([candidate_dir])
        entries = manager.discover_skills()
        frontmatter: dict[str, object] = {}
        found = [entry for entry in entries if entry.skill_dir.name == name]
        if found:
            frontmatter = manager.frontmatter(found[0].location)
    messages = tuple(str(w.message) for w in caught)
    problems = [f"SkillManager warning: {message}" for message in messages]
    names = sorted(entry.name for entry in entries)
    if names != [name]:
        problems.append(f"SkillManager discovered {names}, expected [{name!r}]")
    return problems, frontmatter, messages


def _frontmatter_problems(
    name: str, skill_md: str, catalog: dict[str, object]
) -> list[str]:
    """Strict frontmatter checks; the catalog's lenient parse must agree.

    The sibling parser splits on the first two ``---`` anywhere in the file,
    so a value containing ``---`` would be silently cut. The frontmatter must
    therefore be one ``---`` fenced YAML mapping without ``---`` inside, and
    parse to exactly what the catalog sees.
    """
    lines = skill_md.split("\n")
    closing = [i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"]
    if not lines or lines[0].strip() != "---" or not closing:
        return ["SKILL.md has no '---' fenced frontmatter"]
    block = "\n".join(lines[1 : closing[0]])
    if "---" in block:
        return ["frontmatter contains '---'"]
    try:
        strict = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        return [f"frontmatter is not valid YAML: {exc}"]
    if not isinstance(strict, dict):
        return ["frontmatter is not a mapping"]
    problems: list[str] = []
    if strict != catalog:
        problems.append("the catalog parses the frontmatter differently")
    if strict.get("name") != name:
        problems.append(f"frontmatter name {strict.get('name')!r} != dir {name!r}")
    description = strict.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("description is missing")
    elif len(description.strip()) > MAX_DESCRIPTION:
        problems.append(
            f"description has {len(description.strip())} characters "
            f"(max {MAX_DESCRIPTION})"
        )
    return problems


def _parse_problems(sources: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for label, source in sources.items():
        try:
            ast.parse(source)
        except SyntaxError as exc:
            problems.append(f"syntax error in {label} line {exc.lineno}: {exc.msg}")
    return problems
