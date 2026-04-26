# AI Architect — Skill Profile

## Context
Just as Solution, Application, and Enterprise Architects each operate at distinct altitudes (system, app, enterprise), an **AI Architect** is emerging as a peer role focused on designing AI-powered systems end-to-end. This document outlines the skill set that defines the role.

## Where the AI Architect Sits
- **Enterprise Architect** — defines org-wide tech strategy and standards.
- **Solution Architect** — designs cross-system solutions for a business problem.
- **Application Architect** — owns the internals of a single application.
- **AI Architect** — owns the AI capability layer that cuts across all three: model selection, data/feature pipelines, inference infrastructure, agentic workflows, evaluation, safety, and the human-AI interaction surface.

Think of the AI Architect as the person who answers: *"How does this organization reliably, safely, and cost-effectively turn data + models into production AI behavior?"*

---

## Core Skill Pillars

### 1. AI/ML Foundations
- Solid grasp of classical ML, deep learning, and modern foundation models (LLMs, VLMs, diffusion, embeddings).
- Understanding of training vs. fine-tuning vs. RAG vs. prompting vs. tool-use vs. agentic patterns — and *when* each is the right answer.
- Knowledge of model evaluation: offline metrics, human eval, LLM-as-judge, A/B testing, regression suites.

### 2. Data Architecture for AI
- Data contracts, lineage, and governance for training and inference data.
- Vector stores, hybrid search, knowledge graphs, feature stores.
- Data quality, labeling pipelines, synthetic data strategies, drift detection.
- Privacy-preserving techniques: differential privacy, federated learning, PII redaction.

### 3. System & Solution Design
- End-to-end reference architectures: ingest → process → train/index → serve → monitor → improve.
- Latency/throughput/cost tradeoffs across model size, batching, caching, distillation, quantization.
- Hybrid orchestration: small/large models, local/cloud, multi-vendor failover.
- Agentic system design: tool use, memory, planning, multi-agent coordination, guardrails.

### 4. MLOps / LLMOps / Platform Engineering
- CI/CD for models and prompts, experiment tracking, model/prompt registries, versioning.
- Inference infra: serving frameworks, GPU/accelerator capacity planning, autoscaling, cold-start handling.
- Observability: traces, evals-in-prod, token/cost telemetry, hallucination & safety monitoring.
- Feedback loops: capturing outcomes, RLHF/DPO pipelines, continuous evaluation.

### 5. Responsible AI, Safety & Governance
- Bias, fairness, explainability, and interpretability practices.
- Threat modeling for AI: prompt injection, data exfiltration, jailbreaks, model theft, supply-chain risks.
- Regulatory fluency: EU AI Act, NIST AI RMF, ISO/IEC 42001, sectoral rules (HIPAA, GDPR, SOX).
- Policy design: acceptable use, model cards, system cards, audit trails, red-teaming programs.

### 6. Cloud, Infra & Cost Engineering
- Deep familiarity with at least one hyperscaler AI stack (AWS Bedrock/SageMaker, Azure AI Foundry, GCP Vertex) and open ecosystems (Hugging Face, vLLM, Ray, Kubernetes).
- FinOps for AI: token economics, GPU utilization, caching strategies, request-shaping.
- Reliability engineering: SLAs/SLOs for non-deterministic systems, graceful degradation.

### 7. Software & Integration Architecture
- Strong fundamentals shared with Solution/Application Architects: API design, event-driven systems, microservices, IAM, security.
- Integration patterns: AI gateways, model routers, RAG pipelines, function/tool calling contracts, MCP-style protocols.
- Human-in-the-loop UX patterns and HCI for AI (confidence display, undo, attribution, refusal behavior).

### 8. Strategic & Business Skills
- Translating business problems into AI capability roadmaps; spotting where AI is *not* the right tool.
- Build-vs-buy-vs-fine-tune decisions; vendor and model lifecycle management.
- ROI modeling, total cost of intelligence, value-at-risk analysis.
- Stakeholder communication: explaining probabilistic systems to executives, legal, and end-users.

### 9. Leadership & Cross-Functional Soft Skills
- Bridging data science, engineering, product, security, legal, and compliance.
- Mentoring teams on AI patterns and anti-patterns; running architecture review boards.
- Comfort with ambiguity — AI systems behave statistically, not deterministically.
- Continuous learning discipline: the field re-bases every 6–12 months.

---

## Signature Deliverables of an AI Architect
- AI reference architecture and capability map for the organization.
- Model & prompt governance framework (registry, eval gates, rollout policy).
- AI platform blueprint (gateway, observability, guardrails, cost controls).
- Risk & compliance assessments (AI Act, NIST RMF mappings).
- Decision records: model selection, data residency, agentic boundaries.
- Roadmap aligning AI investments with enterprise architecture.

---

## How It Differs From Adjacent Roles
| Dimension | Solution Architect | Application Architect | Enterprise Architect | **AI Architect** |
|---|---|---|---|---|
| Primary artifact | Solution design | App internal design | Enterprise capability map | AI capability + platform design |
| Determinism | High | High | N/A (strategy) | **Probabilistic** |
| Core risk axis | Integration | Code quality | Strategic alignment | **Safety, drift, hallucination, cost** |
| Lifecycle concern | Releases | Codebase | Portfolio | **Data + Model + Prompt + Eval** |
| Evaluation | Tests | Tests | KPIs | **Evals, red-teams, online metrics** |

---

## Maturity Path
1. **Practitioner** — ML/LLM engineer who ships features.
2. **Lead** — owns one AI product end-to-end including evals and ops.
3. **AI Architect** — owns multiple AI systems, sets patterns, governs platform.
4. **Principal / Chief AI Architect** — defines org-wide AI strategy, partners with CTO/CIO/CISO and Enterprise Architecture.

---

## TL;DR
An AI Architect = (Solution Architect skills) + (deep ML/LLM systems knowledge) + (MLOps/LLMOps platform thinking) + (Responsible-AI governance) + (probabilistic-systems mindset) + (FinOps for compute) — wrapped in strong cross-functional communication.
