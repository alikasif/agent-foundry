# Development Guidelines

This document contains critical information about working with this codebase. Follow these guidelines precisely.

## Core Development Rules

1. Package Management
   - ONLY use uv, NEVER pip
   - Installation: `uv add package`
   - Running tools: `uv run tool`
   - Upgrading: `uv add --dev package --upgrade-package package`
   - FORBIDDEN: `uv pip install`, `@latest` syntax

2. Code Quality
   - Type hints required for all code
   - use pyrefly for type checking
     - run `pyrefly init` to start
     - run `pyrefly check` after every change and fix resultings errors
   - Public APIs must have docstrings
   - Functions must be focused and small
   - Follow existing patterns exactly
   - Line length: 88 chars maximum

3. Testing Requirements
   - Framework: `uv run pytest`
   - Async testing: use anyio, not asyncio
   - Coverage: test edge cases and errors
   - New features require tests
   - Bug fixes require regression tests

4. Code Style
    - PEP 8 naming (snake_case for functions/variables)
    - Class names in PascalCase
    - Constants in UPPER_SNAKE_CASE
    - Document with docstrings
    - Use f-strings for formatting

## Development Philosophy

- **Simplicity**: Write simple, straightforward code
- **Readability**: Make code easy to understand
- **Performance**: Consider performance without sacrificing readability
- **Maintainability**: Write code that's easy to update
- **Testability**: Ensure code is testable
- **Reusability**: Create reusable components and functions
- **Less Code = Less Debt**: Minimize code footprint

## Coding Best Practices

- **Early Returns**: Use to avoid nested conditions
- **Descriptive Names**: Use clear variable/function names (prefix handlers with "handle")
- **Constants Over Functions**: Use constants where possible
- **DRY Code**: Don't repeat yourself
- **SOLID**: Use SOLID principles
- **Functional Style**: Prefer functional, immutable approaches when not verbose
- **Minimal Changes**: Only modify code related to the task at hand
- **Function Ordering**: Define composing functions before their components
- **TODO Comments**: Mark issues in existing code with "TODO:" prefix
- **Simplicity**: Prioritize simplicity and readability over clever solutions
- **Build Iteratively** Start with minimal functionality and verify it works before adding complexity
- **Run Tests**: Test your code frequently with realistic inputs and validate outputs
- **Build Test Environments**: Create testing environments for components that are difficult to validate directly
- **Functional Code**: Use functional and stateless approaches where they improve clarity
- **Clean logic**: Keep core logic clean and push implementation details to the edges
- **File Organsiation**: Balance file organization with simplicity - use an appropriate number of files for the project scale



## Pull Requests

- Create a detailed message of what changed. Focus on the high level description of
  the problem it tries to solve, and how it is solved. Don't go into the specifics of the
  code unless it adds clarity.

## Git Workflow

- Always use feature branches; do not commit directly to `main`
  - Name branches descriptively: `fix/auth-timeout`, `feat/api-pagination`, `chore/ruff-fixes`
  - Keep one logical change per branch to simplify review and rollback
- Create pull requests for all changes
  - Open a draft PR early for visibility; convert to ready when complete
  - Ensure tests pass locally before marking ready for review
  - Use PRs to trigger CI/CD and enable async reviews
- Link issues
  - Before starting, reference an existing issue or create one
  - Use commit/PR messages like `Fixes #123` for auto-linking and closure
- Commit practices
  - Make atomic commits (one logical change per commit)
  - Prefer conventional commit style: `type(scope): short description`
    - Examples: `feat(eval): group OBS logs per test`, `fix(cli): handle missing API key`
  - Squash only when merging to `main`; keep granular history on the feature branch
- Practical workflow
  1. Create or reference an issue
  2. `git checkout -b feat/issue-123-description`
  3. Commit in small, logical increments
  4. `git push` and open a draft PR early
  5. Convert to ready PR when functionally complete and tests pass
  6. Merge after reviews and checks pass

## Python Tools

- Always use context7 for library documentation before suggesting code for external frameworks. Use resolve-library-id first to get the correct version.


## Code Formatting

