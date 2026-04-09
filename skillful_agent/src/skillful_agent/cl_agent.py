import os
import asyncio
import logging
import json
import platform
import pathlib

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from prompts import COMMAND_GENERATION_SYSTEM_PROMPT, FEEDBACK_SYSTEM_PROMPT
from tools import (
    execute_bash_command,
    run_powershell,
    save_reminder,
    list_reminders,
)


logging.basicConfig(level=logging.INFO) # Set the root logger level to INFO
MAX_TURNS = 2


class CommandLineAgent:

    def __init__(self, model_prefix="GEMINI", history_file="history.json"):
        self.model_prefix = model_prefix
        self.history_file = history_file
        
        print(f"Using model: {self.model_prefix}")
        print(f"Using key {os.getenv('GEMINI_API_KEY')}")


    def get_model(self, prefix= None):        

        os.environ["OPENROUTER_API_KEY"] = os.getenv("OPENROUTER_API_KEY")
        model = LiteLlm(model=os.getenv("SKILLFUL_MODEL"), 
                        api_key=os.getenv("OPENROUTER_API_KEY"), 
                        base_url=os.getenv("OPENROUTER_BASE_URL"))
        return model


    def load_skills(self):
        """Load all skills found under the top-level `skills/` directory.

        Returns a list of `SkillToolset` objects (one per skill directory).
        """
        skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
        toolsets = []
        if not skills_dir.is_dir():
            return toolsets

        # Iterate subdirectories and try to load each skill directory
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir():
                continue
            try:
                skill = load_skill_from_dir(child)
                toolsets.append(SkillToolset(skills=[skill]))
            except Exception as e:
                logging.warning("Skipping skill at %s: %s", child, e)

        return toolsets
    
    def get_agent(self):
        tools = [execute_bash_command, run_powershell, save_reminder, list_reminders]

        # Load all discovered skills and add them as toolsets
        skills_toolsets = self.load_skills()
        if skills_toolsets:
            tools.extend(skills_toolsets)

        return Agent(
            name="Command_Line_Agent",
            model=self.get_model(self.model_prefix),
            instruction=COMMAND_GENERATION_SYSTEM_PROMPT,
            tools=tools,
        )


    def setup_session(self):
        session_service_gpt = InMemorySessionService()
        session_gpt = asyncio.run(session_service_gpt.create_session(
            app_name="cl_agent_app",
            user_id="user_1",
            session_id="session_gpt"
        ))
        return session_service_gpt
        

    def setup_runner(self):
        runner_gpt = Runner(
        agent=self.get_agent(),
        app_name="cl_agent_app",
        session_service=self.setup_session(),
        )
        return runner_gpt


    def generate_command(self, user_input: str) -> str:
        """
        Generates a bash command from user input using LiteLLM.
        """
        current_os = platform.system()
        runner = self.setup_runner()  # Ensure runner is set up before calling the agent
        try:
            result = asyncio.run(
                self.call_agent_async(
            f"User: {user_input}\nOperating System: {current_os}\n", 
            runner,
            "user_1",
            "session_gpt"
            ))
            return result
        except Exception as e:
            print(f"Error generating command: {e}")
            return "Error generating command"



    async def call_agent_async(self, query: str, runner, user_id, session_id):

        """Sends a query to the agent and prints the final response."""
        print(f"\n>>> User Query: {query}")

        # Prepare the user's message in ADK format
        content = types.Content(role='user', parts=[types.Part(text=query)])
        
        final_response_text = "Agent did not produce a final response."
        
        # Execute the agent and find the final response
        async for event in runner.run_async(
            user_id=user_id, 
            session_id=session_id, 
            new_message=content
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    final_response_text = event.content.parts[0].text
                break
                
        print(f"<<< Agent Response: {final_response_text}")
        return final_response_text



    def learn(self, user_input: str, generated_command: str, feedback: str):
        """
        Records the interaction and feedback to a history file.
        """
        interaction = {
            "input": user_input,
            "command": generated_command,
            "feedback": feedback
        }

        # Load existing history
        history = []
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r") as f:
                    history = json.load(f)
            except json.JSONDecodeError:
                pass # Start fresh if file is corrupt

        history.append(interaction)

        # Save updated history
        with open(self.history_file, "w") as f:
            json.dump(history, f, indent=4)
