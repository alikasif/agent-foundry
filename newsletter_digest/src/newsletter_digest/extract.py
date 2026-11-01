"""Article text extraction for article fetching (FR-032).

Role: a thin wrapper over `trafilatura.extract` (never its downloaders) with
whitespace tidying, delimiter scrubbing and truncation. Performs no network I/O.

Allowed imports: stdlib and `trafilatura`; no project modules.
"""
