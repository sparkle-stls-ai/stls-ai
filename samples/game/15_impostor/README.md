# 15_impostor — three frameworks playing a social deduction game over secure A2A

A small party game ("The Impostor", in the spirit of Werewolf/Mafia and
Among Us) played by three independent agent processes, each built on a
**different** framework, talking to each other exclusively over
STLS-encrypted A2A — no shared process, no shared memory, no HTTP call
that isn't a real secure A2A session.

| Agent | Port | Framework | Role |
|---|---|---|---|
| `game_master` | 8301 | Google ADK | Judges the round's outcome |
| `player_a_sk` | 8302 | Semantic Kernel | Player |
| `player_b_lc` | 8303 | LangChain/LangGraph (`langchain.agents.create_agent`) | Player |

Unlike most samples in this repo (one agent exposed, one client calling
it), **every process here is both a server and a client**: each has its
own `create_secure_a2a_endpoint` to receive messages, and makes its own
outbound secure A2A calls to the others.

## The game

Each round, the Game Master privately assigns one Player the role of
**Insider** (given the real secret word) and the other **Impostor**
(not given the word at all). Both players give a one-sentence hint
about "their" word, hear each other's hint, may deliberate directly
with each other, and then give a final verdict (who do they think is
the Impostor?). The Game Master judges whether the Insider's verdict
confidently and correctly names the Impostor:

- **Caught** → the Impostor gets one last chance: guess the real word.
  Guess right and the Impostor still wins (a comeback); guess wrong and
  the Insiders win.
- **Not caught** (the verdict was wrong, hedged, or talked out of
  itself during deliberation) → the Impostor's bluff worked, Impostor
  wins outright.

Role assignment is randomized every round, so running several rounds
(`ROUNDS=3 python run_game.py`) naturally rotates which framework plays
Insider vs. Impostor — nothing in the implementation assumes a fixed
mapping between role and framework.

## Why this is a good A2A showcase

The round exercises three genuinely different communication shapes,
each over its own independent STLS session:

1. **Private 1:1 (Game Master → one Player)** — role assignment, hint
   requests, verdict requests. Each Player has its own session; neither
   can see the other's traffic with the Game Master — this is the
   actual security property STLS provides, not just a narrative beat.
2. **Fan-out (Game Master → both Players)** — relaying both hints to
   both players before deliberation.
3. **True peer-to-peer (Player ↔ Player, no Game Master involved at
   all)** — the deliberation step. Each player has a `talk_to_peer`
   tool (a Semantic Kernel `@kernel_function` on Player A, a LangChain
   `@tool` on Player B — see each `peer_tool.py`) that opens its own
   secure A2A session directly to the other player's endpoint. This is
   the part most samples in this repo don't exercise: genuine
   decentralized agent-to-agent dialogue, not a root agent delegating
   to sub-agents.

The final reveal is sent via real SSE streaming
(`messages/sendStream`), which happens to also exercise the A2A
streaming conformance work from issue #14 (oneof-wrapped events,
correct `TASK_STATE_*` enum names) on genuine multi-framework agent
traffic, not just test fixtures.

### A note on the deliberation step's "don't call the tool back" instruction

Each player's system prompt asks it not to invoke `talk_to_peer` in
reply to an incoming `[PEER MESSAGE from ...]` — just answer in plain
text, to keep the exchange from ping-ponging forever. In practice, an
LLM sometimes ignores this and calls the tool anyway for its reply,
producing one extra genuine hop before the other side (usually)
stops it. This is harmless and, if anything, a more realistic
demonstration of autonomous agent-to-agent dialogue — but it's also why
`peer_tool.py` has a real code-level circuit breaker (a time-windowed
call cap), not just a prompt-level one: defense in depth against an
LLM that doesn't follow instructions perfectly, not a replacement for
good instructions.

## Running it

### Prerequisites

Use the **shared root venv** (`stls-ai/.venv`), not any package's own
venv — this sample imports `stls_a2a` plus `google-adk`,
`semantic-kernel`, and `langchain`/`langchain-openai` all at once, same
as `samples/server/10_server_ms`'s multi-framework setup.

Each directory needs its own `.env` (copy from `.env.example`):

```bash
cd game_master   && cp .env.example .env   # fill in GOOGLE_API_KEY
cd ../player_a_sk && cp .env.example .env  # fill in OPENAI_API_KEY
cd ../player_b_lc && cp .env.example .env  # fill in OPENAI_API_KEY
```

All three default to `KEY_PROVIDER_NAME=pqc` — no extra device config
needed (unlike `arqit_py`), each process does its own per-session KEM
handshake.

### Language

`GAME_LANGUAGE` ("en" default | "it") controls what language the
agents play in — hints, deliberation, and verdicts. It must be set
to the **same value in all three `.env` files** (it's what actually
makes each agent answer in that language) **and** passed to
`run_game.py` (which only translates its own prompts/transcript to
match — it doesn't instruct the agents on its own). The
`VERDICT: CAUGHT`/`VERDICT: NOT CAUGHT` line the Game Master uses to
signal its ruling, and the `[PEER MESSAGE from ...]` tag the two
players use to route peer-to-peer messages, both stay in English in
either language — they're parsed literally by code, not meant to be
read as dialogue.

```bash
# in each of game_master/.env, player_a_sk/.env, player_b_lc/.env:
GAME_LANGUAGE=it
```

```bash
GAME_LANGUAGE=it python run_game.py
```

### Start all three servers (one terminal each, or background them)

```bash
cd game_master   && python server.py   # port 8301
cd player_a_sk   && python server.py   # port 8302
cd player_b_lc   && python server.py   # port 8303
```

Verify:

```bash
curl -s http://localhost:8301/.well-known/agent-card.json | head -c 200
curl -s http://localhost:8302/.well-known/agent-card.json | head -c 200
curl -s http://localhost:8303/.well-known/agent-card.json | head -c 200
```

### Play a round

```bash
python run_game.py                              # one round
ROUNDS=3 python run_game.py                      # three rounds, roles re-randomized each time
GAME_LANGUAGE=it ROUNDS=3 python run_game.py     # Italian, three rounds
```

`run_game.py` is a driver script, not a fourth agent — it's the same
pattern as `samples/client/10_client_ms/client_multi_server.py`: a
plain script using `stls_a2a.SecureSessionManager` to talk to all
three agents over secure A2A and print a live transcript.

## Layout

```
game_master/      agent.py (ADK LlmAgent, judges verdicts) + server.py
player_a_sk/      agent.py (Semantic Kernel ChatCompletionAgent + talk_to_peer tool),
                  peer_tool.py (the peer-to-peer channel + its circuit breaker), server.py
player_b_lc/      agent.py (LangChain/LangGraph create_agent + talk_to_peer tool),
                  peer_tool.py (identical in spirit to player_a_sk's — not shared via
                  import, same self-contained-directory convention as
                  samples/server/10_server_ms's server_ms_1/server_ms_2), server.py
run_game.py       the orchestrator/driver — see "Running it" above
```
