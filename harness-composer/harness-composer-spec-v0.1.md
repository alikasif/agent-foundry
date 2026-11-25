# Harness Composer
## Engineering Specification — v0.1

**Status:** Draft  
**Version:** 0.1  
**Project type:** Python-based agent runtime prototype  
**Working name:** Harness Composer

---

## 1. Overview

Harness Composer is a system that automatically constructs an execution harness for a given agent and task.

It accepts three primary inputs:

1. An agent profile describing the model, interfaces, and supported capabilities.
2. A task specification describing the objective, execution requirements, constraints, and expected outcomes.
3. A catalog of available harness components and their contracts.

It analyzes these inputs, selects compatible components, configures them, and produces an executable harness.

The resulting harness hosts the task agent and governs its execution.

The objective is to determine whether automatically composed harnesses can achieve comparable or better task success and reliability than fixed, manually designed harnesses, while reducing unnecessary execution overhead.

### Core idea

An agent determines what it should do to accomplish a task.

A harness determines the environment and execution conditions under which the agent operates.

Harness Composer determines how that environment should be constructed.

---

## 2. Problem Statement

Most agent implementations use a relatively fixed execution environment.

The same agent runtime might be used for:

- Research and information gathering.
- Software engineering.
- Data analysis.
- Document processing.
- Multi-step operational tasks.

However, these tasks have different execution requirements.

A software engineering task may require filesystem access, shell execution, test verification, checkpoints, and recovery.

A research task may require parallel retrieval, source tracking, evidence validation, and summarization.

A simple information retrieval task may require only a search tool and basic output validation.

Using the same harness for every task can introduce unnecessary components, execution overhead, and additional failure modes.

Manually designing a separate harness for every task is also difficult to maintain.

**Harness Composer attempts to automate the selection and composition of the execution environment.**

---

## 3. Goals

The initial version should:

1. Accept a task specification and agent profile.
2. Analyze task execution requirements.
3. Maintain a registry of reusable harness components.
4. Select compatible components based on task requirements.
5. Configure components using structured configuration.
6. Construct an executable harness plan.
7. Validate the plan before execution.
8. Execute an agent using the constructed harness.
9. Verify task completion independently of the agent's claims.
10. Record execution traces and evaluation metrics.

### Non-goals for v0.1

The initial version will not:

- Build or train foundation models.
- Generate arbitrary Python code for runtime execution.
- Create new tools autonomously.
- Support unrestricted multi-agent orchestration.
- Automatically modify its own source code.
- Learn new harness components from execution traces.
- Support arbitrary external side effects.
- Attempt to build a universal agent framework.

These can be considered later extensions.

---

## 4. Terminology and System Boundaries

The system uses the following definitions.

| Term | Definition |
|---|---|
| Model | The underlying LLM producing decisions and responses. |
| Agent | The task-solving logic that uses the model to decide what to do. |
| Harness | The execution environment surrounding the agent. |
| Harness component | A reusable runtime capability, such as context management or verification. |
| Harness plan | A declarative specification of selected and configured components. |
| Harness Composer | The system that constructs and validates the harness plan. |
| Runtime | The engine that instantiates and executes the harness plan. |

The agent and harness must remain independently configurable.

For controlled experimentation, the same agent implementation and model should be usable with multiple different harness configurations.

The harness owns execution orchestration, context delivery, tool dispatch, permissions, state management, verification, and recovery. The agent supplies task-solving decisions through a defined interface.

---

## 5. High-Level Architecture

The system consists of six major subsystems.

### 5.1 Input Manager

Accepts and normalizes the three input definitions:

- Agent Profile
- Task Specification
- Runtime Constraints

It validates their schemas and produces normalized internal representations.

### 5.2 Requirement Analyzer

Analyzes the task and determines the execution requirements.

Example:

Input task:

> Fix the failing tests in this Python repository.

Expected requirements:

- Repository inspection
- Filesystem read and write
- Shell execution
- Test execution
- Execution state management
- Result verification
- Bounded recovery

