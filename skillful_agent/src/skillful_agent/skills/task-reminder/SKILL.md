---
name: task-reminder
description: >
  Sets a task reminder with a name, date, and time. The reminder is written
  to a reminders.json file for later retrieval.
---

# Task Reminder Skill

Use this skill when a user asks to set a reminder or schedule a task for a future date and time.

## Workflow

1. Extract the following information from the user's request:
   - **Task name**: A short description of what to remember
   - **Date**: The date of the reminder (YYYY-MM-DD format)
   - **Time**: The time of the reminder (HH:MM format, 24-hour)

2. If the user did not provide a date or time, ask them for it before proceeding.

3. Validate the date and time formats. If invalid, inform the user and ask for corrections.

4. Call the `save_reminder` tool with parameters:
   - `task_name` (str) — the name/description of the task
   - `reminder_date` (str) — date in YYYY-MM-DD format
   - `reminder_time` (str) — time in HH:MM format

5. Confirm to the user that the reminder was saved successfully, showing the task name, date, and time.

6. If the user asks to view their reminders, call the `list_reminders` tool and display the results in a readable format.

## Examples

- **User**: "Remind me to submit expense report next Friday at 2pm"
  - Save: `task_name="Submit expense report"`, `reminder_date="2026-04-17"`, `reminder_time="14:00"`

- **User**: "Set a reminder for client meeting on April 20th at 10:30 AM"
  - Save: `task_name="Client meeting"`, `reminder_date="2026-04-20"`, `reminder_time="10:30"`

- **User**: "Show me my reminders"
  - Use `list_reminders` to retrieve and display all scheduled reminders.
