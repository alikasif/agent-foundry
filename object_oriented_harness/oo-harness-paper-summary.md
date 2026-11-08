# Object-Oriented Harness (OO-Harness): Summary and Implementation Guide

**Paper:** *Object-Oriented Harness: Evolving Agent Harnesses through Object-Oriented Design*
**arXiv:** [2610.28380](https://arxiv.org/abs/2610.28380) (v1, 20 Oct 2026), cs.CL / cs.SE, CC BY 4.0
**Authors:** Gaotang Li (UIUC; Google Cloud AI Research intern), Bhavana Dalvi Mishra, Yifei Ming, Yanfei Chen, Zhongying CuiZhu, Lesly Miculicich, Long T. Le, Rui Meng, Chun-Liang Li, Chen-Yu Lee, Tomas Pfister (Google Cloud AI Research), Hanghang Tong (UIUC)
**Code:** The paper links no code release.

> Source note: this summary was built from the arXiv HTML version. Figures 4, 5 and 7 are images, so their exact values are missing here. Section 7 lists what the paper leaves unspecified. Every item in the implementation guide (Section 8) that fills one of those gaps is marked **[our choice]**.

---

## 1. TL;DR

A **harness** is all the code around a fixed LLM: prompts, tools, context management, execution control, middleware, file backends and subagents. Existing harness optimizers (Meta-Harness, AHE, HarnessEvolve) evolve **one global harness**. A change that helps one group of tasks often hurts another.

OO-Harness evolves a **class hierarchy of harnesses** instead:

- Each harness is a **Python class**. Children **inherit** from a parent, **override** or **add** methods, and call `super()` to keep the parent's behavior.
- During discovery, an **evolution agent** (a coding agent) looks at failures in one **task cluster** and writes **one subclass** that fixes a shared, harness-fixable mechanism. The subclass is **accepted only if it beats the cluster's current score**.
- At test time, a **router** first runs the base harness. It then reads the task and the **observable base trajectory** and picks either a specialist class or the **base harness**. If it picks the base harness, the base result is reused.
- Both agents gather **design lessons** and **routing lessons** during discovery. These are frozen at deployment.

**Headline result:** OO-Harness scores best on all three benchmarks while taking the fewest steps. On AutomationBench, Mean goes from 61.62 to 77.41 (+15.79 points). Routing on the trajectory, not just the task, matters most. The inheritance structure itself also beats flat copying.

---

## 2. Problem and motivation

- One global harness has to trade off between task types. In AHE's ablation, evolved memory helped hard tasks and hurt easy ones. EvoHarnessBench shows that extra tools, skills or specialists can make behavior worse.
- The goal is specialized harnesses that **share code** but keep **different execution strategies**, with a router that picks one per task.
- Object-oriented inheritance fits this well. Shared fixes live in ancestors, specializations live in subclasses, and each class is a complete, runnable harness.

---

## 3. Core concepts and notation

| Symbol | Meaning |
|---|---|
| `M` | Fixed executor LLM. It never changes; only the code around it evolves. |
| `D_tr` | Discovery (training) tasks. Held-out tasks are disjoint and used only for evaluation. |
| `H0` | Base harness, the root of the hierarchy. |
| `T_H` | Tree of harness classes. It starts as `{H0}`. |
| `C`, `D_c` | Task clusters, and the discovery tasks in cluster `c`. |
| `H_c` | Harness currently deployed for cluster `c`. |
| `π` | Evolution agent. It proposes candidate subclasses. |
| `ρ` | Router. It selects one harness per task at test time. |
| `E` | Execution records: candidate code, trajectories, outcomes, reflections. |
| `L_π` | Design lessons, used when revising code. |
| `L_ρ` | Routing lessons: when a given harness applies. |

### 3.1 Harness representation

- Each harness is a **complete Python class** that is compatible with the scaffold's execution interface.
- A child inherits every method. It may override methods (calling `super()` where useful) or add new ones.
- The router selects **one complete class**, which runs as **one instance**. Ancestors are **not** run as separate agents.
- Changes can target prompts, tools, context management, execution control, and, if the scaffold supports them, middleware, file backends and subagents.
- In the paper's prompts, the parent class is bound as `_Parent` (AutomationBench, Workspace-Bench) or `_Base` (Toolathlon).

Example lineage from the paper's appendix (Toolathlon):

```python
class FormulaLiteralHarness(BaseHarness):
    def manage_context(self, ...):
        ctx = super().manage_context(...)
        # ...keep spreadsheet formula literals intact in context...
        return ctx

class PercentCheckHarness(FormulaLiteralHarness):
    # inherits manage_context unchanged
    def verify_before_finish(self, ...):
        result = super().verify_before_finish(...)
        self.check_saved_percent_values(...)   # new method
        return result
```

Other lineages in the paper:

- **AutomationBench `s9 → s12 → s29`:** each step adds one mechanism through inheritance: a missing-write completion check, then a near-budget reminder, then tool-error guidance.
- **Workspace-Bench `office-doc-write`:** wraps the file backend so text becomes a binary `.docx`, and falls back to the parent backend on failure.
- **Toolathlon `completeness-finalize`:** the child narrows an inherited output-compression rule so it applies only before an early-finish attempt.

---

## 4. Algorithm

### 4.1 Discovery phase (paper's Algorithm 1)

```
Input: H0, fixed M, discovery tasks D_tr, budget T
1.  Run H0 on D_tr; store execution records E
2.  Cluster tasks using these records into {D_c}
3.  Initialize T_H = {H0}; H_c ← H0 for all c
4.  for i = 1..T:
5.      Review clusters; reassign/split/merge if needed
6.      π chooses a cluster c and a parent P ∈ T_H
7.      π builds subclass H′ of P (override/add methods)
8.      Run H′ on D_c (directly, no routing); collect scores + feedback
9.      Compare task-level outcomes; run acceptance checks
10.     If S_c(H′) > S_c^current and checks pass:
            add H′ to T_H; set H_c ← H′ if preferred
11.     Update cluster priority from acceptance history
            (repeated rejections → lower priority)
12.     Store outcome (accepted AND rejected); distill lessons L_π, L_ρ
Output: frozen T_H, {H_c}, records E, lessons L_π, L_ρ
```

Key rules:

- **Local acceptance.** A candidate only has to improve **its target cluster**, not every task. It is evaluated on `D_c` without routing.
- **Any node can be a parent.** The parent does not have to be the current `H_c`. A parent stays selectable after its children are accepted.
- **Clusters can change.** Clusters are reviewed every iteration and can be reassigned, split or merged, especially after repeated failed revisions.
- **Failures are kept.** Rejected proposals stay on disk for later inspection.
- **The evolver is a coding agent with filesystem access.** It can hand evidence inspection to subagents.
- Initial clustering groups tasks "whose base executions suggest they may benefit from a shared harness change." The paper gives no algorithm for this.

### 4.2 Evolution-agent prompt (condensed from the paper)

The paper's condensed instructions:

1. Read the training trajectories and prior changes.
2. Use subagents to inspect the execution evidence.
3. Identify **one shared, harness-fixable mechanism**.
4. Write **one subclass** of the selected parent, calling `super()` to keep inherited behavior.
5. Do **not** hardcode task IDs, answers or filenames.
6. Return a **JSON manifest** describing the change.

Manifest format (the "add" operation):

```json
{
  "op": "add",
  "cluster": "<training cluster>",
  "parent": "<existing node or base>",
  "name": "<candidate name>",
  "seam": "<modified interface>",
  "hypothesis": "<shared failure and mechanism>"
}
```

- AutomationBench also allows `merge` and `split` operations. These name the affected clusters or an observable split criterion.
- Workspace-Bench supplies the target cluster and parent. The `seam` must be one of `system_prompt | tools | middleware | subagents | backend`.
- Toolathlon also asks for an observable **applicability gate**, a condition under which the mechanism should fire.
- Limits: lesson text is capped at **6,000 chars**, and each proposal times out after **2,400 s**. The proposal timeout is separate from the executor rollout budget.

### 4.3 Test-time selection

```
H* = ρ(t, ω(t), {H0} ∪ {H_c : c ∈ C}, L_ρ)
```

- `t` is the task and `ω(t)` is the **observable base trajectory**. **H0 always runs first.**
- The router sees class descriptions, applicability conditions and frozen routing lessons. It never sees grader scores, verdicts or reference answers.
- If `H* = H0`, the base result is **reused** at no extra cost. Otherwise one instance of `H*` runs.
- Agent tools are **disabled** for the router.
- Unknown or invalid choices fall back to **Base**.

AutomationBench uses a two-step router:

1. **Retrieval prompt:** pick the relevant frozen routing rules and synthesize at most 10 lines. Timeout 400 s.
2. **Selection prompt** (timeout 1,200 s), condensed:
   - "Choose one available harness or base using only the Base query, observable trajectory, and retrieved training context."
   - "Choose base when no mechanism clearly fits or a learned anti-pattern applies."
   - Inputs: `AVAILABLE HARNESSES: <frozen catalogue>`, `RETRIEVED CONTEXT: <retrieval response>`, `BASE QUERY + DIGEST: <observable evidence>`
   - Output: `{"choice": "<harness-id|base>", "rule": "<rule-id or empty>", "why": "<one sentence>"}`

AutomationBench router input limits:

| Input | Limit |
|---|---|
| Query | 600 chars |
| Trajectory digest | up to 60 message entries, final digest capped at 5,000 chars; includes write counts and error patterns |
| Each routing lesson | 180 chars |
| Each harness description | 120 chars |

Batch routers (Workspace-Bench and Toolathlon) return:

```json
{"task_to_cluster": {"<task-id>": "<cluster-id|leave-on-base>"}}
```

- **Toolathlon:** batches of 25 tasks; at most 12 routing rules (4,000 chars total); timeout 1,800 s. Per-task features: tool families, number of history events, most frequent tool and event types, error categories.
- **Workspace-Bench:** one call per cohort; timeout 1,200 s. Per-task features: persona, difficulty, requested output-file extensions, file-dependency links, base turn count and tool-call count.

The task-only routing ablation uses a **confidence gate**: below **0.7**, the task stays on base. Output: `{"<task-id>": {"cluster": "...", "confidence": 0..1}}`. The paper notes that this confidence is self-reported and not calibrated.

---

## 5. Experimental setup and results

| Benchmark | Discovery / held-out | Domain | Primary metric |
|---|---|---|---|
| AutomationBench | 300 / 300 | Business workflows on simulated apps | Mean partial credit (PASS = reward ≥ 0.999) |
| Workspace-Bench Lite-100 | 288 / 100 | Files and apps; outputs are docs, spreadsheets, slides, code | Micro rubric pass rate (PASS = all rubrics met) |
| Toolathlon-GYM | 251 / 252 | Multi-tool tasks via MCP servers | PASS (reward ≥ 0.999) |

Setup:

- **Models:** executor **gemini-3.5-flash** (fixed for all methods); evolver **Claude Opus 4.8**. For the router, the paper names Claude Sonnet 5 in one place and Opus 4.8 in others, and does not reconcile the two.
- **Budget and runs:** up to 40 evolution iterations; 3 independent executor runs (mean ± std reported).
- **Baselines:**
  - Base harness
  - **Meta-Harness:** one global harness
  - **AHE:** a global harness snapshot
  - **AAH:** per-group specialists, routed on task description or metadata
  - Parallel best-of-K (K = 2, 5)
  - Sequential refinement (K = 2)

### Main results (held-out, %, mean of 3 runs)

| Method | AB Mean | AB PASS | AB Steps | WS Mean | WS PASS | TG Mean | TG PASS | TG Steps |
|---|---|---|---|---|---|---|---|---|
| Base | 61.62 | 25.44 | 31.27 | 25.45 | 3.67 | 71.87 | 31.15 | 40.50 |
| Parallel K=5 | 60.24 | 24.00 | 31.29 | 37.71 | 7.67 | 75.69 | 32.14 | 38.90 |
| Meta-Harness | 63.92 | 26.33 | 30.05 | 29.56 | 5.54 | 75.84 | 33.45 | 39.85 |
| AHE | 64.18 | 28.11 | 30.61 | 38.43 | 7.78 | 74.48 | 36.23 | 41.02 |
| AAH | 75.40 | 34.31 | 28.89 | 38.62 | 8.00 | 74.99 | 34.32 | 41.23 |
| **OO-Harness** | **77.41** | **37.22** | **27.99** | **39.48** | **8.33** | **77.18** | **37.45** | **37.25** |

Workspace-Bench standard deviations are large (up to ±4.3), so OO-Harness's lead over AAH and AHE there is within noise.

### Ablations (single runs)

**1. Routing inputs.** This is the largest effect.

| Benchmark / metric | Base | Task-only router | Task + trajectory router |
|---|---|---|---|
| AutomationBench Mean | 61.30 | 64.88 | **77.52** |
| Workspace-Bench Mean | 24.57 | 40.03 | **40.56** |
| Toolathlon PASS | 31.35 | 33.33 | **37.70** |

The trajectory adds little on Workspace-Bench because its clusters follow output type, which the task text already reveals.

**2. Inheritance vs. alternatives** (Workspace-Bench):

| Variant | Score |
|---|---|
| **OO** (inherit from the cluster's deployed harness) | **40.56** |
| **Flat** (copy all accepted mechanisms into a new file derived from Base) | ≈35.6 |
| **Base-reset** (one new mechanism on Base, nothing carried forward) | worse than Flat; exact value not given |

**3. Base reuse** (AutomationBench):

- 134 of 300 tasks (44.7%) stayed on Base. Those tasks already did well on Base (77.15 mean).
- The 166 tasks routed to specialists had weak Base runs (44.40 mean).

**4. Out-of-distribution** (Toolathlon, unseen app families): the router sent **every** test task to Base, which preserved performance. Meta-Harness's global harness **lowered** PASS. A safe fallback to Base is a key property of the design.

### Limitations stated by the authors

- The system depends on host APIs and environments: outages, rate limits and interface changes can break evolved harnesses.
- Authenticated services need secure credential and permission handling.

---

## 6. Takeaways for implementation

1. **Route on the base trajectory, not only on the task.** This produces most of the gain. Always run the base harness first and route after seeing its run.
2. **Make "stay on base" the default.** The router should pick a specialist only when a mechanism clearly fits. This guards against out-of-distribution regressions and saves compute, since 44.7% of tasks reused the base result.
3. **One mechanism per subclass, accepted against its cluster only.** Small, local changes with `super()` chaining are easier to accept and easier to compose.
4. **Inheritance beats copying.** Build on the deployed parent instead of re-deriving from base.
5. **Keep rejected candidates and distill lessons.** Lessons feed both the evolver (L_π) and the router (L_ρ).
6. **No answer leakage.** Forbid task IDs, answers and filenames in harness code, and hide grader output from the router and executor.

---

## 7. Gaps in the paper

The paper does not specify:

- The cluster score function `S_c`. Mean reward over `D_c` is the obvious choice.
- The clustering algorithm and the number of clusters.
- The cluster-priority formula.
- The exact acceptance checks and the configuration of the judging agent.
- Full prompt templates and manifest schemas; only condensed versions are given.
- Random seeds.

---

## 8. Implementation blueprint for agent-foundry

This is a suggested Python design that maps the paper onto code. Items marked **[our choice]** fill gaps in the paper.

### 8.1 Package layout

```
oo_harness/
  core/
    harness.py        # BaseHarness: the H0 interface (seams as methods)
    registry.py       # HarnessTree T_H: load/validate/persist subclasses
    records.py        # ExecutionRecord, Trajectory, Outcome (pydantic)
    executor.py       # wraps fixed model M (e.g. claude-sonnet-5-5 / any LLM)
  discovery/
    clustering.py     # initial clusters from base runs + split/merge
    evolver.py        # evolution agent π (coding agent, writes subclass + manifest)
    acceptance.py     # S_c comparison + checks (leakage, regression, judge)
    lessons.py        # distill L_π / L_ρ (capped length)
    loop.py           # Algorithm 1 driver
  routing/
    digest.py         # trajectory → compact digest (caps: 5k chars, 60 msgs)
    router.py         # ρ: retrieval + selection, JSON output, base fallback
  runtime/
    deploy.py         # run H0 → route → reuse or run H*
  harnesses/          # generated subclasses: <name>.py + <name>.manifest.json
  shared/             # records, lessons, cluster state (JSON)
```

### 8.2 Base harness with explicit seams

```python
class BaseHarness:
    """H0. Each public method is a seam that subclasses may override."""
    name = "base"
    description = "Unmodified benchmark harness."   # ≤120 chars, shown to router
    applies_when = ""                              # observable gate (Toolathlon-style)

    def system_prompt(self, task) -> str: ...
    def tools(self, task) -> list[Tool]: ...
    def manage_context(self, messages) -> list[Message]: ...
    def before_step(self, state) -> None: ...          # middleware hook
    def after_tool_call(self, state, call, result): ...
    def verify_before_finish(self, state) -> FinishDecision: ...
    def backend(self) -> FileBackend: ...

    def run(self, task, model) -> Trajectory:
        """Fixed control loop that calls the seams above. Not overridden."""
```

Keeping `run()` fixed and exposing seams **[our choice]** makes the generated subclasses small, reviewable and compatible with the scaffold. This matches the "seam" field of the manifest.

### 8.3 Loading and validating subclasses

- Each generated file defines exactly one class whose parent is injected as `_Parent` (paper convention).
- Validate before running **[our choice]**:
  - AST check that the class subclasses `_Parent`.
  - No literal task IDs or answers. Grep against a list of discovery task IDs and reference answers.
  - Overridden seams exist on the parent.
  - The class imports successfully.
- Persist `T_H` as JSON: `{name: {parent, cluster, seam, hypothesis, file, accepted, score}}`.

### 8.4 Discovery loop: concrete defaults [our choice]

| Gap | Default |
|---|---|
| `S_c` | Mean task reward over `D_c` (or rubric pass rate). Average over k=1 to 3 runs if the executor is noisy. |
| Acceptance | `S_c(H′) > S_c^current + ε`, with no task in `D_c` falling from pass to fail by more than a set limit. Leakage check must pass. Use an LLM judge only for ties or ambiguous cases. |
| Initial clustering | Ask the evolver LLM to group failed or low-score base trajectories by shared failure mechanism (error type, tool family, output type), or embed failure summaries and use k-means/HDBSCAN. Start with roughly 5 to 10 clusters. |
| Priority | `priority_c = (1 − S_c) / (1 + consecutive_rejections_c)`. Sample clusters in proportion to priority. After N rejections in a row (e.g. 3), trigger a split or merge review. |
| Lessons | After each iteration, append one line to L_π ("mechanism X helped/hurt cluster Y because Z") and to L_ρ ("route to H when the trajectory shows signal S"). Re-summarize when over the cap (6,000 chars). |
| Budget | T = 40 iterations; proposal timeout 2,400 s. |

### 8.5 Router

1. Build the digest from the base trajectory: query (≤600 chars), tool calls and results (≤60 entries), write counts, error patterns. Cap at 5,000 chars.
2. **Retrieval call:** select the relevant L_ρ rules (≤10 lines).
3. **Selection call:** send the catalogue of `{name, description, applies_when}` plus the retrieved rules and the digest. Require the JSON `{"choice", "rule", "why"}`.
4. Validate the output: any unknown or invalid choice falls back to `base`. Run the router with no tools.
5. If the choice is `base`, reuse the base result. Otherwise run the chosen class once.

### 8.6 Model choices

The paper used gemini-3.5-flash as the cheap fixed executor and a strong model (Opus-class) for the evolver and router. With Claude models, a reasonable split is:

- **Executor:** `claude-haiku-5-5` or `claude-sonnet-5-5`
- **Evolver and router:** `claude-opus-5-5`

What matters is a cheap, frozen executor and a strong meta-agent.

### 8.7 Minimal MVP order

1. Build `BaseHarness` with seams and the fixed `run()` loop on one benchmark or task set, and record trajectories.
2. Add `HarnessTree` with loading and validation of `_Parent` subclasses, and write one hand-made subclass to test it.
3. Implement the Algorithm 1 loop with LLM clustering and a local-acceptance evolver.
4. Implement the trajectory-digest router with base fallback.
5. Evaluate held-out tasks against the base harness and a global-harness baseline (one subclass applied to all tasks). Track mean, PASS, steps and the base-reuse rate.

---

## 9. References

- Paper: https://arxiv.org/abs/2610.28380
- Baselines:
  - Meta-Harness: https://github.com/stanford-iris-lab/meta-harness
  - AHE: https://github.com/china-qijizhifeng/agentic-harness-engineering
  - AAH: https://github.com/A-EVO-Lab/a-evolve/tree/release/adaptive-auto-harness
- Benchmarks:
  - AutomationBench: https://github.com/zapier/AutomationBench
  - Workspace-Bench: https://github.com/OpenDataBox/Workspace-Bench
  - Toolathlon-GYM: https://github.com/eigent-ai/toolathlon_gym
  - Original Toolathlon: https://github.com/hkust-nlp/Toolathlon
