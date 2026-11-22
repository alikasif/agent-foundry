"""T060: run the fixture-harness evals and the skills comparison; write JSON.

Usage (from the harness-builder directory)::

    uv run python scripts/run_eval_results.py [OUT_JSON] [--real-docker]

``--real-docker`` additionally runs ``pytest -m docker`` against a running
daemon (minutes) and records the per-module counts.

Everything here is offline: the three generated fixture harnesses
(``tests/generator/fixtures``) are written through the generator pipeline
with a scripted generator, then evaluated with ``EvalRunner``. Docker is
EMULATED in-process (the production sandbox code path with the in-process
``hb_entry`` functions), never a real container. TaskCases and the skills
comparison use a scripted ``FakeLLMClient``, so their numbers show that the
mechanics work, not model quality. Live TaskCases run only when
``HARNESS_LIVE=1`` and an API key exist; otherwise they are a recorded skip.
"""

import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import anyio
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness_builder.config import Settings  # noqa: E402
from harness_builder.core.builder import HarnessBuilder  # noqa: E402
from harness_builder.core.protocols import LLMClient  # noqa: E402
from harness_builder.core.types import ToolCall  # noqa: E402
from harness_builder.evals.models import EvalReport  # noqa: E402
from harness_builder.evals.runner import EvalHarness, EvalRunner  # noqa: E402
from harness_builder.evals.skills_compare import compare_skills  # noqa: E402
from harness_builder.executor.fake import FakeExecutor  # noqa: E402
from harness_builder.generator.pipeline import GeneratorPipeline  # noqa: E402
from harness_builder.generator.validate import ValidationPipeline  # noqa: E402
from harness_builder.llm.fake import FakeLLMClient  # noqa: E402
from harness_builder.llm.types import LLMRequest, LLMResponse  # noqa: E402
from harness_builder.permissions.policy import Rule  # noqa: E402
from harness_builder.skills.extension import install_skills  # noqa: E402
from harness_builder.skills.store import SkillStore  # noqa: E402
from harness_builder.spec.loader import load_eval_cases, load_spec  # noqa: E402
from harness_builder.spec.models import SkillsConfig  # noqa: E402
from harness_builder.tools.registry import tool  # noqa: E402
from tests.generator.support import (  # noqa: E402
    DOMAINS,
    GENERATOR_MODEL,
    REQUESTS,
    EmulatedDocker,
    ScriptedGenerator,
    fixture_files,
)
from tests.skills.support import write_live_skill  # noqa: E402

SKILL = "sum-numbers"
STEP_REDUCTION_TARGET = 0.20
"""OQ-15 threshold (ASSUMED): at least 20% fewer steps, no success drop."""


def report_json(report: EvalReport) -> dict[str, Any]:
    """A compact per-case dump of an ``EvalReport``."""
    return {
        "totals": report.totals.model_dump(),
        "cases": [
            {
                "id": c.id,
                "kind": c.kind,
                "status": c.status,
                "success_rate": c.success_rate,
                "steps": c.mean_steps,
                "tokens": c.tokens,
                "cost_usd": c.cost,
                "detail": c.detail,
            }
            for c in report.cases
        ],
    }


def task_script(spec_dir: Path, answer: str) -> Callable[[LLMRequest], LLMResponse]:
    """Scripted run LLM: call the first example tool once, then answer."""
    data = yaml.safe_load((spec_dir / "harness.yaml").read_text(encoding="utf-8"))
    first = data["tools"][0]
    call = ToolCall(
        id="t1", name=first["function"], args=(first.get("examples") or [{}])[0]
    )

    def script(req: LLMRequest) -> LLMResponse:
        if any(m.role == "tool" for m in req.messages):
            return LLMResponse(text=answer)
        return LLMResponse(tool_calls=[call])

    return script


