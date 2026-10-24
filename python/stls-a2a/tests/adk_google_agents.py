from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# ── 1. Tool ───────────────────────────────────────────────────────────────────

def get_stock_price(ticker: str) -> str:
    """Returns the current price of a stock given its ticker (e.g. AAPL, TSLA)."""
    import yfinance as yf

    t = yf.Ticker(ticker)

    # try multiple methods in cascade
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

    return f"Could not retrieve the price of {ticker} at this time."

tools = [FunctionTool(get_stock_price)]

# ── 2. Agent ──────────────────────────────────────────────────────────────────

agent = LlmAgent(
    name="financial_agent",
    model="gemini-2.5-flash",
    description="Financial assistant with access to stock prices.",
    instruction="You are a financial assistant. Use the available tools to answer.",
    tools=tools,
)

# ── 3. Runner + Session ───────────────────────────────────────────────────────

session_service = InMemorySessionService()

runner = Runner(
    agent=agent,
    app_name="financial_agent",
    session_service=session_service,
)

# ── 4. Invoke ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    async def main():
        session = await session_service.create_session(
            app_name="financial_agent",
            user_id="user-1",
        )

        response = runner.run(
            user_id="user-1",
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="What is the price of AAPL?")]
            ),
        )

        for event in response:
            if event.is_final_response():
                print(event.content.parts[0].text)

    asyncio.run(main())