1. Ruff
   - Format: `uv run ruff format .`
   - Check: `uv run ruff check .`
   - Fix: `uv run ruff check . --fix`
   - Critical issues:
     - Line length (88 chars)
     - Import sorting (I001)
     - Unused imports
   - Line wrapping:
     - Strings: use parentheses
     - Function calls: multi-line with proper indent
     - Imports: split into multiple lines

2. Type Checking
  - run `pyrefly init` to start
  - run `pyrefly check` after every change and fix resultings errors
   - Requirements:
     - Explicit None checks for Optional
     - Type narrowing for strings
     - Version warnings can be ignored if checks pass


## Error Resolution

1. CI Failures
   - Fix order:
     1. Formatting
     2. Type errors
     3. Linting
   - Type errors:
     - Get full line context
     - Check Optional types
     - Add type narrowing
     - Verify function signatures

2. Common Issues
   - Line length:
     - Break strings with parentheses
     - Multi-line function calls
     - Split imports
   - Types:
     - Add None checks
     - Narrow string types
     - Match existing patterns

3. Best Practices
   - Check git status before commits
   - Run formatters before type checks
  - Keep changes minimal
  - Follow existing patterns
  - Document public APIs
  - Test thoroughly

## Agents & Orchestration
This repository defines a set of workflow agents under `.claude/agents/`. Use this file as a concise index for available agents and how they are invoked.

**Orchestration flow:**
```
software-team-orchestrator
  Phase 1 (mandatory): product-requirements-agent → writes <project_dir>/shared/prd.md → PRD_COMPLETE → user approval
  Phase 2 (optional):  design-architecture-agent  → writes <project_dir>/shared/designs.md → DESIGN_COMPLETE
  Phase 3 (mandatory): planning-subagent           → writes <project_dir>/shared/plan.md + task_list.json → user approval
  Phase 4 (mandatory): ralph-execution-engine      → dispatches specialists → RALPH_COMPLETE
```

Core agents

- `software-team-orchestrator`: Lead orchestrator. Runs 4 phases (requirements → optional design → planning → execution). All `shared/` files go inside `<project_dir>/shared/`.
- `product-requirements-agent`: Asks clarifying questions, writes `<project_dir>/shared/prd.md`, prints `PRD_COMPLETE`. Invoked by orchestrator Phase 1.
- `design-architecture-agent`: **Optional.** Writes `<project_dir>/shared/designs.md`, prints `DESIGN_COMPLETE`. Invoked by orchestrator Phase 2 only for complex/multi-service scope.
- `planning-subagent`: Researches codebase + PRD, **writes** `<project_dir>/shared/plan.md` and `<project_dir>/shared/task_list.json`. Invoked by orchestrator Phase 3.
- `ralph-execution-engine`: Execution loop. Reads `<project_dir>/shared/task_list.json`, dispatches specialists in parallel, resolves dependencies, writes `<project_dir>/shared/execution_summary.md`, prints `RALPH_COMPLETE`.
- `project-scaffolder`: Dispatched by ralph for `project_structure` tasks. Creates directories and stub configs. Local commits only — no git push.
- `python-backend-coder`: Dispatched by ralph for `python_coder` tasks. Implements Python source files. Local commits only.
- `python-test-runner`: Dispatched by ralph for `python_test` tasks. Writes and runs pytest tests (anyio for async).
- `docs-writer`: Dispatched by ralph for `documentation` tasks. Produces READMEs, API docs, user guides.
- `backend-code-reviewer`: Dispatched by ralph for `backend_reviewer` tasks. Reviews python_coder output. Writes results to `<project_dir>/shared/reviews/<task_id>_review.md`.
- `arch-reviewer`: Dispatched by ralph for `architecture_reviewer` tasks. Reviews overall design. Writes results to `<project_dir>/shared/reviews/<task_id>_arch_review.md`.

Notes

- **`git-remote-pusher` is excluded from all operations.** Never invoke it. Never mention it. Local commits are the only git operations allowed.
- All coordination files (`prd.md`, `designs.md`, `plan.md`, `task_list.json`, `learnings.md`, `execution_summary.md`, `reviews/`) live under `<project_dir>/shared/` — always project-local, never in a global directory.
- Agent definitions are stored in `.claude/agents/` as markdown files with YAML frontmatter.
- If you add or change agents, update `.claude/agents/<agent>.md` and this section in `CLAUDE.md`.
