"""Shared helpers for the T055 promotion/approval/hygiene tests (offline).

Every LLM is a ``FakeLLMClient`` and every sandbox a ``FakeExecutor``; nothing
here touches Docker or the network. One executor per role: the validator's
test run needs the runner's pass sentinel, skill scripts and ``run_code`` do
not.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path

import anyio
from rich.console import Console

from harness_builder.core.types import ModelConfig
from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.skills.approval import ApprovalGate, PromotionApprover
from harness_builder.skills.promotion import PromotionService, PromotionSource
from harness_builder.skills.store import SkillStore
from harness_builder.skills.validation import CandidateValidator

PROMOTION_MODEL = ModelConfig(name="fake/promotion")
SKILL_NAME = "sum-numbers"
DESCRIPTION = "Add up a list of numbers and return their total sum."
PASS_LINE = "HB-SKILL-TESTS ran=1 failed=0"

SCRIPT = """\
import json
import sys


def main(args: dict) -> object:
    return {"total": sum(args["numbers"])}


if __name__ == "__main__":
    print(json.dumps(main(json.load(sys.stdin))))
"""

TEST = """\
import skill_script


def test_sum():
    assert skill_script.main({"numbers": [1, 2]}) == {"total": 3}
"""

SOURCE = PromotionSource(
    run_id="run-1",
    task="sum the numbers 1 and 2",
    language="python",
    code="FINAL(str(sum([1, 2])))",
    final_value="3",
)


def skill_reply(
    name: str = SKILL_NAME,
    description: str = DESCRIPTION,
    *,
    script: str = SCRIPT,
    test: str = TEST,
    instructions: str = "Run scripts/<name>.py with {'numbers': [...]}.",
) -> str:
    """A promotion-LLM reply in the JSON shape ``PromotionService`` expects."""
    return json.dumps(
        {
            "name": name,
            "description": description,
            "triggers": ["sum", "total", "numbers"],
            "instructions": instructions,
            "script": script,
            "test": test,
        }
    )


def promotion_service(store: SkillStore, *replies: str) -> PromotionService:
    """A ``PromotionService`` whose fake LLM returns ``replies`` in order."""
    llm = FakeLLMClient([LLMResponse(text=reply) for reply in replies])
    return PromotionService(store, llm, PROMOTION_MODEL)


async def make_candidate(store: SkillStore, reply: str | None = None) -> str:
    """Generate one complete pending candidate; return its cid."""
    return await promotion_service(store, reply or skill_reply()).propose(SOURCE)


def make_candidate_sync(store: SkillStore, reply: str | None = None) -> str:
    """``make_candidate`` for sync tests (CLI commands call ``anyio.run``)."""
    return anyio.run(make_candidate, store, reply)


def passing_sandbox() -> FakeExecutor:
    """A test sandbox whose candidate test run passes (exit 0 plus sentinel)."""
    return FakeExecutor([ExecResult(exit_code=0, stdout=f"PASS test_sum\n{PASS_LINE}")])


def failing_sandbox() -> FakeExecutor:
    """A test sandbox whose candidate test run fails."""
    stdout = "FAIL test_sum\nHB-SKILL-TESTS ran=1 failed=1"
    return FakeExecutor([ExecResult(exit_code=1, stdout=stdout)])


class UnavailableSandbox(FakeExecutor):
    """A sandbox whose ``run`` raises ``DockerUnavailableError``."""

    async def run(self, req: ExecRequest) -> ExecResult:
        """Record ``req``, then fail as if the daemon were gone."""
        self.requests.append(req)
        raise DockerUnavailableError("daemon is not reachable (test)")


def make_gate(
    store: SkillStore,
    approver: PromotionApprover | None = None,
    *,
    sandbox: FakeExecutor | None = None,
    auto_approve: bool = False,
) -> ApprovalGate:
    """An ``ApprovalGate`` with a quiet console and a passing sandbox."""
    validator = CandidateValidator(store, sandbox or passing_sandbox())
    return ApprovalGate(
        store,
        validator,
        approver,
        console=Console(quiet=True),
        auto_approve=auto_approve,
    )


def live_names(store: SkillStore) -> list[str]:
    """Names the catalog discovers in the live library (fresh manager)."""
    return sorted(entry.name for entry in store.catalog_entries())


def write_live_skill(
    store: SkillStore, name: str, description: str, *, body: str = "Body."
) -> Path:
    """Write ``live/<name>/SKILL.md`` as bytes (no CRLF); return the skill dir."""
    skill = store.live / name
    skill.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    (skill / "SKILL.md").write_bytes(text.encode("utf-8"))
    return skill


Snapshot = dict[str, tuple[int, int]]


def tree_snapshot(root: Path) -> Snapshot:
    """``{relative path: (size, mtime_ns)}`` for every entry under ``root``."""
    snapshot: Snapshot = {}
    for directory, dirs, files in os.walk(root):
        for entry in (*dirs, *files):
            path = Path(directory) / entry
            info = path.stat()
            size = info.st_size if path.is_file() else -1
            snapshot[path.relative_to(root).as_posix()] = (size, info.st_mtime_ns)
    return snapshot


CliEnv = dict[str, str | None]
CLI_ENV: CliEnv = {
    "OPENROUTER_API_KEY": "",
    "COLUMNS": "200",
    "PYTHONIOENCODING": "utf-8",
}
"""Hermetic CliRunner environment (wide rich tables, UTF-8 output)."""


def isolate_cli_env(tmp_path: Path, setenv: Callable[[str], None]) -> Path:
    """Return an empty cwd under ``tmp_path``; ``setenv`` deletes one variable.

    Callers pass ``monkeypatch.delenv`` for every ``HARNESS_*``/``OPENROUTER_*``
    variable so neither a developer's model nor a skills root leaks in.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    for key in list(os.environ):
        if key.startswith(("HARNESS_", "OPENROUTER_")):
            setenv(key)
    return cwd
