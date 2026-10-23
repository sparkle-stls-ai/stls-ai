import os

from dotenv import load_dotenv

load_dotenv()

from google.adk.agents import Agent

SERVER_LABEL = os.getenv("SERVER_LABEL", "server-ms-2")


def ping(message: str) -> dict:
    """Returns an echo tagged with the label of the server that answered."""
    return {"server": SERVER_LABEL, "echo": message}


agent = Agent(
    name=SERVER_LABEL.replace("-", "_"),
    model="gemini-2.5-flash",
    description=f"Minimal diagnostic agent for {SERVER_LABEL} (multi-server test).",
    instruction=(
        "You are a diagnostic test agent.\n"
        "Step 1: ALWAYS call the `ping` tool, passing the user's message as `message`.\n"
        "Step 2: reply with ONLY the exact JSON object returned by the tool — "
        "no markdown, no extra text."
    ),
    tools=[ping],
)
