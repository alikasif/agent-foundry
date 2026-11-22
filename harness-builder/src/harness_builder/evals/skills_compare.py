"""Skills eval (FR-057): the same eval cases with an empty and a seeded library.

:func:`compare_skills` runs one ``EvalRunner`` per configuration and pairs
the reports case by case (success rate and mean steps). Both configurations
install the skills extension the same way (same tools, same ``top_k``); only
the library differs:

- ``empty``: a fresh skills root whose ``.agents/skills`` is empty;
- ``seeded``: a fresh skills root holding a copy of the seeded live library.

Both roots are temporary copies, so the runs' usage counters
(``skills-meta/stats.json``) never touch the real library, and
``include_user_skills`` is forced off so ``~/.agents/skills`` cannot leak into
the empty run. The caller builds the runner for a ``SkillsConfig`` (the CLI
installs the skills extension on a spec-built harness); this module imports
only eval models and the spec leaf.
"""

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harness_builder.evals.models import CaseResult, EvalReport
from harness_builder.evals.runner import EvalRunner
from harness_builder.spec.models import SkillsConfig

LIVE_SUBDIR = Path(".agents") / "skills"
"""The live library under a skills root (designs.md 4.7)."""

RunnerFactory = Callable[[SkillsConfig], EvalRunner]
"""Builds an ``EvalRunner`` whose harnesses install skills from the config."""


@dataclass(frozen=True)
class CaseComparison:
    """One case under both configurations (None when a side lacks the case)."""

    id: str
    empty: CaseResult | None
    seeded: CaseResult | None

    @property
    def rate_delta(self) -> float | None:
        """Seeded minus empty success rate, when both were decided."""
        return _delta(
            self.empty.success_rate if self.empty else None,
            self.seeded.success_rate if self.seeded else None,
        )

    @property
    def steps_delta(self) -> float | None:
        """Seeded minus empty mean steps, when both were decided."""
        return _delta(
            self.empty.mean_steps if self.empty else None,
            self.seeded.mean_steps if self.seeded else None,
        )


@dataclass(frozen=True)
class SkillsComparison:
    """Both eval reports plus the case-by-case pairing."""

    empty: EvalReport
    seeded: EvalReport
    seeded_skills: int
    """Skill directories copied into the seeded library."""

    @property
    def cases(self) -> list[CaseComparison]:
        """Cases in the empty report's order, then seeded-only cases."""
        empty = {case.id: case for case in self.empty.cases}
        seeded = {case.id: case for case in self.seeded.cases}
        ids = [*empty, *(case_id for case_id in seeded if case_id not in empty)]
        return [
            CaseComparison(case_id, empty.get(case_id), seeded.get(case_id))
            for case_id in ids
        ]

    @property
    def incomplete(self) -> bool:
        """True when either configuration has an incomplete case."""
        return bool(self.empty.totals.incomplete or self.seeded.totals.incomplete)


async def compare_skills(
    runner_for: RunnerFactory,
    seeded_library: Path | None,
    base: SkillsConfig | None = None,
) -> SkillsComparison:
    """Run the eval with an empty library, then with ``seeded_library``.

    ``seeded_library`` is a live skills directory (``<root>/.agents/skills``);
    None or a missing directory seeds nothing (both runs then use an empty
    library). ``base`` supplies ``top_k`` (default ``SkillsConfig()``); its
    ``root`` and ``include_user_skills`` are replaced. The temporary roots
    are removed afterwards.
    """
    config = (base or SkillsConfig()).model_copy(
        update={"enabled": True, "include_user_skills": False}
    )
    with tempfile.TemporaryDirectory(
        prefix="hb-skills-eval-", ignore_cleanup_errors=True
    ) as tmp:
        empty_root = Path(tmp) / "empty"
        seeded_root = Path(tmp) / "seeded"
        (empty_root / LIVE_SUBDIR).mkdir(parents=True)
        if seeded_library is not None and seeded_library.is_dir():
            shutil.copytree(seeded_library, seeded_root / LIVE_SUBDIR, symlinks=True)
        else:
            (seeded_root / LIVE_SUBDIR).mkdir(parents=True)
        seeded_skills = sum(
            1
            for path in (seeded_root / LIVE_SUBDIR).iterdir()
            if path.is_dir() and not path.is_symlink()  # T056 m12
        )
        empty = await runner_for(
            config.model_copy(update={"root": str(empty_root)})
        ).run()
        seeded = await runner_for(
            config.model_copy(update={"root": str(seeded_root)})
        ).run()
    return SkillsComparison(empty=empty, seeded=seeded, seeded_skills=seeded_skills)


def _delta(empty: float | None, seeded: float | None) -> float | None:
    if empty is None or seeded is None:
        return None
    return seeded - empty
