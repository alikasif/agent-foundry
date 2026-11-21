"""Shared fixtures for unit tests that need the real sandbox image."""

import subprocess

import pytest

from harness_builder.executor.image_build import build_command, image_tag

BUILD_TIMEOUT_S = 900.0


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    """The real sandbox image tag, built once per session if it is missing.

    Only ``docker``-marked tests use it (the root conftest skips them when
    ``docker info`` fails). Skips, never fails, when the build cannot finish.
    TODO: the same fixture lives in ``tests/generator`` and ``tests/docker``;
    consolidate into one root-level fixture.
    """
    tag = image_tag()
    inspect = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if inspect.returncode == 0:
        return tag
    try:
        proc = subprocess.run(
            build_command(),
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(f"sandbox image {tag} not built: build exceeded {BUILD_TIMEOUT_S}s")
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lines[-1] if lines else f"exit {proc.returncode}"
        pytest.skip(f"sandbox image {tag} not built: {detail}")
    return tag
