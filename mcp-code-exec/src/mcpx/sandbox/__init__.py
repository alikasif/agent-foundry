"""Run agent-written Python against MCP tools: Monty plus a host-side gateway.

``MontyRuntime`` runs each script in a restricted interpreter in a worker process
and ``HostGateway`` is the one door back to the host: it forwards a script's tool
calls to an MCP connection under a call budget. See ``mcpx.sandbox.monty_runtime``
for what the sandbox does and does not isolate.
"""
