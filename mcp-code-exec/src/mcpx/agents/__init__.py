"""The two benchmark arms on Google ADK and the single-run entry point.

``direct``: every MCP tool schema goes to the model and every result comes back
through it. ``code``: the model gets one ``run_python`` tool and reaches the MCP
tools as importable Python, keeping intermediate results in the sandbox.
"""
