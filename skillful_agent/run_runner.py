#!/usr/bin/env python
from pathlib import Path
import sys

# Ensure `src` is on sys.path so `skillful_agent` is importable
sys.path.insert(0, str(Path(__file__).parent / "src"))

from skillful_agent.runner import main

if __name__ == "__main__":
    main()
