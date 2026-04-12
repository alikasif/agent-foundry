#!/usr/bin/env python
"""Convenience entry point for running the Skillful Agent from the project root."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))

from skillful_agent.main import main  # noqa: E402

if __name__ == "__main__":
    main()
