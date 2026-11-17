"""
agent.py — Game Master, Google ADK
------------------------------------
The Game Master in "The Impostor" (see ../README.md). Unlike the two
Players, the Game Master has one focused job, done with real LLM
reasoning rather than a deterministic rule: judging whether the
Insider's final verdict confidently and correctly names the Impostor.

Everything else (role assignment, hint collection/relay, telling
players when to deliberate) is mechanical templating with no need for
judgment calls — that part lives in ../run_game.py, which talks to
this agent over secure A2A exactly like it talks to the two Players,
for the one step that genuinely needs a decision: is this player's
accusation confident and correct, or not?

Why this needs real reasoning rather than keyword matching: a
convincing Impostor can talk an Insider into self-doubt ("actually
now that I think about it, maybe I'm wrong..."), and the Insider's
final verdict may name the right player for the wrong reason, or
hedge enough that it shouldn't count as a confident accusation. A
human game master would judge the same way.

GAME_LANGUAGE ("en" default | "it") only changes the language of the
reasoning sentence the model produces — the "VERDICT: CAUGHT"/
"VERDICT: NOT CAUGHT" literal is always in English, in both
languages, since ../run_game.py parses it verbatim.
"""
import os

from dotenv import load_dotenv
from google.adk.agents import LlmAgent

load_dotenv()

DEFAULT_AGENT_MODEL = "gemini-2.5-flash"
AGENT_MODEL = (os.getenv("AGENT_MODEL") or DEFAULT_AGENT_MODEL).strip()
GAME_LANGUAGE = (os.getenv("GAME_LANGUAGE") or "en").strip().lower()

_INSTRUCTIONS = {
    "en": (
        "You are the Game Master for a social deduction game called 'The "
        "Impostor'. You will be given a judging request containing: the "
        "secret word, which player is the Impostor, and that player's "
        "verdict text (who they accused of being the Impostor, and why).\n"
        "\n"
        "Decide ONLY this: does the verdict confidently and correctly name "
        "the actual Impostor? A hedge, a wrong guess, or genuine uncertainty "
        "all count as NOT a confident correct accusation — the Impostor's "
        "bluff worked.\n"
        "\n"
        "Respond with EXACTLY two lines:\n"
        "VERDICT: CAUGHT\n"
        "or\n"
        "VERDICT: NOT CAUGHT\n"
        "followed by one short sentence of reasoning, in English, on the "
        "next line. No other text."
    ),
    "it": (
        "Sei il Game Master di un gioco di deduzione sociale chiamato 'The "
        "Impostor'. Riceverai una richiesta di giudizio contenente: la "
        "parola segreta, quale giocatore è l'Impostore, e il testo del "
        "verdetto di quel giocatore (chi accusa di essere l'Impostore, e "
        "perché).\n"
        "\n"
        "Decidi SOLO questo: il verdetto nomina con sicurezza e "
        "correttamente il vero Impostore? Un'esitazione, una supposizione "
        "sbagliata o un'incertezza genuina contano tutte come un'accusa "
        "NON sicura e corretta — il bluff dell'Impostore ha funzionato.\n"
        "\n"
        "Rispondi con ESATTAMENTE due righe, in questo formato letterale "
        "(mantieni queste due righe in inglese, invariate):\n"
        "VERDICT: CAUGHT\n"
        "oppure\n"
        "VERDICT: NOT CAUGHT\n"
        "seguite da una breve frase di motivazione, in italiano, sulla "
        "riga successiva. Nessun altro testo."
    ),
}

agent = LlmAgent(
    name="game_master_adk",
    model=AGENT_MODEL,
    description="Game Master for the social deduction game 'The Impostor'.",
    instruction=_INSTRUCTIONS.get(GAME_LANGUAGE, _INSTRUCTIONS["en"]),
)


if __name__ == "__main__":
    import asyncio
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    async def main():
        runner = InMemoryRunner(agent=agent, app_name="impostor_gm")
        session = await runner.session_service.create_session(
            app_name="impostor_gm", user_id="test-user"
        )
        prompt = (
            "Secret word: VOLCANO. The Impostor is player-b. "
            "player-a's verdict: 'I accuse player-b — their hint about "
            "\"spewing lava\" felt too specific for someone bluffing, but "
            "then again volcanoes are famous for lava so maybe that's not "
            "proof. I'll go with player-b anyway, fairly confident.'"
        )
        async for event in runner.run_async(
            user_id="test-user",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        print(part.text)

    asyncio.run(main())
