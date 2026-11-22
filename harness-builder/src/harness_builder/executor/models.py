"""Executor value models (designs.md 4.2, FR-045). Leaf module: stdlib + pydantic.

Numeric defaults are ASSUMED (OQ-13): 30 s timeout, 64 KiB output cap,
512 MiB memory, 1 CPU, 128 pids, network off, stateless containers.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Language = Literal["python", "bash"]
Entry = Literal["run", "schema", "call"]


class ExecRequest(BaseModel):
    """One code execution inside a fresh sandbox container.

    ``entry`` picks the ``hb_entry.py`` mode (T039, T036 C1). ``run`` executes
    ``code`` in ``language`` and takes no ``entry_args``. ``schema`` and
    ``call`` run a generated tool module (``entry_args`` = module path and
    function name); they stage no code, so ``language`` must be ``python``
    and ``code`` empty, and they never report FINAL.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: Language
    code: str
    workdir: Path
    timeout_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    network: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    ro_mounts: dict[Path, str] = Field(default_factory=dict)
    stdin: str | None = None
    run_id: str | None = None
    entry: Entry = "run"
    entry_args: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_entry(self) -> "ExecRequest":
        if self.entry == "run":
            if self.entry_args:
                raise ValueError("entry 'run' takes no entry_args")
            return self
        if self.language != "python" or self.code:
            raise ValueError(
                f"entry {self.entry!r} needs language 'python' and empty code"
            )
        return self


class ExecResult(BaseModel):
    """Outcome of one execution; never raised, always returned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False
    oom_killed: bool = False
    duration_s: float = Field(default=0.0, json_schema_extra={"volatile": True})
    final_set: bool = False
    final_value: str | None = None
    container_name: str = Field(default="", json_schema_extra={"volatile": True})
    image: str = ""


class SandboxConfig(BaseModel):
    """Resource and isolation limits for the Docker executor (ASSUMED defaults).

    ``image`` None means: use ``HARNESS_DOCKER_IMAGE`` or the content-hash tag
    computed by ``executor.image_build``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str | None = None
    # Limits must be positive: pids <= 0 would disable the fork-bomb cap.
    timeout_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    output_cap_bytes: int = Field(default=65536, gt=0)
    memory_mb: int = Field(default=512, gt=0)
    cpus: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    pids: int = Field(default=128, gt=0)
    network: bool = False
    state_mode: Literal["stateless", "session"] = "stateless"
