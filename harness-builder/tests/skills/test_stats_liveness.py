"""T059 (T058 MA-3, T056 m3/m15/m16): the stats write cannot stall a run.

The ``skills:stats`` hook runs the write in a worker thread with a short lock
deadline, both platforms poll a non-blocking process lock, and ``write_stats``
takes the same locks as ``record_use``.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest

from harness_builder.core.types import Message, Observation, ToolCall
from harness_builder.skills import extension as ext_mod
from harness_builder.skills.extension import ACTIVATE_SKILL, SkillsExtension
from harness_builder.skills.store import (
    STATS_LOCK_FILE,
    SkillStats,
    SkillStore,
    _process_lock,  # pyrefly: ignore[missing-import]
)

from .support import write_live_skill  # pyrefly: ignore[missing-import]


@dataclass
class Ctx:
    """A minimal ``RunContextView``."""

    run_id: str = "run-a"
    depth: int = 0
    workdir: Path | None = None
    messages: list[Message] = field(default_factory=list)
    ledger: Any = None
    recorder: Any = None


@pytest.fixture
def store(tmp_path: Path) -> SkillStore:
    """A store whose meta dir exists."""
    store = SkillStore(tmp_path)
    store.meta.mkdir(parents=True)
    return store


def test_record_use_times_out_when_the_process_lock_is_held(store: SkillStore) -> None:
    with _process_lock(store.meta / STATS_LOCK_FILE, 1.0):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            store.record_use("x", True, lock_timeout_s=0.1)
        assert time.monotonic() - started < 2.0
    assert store.record_use("x", True).usage_count == 1  # free again


def test_write_stats_takes_the_lock(store: SkillStore) -> None:
    with _process_lock(store.meta / STATS_LOCK_FILE, 1.0):
        with pytest.raises(TimeoutError):
            store.write_stats({"x": SkillStats()}, lock_timeout_s=0.1)
    assert not store.stats_path.exists()
    store.write_stats({"x": SkillStats(usage_count=3)})
    assert store.read_stats()["x"].usage_count == 3


@pytest.mark.anyio
async def test_hook_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SkillStore(tmp_path)
    write_live_skill(store, "pdf-merge", "Merge PDF files into one document.")
    extension = SkillsExtension(store)

    ctx = Ctx()
    await extension.contribute("merge pdf", ctx)
    monkeypatch.setattr(ext_mod, "STATS_HOOK_LOCK_TIMEOUT_S", 0.5)
    call = ToolCall(id="c", name=ACTIVATE_SKILL, args={"name": "pdf-merge"})
    obs = Observation(call_id="c", ok=True, content="x")
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await anyio.sleep(0.01)
            ticks += 1

    store.meta.mkdir(parents=True, exist_ok=True)
    with _process_lock(store.meta / STATS_LOCK_FILE, 1.0):
        async with anyio.create_task_group() as tg:
            tg.start_soon(ticker)
            with pytest.raises(TimeoutError):
                await extension.record_stats(call, obs, ctx)
            tg.cancel_scope.cancel()
    assert ticks >= 10  # the loop kept running while the hook waited
    assert store.read_stats() == {}