The analyzer should distinguish explicitly specified requirements from inferred requirements.

It should also represent uncertainty where the task description is incomplete.

### 5.3 Harness Planner

Selects the required components from the component catalog.

It evaluates:

- Task requirements.
- Agent capabilities.
- Component dependencies.
- Component compatibility.
- Execution constraints.
- Expected resource consumption.
- Security requirements.

The planner produces a proposed harness plan.

For v0.1, an LLM may assist with requirement interpretation and component selection, but it must produce structured output. The resulting plan is subsequently checked by deterministic validation logic.

### 5.4 Harness Compiler

Converts the proposed plan into a concrete, declarative harness configuration.

The compiler:

- Resolves component dependencies.
- Connects component interfaces.
- Applies configuration defaults.
- Resolves execution ordering.
- Produces a versioned harness manifest.

The compiler must not execute arbitrary generated code.

### 5.5 Harness Validator

Checks the resulting harness before execution.

Validation includes:

- Required capabilities are available.
- Component dependencies are satisfied.
- Component interfaces are compatible.
- Permissions are valid.
- Tool access respects runtime constraints.
- Verification requirements are covered.
- Resource limits are within configured bounds.

An invalid harness must not be executed.

### 5.6 Harness Runtime

Instantiates the validated plan and executes the task agent.

It coordinates:

- Context assembly.
- Model invocation.
- Tool execution.
- State persistence.
- Permission checks.
- Verification.
- Recovery.
- Execution tracing.

The runtime must not treat an agent's final response as sufficient evidence of task completion.

---

## 6. Execution Flow

The expected lifecycle is:

1. Receive an agent profile and task specification.
2. Normalize the inputs.
3. Analyze task requirements.
4. Retrieve compatible harness components.
5. Generate a proposed harness plan.
6. Compile the proposed plan.
7. Validate component contracts and runtime policies.
8. Instantiate the harness.
9. Execute the agent.
10. Verify the result.
11. Initiate bounded recovery when appropriate.
12. Record execution results and metrics.

A simplified representation:

```text
Agent Profile + Task Specification
                 |
                 v
          Requirement Analyzer
                 |
                 v
           Harness Planner
                 |
                 v
          Harness Compiler
                 |
                 v
          Harness Validator
                 |
                 v
           Harness Runtime
                 |
                 v
             Task Agent
                 |
                 v
          Result Verification
                 |
          +------+------+
          |             |
       Success        Failure
          |             |
          v             v
       Complete      Recovery
                        |
                        v
                  Re-verification
```

---

## 7. Harness Component Architecture

A harness is constructed from reusable components.

Each component must implement a standard contract.

### 7.1 Initial component categories

| Category | Initial components |
|---|---|
| Planning | Direct execution, plan-and-execute |
| Context management | Full context, retrieval, summarization |
| Execution | Sequential execution, parallel execution |
| Memory | Scratchpad, persistent run state |
| Verification | Schema validator, test runner, result evaluator |
| Recovery | Retry, repair, checkpoint and resume |
| Governance | Permission enforcement, approval gate, sandbox |

Not all components need to be implemented in the first iteration.

### 7.2 Component contract

Each component must expose metadata that allows the planner and compiler to reason about its use.

Illustrative contract:

```python
class HarnessComponent:
    name: str
    version: str

    provides: list[str]
    requires: list[str]

    input_schema: dict
    output_schema: dict

    permissions: list[str]

    dependencies: list[str]
    incompatible_with: list[str]

    estimated_cost: dict
    configuration_schema: dict
```

The catalog should contain actual executable implementations alongside their metadata.

A component must not be selectable based only on its natural-language description.

### 7.3 Component design principles

Components should be:

- Independently testable.
- Explicit about their inputs and outputs.
- Versioned.
- Deterministic wherever practical.
- Configurable without changing their implementation.
- Composable with other compatible components.

The component catalog is the source of truth for what the compiler is allowed to construct.

---

## 8. Harness Plan Representation

The compiler must produce a declarative manifest rather than arbitrary executable code.

