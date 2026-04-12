"""SkillfulAgent — general-purpose agent with progressive skill disclosure."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from google.adk.agents import Agent
from google.adk.events import Event
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from skillful_agent.prompts import format_system_prompt
from skillful_agent.skill_manager import SkillManager
from skillful_agent.tools import (
    activate_skill,
    execute_bash_command,
    get_current_date,
    list_reminders,
    run_powershell,
    save_reminder,
)


class SkillfulAgent:
    """General-purpose agent with agentskills.io progressive skill disclosure.

    Skills are surfaced to the model as a Tier-1 catalog at session start.
    When the model calls ``activate_skill``, the full SKILL.md body is returned
    as a tool result (Tier 2) and stays in conversation history for the session.
    """

    def __init__(self) -> None:
        self.skill_manager = SkillManager()
        self._session_service = InMemorySessionService()
        self._agent = self._build_agent()
        self._runner: Runner | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_session(self, user_id: str = "user_1") -> str:
        """Create a new session and return its ID.

        The SkillManager instance is stored in session state so that
        ``activate_skill`` can access it without a global variable.

        Args:
            user_id: Identifier for the user.
        """
        session = await self._session_service.create_session(
            app_name="skillful_agent_app",
            user_id=user_id,
            state={
                "active_skills": [],
                "_skill_manager": self.skill_manager,
            },
        )
        return session.id

    async def event_stream(
        self, text: str, user_id: str, session_id: str
    ) -> AsyncIterator[Event]:
        """Yield raw ADK events for a user message.

        Callers can consume this directly for custom display logic.
        ``query()`` is a convenience wrapper over this generator.

        Args:
            text: The user's input text.
            user_id: Session owner identifier.
            session_id: Active session ID from create_session().
        """
        runner = self._get_runner()
        content = types.Content(role="user", parts=[types.Part(text=text)])
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            yield event

    async def query(self, text: str, user_id: str, session_id: str) -> str:
        """Send a user message and return the agent's final response.

        Convenience wrapper over ``event_stream()``. Use ``event_stream()``
        directly when you need to observe intermediate tool calls.

        Args:
            text: The user's input text.
            user_id: Session owner identifier.
            session_id: Active session ID from create_session().
        """
        final_response = "Agent did not produce a response."
        async for event in self.event_stream(text, user_id, session_id):
            if event.is_final_response():
                if event.content and event.content.parts:
                    final_response = event.content.parts[0].text or final_response
                break
        return final_response

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_agent(self) -> Agent:
        catalog = self.skill_manager.build_catalog_text()
        return Agent(
            name="Skillful_Agent",
            model=self._get_model(),
            instruction=format_system_prompt(catalog),
            tools=[
                get_current_date,
                execute_bash_command,
                run_powershell,
                save_reminder,
                list_reminders,
                activate_skill,
            ],
        )

    def _get_runner(self) -> Runner:
        if self._runner is None:
            self._runner = Runner(
                agent=self._agent,
                app_name="skillful_agent_app",
                session_service=self._session_service,
            )
        return self._runner

    def _get_model(self) -> LiteLlm:
        return LiteLlm(
            model=os.environ["SKILLFUL_MODEL"],
            api_key=os.environ["OPENROUTER_API_KEY"],
        )
