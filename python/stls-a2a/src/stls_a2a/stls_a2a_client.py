import httpx
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class A2AClient:
    """
    Utility client for interacting with an STLS-A2A server.

    Covers discovery (/.well-known/agent.json), lookup by name,
    plain JSON-RPC calls, and SSE streaming calls.

    Example:
        client = A2AClient("https://agent.example.com")

        # Discovery
        agents = client.get_agents_list()

        # Synchronous plain call
        result = await client.call("financial_agent", "messages/send",
                                   {"message": {"role": "user", "parts": [{"type": "text", "text": "AAPL?"}]}})

        # SSE streaming
        async for event in client.stream("financial_agent", "messages/sendStream", params):
            print(event)
    """

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
        stream_timeout: float = 300.0,
        cards_path: str = "/.well-known/agent.json",
    ):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.api_key = api_key
        self.timeout = timeout
        self.stream_timeout = stream_timeout
        self.cards_path = cards_path
        self._cards_cache: list[dict] | None = None

    # ── Authentication ────────────────────────────────────────────────────────

    def _auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def _fetch_cards(self, use_cache: bool = True) -> list[dict]:
        """
        Fetches the cards from /.well-known and always normalizes into a list.
        In-memory cache, invalidable with use_cache=False.
        Uses httpx.AsyncClient so as not to block the event loop.
        """
        if use_cache and self._cards_cache is not None:
            return self._cards_cache

        url = f"{self.base_url}{self.cards_path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=self._auth_headers())
        response.raise_for_status()
        data = response.json()
        self._cards_cache = [data] if isinstance(data, dict) else data
        return self._cards_cache

    def invalidate_cache(self) -> None:
        """Invalidates the card cache (useful after a server reload)."""
        self._cards_cache = None

    async def get_agents_list(self) -> Dict[str, Any]:
        """Returns the name and description of all registered agents."""
        logger.info("get_agents_list()")
        try:
            agents = [
                {"name": c["name"], "description": c["description"]}
                for c in await self._fetch_cards()
                if isinstance(c, dict)
            ]
            return {"status": "success", "report": agents}
        except httpx.HTTPStatusError as e:
            return {"status": "error", "error_message": f"HTTP {e.response.status_code}: {e.response.text}"}
        except Exception as e:
            return {"status": "error", "error_message": str(e)}

    async def get_agent_card(self, agent_name: str) -> Dict[str, Any]:
        """Returns the full card of an agent by name."""
        logger.info("get_agent_card(%s)", agent_name)
        try:
            for card in await self._fetch_cards():
                if isinstance(card, dict) and card.get("name") == agent_name:
                    return {"status": "success", "report": card}
            return {"status": "error", "error_message": f"Agent '{agent_name}' not found."}
        except httpx.HTTPStatusError as e:
            return {"status": "error", "error_message": f"HTTP {e.response.status_code}: {e.response.text}"}
        except Exception as e:
            return {"status": "error", "error_message": str(e)}

    def _resolve_paths(self, card: dict) -> tuple[str | None, str | None]:
        """
        Extracts the absolute (plain_url, secure_url) from the card.
        Uses the spec-compliant url/secureUrl fields if present,
        otherwise falls back to the legacy path field.
        """
        def _abs(p: str | None) -> str | None:
            if not p:
                return None
            return p if p.startswith("http") else f"{self.base_url}{p}"

        plain_url  = _abs(card.get("url"))
        secure_url = _abs(card.get("secureUrl"))

        # legacy fallback: path = "/agents/xxx/secure-a2a"
        if not plain_url and not secure_url:
            legacy = card.get("path", "")
            secure_url = _abs(legacy) if legacy else None
            plain_url  = _abs(legacy.replace("secure-a2a", "a2a")) if legacy else None

        return plain_url, secure_url

    async def get_agent_paths(self, agent_name: str) -> Dict[str, Any]:
        """Returns url, secureUrl, and legacy path of an agent by name."""
        logger.info("get_agent_paths(%s)", agent_name)
        try:
            for card in await self._fetch_cards():
                if isinstance(card, dict) and card.get("name") == agent_name:
                    plain_url, secure_url = self._resolve_paths(card)
                    return {
                        "status": "success",
                        "report": {
                            "name":       card["name"],
                            "url":        plain_url,
                            "secure_url": secure_url,
                            "path":       card.get("path"),   # legacy, can be None
                        },
                    }
            return {"status": "error", "error_message": f"Agent '{agent_name}' not found."}
        except Exception as e:
            return {"status": "error", "error_message": str(e)}

    # ── Plain JSON-RPC calls ───────────────────────────────────────────────

    async def call(
        self,
        agent_name: str,
        method: str,
        params: Dict[str, Any],
        *,
        rpc_id: int | str = 1,
        expect_error: bool = False,
        expect_401: bool = False,
    ) -> Dict[str, Any]:
        """
        Calls the plain /a2a endpoint (standard JSON-RPC, unencrypted).

        Args:
            agent_name:   agent name (lookup via card)
            method:       A2A method, e.g. "messages/send", "tasks/get"
            params:       JSON-RPC message params
            rpc_id:       JSON-RPC id (default 1)
            expect_error: if True, doesn't raise an exception for a JSON-RPC error
            expect_401:   if True, doesn't raise an exception for HTTP 401
        """
        result = await self.get_agent_paths(agent_name)
        if result["status"] == "error":
            return result

        plain_url = result["report"]["url"]
        if not plain_url:
            return {"status": "error", "error_message": f"No plain endpoint for '{agent_name}'."}

        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id}
        logger.info("call(%s) → POST %s [%s]", agent_name, plain_url, method)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(plain_url, json=payload, headers=self._auth_headers())

        if resp.status_code == 401:
            if expect_401:
                return {
                    "status_code": 401,
                    "www_authenticate": resp.headers.get("WWW-Authenticate", ""),
                    "body": resp.json() if "application/json" in resp.headers.get("content-type", "") else resp.text,
                }
            resp.raise_for_status()

        resp.raise_for_status()
        response = resp.json()
        if response.get("error") and not expect_error:
            raise Exception(f"A2A error [{agent_name}]: {response['error']}")
        return response

    # ── Plain SSE streaming ───────────────────────────────────────────────────

    async def stream(
        self,
        agent_name: str,
        method: str,
        params: Dict[str, Any],
        *,
        rpc_id: int | str = 1,
    ):
        """
        Calls the plain /a2a/stream endpoint (SSE, unencrypted).
        Async generator: yields each SSE event as a dict
        {"event": str, "data": dict}.

        Example:
            async for event in client.stream("financial_agent", "messages/sendStream", params):
                if event["event"] == "TaskArtifactUpdateEvent":
                    print(event["data"])
        """
        result = await self.get_agent_paths(agent_name)
        if result["status"] == "error":
            raise ValueError(result["error_message"])

        plain_url = result["report"]["url"]
        if not plain_url:
            raise ValueError(f"No plain endpoint for '{agent_name}'.")

        stream_url = plain_url.rstrip("/") + "/stream"
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id}
        logger.info("stream(%s) → POST %s [%s]", agent_name, stream_url, method)

        # Structured timeout: stream_timeout only applies as a read timeout between
        # one SSE event and the next. With an STLS server >= 0.1.4, the periodic
        # keep-alive (STLS_SSE_HEARTBEAT_SECONDS) resets this timer, so it only
        # fires if the server is actually dead.
        _sse_timeout = httpx.Timeout(
            connect=10.0, read=self.stream_timeout, write=30.0, pool=10.0,
        )
        async with httpx.AsyncClient(timeout=_sse_timeout) as client:
            async with client.stream(
                "POST", stream_url,
                json=payload,
                headers={**self._auth_headers(), "Accept": "text/event-stream"},
            ) as resp:
                resp.raise_for_status()
                event_type = ""
                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        event_type = ""
                        continue
                    if line.startswith("event:"):
                        event_type = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        raw_data = line[len("data:"):].strip()
                        try:
                            import json
                            yield {"event": event_type, "data": json.loads(raw_data)}
                        except Exception:
                            yield {"event": event_type, "data": raw_data}