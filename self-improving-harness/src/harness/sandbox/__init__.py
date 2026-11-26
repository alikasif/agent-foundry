"""Docker sandbox that runs model-written code."""

import os

# The host runs ``docker`` and ``git``. Windows would look for them in the current
# folder first, which can be the workspace, where the agent can write a ``docker.exe``.
os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
