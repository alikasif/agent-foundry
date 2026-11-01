"""SSRF policy and enforcement for article fetching (FR-031).

Role: URL policy checks, public-address test, blocked networks, the guarded
network backend and transport builder.

Allowed imports: stdlib, `anyio`, `httpcore` and `httpx`; no project modules.
"""
