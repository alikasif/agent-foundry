"""Shared offline helpers for the agent tests: no model is ever reached."""

import os
import socket
from dataclasses import dataclass

import psutil
from google.adk.models.lite_llm import LiteLlm

from mcpx.llm.models import ModelConfig


def closed_port() -> int:
    """Return a loopback port that nothing listens on (bound, then released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass(frozen=True)
class UnreachableModel(ModelConfig):
    """A ModelConfig whose LiteLLM points at a closed local port: fails fast."""

    port: int = 0

    def to_llm(self) -> LiteLlm:
        return LiteLlm(
            model=f"openrouter/{self.model_id}",
            api_base=f"http://127.0.0.1:{self.port}",
            api_key="dummy-not-a-key",
            timeout=self.timeout_s,
            num_retries=0,
        )


def python_children() -> list[psutil.Process]:
    """Live python child processes of this test process."""
    me = psutil.Process(os.getpid())
    found = []
    for proc in me.children(recursive=True):
        try:
            if "python" in proc.name().lower() and proc.is_running():
                found.append(proc)
        except psutil.NoSuchProcess:
            continue
    return found