Illustrative output:

```yaml
harness:
  name: coding_harness
  version: 0.1

  agent:
    interface: tool_calling

  components:
    - id: context
      type: repository_context
      config:
        include_project_instructions: true

    - id: execution
      type: sequential_execution
      config:
        max_iterations: 20

    - id: verification
      type: test_runner
      config:
        command: pytest

    - id: recovery
      type: bounded_repair
      config:
        max_attempts: 2

    - id: governance
      type: sandbox
      config:
        filesystem_scope: workspace

  policies:
    max_execution_time_seconds: 600
    max_tool_calls: 50
    allow_external_writes: false
```

This manifest is an execution artifact. It should be inspectable, reproducible, versioned, and independently validated.

The exact schema will be formalized during implementation.

---

## 9. Agent Interface

The harness must not depend on one specific agent framework.

For v0.1, define a minimal agent interface:

```python
class Agent:
    def invoke(self, context, state) -> AgentResponse:
        ...

class AgentResponse:
    message: str | None
    tool_calls: list
    final: bool
```

The agent receives the context assembled by the harness and returns a response containing either a tool request or a final answer.

The harness controls whether a requested action is allowed, how it is executed, and how the result is returned.

The first implementation will use one generic tool-using agent and one fixed model configuration.

---

## 10. Initial Task Families

To test whether harness composition is useful beyond one narrow example, the prototype should support three task families.

### Task A: Research

Example:

> Research the architecture of a technology and produce a source-backed summary.

Expected capabilities:

- Information retrieval.
- Parallel execution.
- Evidence collection.
- Source tracking.
- Output verification.

### Task B: Software Engineering

Example:

> Inspect a Python repository, identify a bug, implement a fix, and run the tests.

Expected capabilities:

- Repository context.
- Filesystem access.
- Shell execution.
- Test execution.
- Checkpointing.
- Bounded repair.
- Diff verification.

### Task C: Data Analysis

Example:

> Analyze a CSV file, calculate selected statistics, and produce a report.

Expected capabilities:

- File inspection.
- Data loading.
- Code execution.
- Result validation.
- Report generation.

These tasks are chosen to exercise substantially different execution requirements.

---

## 11. Safety and Execution Constraints

Harness construction must be constrained by a trusted runtime boundary.

The initial implementation will use:

- Explicit tool allowlists.
- Default-deny permissions.
- Sandboxed code execution.
- Configured filesystem boundaries.
- Execution timeouts.
- Maximum tool-call and iteration limits.
- Bounded retries.
- No unrestricted external writes.
- Explicit approval for sensitive operations.

A generated harness plan cannot grant itself additional permissions.

The planner may recommend a capability, but the runtime must enforce the actual permissions available to it.

Tool owners remain responsible for declaring their tool's effect and idempotency guarantees. The runtime must respect those contracts when executing or retrying operations.

---

## 12. Observability and Execution Records

Every run must produce an execution record.

Record at least:

- Task identifier and specification.
- Agent and model configuration.
- Selected harness components and versions.
- Original and compiled harness plans.
- Requirement analysis.
- Model invocations.
- Context supplied to the model.
- Tool requests and execution results.
- Permission decisions.
- Verification results.
- Recovery attempts.
- Token consumption.
- Execution duration.
- Estimated and actual cost where available.
- Final task outcome.

These records are necessary both for debugging and for evaluating whether automatic composition is beneficial.

The system should support replaying a run using the same task, agent configuration, and harness manifest where external dependencies permit reproducibility.

---

## 13. Evaluation Framework

The central hypothesis is:

> A task-specific, automatically composed harness can achieve the required task reliability with less unnecessary execution overhead than a fixed general-purpose harness.

We will compare three configurations.

| Configuration | Description |
|---|---|
| Fixed harness | One predefined harness used for all tasks. |
| Manual harness | A manually designed harness for each task family. |
| Composed harness | A harness automatically selected and configured by Harness Composer. |

### Metrics

**Effectiveness**

