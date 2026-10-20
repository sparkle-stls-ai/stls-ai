"""
Esempio: Agente Custom
======================

Scenario:
- Agente "Calculator" che esegue operazioni matematiche
- Esposto tramite A2A Protocol
- Tutti i messaggi criptati con chiavi derivate univoche per sessione
"""

from typing import Dict

class MockAgent:
    def __init__(self, name: str, system_instruction: str):
        self.name = name
        self.system_instruction = system_instruction
        self.tools = []
    
    def add_tool(self, tool_func):
        self.tools.append(tool_func)
    
    async def process_message(self, message: str, params: Dict = None) -> Dict:
        """
        Simula processamento messaggio.

        Metodi supportati:
            calculate  → esegue espressioni matematiche
            get_info   → restituisce info sull'agente
            messages/send, message/send → delegato a calculate se params ha 'expression'

        Metodi sconosciuti → restituisce errore JSON-RPC -32601 (non un result)
        in modo che handle_request() in AgentAdapter lo propaghi correttamente.
        """
        # Normalizza alias A2A spec → metodo interno
        if message in ("messages/send", "message/send"):
            # Estrai il testo dal payload A2A standard
            text = ""
            parts = (params or {}).get("message", {}).get("parts", [])
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "text":
                    text = p.get("text", "")
                    break
            # Prova a usare il testo come expression se contiene operatori
            expression = (params or {}).get("expression") or text
            return self._handle_calculate({"expression": expression})

        if message == "calculate":
            return self._handle_calculate(params or {})

        if message == "get_info":
            return {"agent": self.name, "status": "active", "tools": [t.__name__ for t in self.tools]}

        # Metodo non riconosciuto → errore JSON-RPC -32601
        # NON restituire un result con {"error": "..."} perché agent_response_to_a2a
        # lo passerebbe come result valido al client.
        # Restituisce direttamente un dict con "error" strutturato in modo che
        # CustomFunctionAdapter.invoke() → AgentResponse(error=...) lo propaghi.
        raise ValueError(f"Method '{message}' not found (-32601)")
    
    def _handle_calculate(self, params: Dict) -> Dict:
        """Handler per calcoli"""
        expression = params.get('expression', '')
        try:
            # Valutazione sicura (in produzione usa parser appropriato)
            result = eval(expression, {"__builtins__": {}}, {
                "add": lambda a, b: a + b,
                "sub": lambda a, b: a - b,
                "mul": lambda a, b: a * b,
                "div": lambda a, b: a / b if b != 0 else float('inf')
            })
            return {
                "expression": expression,
                "result": result,
                "status": "success"
            }
        except Exception as e:
            return {
                "expression": expression,
                "error": str(e),
                "status": "failed"
            }

agent = MockAgent(
        name="calculator",
        system_instruction="Performs mathematical calculations"
    )