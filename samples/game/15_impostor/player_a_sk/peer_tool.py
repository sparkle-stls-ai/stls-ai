"""
peer_tool.py
------------
Framework-agnostic helper wrapping a direct, secure A2A call to this
player's peer (the OTHER player, not the Game Master). Each framework
wrapper (Semantic Kernel here, LangChain in player_b_lc/) decorates
the same underlying coroutine as its own native tool type.

This is the genuinely peer-to-peer leg of the game: when this
player's own LLM decides to call this tool, it opens (or reuses) its
own secure A2A session directly toward the peer's endpoint — no
Game Master in the middle.

Safety: an LLM asked to "discuss with your peer" could, in principle,
keep calling this tool indefinitely (e.g. if it decides every reply
deserves a rebuttal). A hard, time-windowed cap bounds this
independently of how well the prompt is followed — defense in depth,
not reliance on prompt engineering alone.
"""
import os
import time

from stls_a2a import SecureSessionManager, extract_a2a_text

PEER_URL = os.environ["PEER_URL"]  # the OTHER player's base A2A URL
SELF_LABEL = os.getenv("SELF_LABEL", "player")
PROVIDER_NAME = os.getenv("KEY_PROVIDER_NAME", "pqc").strip().lower()
GAME_LANGUAGE = (os.getenv("GAME_LANGUAGE") or "en").strip().lower()

_BUDGET_EXCEEDED_MSG = {
    "en": (
        "[deliberation budget exhausted for now — stop calling this tool "
        "and give your final verdict based on what's already been said]"
    ),
    "it": (
        "[budget di confronto esaurito per ora — smetti di chiamare questo "
        "strumento e dai il tuo verdetto finale in base a quanto già detto]"
    ),
}

_manager: SecureSessionManager | None = None

# Defense in depth against a runaway deliberation loop (see module
# docstring) — not a replacement for the prompt-level instruction that
# each player's system prompt gives ("don't call this tool in reply to
# a peer message, only when the Game Master asks you to deliberate").
_MAX_CALLS_PER_WINDOW = 4
_WINDOW_SECONDS = 120.0
_call_timestamps: list[float] = []


def _get_manager() -> SecureSessionManager:
    global _manager
    if _manager is None:
        _manager = SecureSessionManager(client_id=SELF_LABEL, provider_name=PROVIDER_NAME)
    return _manager


def _budget_exceeded() -> bool:
    now = time.monotonic()
    while _call_timestamps and now - _call_timestamps[0] > _WINDOW_SECONDS:
        _call_timestamps.pop(0)
    if len(_call_timestamps) >= _MAX_CALLS_PER_WINDOW:
        return True
    _call_timestamps.append(now)
    return False


async def talk_to_peer_impl(message: str) -> str:
    """
    Sends `message` directly to the peer's secure A2A endpoint and
    returns their reply text. This is a real, independently-encrypted
    STLS session between the two player processes — the Game Master
    never sees this exchange.
    """
    if _budget_exceeded():
        return _BUDGET_EXCEEDED_MSG.get(GAME_LANGUAGE, _BUDGET_EXCEEDED_MSG["en"])

    manager = _get_manager()
    adapter = await manager.get_adapter(PEER_URL)
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "messages/send",
        "params": {
            "message": {
                "role": "user",
                # Tagged so the peer's own system prompt can tell a
                # peer-to-peer message apart from a Game Master
                # instruction, and reply in plain text instead of
                # calling its own tool back (the other half of the
                # anti-runaway-loop defense — see that player's agent.py).
                "parts": [{"type": "text", "text": f"[PEER MESSAGE from {SELF_LABEL}] {message}"}],
            }
        },
    }
    response = await adapter.send_secure_message(payload)
    return extract_a2a_text(response) or "[no reply]"


async def aclose_peer_tool() -> None:
    if _manager is not None:
        await _manager.aclose()