- Task success rate.
- Verification pass rate.
- Completion rate.

**Efficiency**

- Total tokens consumed.
- Model invocation count.
- Tool invocation count.
- Execution latency.
- Estimated execution cost.

**Reliability**

- Recovery success rate.
- Number of failed tool operations.
- Unnecessary retries.
- Invalid harness plans.
- Execution failures.

**Composition quality**

- Unnecessary components selected.
- Required components omitted.
- Component compatibility errors.
- Harness construction overhead.

### Important experimental controls

- Keep the model fixed across comparisons.
- Use the same agent implementation.
- Use the same task set.
- Keep task-specific evaluation criteria independent of the planner.
- Repeat tasks to account for model non-determinism.
- Include tasks not used to develop the component-selection rules.

The manually designed harness is an important baseline. The composer must demonstrate value beyond merely reproducing configurations that a developer could easily write by hand.

---

## 14. Implementation Plan

### Phase 1 — Minimal runtime

Build:

- Model adapter.
- Agent interface.
- Tool registry.
- Execution loop.
- Basic state management.
- Execution tracing.

**Deliverable:** A working agent that can use tools through a controlled runtime.

### Phase 2 — Component registry

Build:

- Component interface.
- Component metadata.
- Configuration validation.
- Dependency resolution.
- Initial component implementations.

**Deliverable:** A runtime that can instantiate a harness from a manually written manifest.

### Phase 3 — Harness compiler and validator

Build:

- Task specification schema.
- Requirement analyzer.
- Component-selection logic.
- Declarative manifest generation.
- Static compatibility validation.

**Deliverable:** A valid harness manifest generated from task requirements.

### Phase 4 — Harness Composer

Build:

- Planner.
- Compiler integration.
- Validation feedback.
- Harness instantiation.
- Execution integration.

**Deliverable:** A complete end-to-end system that automatically constructs and executes a harness.

### Phase 5 — Evaluation

Build:

- Benchmark task set.
- Fixed harness baseline.
- Manual harness baseline.
- Evaluation and metrics pipeline.
- Ablation experiments.

**Deliverable:** Evidence showing where automated composition helps, where it fails, and what overhead it introduces.

---

## 15. Acceptance Criteria for v0.1

The prototype is considered functionally complete when:

1. It accepts a valid agent profile and task specification.
2. It can identify task execution requirements.
3. It can select components from a predefined catalog.
4. It produces a valid declarative harness manifest.
5. It rejects incompatible or unsafe configurations.
6. It can instantiate and execute the generated harness.
7. It can execute tasks from all three initial task families.
8. It independently verifies task completion.
9. It supports bounded recovery.
10. It captures sufficient execution traces to reproduce and evaluate runs.

Performance improvements are an experimental objective, not a prerequisite for claiming that the prototype works.

---

## 16. Future Research Directions

The initial system deliberately uses a fixed component catalog. This creates a foundation for several more ambitious directions.

### A. Minimum sufficient harness

Find the least complex harness that achieves a specified task success threshold.

### B. Adaptive harness construction

Construct different harnesses based on task complexity, uncertainty, and execution risk.

### C. Runtime harness adaptation

Allow the harness to change during execution when new requirements or failure conditions emerge, while preserving safety constraints.

### D. Harness evolution

Use execution traces and evaluation results to improve component selection and configuration over time.

### E. Component synthesis

Allow the system to create new harness components when the existing catalog cannot satisfy a requirement. Generated components would need testing, sandboxing, validation, and versioning before registration.

### F. Cross-task learning

Determine whether successful harness patterns can be transferred to previously unseen tasks and domains.

These are extensions, not assumptions built into v0.1.

---

## 17. Key Design Principle

Harness Composer should not attempt to create the most sophisticated harness.

It should construct the **simplest harness that provides the required execution capabilities, satisfies the runtime constraints, and can independently establish task completion.**

The quality of the system will be determined not by how many components it can assemble, but by whether its choices produce measurably better execution.

---

**End of specification — v0.1**
