"""GeneratorPipeline: draft -> validate -> repair -> finalize (designs.md 1.2, 9).

1. **Draft.** The generator harness (``generator.harness``) writes into
   ``<out>.staging/``; a stale staging directory from a crashed run is
   removed first.
2. **Validate.** ``ValidationPipeline`` runs on the staging directory.
3. **Repair.** A ``fail`` report starts another generator run whose task is
   the original request plus the report (the staged files persist, so the
   generator reads and fixes them). ``max_attempts`` counts every run, the
   draft included (3 ASSUMED). ``incomplete`` is never repaired (Docker or a
   linter missing is not the generator's to fix), and a generator run that
   stopped with ``budget`` or ``error`` ends the loop. One budget ledger
   covers all attempts.
4. **Finalize.** Inside staging: ``README.md`` from the template when the
   generator wrote none, a FAILED (or INCOMPLETE) banner on top of it,
   ``reports/validation.{json,md}`` and ``.harness-meta.json``. Then one
   rename moves staging to ``<out>/``. A failing harness is still written,
   with ``status: fail``; it is never reported as passing.
"""

import json
import os
import re
import shutil
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from string import Template

import anyio
from pydantic import BaseModel, ConfigDict

from harness_builder.budget.ledger import BudgetLedger
from harness_builder.config import Settings
from harness_builder.core.builder import Harness
from harness_builder.core.protocols import LLMClient
from harness_builder.core.types import BudgetLimits, RunResult, StopInfo
from harness_builder.errors import ConfigError
from harness_builder.generator.harness import (
    GENERATOR_BUDGET,
    GENERATOR_NAME,
    REPORTS_DIR,
    STAGING_MARKER,
    TEMPLATE_DIR,
    AskFn,
    generator_builder,
)
from harness_builder.generator.request import GenerationRequest
from harness_builder.generator.validate import (
    EXIT_CODES,
    ValidationPipeline,
    ValidationReport,
    ValidationStatus,
)
from harness_builder.spec.loader import META_FILE

DEFAULT_MAX_ATTEMPTS = 3
"""Generator runs per ``harness new``, the first draft included (ASSUMED)."""
DEFAULT_OUTPUT_PARENT = Path("harnesses")
"""Default parent of generated harnesses, relative to the CWD (U7 ASSUMED)."""
STAGING_SUFFIX = ".staging"
FINAL_STOPS = frozenset({"budget", "error"})
"""Generator stop reasons that end the repair loop."""
MOVE_RETRIES = 5
"""Bounded retries of the final rename (transient Windows ``PermissionError``)."""
BANNERS: dict[ValidationStatus, str] = {
    "fail": (
        "> **FAILED VALIDATION.** This generated harness did not pass "
        "validation after {attempts} attempt(s); see `reports/validation.md`. "
        "`harness run` refuses it without `--allow-failed`.\n\n"
    ),
    "incomplete": (
        "> **VALIDATION INCOMPLETE.** Validation could not finish (for "
        "example, Docker is unavailable), so the generated tools are not fully "
        "checked; see `reports/validation.md` and run `harness validate` "
        "again.\n\n"
    ),
}


