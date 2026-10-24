from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage
from langchain.agents import create_agent  # ← new, correct import
from dotenv import load_dotenv

#import inspect
#print(inspect.signature(create_agent))

load_dotenv()

@tool
def get_stock_price(ticker: str) -> str:
    """Returns the current price of a stock given its ticker (e.g. AAPL, TSLA)."""
    import yfinance as yf
    price = yf.Ticker(ticker).fast_info["last_price"]
    return f"The price of {ticker} is ${price:.2f}"

tools = [get_stock_price]

llm = ChatOpenAI(model="gpt-4o", temperature=0)

agent = create_agent(
    model=llm,
    tools=tools,
    system_prompt="You are a financial assistant."
)


if __name__ == "__main__":
    result = agent.invoke({
        "messages": [{"role": "user", "content": "What is the price of AAPL?"}]
    })
    print(result["messages"][-1].content)