---
name: token-budget
description: >
  Manages a session token budget: set a max-token limit, view remaining budget,
  and reset the counter. The framework automatically injects current budget status
  into every LLM invocation so you can steer response length accordingly.
---

# Token Budget Skill

Use this skill when a user asks to set, view, or reset a token budget for the session.

## How the budget system works

The framework prepends a `<budget_status>` block to every user message once a budget
is configured. This block shows tokens used, total limit, and remaining tokens. You do
not need to fetch this information manually — it arrives automatically with each turn.

When you see a `<budget_status>` block in a user message:
- Acknowledge the budget situation if it is relevant (e.g., budget is low)
- When remaining tokens < 20% of the limit: keep your response concise and warn the user
- When remaining tokens = 0: inform the user the budget is exhausted and suggest they
  reset it or set a higher limit

## Workflow

1. **Set a budget** — when the user asks to enable or set a token budget:

   Run `set_budget.py` with the desired limit (number of tokens):

   **On Windows** — use `run_powershell`:
   ```
   uv run python "<skill_dir>/scripts/set_budget.py" MAX_TOKENS
   ```

   **On Linux/macOS** — use `execute_bash_command`:
   ```
   uv run python "<skill_dir>/scripts/set_budget.py" MAX_TOKENS
   ```

   Replace `MAX_TOKENS` with the numeric limit (e.g., `50000`).
   Echo the exact stdout from the script to the user.

2. **View budget status** — when the user asks to check how much budget remains:

   **On Windows** — use `run_powershell`:
   ```
   uv run python "<skill_dir>/scripts/view_budget.py"
   ```

   **On Linux/macOS** — use `execute_bash_command`:
   ```
   uv run python "<skill_dir>/scripts/view_budget.py"
   ```

   Echo the exact stdout to the user verbatim.

3. **Reset the counter** — when the user asks to reset the token counter (keeps the
   same limit, resets used count to zero):

   **On Windows** — use `run_powershell`:
   ```
   uv run python "<skill_dir>/scripts/reset_budget.py"
   ```

   **On Linux/macOS** — use `execute_bash_command`:
   ```
   uv run python "<skill_dir>/scripts/reset_budget.py"
   ```

   Echo the exact stdout to the user verbatim.

## Examples

- **User**: "Set a token budget of 50,000 tokens"
  - Run: `uv run python "<skill_dir>/scripts/set_budget.py" 50000`

- **User**: "How much of my token budget is left?"
  - Run: `uv run python "<skill_dir>/scripts/view_budget.py"` and paste output verbatim

- **User**: "Reset my token counter"
  - Run: `uv run python "<skill_dir>/scripts/reset_budget.py"` and paste output verbatim

- **Ongoing**: You see `<budget_status>` blocks in user messages — read them and adjust
  response length and depth accordingly. No tool call needed.
