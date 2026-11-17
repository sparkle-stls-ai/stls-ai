"""
run_game.py — orchestrator / driver for "The Impostor"
---------------------------------------------------------
Not one of the three agents — a demo driver, in the same spirit as
samples/client/10_client_ms/client_multi_server.py: a plain script
using SecureSessionManager to talk to all three agents over secure
A2A, printing a live transcript of one full round.

Prerequisites: all three servers running (see ../README.md for exact
commands) — game_master (8301), player_a_sk (8302), player_b_lc (8303).

Run:
    python run_game.py
    GAME_LANGUAGE=it python run_game.py       # Italian version — see below

GAME_LANGUAGE ("en" default | "it") must match the same env var set
on all three servers' .env files: it's what makes each agent actually
answer in that language, this script only translates its own prompts
and transcript to match. Mixing languages across processes still
works technically (A2A doesn't care), it just produces a transcript
where the driver asks in one language and an agent replies in
another.
"""
import os
import random
import uuid

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging

logging.basicConfig(
    level=logging.WARNING,  # keep the transcript readable; raise to INFO for wire-level detail
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

from stls_a2a import SecureSessionManager, extract_a2a_text

PROVIDER_NAME = os.getenv("KEY_PROVIDER_NAME", "pqc").strip().lower()
GAME_LANGUAGE = (os.getenv("GAME_LANGUAGE") or "en").strip().lower()

GAME_MASTER_URL = "http://localhost:8301/agents/game-master"
PLAYERS = {
    "player-a": "http://localhost:8302/agents/player-a",
    "player-b": "http://localhost:8303/agents/player-b",
}

WORD_BANKS = {
    "en": ["VOLCANO", "LIGHTHOUSE", "ACCORDION", "GLACIER", "TRAMPOLINE", "CACTUS"],
    "it": ["VULCANO", "FARO", "FISARMONICA", "GHIACCIAIO", "TRAMPOLINO", "CACTUS"],
}
WORD_BANK = WORD_BANKS.get(GAME_LANGUAGE, WORD_BANKS["en"])

# Narrator text + the prompts sent to the agents over A2A. "insider"/
# "impostor" here are just the format-argument names — the printed
# labels embedded in these templates (INSIDER/IMPOSTOR vs. INFORMATO/
# IMPOSTORE) are what actually changes per language.
STRINGS = {
    "en": {
        "round_header": "ROUND {round_no} — provider={provider}",
        "secret_note": "(secret, for the transcript only — neither player sees this line)",
        "secret_line": "Secret word: {word}  |  Impostor: {impostor}  |  Insider: {insider}\n",
        "insider_role_msg": (
            "You are the INSIDER this round. The secret word is: {word}. "
            "Wait for my next message asking for your hint."
        ),
        "impostor_role_msg": (
            "You are the IMPOSTOR this round. You do not know the secret word. "
            "Wait for my next message asking for your hint."
        ),
        "roles_assigned": "Roles assigned privately",
        "roles_detail": "{insider}=Insider, {impostor}=Impostor",
        "ask_hint": "Give your one-sentence hint now.",
        "relay_hints": (
            "Here are both hints this round:\n"
            "- player-a said: \"{hint_a}\"\n"
            "- player-b said: \"{hint_b}\"\n"
            "You may now deliberate with {other} directly using your "
            "talk_to_peer tool if you want (keep it short), then reply "
            "with your final verdict: who do you accuse of being the "
            "Impostor, and how confident are you?"
        ),
        "ask_verdict": (
            "Please restate your final verdict clearly now: who do you "
            "accuse of being the Impostor, and how confident are you?"
        ),
        "judge_prompt": (
            "Secret word: {word}. The Impostor is {impostor}. "
            "{insider}'s verdict: '{verdict}'"
        ),
        "judgment_label": "Game Master's judgment",
        "empty": "(empty)",
        "comeback_prompt": (
            "You've been caught, {impostor}. One last chance — "
            "guess the secret word."
        ),
        "outcome_comeback_win": (
            "{impostor} was CAUGHT but guessed the word correctly — "
            "Impostor wins the comeback!"
        ),
        "outcome_caught": (
            "{impostor} was CAUGHT and failed to guess the word — Insiders win!"
        ),
        "outcome_not_caught": (
            "{insider}'s accusation didn't stick — {impostor} (the Impostor) "
            "wins the round!"
        ),
        "result_line": (
            "\n  🏆 RESULT: secret word was {word} ({impostor} was the "
            "Impostor). {outcome}"
        ),
        "reveal_header": (
            "  🎬 Final reveal (streamed to each player — their in-character "
            "reaction, not the result line above):"
        ),
        "reveal_to": "  --- to {label} ---",
        "reveal_text": (
            "The round is over. The secret word was {word}, and the "
            "Impostor was {impostor}. {outcome}"
        ),
        "done": "\n=== Done ===\n",
    },
    "it": {
        "round_header": "TURNO {round_no} — provider={provider}",
        "secret_note": "(segreto, solo per il resoconto — nessuno dei due giocatori legge questa riga)",
        "secret_line": "Parola segreta: {word}  |  Impostore: {impostor}  |  Informato: {insider}\n",
        "insider_role_msg": (
            "Questo turno sei l'INFORMATO. La parola segreta è: {word}. "
            "Aspetta il mio prossimo messaggio in cui ti chiederò il tuo indizio."
        ),
        "impostor_role_msg": (
            "Questo turno sei l'IMPOSTORE. Non conosci la parola segreta. "
            "Aspetta il mio prossimo messaggio in cui ti chiederò il tuo indizio."
        ),
        "roles_assigned": "Ruoli assegnati privatamente",
        "roles_detail": "{insider}=Informato, {impostor}=Impostore",
        "ask_hint": "Dai il tuo indizio di una sola frase, ora.",
        "relay_hints": (
            "Ecco gli indizi di questo turno:\n"
            "- player-a ha detto: \"{hint_a}\"\n"
            "- player-b ha detto: \"{hint_b}\"\n"
            "Ora puoi confrontarti direttamente con {other} usando il tuo "
            "strumento talk_to_peer, se vuoi (sii breve), poi rispondi con "
            "il tuo verdetto finale: chi accusi di essere l'Impostore, e "
            "quanto sei sicuro?"
        ),
        "ask_verdict": (
            "Ripeti chiaramente ora il tuo verdetto finale: chi accusi di "
            "essere l'Impostore, e quanto sei sicuro?"
        ),
        "judge_prompt": (
            "Parola segreta: {word}. L'Impostore è {impostor}. "
            "Il verdetto di {insider}: '{verdict}'"
        ),
        "judgment_label": "Verdetto del Game Master",
        "empty": "(vuoto)",
        "comeback_prompt": (
            "Sei stato scoperto, {impostor}. Un'ultima possibilità — "
            "indovina la parola segreta."
        ),
        "outcome_comeback_win": (
            "{impostor} è stato SCOPERTO ma ha indovinato la parola — "
            "l'Impostore vince con una rimonta!"
        ),
        "outcome_caught": (
            "{impostor} è stato SCOPERTO e non ha indovinato la parola — "
            "vincono gli Informati!"
        ),
        "outcome_not_caught": (
            "L'accusa di {insider} non ha convinto — vince {impostor} "
            "(l'Impostore)!"
        ),
        "result_line": (
            "\n  🏆 RISULTATO: la parola segreta era {word} ({impostor} era "
            "l'Impostore). {outcome}"
        ),
        "reveal_header": (
            "  🎬 Rivelazione finale (in streaming a ciascun giocatore — la "
            "loro reazione nel personaggio, non la riga del risultato sopra):"
        ),
        "reveal_to": "  --- a {label} ---",
        "reveal_text": (
            "Il turno è finito. La parola segreta era {word}, e l'Impostore "
            "era {impostor}. {outcome}"
        ),
        "done": "\n=== Fine ===\n",
    },
}
S = STRINGS.get(GAME_LANGUAGE, STRINGS["en"])


def _ok(label: str, detail: str = "") -> None:
    print(f"  ✅ {label}{': ' + detail if detail else ''}")


def _say(speaker: str, text: str) -> None:
    print(f"  🗣️  {speaker}: {text}")


def build_message(text: str, context_id: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "messages/send",
        "params": {
            "message": {
                "role": "user",
                "contextId": context_id,
                "parts": [{"type": "text", "text": text}],
            }
        },
    }


async def ask(manager: SecureSessionManager, base_url: str, text: str, context_id: str) -> str:
    adapter = await manager.get_adapter(base_url)
    response = await adapter.send_secure_message(build_message(text, context_id))
    return extract_a2a_text(response) or ""


async def announce_streamed(manager: SecureSessionManager, base_url: str, text: str, context_id: str) -> None:
    """
    The final reveal, via real SSE streaming (messages/sendStream) — a
    dramatic touch that happens to also exercise the A2A 1.0 streaming
    conformance fixes from issue #14 (oneof-wrapped events, correct
    TASK_STATE_* enum names) on genuine agent traffic, not just test
    fixtures.
    """
    adapter = await manager.get_adapter(base_url)
    message = build_message(text, context_id)
    message["method"] = "messages/sendStream"
    async for chunk in adapter.stream_secure_message(message):
        if chunk["event"] == "TaskArtifactUpdateEvent":
            for part in chunk["parts"]:
                if part.get("type") == "text":
                    print(part["text"], end="", flush=True)
        elif chunk["event"] == "TaskStatusUpdateEvent" and chunk.get("final"):
            print()


async def play_round(manager: SecureSessionManager, round_no: int) -> None:
    print("\n" + "=" * 70)
    print(S["round_header"].format(round_no=round_no, provider=PROVIDER_NAME))
    print("=" * 70)

    word = random.choice(WORD_BANK)
    insider_label, impostor_label = random.sample(list(PLAYERS.keys()), 2)
    context_id = str(uuid.uuid4())

    print(f"  {S['secret_note']}")
    print(f"  {S['secret_line'].format(word=word, impostor=impostor_label, insider=insider_label)}")

    # ── 1. Assign roles privately ──────────────────────────────────────────
    await ask(
        manager, PLAYERS[insider_label],
        S["insider_role_msg"].format(word=word),
        context_id,
    )
    await ask(
        manager, PLAYERS[impostor_label],
        S["impostor_role_msg"],
        context_id,
    )
    _ok(S["roles_assigned"], S["roles_detail"].format(insider=insider_label, impostor=impostor_label))

    # ── 2. Collect hints ────────────────────────────────────────────────────
    hints: dict[str, str] = {}
    for label, url in PLAYERS.items():
        hints[label] = await ask(manager, url, S["ask_hint"], context_id)
        _say(label, hints[label])

    # ── 3. Relay both hints to both players, invite deliberation ───────────
    for label, url in PLAYERS.items():
        other = next(l for l in PLAYERS if l != label)
        await ask(
            manager, url,
            S["relay_hints"].format(hint_a=hints["player-a"], hint_b=hints["player-b"], other=other),
            context_id,
        )
        # the deliberation itself happens inside this same call, via the
        # player's own tool-calling loop — nothing else to do here

    # ── 4. Collect verdicts ─────────────────────────────────────────────────
    verdicts: dict[str, str] = {}
    for label, url in PLAYERS.items():
        verdicts[label] = await ask(manager, url, S["ask_verdict"], context_id)
        _say(label, verdicts[label])

    # ── 5. Judge the Insider's verdict (real A2A call to the Game Master) ──
    judge_prompt = S["judge_prompt"].format(
        word=word, impostor=impostor_label, insider=insider_label, verdict=verdicts[insider_label],
    )
    judgment = await ask(manager, GAME_MASTER_URL, judge_prompt, context_id)
    # The "VERDICT: CAUGHT" literal is always in English — see
    # game_master/agent.py — regardless of GAME_LANGUAGE.
    caught = judgment.strip().upper().startswith("VERDICT: CAUGHT")
    _ok(S["judgment_label"], judgment.splitlines()[0] if judgment else S["empty"])

    # ── 6. Resolve + optional comeback guess ────────────────────────────────
    if caught:
        guess = await ask(
            manager, PLAYERS[impostor_label],
            S["comeback_prompt"].format(impostor=impostor_label),
            context_id,
        )
        _say(impostor_label, guess)
        impostor_wins = word.lower() in guess.lower()
        outcome = (
            S["outcome_comeback_win"].format(impostor=impostor_label)
            if impostor_wins else
            S["outcome_caught"].format(impostor=impostor_label)
        )
    else:
        outcome = S["outcome_not_caught"].format(insider=insider_label, impostor=impostor_label)

    print(S["result_line"].format(word=word, impostor=impostor_label, outcome=outcome))
    print(S["reveal_header"])
    for label, url in PLAYERS.items():
        print(S["reveal_to"].format(label=label))
        await announce_streamed(
            manager, url,
            S["reveal_text"].format(word=word, impostor=impostor_label, outcome=outcome),
            context_id,
        )


async def main() -> None:
    rounds = int(os.getenv("ROUNDS", "1"))
    manager = SecureSessionManager(client_id="impostor-referee", provider_name=PROVIDER_NAME)
    try:
        for round_no in range(1, rounds + 1):
            # Insider/Impostor is picked fresh (random.sample) each round,
            # so playing several rounds naturally rotates which framework
            # plays which role — no explicit bookkeeping needed.
            await play_round(manager, round_no=round_no)
    finally:
        await manager.aclose()
    print(S["done"])


if __name__ == "__main__":
    asyncio.run(main())
