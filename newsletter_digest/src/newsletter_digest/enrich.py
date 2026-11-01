"""Reading-list enrichment pipeline for article fetching (FR-023 to FR-036).

Role: candidate selection, fetching, summarising, rate pacing and merging the
results back into the `ReadingList`.

Allowed imports: project modules `fetch`, `prompts`, `llm` (the `ModelFactory`
type only), `links`, `models` and `config`; external `anyio` and `langchain_core`.
"""
