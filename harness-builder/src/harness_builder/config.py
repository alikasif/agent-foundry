"""Settings loaded from the environment and an optional ``.env`` file.

Nothing is read or validated at import. ``Settings.from_env()`` never mutates
``os.environ`` (so sandbox env allowlists stay meaningful); process environment
values take precedence over ``.env`` values. A missing API key is reported by
the LLM client when it is built, not here.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict

ENV_VARS: dict[str, str] = {
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "model": "HARNESS_MODEL",
    "trace_dir": "HARNESS_TRACE_DIR",
    "skills_root": "HARNESS_SKILLS_ROOT",
    "docker_image": "HARNESS_DOCKER_IMAGE",
}
"""Settings field name -> environment variable name."""

DEFAULT_TRACE_DIR = Path(".harness") / "traces"


class Settings(BaseModel):
    """Runtime settings; every field is optional except the trace dir default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    openrouter_api_key: str | None = None
    model: str | None = None
    trace_dir: Path = DEFAULT_TRACE_DIR
    skills_root: Path | None = None
    docker_image: str | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        dotenv_path: Path | None = None,
    ) -> "Settings":
        """Build settings from ``environ`` (default ``os.environ``) over ``.env``.

        ``dotenv_path`` defaults to ``./.env``; a missing file is ignored.
        Empty values count as unset.
        """
        path = dotenv_path if dotenv_path is not None else Path.cwd() / ".env"
        file_values = dotenv_values(path) if path.is_file() else {}
        env = os.environ if environ is None else environ
        values: dict[str, str] = {}
        for field, var in ENV_VARS.items():
            value = env.get(var) or file_values.get(var)
            if value:
                values[field] = value
        return cls.model_validate(values)
