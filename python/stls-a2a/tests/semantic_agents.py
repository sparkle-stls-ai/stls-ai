import asyncio
from semantic_kernel import Kernel
from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion
from semantic_kernel.functions import kernel_function
from dotenv import load_dotenv

load_dotenv()

# ── 1. Plugin con tools ───────────────────────────────────────────────────────

class FinancePlugin:
    """Plugin finanziario con tools per recuperare dati di mercato."""

    @kernel_function(
        name="get_stock_price",
        description="Restituisce il prezzo corrente di un'azione dato il ticker (es. AAPL, TSLA)."
    )
    def get_stock_price(self, ticker: str) -> str:
        import yfinance as yf
        t = yf.Ticker(ticker)
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
        return f"Impossibile recuperare il prezzo di {ticker}."

    @kernel_function(
        name="get_company_info",
        description="Restituisce informazioni sull'azienda dato il ticker."
    )
    def get_company_info(self, ticker: str) -> str:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return f"{info.get('longName')} - Settore: {info.get('sector')} - {info.get('longBusinessSummary', '')[:200]}"


# ── 2. Kernel + Service ───────────────────────────────────────────────────────

kernel = Kernel()

kernel.add_service(OpenAIChatCompletion(
    service_id="financial_agent",
    ai_model_id="gpt-4o",
))

kernel.add_plugin(FinancePlugin(), plugin_name="Finance")

# ── 3. Agent ──────────────────────────────────────────────────────────────────

agent = ChatCompletionAgent(
    kernel=kernel,
    name="financial_agent",
    instructions="Sei un assistente finanziario. Usa i tools disponibili per rispondere.",
)

# ── 4. Invoca ─────────────────────────────────────────────────────────────────

async def main():
    from semantic_kernel.contents import ChatHistory

    history = ChatHistory()
    history.add_user_message("Qual è il prezzo di AAPL?")

    response = await agent.get_response(messages=history)
    print(response.content)

    # oppure con streaming
    # async for chunk in agent.invoke_stream(messages=history):
    #     print(chunk.content, end="", flush=True)

if __name__ == "__main__":
    asyncio.run(main())