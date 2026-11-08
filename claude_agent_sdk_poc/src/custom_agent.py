import asyncio
from dotenv import load_dotenv
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

async def main():
    options = ClaudeAgentOptions(
        model="claude-sonnet-5",
        allowed_tools=["Read", "Glob", "Grep"],
        max_turns=8,
        cwd="/path/to/repo",
    )

    async for message in query(
        prompt="Summarize the open TODOs in this repo",
        options=options,
    ):
        if isinstance(message, ResultMessage) and not message.is_error:
            print(message.result)

load_dotenv()  # Load environment variables from .env file
asyncio.run(main())