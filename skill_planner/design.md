
---

# **Design Doc: Skill Planner Agent**

## 1) Purpose

**Goal:**
Transform a natural-language task into a **validated execution plan** composed of capabilities (skills), with explicit data flow and constraints.

**Non-Goals:**

* No tool/model execution
* No retries/fallback at runtime
* No cost/latency optimization during execution

---

## 2) System Overview

```text
User Input
   ↓
Intent/Capability Resolver
   ↓
Task Decomposer (LLM, constrained)
   ↓
Capability Mapper
   ↓
Plan Compiler (DAG builder)
   ↓
Plan Validator
   ↓
Plan Spec (JSON)
```

---

## 3) Core Concepts

### 3.1 Capability

Abstract operation with strict I/O contract.

```json
{
  "name": "analyze_company",
  "inputs": { "company": "string" },
  "outputs": {
    "financial_health": "string",
    "risks": "array[string]"
  }
}
```

---

### 3.2 Plan (DAG)

* Nodes = steps (capability invocations)
* Edges = data dependencies via variable bindings

---

### 3.3 Plan Spec (Contract)

**Strict JSON schema** (no free-form execution logic).

```json
{
  "version": "1.0",
  "goal": "string",
  "steps": [ ... ],
  "final_output": "string",
  "constraints": { ... },
  "execution_hints": { ... }
}
```

---

## 4) Module Design

---

## 4.1 Capability Registry

### Responsibility

Store and serve capability definitions.

### Storage

* JSON/YAML files or DB table

### Interface

```python
class CapabilityRegistry:
    def get(name: str) -> Capability
    def list() -> List[Capability]
```

### Data Model

```python
class Capability:
    name: str
    description: str
    input_schema: dict
    output_schema: dict
```

---

## 4.2 Intent / Capability Resolver

### Responsibility

Map raw user input → primary capability (or multi-capability intent)

### Implementation Options

#### v1 (recommended)

* embedding similarity

```python
def resolve(text: str) -> str:
    emb = embed(text)
    return nearest(capability_embeddings, emb)
```

#### v2

* LLM classification (strict JSON output)

---

## 4.3 Task Decomposer (LLM, constrained)

### Responsibility

Break task into ordered capability steps

### Prompt Template (strict)

```text
You are a planner. Convert the task into a sequence of capabilities.
Return ONLY JSON.

Capabilities available:
- analyze_company(company)
- compare_companies(company_a, company_b)

Task: {user_input}
```

### Output

```json
[
  {"capability": "analyze_company", "args": {"company": "Infosys"}},
  {"capability": "analyze_company", "args": {"company": "TCS"}},
  {"capability": "compare_companies"}
]
```

---

### Guardrails

* JSON schema validation
* max steps limit
* allowed capability whitelist

---

## 4.4 Capability Mapper

### Responsibility

Validate and enrich decomposed steps using registry

```python
def map_steps(raw_steps):
    for step in raw_steps:
        assert step.capability in registry
```

---

## 4.5 Plan Compiler

### Responsibility

Convert linear steps → DAG with data bindings

---

### Algorithm

```python
def compile_plan(steps):
    outputs = {}
    compiled = []

    for i, step in enumerate(steps):
        step_id = f"s{i+1}"

        # resolve inputs
        inputs = {}
        for k, v in step.args.items():
            if v in outputs:
                inputs[k] = f"${outputs[v]}"
            else:
                inputs[k] = v

        output_var = f"{step_id}_out"
        outputs[step.capability] = output_var

        compiled.append({
            "id": step_id,
            "capability": step.capability,
            "input": inputs,
            "output": output_var
        })

    return compiled
```

---

### Note

You may need **entity tracking** for multi-instance capabilities.

---

## 4.6 Plan Validator

### Responsibility

Ensure plan correctness before returning

---

### Checks

#### 1. Schema Validation

* JSON schema compliance

#### 2. Capability Validation

* all capabilities exist

#### 3. Input Completeness

* required inputs present

#### 4. Data Dependency Integrity

* all `$refs` resolvable

#### 5. Cycle Detection

* ensure DAG (no loops)

```python
def detect_cycle(graph):
    # DFS cycle detection
```

---

## 4.7 Plan Assembler

### Responsibility

Produce final plan spec

```python
def assemble(goal, steps):
    return {
        "version": "1.0",
        "goal": goal,
        "steps": steps,
        "final_output": steps[-1]["output"]
    }
```

---

## 5) API Design

---

### 5.1 Plan Endpoint

```python
POST /plan
```

### Request

```json
{
  "task": "Compare Infosys and TCS"
}
```

---

### Response

```json
{
  "plan": { ... }
}
```

---

### Python SDK

```python
plan = planner.plan("Compare Infosys and TCS")
```

---

## 6) Execution Contract (for external runtimes)

Executor must support:

```python
for step in plan["steps"]:
    result = run_capability(step["capability"], step["input"])
    store(step["output"], result)
```

---

## 7) Error Handling

### Planner Errors

* unknown capability
* invalid decomposition
* missing inputs

### Strategy

* retry LLM decomposition (max 2)
* fallback to simpler plan

---

## 8) Performance Considerations

* cache capability embeddings
* cache frequent plans
* parallel LLM calls (if multi-pass)

---

## 9) Extensibility

### Add new capability

* update registry
* add embedding
* no planner change needed

---

### Add constraints (future)

```json
"constraints": {
  "max_cost": 0.05,
  "latency": "fast"
}
```

---

## 10) Minimal MVP Scope

* 5 capabilities
* embedding-based resolver
* single-pass LLM decomposition
* linear → DAG compiler
* strict validator

---

## 11) Testing Strategy

### Unit Tests

* capability mapping
* DAG compilation
* validation rules

### Integration Tests

* end-to-end plan generation

### Golden Tests

```python
input → expected plan
```

---

## 12) Observability

Log:

* input task
* generated steps
* validation errors
* final plan

---

## 13) Risks & Mitigations

| Risk                 | Mitigation                |
| -------------------- | ------------------------- |
| LLM hallucination    | strict schema + whitelist |
| invalid chaining     | validator                 |
| capability explosion | controlled registry       |

---

## 14) Future Enhancements

* multi-branch DAG (parallel steps)
* plan optimization
* cost-aware planning
* interactive plan refinement

---

# Final Summary

This system is:

> **A compiler from natural language → executable workflow DAG**

Key properties:

* deterministic output format
* capability abstraction
* execution-agnostic
* schema-driven reliability

---

If you want next, I can:

* generate a **ready-to-run Python starter repo (agentfoundry compatible)**
* or define a **strict JSON Schema for plan validation + pydantic models** so you can enforce correctness from day one.
