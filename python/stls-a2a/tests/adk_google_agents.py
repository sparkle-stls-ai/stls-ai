from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# ── 1. Tool ───────────────────────────────────────────────────────────────────

def get_stock_price(ticker: str) -> str:
    """Restituisce il prezzo corrente di un'azione dato il ticker (es. AAPL, TSLA)."""
    import yfinance as yf
    
    t = yf.Ticker(ticker)
    
    # prova più metodi in cascata
    try:
        price = t.info.get("currentPrice") or t.info.get("regularMarketPrice")
        if price:
            return f"Il prezzo di {ticker} è ${price:.2f}"
    except Exception:
        pass

    try:
        hist = t.history(period="1d")
        if not hist.empty:
            price = hist["Close"].iloc[-1]
            return f"Il prezzo di {ticker} è ${price:.2f}"
    except Exception:
        pass

    return f"Impossibile recuperare il prezzo di {ticker} al momento."

tools = [FunctionTool(get_stock_price)]

# ── 2. Agent ──────────────────────────────────────────────────────────────────

agent = LlmAgent(
    name="financial_agent",
    model="gemini-2.5-flash",
    description="Assistente finanziario con accesso ai prezzi azionari.",
    instruction="Sei un assistente finanziario. Usa i tools disponibili per rispondere.",
    tools=tools,
)

# ── 3. Runner + Session ───────────────────────────────────────────────────────

session_service = InMemorySessionService()

runner = Runner(
    agent=agent,
    app_name="financial_agent",
    session_service=session_service,
)

# ── 4. Invoca ─────────────────────────────────────────────────────────────────

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
                parts=[types.Part(text="Qual è il prezzo di AAPL?")]
            ),
        )

        for event in response:
            if event.is_final_response():
                print(event.content.parts[0].text)

    asyncio.run(main())