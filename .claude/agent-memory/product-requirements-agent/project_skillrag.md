---
name: skillrag-project
description: New skillrag subproject — a multi-skill RAG system for AI agents with progressive disclosure and local ChromaDB indices
type: project
---

skillrag is a greenfield subproject at `D:\GitHub\agent-foundry\skillrag\` — a multi-skill RAG system for AI agents. Each skill is a self-contained directory with SKILL.md, query/index/rebuild scripts, and a local ChromaDB vector index. The LLM discovers skills from a lightweight catalog at session start and activates them on demand via progressive disclosure.

**Why:** Static SKILL.md files don't scale to large knowledge bases — agents need a way to selectively query large document corpora without embedding everything in context.

**How to apply:** All coding work goes through the software-team-orchestrator. The PRD is at `skillrag/shared/prd.md`. This is Phase 1 complete — awaiting user approval before Phase 2 (design architecture).
