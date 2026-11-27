"""The copy the agent works on, and how its changes reach the user's folder."""

import os

# This package runs ``git`` for the user's project, where applied changes may
# have put a ``git.exe`` that the agent wrote. Windows would look in the current
# folder first.
os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
