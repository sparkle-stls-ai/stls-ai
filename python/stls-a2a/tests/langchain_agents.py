from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage
from langchain.agents import create_agent  # ← nuovo import corretto
from dotenv import load_dotenv

#import inspect
#print(inspect.signature(create_agent))

load_dotenv()

@tool
def get_stock_price(ticker: str) -> str:
    """Restituisce il prezzo corrente di un'azione dato il ticker (es. AAPL, TSLA)."""
    import yfinance as yf
    price = yf.Ticker(ticker).fast_info["last_price"]
    return f"Il prezzo di {ticker} è ${price:.2f}"

tools = [get_stock_price]

llm = ChatOpenAI(model="gpt-4o", temperature=0)

agent = create_agent(
    model=llm,
    tools=tools,
    system_prompt="Sei un assistente finanziario."
)


if __name__ == "__main__":
    result = agent.invoke({
        "messages": [{"role": "user", "content": "Qual è il prezzo di AAPL?"}]
    })
    print(result["messages"][-1].content)