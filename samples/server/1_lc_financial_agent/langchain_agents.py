from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from dotenv import load_dotenv

load_dotenv()


@tool
def get_stock_price(ticker: str) -> str:
    """Returns the current price of a stock given its ticker (e.g. AAPL, TSLA)."""
    import yfinance as yf
    price = yf.Ticker(ticker).fast_info["last_price"]
    return f"The price of {ticker} is ${price:.2f}"


llm = ChatOpenAI(model="gpt-4o", temperature=0)

agent = create_agent(
    model=llm,
    tools=[get_stock_price],
    system_prompt="You are a financial assistant.",
    name="financial_agent_lc",
)

if __name__ == "__main__":
    result = agent.invoke({
        "messages": [{"role": "user", "content": "What is the price of AAPL?"}]
    })
    print(result["messages"][-1].content)
