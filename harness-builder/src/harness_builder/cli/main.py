"""Command line entry point for harness-builder."""

import click


@click.group()
def main() -> None:
    """harness-builder: build, run, replay and evaluate agent harnesses."""
