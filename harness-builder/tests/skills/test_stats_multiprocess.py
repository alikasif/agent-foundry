"""T056 MJ3-R2: ``record_use`` from several processes loses no entry.

Windows raises ``PermissionError`` on a read that overlaps another process's
``os.replace``; the old code read that as ``{}`` and rewrote the file with
only its own skill. Workers are real processes (``multiprocessing`` spawn).
"""

import multiprocessing
from pathlib import Path

import pytest

from harness_builder.skills.store import SkillStore

pytestmark = pytest.mark.timeout(180)

PROCESSES = 4
USES = 100
SEED_USES = 10


def worker(root: str, name: str, uses: int, out: "multiprocessing.Queue[int]") -> None:
    """Record ``uses`` uses of ``name``; report how many raised ``OSError``."""
    store = SkillStore(Path(root))
    failed = 0
    for _ in range(uses):
        try:
            store.record_use(name, True)
        except OSError:
            failed += 1
    out.put(failed)


def test_processes_lose_no_entry_and_no_count(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    for _ in range(SEED_USES):
        store.record_use("seed", True)
    ctx = multiprocessing.get_context("spawn")
    out: multiprocessing.Queue[int] = ctx.Queue()
    names = [f"p{i}" for i in range(PROCESSES)]
    procs = [
        ctx.Process(target=worker, args=(str(tmp_path), n, USES, out)) for n in names
    ]
    for proc in procs:
        proc.start()
    failed = sorted(out.get(timeout=150) for _ in procs)
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0

    final = store.read_stats()
    assert set(final) == {"seed", *names}
    assert final["seed"].usage_count == SEED_USES
    # a use that raised is dropped, never another skill's counters
    assert sum(final[n].usage_count for n in names) == PROCESSES * USES - sum(failed)
    for name in names:
        assert USES - sum(failed) <= final[name].usage_count <= USES
    assert not list(store.meta.glob("stats.json.corrupt-*"))