async def eval_domain(domain: str, work: Path) -> dict[str, Any]:
    """Generate ``domain`` (emulated sandbox), then evaluate it."""
    settings = Settings(trace_dir=work / "traces")
    sandbox = EmulatedDocker()
    pipeline = GeneratorPipeline(
        validator=ValidationPipeline(settings=settings, executor=sandbox),
        llm=ScriptedGenerator(fixture_files(domain)).client,
        model=GENERATOR_MODEL,
        settings=settings,
    )
    out = work / domain
    result = await pipeline.run(REQUESTS[domain], out)
    spec, base_dir, source = load_spec(out)
    cases = load_eval_cases(spec, base_dir, source)
    answers = {c.id: getattr(c.check, "text", "") for c in cases if c.kind == "task"}
    answer = next(iter(answers.values()), "")
    live = os.environ.get("HARNESS_LIVE") == "1"
    skip = None
    if live and not (
        os.environ.get("OPENROUTER_API_KEY") or os.environ.get("HARNESS_API_KEY")
    ):
        live = False
    if os.environ.get("HARNESS_LIVE") != "1":
        skip = "HARNESS_LIVE is not 1"
    elif not live:
        skip = "no API key in the environment"
    script = task_script(out, answer)

    def factory(llm: LLMClient | None) -> EvalHarness:
        builder = (
            HarnessBuilder.from_spec(out, settings=settings)
            .executor(EmulatedDocker())
            .model("test/model")
            .trace_dir(None)
        )
        return builder.llm(llm or FakeLLMClient(script)).build()

    # TaskCases run with the FAKE llm (live=True only gates TaskCase execution).
    report = await EvalRunner(factory, cases, live=True, llm=None).run()
    return {
        "domain": domain,
        "generation_status": result.status,
        "mode": "offline",
        "llm": "fake (scripted)",
        "docker": "emulated (in-process hb_entry; no container)",
        "tool_cases_deterministic": True,
        "live_skip": skip,
        **report_json(report),
    }


@tool
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


def skills_script(req: LLMRequest) -> LLMResponse:
    """Seeded: activate the offered skill, answer. Empty: two add calls first."""
    results = [m for m in req.messages if m.role == "tool"]
    offered = f"<name>{SKILL}</name>" in (req.system or "")
    if offered:
        if not results:
            call = ToolCall(id="a1", name="activate_skill", args={"name": SKILL})
            return LLMResponse(tool_calls=[call])
        return LLMResponse(text="The total is 15.")
    if len(results) == 0:
        return LLMResponse(
            tool_calls=[ToolCall(id="c1", name="add", args={"a": 4, "b": 5})]
        )
    if len(results) == 1:
        return LLMResponse(
            tool_calls=[ToolCall(id="c2", name="add", args={"a": 9, "b": 6})]
        )
    return LLMResponse(text="The total is 15.")


async def skills_comparison(work: Path) -> dict[str, Any]:
    """FR-057 mechanics: empty vs seeded library with a scripted LLM."""
    cases = load_eval_cases_inline()
    library = work / "lib"
    write_live_skill(
        SkillStore(library), SKILL, "Add up a list of numbers.", body="Sum."
    )

    def runner_for(cfg: SkillsConfig) -> EvalRunner:
        def factory(llm: LLMClient | None) -> EvalHarness:
            builder = (
                HarnessBuilder("skills-compare", Settings(model="fake/model"))
                .system_prompt("Sum numbers. Prefer an offered skill.")
                .tools(add)
                .permissions(
                    [
                        Rule(n, "allow")
                        for n in (
                            "add",
                            "activate_skill",
                            "read_skill_resource",
                            "run_skill_script",
                        )
                    ]
                )
                .llm(llm or FakeLLMClient(skills_script))
                .executor(FakeExecutor())
                .trace_dir(None)
            )
            install_skills(cfg, builder)
            return builder.build()

        return EvalRunner(factory, cases, runs=2, live=True)

    cmp = await compare_skills(
        runner_for, library / ".agents" / "skills", SkillsConfig()
    )
    empty, seeded = cmp.empty.totals, cmp.seeded.totals
    reduction = (
        None
        if not empty.mean_steps or seeded.mean_steps is None
        else (empty.mean_steps - seeded.mean_steps) / empty.mean_steps
    )
    no_drop = (seeded.success_rate or 0) >= (empty.success_rate or 0)
    return {
        "mode": "offline",
        "llm": "fake (scripted, engineered to differ)",
        "docker": "none needed (FakeExecutor; run_skill_script never called)",
        "seeded_skills": cmp.seeded_skills,
        "empty": report_json(cmp.empty),
        "seeded": report_json(cmp.seeded),
        "step_reduction": reduction,
        "success_not_dropped": no_drop,
        "oq15_threshold": STEP_REDUCTION_TARGET,
        "oq15_mechanics_ok": reduction is not None
        and reduction >= STEP_REDUCTION_TARGET
        and no_drop,
        "oq15_measured_live": False,
        "note": "Scripted model: proves the comparison mechanics and arithmetic "
        "only. The OQ-15 criterion is NOT measured (needs a live LLM).",
    }


