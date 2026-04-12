"""Tools available to the Skillful Agent."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from google.adk.tools import ToolContext

from skillful_agent.skill_manager import SkillManager

# Absolute path to the reminders file — resolved relative to this module so it
# is always correct regardless of the process working directory.
_REMINDERS_PATH = Path(__file__).resolve().parent.parent.parent / "reminders.json"


def get_current_date() -> str:
    """Return today's date and current time.

    Use this instead of running shell commands when you need the current date
    or time. Returns a human-readable string with date, time, and day of week.
    """
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M (%A)")


def execute_bash_command(command: str) -> str:
    """Execute a bash command and return the output.

    Use for Linux/macOS shell commands. For Windows, prefer run_powershell.
    Do NOT use this to get the current date — call get_current_date() instead.

    Args:
        command: The shell command to execute.
    """
    print(f"\nRunning bash command: {command}\n")
    try:
        result = subprocess.run(
            command,
            shell=True,  # noqa: S602
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 30 seconds"
    except subprocess.CalledProcessError as exc:
        return f"Error executing command: {exc.stderr.strip()}"
    except Exception as exc:  # noqa: BLE001
        return f"An unexpected error occurred: {exc}"


def run_powershell(code: str) -> str:
    """Run PowerShell code and return the output.

    Use for Windows-specific commands or when PowerShell syntax is required.

    Args:
        code: The PowerShell code to execute.
    """
    print(f"\nRunning PowerShell command: {code}\n")
    process = subprocess.Popen(
        ["powershell", "-Command", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        output, error = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        return "Error: PowerShell command timed out after 30 seconds"
    if process.returncode != 0:
        return f"Error: {error}"
    return output


def save_reminder(
    task_name: str,
    reminder_date: str,
    reminder_time: str,
) -> str:
    """Save a reminder entry to the reminders store.

    Args:
        task_name: Description of the task to remember.
        reminder_date: Date in YYYY-MM-DD format.
        reminder_time: Time in HH:MM format (24-hour).
    """
    reminders: list[dict[str, str]] = []
    if _REMINDERS_PATH.exists():
        try:
            reminders = json.loads(_REMINDERS_PATH.read_text())
        except json.JSONDecodeError:
            pass

    reminders.append({"task": task_name, "date": reminder_date, "time": reminder_time})
    _REMINDERS_PATH.write_text(json.dumps(reminders, indent=4))

    return f"Reminder saved: '{task_name}' on {reminder_date} at {reminder_time}"


def list_reminders() -> str:
    """Read and return all saved reminders."""
    if not _REMINDERS_PATH.exists():
        return "No reminders found."

    try:
        reminders: list[dict[str, str]] = json.loads(_REMINDERS_PATH.read_text())
    except json.JSONDecodeError:
        return "No reminders found."

    if not reminders:
        return "No reminders found."

    lines = ["Your reminders:"]
    for i, r in enumerate(reminders, 1):
        lines.append(
            f"{i}. {r.get('task', 'Unknown')} — "
            f"{r.get('date', '?')} at {r.get('time', '?')}"
        )
    return "\n".join(lines)


def activate_skill(skill_name: str, tool_context: ToolContext) -> str:
    """Load a skill's full instructions into context (agentskills.io Tier 2).

    Call this when a user query matches a skill listed in the skill catalog.
    Returns the skill's complete instructions and available resources.
    The skill remains active for the rest of the session — no need to activate again.

    Args:
        skill_name: Exact name of the skill as listed in the catalog.
    """
    active: list[str] = tool_context.state.get("active_skills", [])

    if skill_name in active:
        return f"Skill '{skill_name}' is already active in this session."

    skill_manager: SkillManager = tool_context.state["_skill_manager"]
    body = skill_manager.load_skill_body(skill_name)

    if body is None:
        available = ", ".join(skill_manager.available_names())
        return (
            f"Skill '{skill_name}' not found. Available skills: {available or 'none'}"
        )

    entry = next(e for e in skill_manager.discover_skills() if e.name == skill_name)
    resources = skill_manager.list_skill_resources(skill_name)

    # Mark as active for deduplication
    tool_context.state["active_skills"] = [*active, skill_name]

    # Build structured response per agentskills.io spec
    resource_block = ""
    if resources:
        resource_lines = "\n".join(f"  <file>{r}</file>" for r in resources)
        resource_block = f"\n<skill_resources>\n{resource_lines}\n</skill_resources>"

    return (
        f'<skill_content name="{skill_name}">\n'
        f"{body}\n\n"
        f"Skill directory: {entry.skill_dir}\n"
        f"Relative paths in this skill are relative to the skill directory."
        f"{resource_block}\n"
        f"</skill_content>"
    )
