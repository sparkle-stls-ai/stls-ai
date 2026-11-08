import re

from stls_a2a import AgentResponse

# Tokens that look like a ticker (1-5 uppercase letters) but are common
# words that show up in a typical English query — excluded from the
# fallback scan below so e.g. "What is the price of AAPL?" doesn't match "IS".
_STOPWORDS = {
    "THE", "FOR", "WHAT", "IS", "OF", "PRICE", "STOCK", "AND", "ARE",
    "YOU", "CAN", "TELL", "ME", "A", "AN", "TO", "AT", "ON", "IN",
    "GET", "SHOW", "CURRENT", "TODAY", "NOW", "PLEASE", "HI", "HELLO",
}

_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
_TICKER_KV_RE = re.compile(r"ticker\s*[:=]\s*([A-Za-z]{1,5})\b", re.IGNORECASE)
_UPPER_TOKEN_RE = re.compile(r"\b[A-Z]{1,5}\b")


class FinancialAgent:
    """
    Deterministic, LLM-free financial agent: plain Python class, no model
    call, no API key. Looks up a stock ticker's current price via
    `yfinance` and formats a plain-text answer.

    Instances are directly callable — `async def __call__(self, request)` —
    which is exactly the `invoke_func` shape stls_a2a.CustomFunctionAdapter
    expects, so `agent=financial_agent` can be passed straight to
    create_secure_a2a_endpoint(framework='custom') in server.py. Passing the
    instance (not a bare function) also means stls_a2a's generic agent-card
    builder picks up `.name`/`.description` below instead of falling back to
    the Python type name ("function").
    """

    def __init__(self) -> None:
        self.name = "financial_agent_custom"
        self.description = (
            "Deterministic, LLM-free financial agent — regex-based ticker "
            "extraction + a live yfinance lookup. No model call, no API key."
        )

    async def __call__(self, request) -> AgentResponse:
        """
        Entry point used by stls_a2a.CustomFunctionAdapter: adapts the raw
        A2A request into a call to handle(), and the plain-text result back
        into an AgentResponse.
        """
        text = ""
        parts = (request.params or {}).get("message", {}).get("parts", [])
        for p in parts:
            if not isinstance(p, dict):
                continue
            # The A2A spec field is "kind" (e.g. {"kind": "text", ...}); some
            # clients/SDKs send "type" instead (or both) — check either.
            if (p.get("kind") or p.get("type")) == "text":
                text = p.get("text", "")
                break
        return AgentResponse(result=self.handle(text), id=request.id)

    def extract_ticker(self, text: str) -> str | None:
        """
        Best-effort, regex-only ticker extraction from free text. Tries,
        in order:
          1. A cashtag ("$AAPL")
          2. An explicit "ticker: AAPL" / "ticker=AAPL"
          3. The last 1-5 letter all-caps token that isn't a common word
             (e.g. "What is the price of AAPL?" -> "AAPL")
        Returns None if nothing looks like a ticker.
        """
        m = _CASHTAG_RE.search(text)
        if m:
            return m.group(1).upper()

        m = _TICKER_KV_RE.search(text)
        if m:
            return m.group(1).upper()

        candidates = [
            tok for tok in _UPPER_TOKEN_RE.findall(text)
            if tok not in _STOPWORDS
        ]
        return candidates[-1] if candidates else None

    def get_stock_price(self, ticker: str) -> str:
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

    def handle(self, text: str) -> str:
        """Entry point used by the A2A handler: text in, text answer out."""
        ticker = self.extract_ticker(text)
        if not ticker:
            return (
                "I can look up a stock price — ask me something like "
                "'What is the price of AAPL?' or '$TSLA', or send 'ticker: MSFT'."
            )
        return self.get_stock_price(ticker)


agent = FinancialAgent()


if __name__ == "__main__":
    print(agent.handle("What is the price of AAPL?"))
