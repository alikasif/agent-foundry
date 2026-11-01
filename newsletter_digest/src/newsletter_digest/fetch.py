"""Guarded page fetching for article fetching (FR-031, FR-033).

Role: `FetchConfig`, `PageFetcher` (owns one `httpx.AsyncClient`), response
classification, the bounded body reader and the per-run request ledger.

Allowed imports: project modules `netguard`, `extract`, `links`, `models` and
`config`; external `httpx` and `anyio`.
"""
