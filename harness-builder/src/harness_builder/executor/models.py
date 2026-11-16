"""Executor value models (designs.md 4.2, FR-045). Leaf module: stdlib + pydantic.

Numeric defaults are ASSUMED (OQ-13): 30 s timeout, 64 KiB output cap,
512 MiB memory, 1 CPU, 128 pids, network off, stateless containers.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Language = Literal["python", "bash"]


class ExecRequest(BaseModel):
    """One code execution inside a fresh sandbox container."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: Language
    code: str
    workdir: Path
    timeout_s: float = 30.0
    network: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    ro_mounts: dict[Path, str] = Field(default_factory=dict)
    stdin: str | None = None
    run_id: str | None = None


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
    timeout_s: float = 30.0
    output_cap_bytes: int = 65536
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    network: bool = False
    state_mode: Literal["stateless", "session"] = "stateless"
