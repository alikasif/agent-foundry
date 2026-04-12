---
name: task-reminder
description: >
  Sets a task reminder with a name, date, and time. The reminder is written
  to a reminders.json file for later retrieval.
---

# Task Reminder Skill

Use this skill when a user asks to set a reminder or schedule a task for a future date and time.

## Workflow

> **Note:** The `task-reminder` skill uses standalone scripts — do NOT call `save_reminder` or `list_reminders` as built-in tools. Instead, invoke the scripts via shell as described below.

1. Extract the following information from the user's request:
   - **Task name**: A short description of what to remember
   - **Date**: The date of the reminder (YYYY-MM-DD format)
   - **Time**: The time of the reminder (HH:MM format, 24-hour)

2. If the user did not provide a date or time, ask them for it before proceeding.

3. Validate the date and time formats. If invalid, inform the user and ask for corrections.

4. Run the save script to store the reminder. The `<skill_dir>` path is shown in the
   `<skill_content>` block returned when this skill was activated (look for
   `Skill directory: <path>`).

   **On Windows** — use `run_powershell`:
   ```
   uv run python "<skill_dir>/scripts/save_reminder.py" "<task_name>" "<reminder_date>" "<reminder_time>"
   ```

   **On Linux/macOS** — use `execute_bash_command`:
   ```
   uv run python "<skill_dir>/scripts/save_reminder.py" "<task_name>" "<reminder_date>" "<reminder_time>"
   ```

   Replace `<task_name>`, `<reminder_date>`, and `<reminder_time>` with the actual values.
   Quote all arguments to handle spaces in task names.

5. Confirm to the user that the reminder was saved successfully, showing the task name, date, and time.

6. If the user asks to view their reminders, run the list script and display the results:

   **On Windows** — use `run_powershell`:
   ```
   uv run python "<skill_dir>/scripts/list_reminders.py"
   ```

   **On Linux/macOS** — use `execute_bash_command`:
   ```
   uv run python "<skill_dir>/scripts/list_reminders.py"
   ```

## Examples

- **User**: "Remind me to submit expense report next Friday at 2pm"
  - Run: `uv run python "<skill_dir>/scripts/save_reminder.py" "Submit expense report" "2026-04-17" "14:00"`

- **User**: "Set a reminder for client meeting on April 20th at 10:30 AM"
  - Run: `uv run python "<skill_dir>/scripts/save_reminder.py" "Client meeting" "2026-04-20" "10:30"`

- **User**: "Show me my reminders"
  - Run: `uv run python "<skill_dir>/scripts/list_reminders.py"` and display the output to the user.
