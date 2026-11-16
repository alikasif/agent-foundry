"""In-memory trace recorder that writes ``<trace_dir>/<run_id>.json`` (FR-012).

``TraceRecorder`` implements ``core.protocols.TraceSink``. Events are redacted
when recorded and grouped per step; ``finish`` builds the ``Trace``, writes it
when a trace dir is set (U12: ``HARNESS_TRACE_DIR``, default
``.harness/traces``) and returns it. Child runs get a recorder from
``child()`` and are embedded in the parent as ``SubagentEvent``s.
"""

import hashlib
import json
import platform
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from harness_builder.core.types import StopInfo, Usage
from harness_builder.llm.types import LLMRequest
from harness_builder.trace.models import (
    LlmCallEvent,
    SubagentEvent,
    Trace,
    TraceEvent,
    TraceStep,
    stable_dump,
)
from harness_builder.trace.redact import Redactor

ENV_PACKAGES: dict[str, str] = {
    "harness_builder": "harness-builder",
    "adk": "google-adk",
    "litellm": "litellm",
}
"""Trace ``env`` key -> distribution name (read from metadata, not imported)."""


def canonical_json(value: Any) -> str:
    """Canonical JSON of ``value``: volatile fields dropped, keys sorted."""
    return json.dumps(
        stable_dump(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def config_hash(config: BaseModel | Mapping[str, Any]) -> str:
    """sha256 hex of the canonical JSON of a resolved harness config.

    Code-built and spec-built harnesses must hash the same config object
    through this function so their ``Trace.config_hash`` values match.
    """
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def request_fingerprint(request: LLMRequest) -> str:
    """sha256 hex of the canonical, volatile-free JSON of an LLM request."""
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()


def runtime_env() -> dict[str, str]:
    """Versions for ``Trace.env``; missing distributions are left out."""
    env = {"python": platform.python_version(), "os": platform.platform()}
    for key, dist in ENV_PACKAGES.items():
        try:
            env[key] = version(dist)
        except PackageNotFoundError:
            continue
    return env


def trace_path(trace_dir: Path, run_id: str) -> Path:
    """Path of the trace file for ``run_id`` inside ``trace_dir``."""
    return trace_dir / f"{run_id}.json"


def write_trace(trace: Trace, path: Path) -> Path:
    """Write ``trace`` as UTF-8 JSON to ``path``, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trace.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_trace(path: Path) -> Trace:
    """Read and validate a trace JSON file."""
    return Trace.model_validate_json(path.read_text(encoding="utf-8"))


def _total_usage(steps: list[TraceStep]) -> Usage:
    """Sum own LLM calls plus the usage of embedded child traces."""
    input_tokens = output_tokens = llm_calls = 0
    costs: list[float | None] = []
    for step in steps:
        for event in step.events:
            if isinstance(event, LlmCallEvent):
                input_tokens += event.usage.input_tokens
                output_tokens += event.usage.output_tokens
                llm_calls += 1
                cost = event.cost_usd
                costs.append(cost if cost is not None else event.usage.cost_usd)
            elif isinstance(event, SubagentEvent):
                child = event.child.usage
                input_tokens += child.input_tokens
                output_tokens += child.output_tokens
                llm_calls += child.llm_calls
                if child.llm_calls:
                    costs.append(child.cost_usd)
    known = [cost for cost in costs if cost is not None]
    total_cost = sum(known) if costs and len(known) == len(costs) else None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=total_cost,
        llm_calls=llm_calls,
    )


class TraceRecorder:
    """Collects redacted events per step and produces the final ``Trace``."""

    def __init__(
        self,
        *,
        run_id: str,
        harness_name: str,
        config_hash: str,
        strategy: str,
        model: str,
        task: str,
        parent_run_id: str | None = None,
        depth: int = 0,
        spec_snapshot: dict[str, Any] | None = None,
        redactor: Redactor | None = None,
        trace_dir: Path | None = None,
    ) -> None:
        """Create a recorder; ``trace_dir`` None means ``finish`` writes nothing.

        ``redactor`` defaults to ``Redactor()`` (secret-named env vars of
        ``os.environ``); pass ``Redactor.from_settings`` to cover ``.env``.
        """
        self.run_id = run_id
        self.harness_name = harness_name
        self.config_hash = config_hash
        self.strategy = strategy
        self.model = model
        self.task = task
        self.parent_run_id = parent_run_id
        self.depth = depth
        self.spec_snapshot = spec_snapshot
        self.redactor = redactor if redactor is not None else Redactor()
        self.trace_dir = trace_dir
        self.path: Path | None = None
        self._steps: list[tuple[int, list[TraceEvent]]] = []
        self._trace: Trace | None = None

    def start_step(self, index: int) -> None:
        """Open step ``index``; indices must increase strictly."""
        self._ensure_open()
        if self._steps and index <= self._steps[-1][0]:
            raise ValueError(
                f"step index {index} must be greater than {self._steps[-1][0]}"
            )
        self._steps.append((index, []))

    def record(self, event: TraceEvent) -> None:
        """Redact ``event`` and append it to the current step.

        Events recorded before any ``start_step`` open step 0 implicitly.
        """
        self._ensure_open()
        if not self._steps:
            self._steps.append((0, []))
        self._steps[-1][1].append(self.redactor.redact_model(event))

    def child(
        self,
        *,
        run_id: str,
        task: str,
        harness_name: str | None = None,
        config_hash: str | None = None,
        strategy: str | None = None,
        model: str | None = None,
    ) -> "TraceRecorder":
        """Recorder for a sub-agent run: depth + 1, same redactor, no file.

        Record its finished trace here as ``SubagentEvent(call_id, child)``.
        """
        return TraceRecorder(
            run_id=run_id,
            harness_name=harness_name or self.harness_name,
            config_hash=config_hash or self.config_hash,
            strategy=strategy or self.strategy,
            model=model or self.model,
            task=task,
            parent_run_id=self.run_id,
            depth=self.depth + 1,
            redactor=self.redactor,
        )

    def finish(self, stop: StopInfo) -> Trace:
        """Build the trace, write it when ``trace_dir`` is set, and return it."""
        self._ensure_open()
        steps = [TraceStep(index=index, events=events) for index, events in self._steps]
        trace = Trace(
            run_id=self.run_id,
            parent_run_id=self.parent_run_id,
            depth=self.depth,
            harness_name=self.harness_name,
            config_hash=self.config_hash,
            spec_snapshot=self.spec_snapshot,
            strategy=self.strategy,
            model=self.model,
            task=self.task,
            steps=steps,
            usage=_total_usage(steps),
            stop=stop,
            env=runtime_env(),
        )
        self._trace = self.redactor.redact_model(trace)
        if self.trace_dir is not None:
            self.path = write_trace(
                self._trace, trace_path(self.trace_dir, self.run_id)
            )
        return self._trace

    def _ensure_open(self) -> None:
        if self._trace is not None:
            raise RuntimeError(f"trace {self.run_id} is already finished")
