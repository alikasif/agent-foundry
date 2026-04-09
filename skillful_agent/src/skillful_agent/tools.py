import os
import subprocess


def execute_bash_command(command: str) -> str:
    """
    Executes a bash command and returns the output.

    WARNING: Uses shell=True, which allows complex shell syntax (pipes, redirects)
    but is vulnerable to shell injection if untrusted input reaches this function.
    In production, ensure only sanitized, LLM-generated commands are passed here.
    """
    print(f"\n\nRunning bash command: {command}\n\n")
    try:
        # Using shell=True to allow complex commands (pipes, redirects, etc.)
        # Note: In a production environment, this requires strict sanitation.
        result = subprocess.run(
            command,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f"Error executing command: {e.stderr.strip()}"
    except Exception as e:
        return f"An unexpected error occurred: {str(e)}"


def run_powershell(code: str) -> str:
    """Runs PowerShell code and returns the output."""

    print(f"\n\nRunning PowerShell command: {code}\n\n")
    
    process = subprocess.Popen(
        ["powershell", "-Command", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output, error = process.communicate()

    if process.returncode != 0:
        return f"Error: {error}"

    return output


def save_reminder(
    task_name: str,
    reminder_date: str,
    reminder_time: str,
    file_path: str = "reminders.json",
) -> str:
    """
    Writes a reminder entry to a JSON file.

    Args:
        task_name: Description of the task to remember.
        reminder_date: Date in YYYY-MM-DD format.
        reminder_time: Time in HH:MM format (24-hour).
        file_path: Path to the JSON file storing reminders.

    Returns:
        Confirmation message with the saved reminder details.
    """
    import json

    reminders = []
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                reminders = json.load(f)
        except json.JSONDecodeError:
            pass

    reminder = {
        "task": task_name,
        "date": reminder_date,
        "time": reminder_time,
    }
    reminders.append(reminder)

    with open(file_path, "w") as f:
        json.dump(reminders, f, indent=4)

    return f"Reminder saved: '{task_name}' on {reminder_date} at {reminder_time}"


def list_reminders(file_path: str = "reminders.json") -> str:
    """
    Reads and returns all reminders from the JSON file.

    Args:
        file_path: Path to the JSON file storing reminders.

    Returns:
        Formatted string of all reminders, or a message if none exist.
    """
    import json

    if not os.path.exists(file_path):
        return "No reminders found."

    try:
        with open(file_path, "r") as f:
            reminders = json.load(f)
    except json.JSONDecodeError:
        return "No reminders found."

    if not reminders:
        return "No reminders found."

    lines = ["Your reminders:"]
    for i, r in enumerate(reminders, 1):
        lines.append(f"{i}. {r.get('task', 'Unknown')} - {r.get('date', '?')} at {r.get('time', '?')}")
    return "\n".join(lines)