def load_eval_cases_inline() -> list[Any]:
    """The one TaskCase of the skills comparison."""
    from harness_builder.evals.models import EVAL_CASES

    return list(
        EVAL_CASES.validate_python(
            [
                {
                    "kind": "task",
                    "id": "sum-4-5-6",
                    "input": "Sum 4, 5 and 6 and report the total.",
                    "check": {"type": "contains", "text": "15"},
                }
            ]
        )
    )


def docker_state() -> dict[str, Any]:
    """Whether a Docker daemon answers (`docker info`)."""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}
    if proc.returncode == 0 and proc.stdout.strip():
        return {"available": True, "server_version": proc.stdout.strip()}
    return {"available": False, "detail": (proc.stderr or proc.stdout).strip()[:300]}


def real_docker_tests() -> dict[str, Any]:
    """Run ``pytest -m docker`` against the real daemon and summarise it.

    Per-file counts come from a junit file. This measures the sandbox,
    isolation, limits and the real-container paths of stage C, ContainerTool,
    strategy swap and skills, not model quality.
    """
    with tempfile.TemporaryDirectory(prefix="hb-real-docker-") as tmp:
        xml = Path(tmp) / "junit.xml"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-m", "docker", "-q",
             "-p", "no:cacheprovider", f"--junitxml={xml}"],
            cwd=ROOT, capture_output=True, text=True, timeout=1800,
        )  # fmt: skip
        root = ET.parse(xml).getroot()
    files: dict[str, dict[str, int]] = {}
    for case in root.iter("testcase"):
        name = (case.get("classname") or "").rsplit(".", 1)[0] or "unknown"
        counts = files.setdefault(name, {"passed": 0, "failed": 0, "skipped": 0})
        if case.find("skipped") is not None:
            counts["skipped"] += 1
        elif case.find("failure") is not None or case.find("error") is not None:
            counts["failed"] += 1
        else:
            counts["passed"] += 1
    return {
        "date": date.today().isoformat(),
        "pytest_exit_code": proc.returncode,
        "summary_line": proc.stdout.strip().splitlines()[-1],
        "by_module": dict(sorted(files.items())),
    }


async def main(out_path: Path, with_real_docker: bool) -> None:
    """Run everything and write ``out_path``."""
    with tempfile.TemporaryDirectory(prefix="hb-t060-") as tmp:
        work = Path(tmp)
        domains = [await eval_domain(d, work) for d in DOMAINS]
        skills = await skills_comparison(work)
    live = os.environ.get("HARNESS_LIVE") == "1"
    data = {
        "task": "T060",
        "platform": platform.platform(),
        "docker": docker_state(),
        "real_docker_tests": (
            real_docker_tests()
            if with_real_docker
            else "not run (pass --real-docker with a running daemon)"
        ),
        "harness_live_env": os.environ.get("HARNESS_LIVE"),
        "live_taskcases": {
            "run": False,
            "skip_reason": "HARNESS_LIVE is not 1" if not live else "see per-domain",
        },
        "fixture_harness_evals": domains,
        "skills_comparison": skills,
    }
    out_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--real-docker"]
    target = Path(args[0]) if args else ROOT / "shared" / "eval_results.json"
    anyio.run(main, target, "--real-docker" in sys.argv)
