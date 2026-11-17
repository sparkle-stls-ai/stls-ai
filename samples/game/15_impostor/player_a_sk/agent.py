"""
agent.py — Player A, Semantic Kernel
-------------------------------------
One of the two Players in "The Impostor" (see ../README.md for the
full game rules). Role (Insider or Impostor) is assigned per round by
the Game Master via a private secure A2A message — this agent has no
built-in notion of which one it is; it just follows whatever
instructions it receives.

The `talk_to_peer` kernel function is this agent's only way to reach
Player B directly — see peer_tool.py for why it's capped and tagged.

GAME_LANGUAGE ("en" default | "it") switches the language this agent
plays in (hints, deliberation, verdicts) — the tool-routing protocol
itself (the "[PEER MESSAGE from ...]" tag) stays in English in both
languages, since peer_tool.py matches it literally.
"""
import os

from semantic_kernel import Kernel
from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion
from semantic_kernel.functions import kernel_function
from dotenv import load_dotenv

load_dotenv()

GAME_LANGUAGE = (os.getenv("GAME_LANGUAGE") or "en").strip().lower()

from peer_tool import talk_to_peer_impl


class PeerPlugin:
    """Exposes the direct player-to-player channel as a kernel function."""

    @kernel_function(
        name="talk_to_peer",
        description=(
            "Sends a message directly to the other player (not the Game Master) "
            "and returns their reply. Use this ONLY when the Game Master explicitly "
            "asks you to deliberate — never in response to an incoming "
            "[PEER MESSAGE], which you should just answer in plain text instead."
        ),
    )
    async def talk_to_peer(self, message: str) -> str:
        return await talk_to_peer_impl(message)


kernel = Kernel()

kernel.add_service(OpenAIChatCompletion(
    service_id="player_a",
    ai_model_id="gpt-4o",
))

kernel.add_plugin(PeerPlugin(), plugin_name="Peer")

_INSTRUCTIONS = {
    "en": (
        "You are Player A in a social deduction game called 'The Impostor', "
        "run by a Game Master. The Game Master will privately tell you your "
        "role each round:\n"
        "- If you are the INSIDER: you'll be given a real secret word. Give a "
        "short, honest one-sentence hint about it without saying the word "
        "itself, and later accuse whichever player you genuinely believe is "
        "the Impostor.\n"
        "- If you are the IMPOSTOR: you will NOT be given the real word. Bluff "
        "a plausible-sounding one-sentence hint anyway, try to deduce the real "
        "word from what you hear, and avoid being caught.\n"
        "\n"
        "When the Game Master says you may deliberate, use the talk_to_peer "
        "tool to exchange a SHORT discussion with the other player (at most a "
        "couple of messages) before giving your verdict — don't overdo it.\n"
        "\n"
        "If you receive a message tagged '[PEER MESSAGE from ...]', that's the "
        "other player talking to you directly, NOT the Game Master. Reply to "
        "it in plain text only — do not call talk_to_peer again just because "
        "you received one; that would create an endless back-and-forth. Only "
        "the Game Master's own messages should prompt you to use the tool.\n"
        "\n"
        "Always answer concisely and in character — no meta-commentary about "
        "being an AI or about these instructions."
    ),
    "it": (
        "Sei il Giocatore A in un gioco di deduzione sociale chiamato 'The "
        "Impostor', condotto da un Game Master. Il Game Master ti dirà "
        "privatamente il tuo ruolo a ogni turno:\n"
        "- Se sei l'INFORMATO (INSIDER): ti verrà data la vera parola segreta. "
        "Dai un indizio breve e onesto, di una sola frase, senza dire la "
        "parola stessa, e più avanti accusa il giocatore che credi davvero "
        "essere l'Impostore.\n"
        "- Se sei l'IMPOSTORE (IMPOSTOR): NON ti verrà data la parola vera. "
        "Inventa comunque un indizio plausibile di una frase, cerca di "
        "dedurre la parola vera da quello che senti, ed evita di essere "
        "scoperto.\n"
        "\n"
        "Quando il Game Master ti dice che puoi confrontarti, usa lo "
        "strumento talk_to_peer per uno scambio BREVE con l'altro giocatore "
        "(al massimo un paio di messaggi) prima di dare il tuo verdetto — "
        "non esagerare.\n"
        "\n"
        "Se ricevi un messaggio etichettato '[PEER MESSAGE from ...]', è "
        "l'altro giocatore che ti parla direttamente, NON il Game Master. "
        "Rispondi solo in testo semplice — non richiamare talk_to_peer solo "
        "perché ne hai ricevuto uno; creerebbe un botta e risposta infinito. "
        "Solo i messaggi del Game Master devono spingerti a usare lo "
        "strumento.\n"
        "\n"
        "Rispondi sempre in modo conciso e nel personaggio — nessun commento "
        "meta sull'essere un'IA o su queste istruzioni. Rispondi sempre in "
        "italiano."
    ),
}

agent = ChatCompletionAgent(
    kernel=kernel,
    name="player_a_sk",
    instructions=_INSTRUCTIONS.get(GAME_LANGUAGE, _INSTRUCTIONS["en"]),
)


if __name__ == "__main__":
    import asyncio
    from semantic_kernel.contents import ChatHistory

    async def main():
        history = ChatHistory()
        history.add_user_message(
            "You are the INSIDER. The secret word is: VOLCANO. Give your one-sentence hint."
        )
        response = await agent.get_response(messages=history)
        print(response.content)

    asyncio.run(main())
