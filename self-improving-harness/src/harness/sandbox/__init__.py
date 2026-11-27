"""Docker sandbox that runs model-written code."""

import os

# The host runs ``docker`` and ``git``. Windows would look for them in the current
# folder first, which can be the user's project, where applied changes may have put
# a ``docker.exe`` that the agent wrote.
os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
