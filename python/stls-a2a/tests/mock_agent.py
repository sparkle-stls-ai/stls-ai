"""
Example: Custom Agent
======================

Scenario:
- "Calculator" agent that performs mathematical operations
- Exposed via the A2A Protocol
- All messages encrypted with unique per-session derived keys
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
        Simulates message processing.

        Supported methods:
            calculate  → evaluates mathematical expressions
            get_info   → returns info about the agent
            messages/send, message/send → delegated to calculate if params has 'expression'

        Unknown methods → returns a JSON-RPC -32601 error (not a result)
        so that handle_request() in AgentAdapter propagates it correctly.
        """
        # Normalize A2A spec alias → internal method
        if message in ("messages/send", "message/send"):
            # Extract the text from the standard A2A payload
            text = ""
            parts = (params or {}).get("message", {}).get("parts", [])
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "text":
                    text = p.get("text", "")
                    break
            # Try to use the text as an expression if it contains operators
            expression = (params or {}).get("expression") or text
            return self._handle_calculate({"expression": expression})

        if message == "calculate":
            return self._handle_calculate(params or {})

        if message == "get_info":
            return {"agent": self.name, "status": "active", "tools": [t.__name__ for t in self.tools]}

        # Unrecognized method → JSON-RPC -32601 error
        # Do NOT return a result with {"error": "..."} because agent_response_to_a2a
        # would pass it through as a valid result to the client.
        # Return a dict with a structured "error" directly so that
        # CustomFunctionAdapter.invoke() → AgentResponse(error=...) propagates it.
        raise ValueError(f"Method '{message}' not found (-32601)")

    def _handle_calculate(self, params: Dict) -> Dict:
        """Handler for calculations"""
        expression = params.get('expression', '')
        try:
            # Safe evaluation (use a proper parser in production)
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