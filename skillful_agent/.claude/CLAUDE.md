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
This repository defines a set of workflow agents under `.claude/agents/`. Use this file as a concise index for available agents and how they are typically invoked by the `software-team-orchestrator`.

Core agents

- `software-team-orchestrator`: Lead orchestrator that coordinates research, requirements clarification, planning, scaffolding, specialist agents, reviewers, and completion phases.
- `product-requirements-agent`: Gathers functional and non-functional requirements by asking clarifying questions, produces a repository-level PRD at `shared/product_requirements.md`, and flags open questions for the Lead Agent. Invoked by `software-team-orchestrator` as Phase 1.5; the orchestrator pauses for explicit PRD approval before proceeding to planning.
- `design-architecture-agent`: Produces flow designs, component diagrams, data model sketches, and architecture tradeoffs. Runs after `product-requirements-agent` and before `planning-subagent` to provide a concrete design input for planning.
- `planning-subagent`: Researches the codebase, identifies modules, maps dependencies, and produces structured findings for planning.
- `project-scaffolder`: Creates initial project scaffolds and writes `shared/project_structure.json`.
- `python-backend-coder`: Implements backend services and outputs API contracts to `shared/api/`.
- `python-test-runner`: Writes and runs tests for assigned modules (uses anyio for async tests).
- `docs-writer`: Produces documentation, READMEs, and API docs.
- `backend-code-reviewer`: Performs code reviews for backend changes.
- `arch-reviewer`: Reviews architectural decisions when multiple code agents are active.
- `git-remote-pusher`: Handles remote git pushes for feature branches and milestones.
- `ralph-execution-engine`: Optional execution engine for orchestrating many specialist agents.

Notes

- Agent definitions are stored in `.claude/agents/` as markdown files with YAML frontmatter.
- Per-agent persistent memory is stored under `.claude/agent-memory/<agent-name>/`.
- The `product-requirements-agent` writes the PRD to `shared/product_requirements.md` and should commit using a conventional commit message: `docs: add product requirements PRD` on a feature branch created for the request.
- The `design-architecture-agent` writes flow and architecture artifacts to `shared/designs.md` or feature-specific files under `shared/designs/` and lists open architecture decisions.
- If you add or change agents, update `.claude/agents/<agent>.md` and this section in `CLAUDE.md`.