class GenerationResult(BaseModel):
    """Outcome of ``GeneratorPipeline.run``.

    ``status`` is the validation status of the written harness (``pass``
    only when the final validation passed); ``generator_stop`` is how the
    last generator run ended.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_dir: Path
    status: ValidationStatus
    attempts: int
    report: ValidationReport
    generator_stop: StopInfo

    @property
    def exit_code(self) -> int:
        """0 pass, 1 fail, 3 incomplete (as ``harness validate``)."""
        return EXIT_CODES[self.status]


class GeneratorPipeline:
    """Generates, validates, repairs and writes a harness directory.

    ``validator`` defaults to ``ValidationPipeline(settings=settings)``
    (Docker for generated tools); pass a configured one for ``--trust-tools``
    or ``--live``. ``llm`` and ``model`` override the generator's LLM client
    and model (default ``HARNESS_MODEL``); ``ask`` answers ``ask_user`` in
    interactive requests; ``budget`` limits all generator runs together.
    """

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        validator: ValidationPipeline | None = None,
        llm: LLMClient | None = None,
        model: str | None = None,
        settings: Settings | None = None,
        ask: AskFn | None = None,
        budget: BudgetLimits = GENERATOR_BUDGET,
    ) -> None:
        """Store the options; raises ``ValueError`` for ``max_attempts < 1``."""
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self.max_attempts = max_attempts
        self.validator = validator
        self.llm = llm
        self.model = model
        self.settings = settings
        self.ask = ask
        self.budget = budget

    async def run(
        self, request: GenerationRequest, out: Path | str | None = None
    ) -> GenerationResult:
        """Generate ``request`` into ``out`` (default ``default_output_dir``).

        Raises ``ConfigError`` when ``out`` exists and is not an empty
        directory, or when the generator harness does not build (for example
        no model). Harness problems never raise: they end in the report.
        """
        settings = self.settings or Settings.from_env()
        target = Path(out if out is not None else default_output_dir(request))
        target = target.resolve()
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ConfigError(f"{target} exists and is not empty; choose another -o")
        staging = target.with_name(target.name + STAGING_SUFFIX)
        builder = generator_builder(
            request, staging, settings=settings, ask=self.ask, budget=self.budget
        )
        if self.llm is not None:
            builder.llm(self.llm)
        if self.model is not None:
            builder.model(self.model)
        harness = builder.build()
        validator = self.validator or ValidationPipeline(settings=settings)
        if staging.exists():
            if not (staging / STAGING_MARKER).is_file():
                raise ConfigError(
                    f"{staging} exists and is not a harness-builder staging "
                    "directory; move it away or choose another -o"
                )
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        (staging / STAGING_MARKER).write_text(str(target), encoding="utf-8")

        ledger = BudgetLedger.from_limits(harness.config.budget, harness.price_lookup)
        attempts = 1
        result, report = await _attempt(
            harness, ledger, validator, staging, request.to_prompt()
        )
        while (
            report.status == "fail"
            and attempts < self.max_attempts
            and result.stop.reason not in FINAL_STOPS
        ):
            task = repair_prompt(request, report, attempts)
            attempts += 1
            result, report = await _attempt(harness, ledger, validator, staging, task)

        report = report.model_copy(update={"attempts": attempts})
        _finalize(staging, target.name, request, report, harness, result.stop)
        await _move(staging, target)
        (target / STAGING_MARKER).unlink(missing_ok=True)
        return GenerationResult(
            output_dir=target,
            status=report.status,
            attempts=attempts,
            report=report,
            generator_stop=result.stop,
        )


async def _attempt(
    harness: Harness,
    ledger: BudgetLedger,
    validator: ValidationPipeline,
    staging: Path,
    task: str,
) -> tuple[RunResult, ValidationReport]:
    """One generator run on the shared ledger, then validation of staging."""
    result, _ = await harness.run_traced(task, ledger=ledger)
    return result, await validator.run(staging)


def default_output_dir(request: GenerationRequest) -> Path:
    """``./harnesses/<slug of the description>`` (U7 ASSUMED)."""
    words = re.sub(r"[^a-z0-9]+", " ", request.description.lower()).split()
    slug = "-".join(words[:6])[:40].strip("-")
    return DEFAULT_OUTPUT_PARENT / (slug or "harness")


def repair_prompt(
    request: GenerationRequest, report: ValidationReport, attempt: int
) -> str:
    """The next run's task: the request plus attempt ``attempt``'s report."""
    return "\n".join(
        [
            request.to_prompt(),
            "",
            f"Attempt {attempt} failed validation. The files you wrote are "
            "still in the harness directory: read them with read_file, fix "
            "every problem below with write_file, then call submit again.",
            "",
            report.to_markdown(),
        ]
    )


def generator_version() -> str:
    """The installed harness-builder version (``unknown`` outside a package)."""
    try:
        return version("harness-builder")
    except PackageNotFoundError:
        return "unknown"


def _finalize(
    staging: Path,
    name: str,
    request: GenerationRequest,
    report: ValidationReport,
    harness: Harness,
    stop: StopInfo,
) -> None:
    """Write README (template, banner), reports and meta inside staging."""
    readme = staging / "README.md"
    if readme.is_file():
        text = readme.read_bytes().decode("utf-8", errors="replace")
    else:
        template = (TEMPLATE_DIR / "README.md").read_text(encoding="utf-8")
        text = Template(template).safe_substitute(
            name=name, description=request.description
        )
    banner = BANNERS.get(report.status)
    if banner is not None:
        text = banner.format(attempts=report.attempts) + text
    readme.write_bytes(text.encode("utf-8"))

    reports = staging / REPORTS_DIR
    reports.mkdir(exist_ok=True)
    (reports / "validation.json").write_bytes((report.to_json() + "\n").encode())
    (reports / "validation.md").write_bytes(report.to_markdown().encode("utf-8"))
    meta = {
        "status": report.status,
        "attempts": report.attempts,
        "generator": GENERATOR_NAME,
        "generator_version": generator_version(),
        "model": harness.config.model.name,
        "generator_stop": stop.reason,
        "trusted_tools": report.trusted,
        "all_cases_ran": report.all_cases_ran,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "request": request.model_dump(mode="json"),
    }
    (staging / META_FILE).write_bytes((json.dumps(meta, indent=2) + "\n").encode())


async def _move(staging: Path, target: Path) -> None:
    """Rename staging to ``target`` (an empty ``target`` is removed first)."""
    try:
        if target.exists():
            target.rmdir()
    except OSError as exc:
        raise ConfigError(
            f"{target} appeared while generating and is not empty; "
            f"the result stays in {staging}: {exc}"
        ) from exc
    for attempt in range(MOVE_RETRIES):
        try:
            os.replace(staging, target)
            return
        except PermissionError:
            if attempt == MOVE_RETRIES - 1:
                raise
            await anyio.sleep(0.1 * (attempt + 1))
