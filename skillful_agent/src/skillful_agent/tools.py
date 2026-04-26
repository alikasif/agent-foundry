"""Tools available to the Skillful Agent."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import ToolContext
from google.genai import types

from skillful_agent.skill_manager import SkillManager

logger = logging.getLogger(__name__)


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


async def _run_agent_skill(
    skill_name: str,
    body: str,
    task_description: str,
) -> str:
    """Spawn an ephemeral ADK sub-agent to execute an agent-mode skill.

    The sub-agent receives the skill body as its instruction and
    ``task_description`` as the user message.  It has access to
    execute_bash_command, run_powershell, and get_current_date — but NOT
    activate_skill, preventing recursive delegation.

    Args:
        skill_name: Human-readable name used for session/agent labels.
        body: Full SKILL.md body used as the sub-agent instruction.
        task_description: The user's task prompt forwarded to the sub-agent.

    Returns:
        The sub-agent's final response text, or a descriptive error string.
    """
    try:
        safe_name = re.sub(r"[^a-zA-Z0-9]", "_", skill_name)
        model = LiteLlm(
            model=os.environ.get(
                "SKILLFUL_MODEL", ""
            ),
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        )
        sub_agent = Agent(
            name=f"skill_agent_{safe_name}",
            model=model,
            instruction=body,
            tools=[execute_bash_command, run_powershell, get_current_date],
        )
        session_service = InMemorySessionService()
        app_name = f"skill_runner_{safe_name}"
        session = await session_service.create_session(
            app_name=app_name,
            user_id="skill_runner",
            session_id=f"session_{safe_name}",
        )
        runner = Runner(
            agent=sub_agent,
            app_name=app_name,
            session_service=session_service,
        )
        user_content = types.Content(
            role="user",
            parts=[types.Part(text=task_description)],
        )
        final_text: str | None = None
        async for event in runner.run_async(
            user_id=session.user_id,
            session_id=session.id,
            new_message=user_content,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(
                    p.text
                    for p in event.content.parts
                    if hasattr(p, "text") and isinstance(p.text, str)
                )
        if final_text is None:
            return f"Skill '{skill_name}' sub-agent returned no response."
        return final_text
    except Exception as exc:  # noqa: BLE001
        logger.error("Agent-mode skill '%s' failed: %s", skill_name, exc)
        return f"Error running skill '{skill_name}' as agent: {exc}"


async def activate_skill(
    skill_name: str,
    task_description: str,
    tool_context: ToolContext,
) -> str:
    """Load or execute a skill by name (agentskills.io Tier 2).

    For ``inline`` skills: loads the full SKILL.md body into context as a
    ``<skill_content>`` XML block and marks the skill active for the session.
    Call once per session — subsequent calls return an "already active" notice.

    For ``agent`` skills: spawns an ephemeral sub-agent pre-loaded with the
    skill's instructions and the three base tools (execute_bash_command,
    run_powershell, get_current_date).  ``task_description`` is forwarded as
    the user prompt.  The skill is NOT marked active — it can be called
    multiple times for different tasks.

    Args:
        skill_name: Exact name of the skill as listed in the catalog.
        task_description: The task to delegate.  Ignored for inline skills;
            used as the user prompt for agent-mode skills.
        tool_context: ADK tool context providing session state.
    """
    skill_manager: SkillManager = tool_context.state["_skill_manager"]
    body = skill_manager.load_skill_body(skill_name)

    if body is None:
        available = ", ".join(skill_manager.available_names())
        return (
            f"Skill '{skill_name}' not found. Available skills: {available or 'none'}"
        )

    entry = next(
        (e for e in skill_manager.discover_skills() if e.name == skill_name),
        None,
    )
    if entry is None:
        # Should not happen given body is not None, but guard defensively
        return f"Skill '{skill_name}' not found."

    if entry.mode == "agent":
        return await _run_agent_skill(skill_name, body, task_description)

    # --- inline mode ---
    active: list[str] = tool_context.state.get("active_skills", [])
    if skill_name in active:
        return f"Skill '{skill_name}' is already active in this session."

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
