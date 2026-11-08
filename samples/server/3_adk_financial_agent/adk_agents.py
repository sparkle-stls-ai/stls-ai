import os

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

load_dotenv()

# LLM model passed in externally (AGENT_MODEL environment variable), same
# convention as samples/server/5_adk_doc_gen.
DEFAULT_AGENT_MODEL = "gemini-3.5-flash"
AGENT_MODEL = (os.getenv("AGENT_MODEL") or DEFAULT_AGENT_MODEL).strip()


def get_stock_price(ticker: str) -> str:
    """Returns the current price of a stock given its ticker (e.g. AAPL, TSLA)."""
    import yfinance as yf

    t = yf.Ticker(ticker)

    try:
        price = t.info.get("currentPrice") or t.info.get("regularMarketPrice")
        if price:
            return f"The price of {ticker} is ${price:.2f}"
    except Exception:
        pass

    try:
        hist = t.history(period="1d")
        if not hist.empty:
            price = hist["Close"].iloc[-1]
            return f"The price of {ticker} is ${price:.2f}"
    except Exception:
        pass

    return f"Could not retrieve the price of {ticker} right now."


tools = [FunctionTool(get_stock_price)]

agent = LlmAgent(
    name="financial_agent_adk",
    model=AGENT_MODEL,
    description="Financial assistant with access to stock prices.",
    instruction="You are a financial assistant. Use the available tools to answer.",
    tools=tools,
)


if __name__ == "__main__":
    import asyncio

    from google.adk.runners import InMemoryRunner
    from google.genai import types

    async def main():
        runner = InMemoryRunner(agent=agent, app_name="financial_agent")
        session = await runner.session_service.create_session(
            app_name="financial_agent",
            user_id="user-1",
        )

        async for event in runner.run_async(
            user_id="user-1",
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="What is the price of AAPL?")],
            ),
        ):
            if event.is_final_response():
                print(event.content.parts[0].text)

    asyncio.run(main())
