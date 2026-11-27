# Third-Party Notices

This file lists the open-source dependencies used by the seven
dual-licensed STLS-AI packages (see [`README.md#licensing`](README.md#licensing)),
and their licenses, as of this writing. It does not cover `samples/`,
which carries its own, separate set of dependencies and is excluded
from the dual-licensing grant.

None of these packages are vendored or redistributed as part of this
repository's own source — they are declared as dependencies and
resolved by `pip`/`npm` at install time, each bringing its own license
file. This notice is provided as a courtesy for due-diligence review,
not because redistribution here triggers an attribution obligation.

## Weak copyleft (LGPL) dependencies — read this first

Two dependencies are LGPL-licensed:

- **`psycopg2-binary`** (direct, `python/stls-a2a`) — LGPL with an
  explicit exception permitting linking against OpenSSL.
- **`pybars4`** (transitive, pulled in by `semantic-kernel` for prompt
  templating) — LGPL-3.0.

Both are used only as ordinary runtime dependencies (installed via pip,
imported unmodified, never statically bundled into a single compiled
artifact). LGPL explicitly permits this kind of use by a work under a
different license — it does not require `stls-a2a` itself to become
LGPL, and does not affect the AGPLv3/commercial dual-licensing of this
project. No other GPL/AGPL/LGPL/SSPL/BUSL-licensed package was found,
either direct or transitive — see "Scan coverage" below.

## Direct dependencies by package

### `python/stls-core`
| Package | License |
|---|---|
| cryptography | Apache-2.0 OR BSD-3-Clause |
| pydantic | MIT |
| python-dotenv | BSD-3-Clause |
| redis | MIT |
| asyncpg | Apache-2.0 |
| liboqs-python (`pqc` extra) | MIT |

### `python/stls-identity`
| Package | License |
|---|---|
| stls_core | AGPL-3.0-or-later OR commercial (this project) |
| pydantic | MIT |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| base58 | MIT |
| redis | MIT |
| fastapi | MIT |
| rfc8785 | Apache-2.0 |
| asyncpg | Apache-2.0 |
| httpx | BSD-3-Clause |

### `python/stls-a2a`

Since issue #65, the agent-framework SDKs below are no longer pulled in
by a plain `pip install stls_a2a` — each is an opt-in extra
(`[google-adk]`, `[langchain]`, `[langgraph]`, `[semantic-kernel]`, or
`[all]` for every one of them at once). Listed separately so this table
reflects what actually gets installed, not a superset.

Always installed:

| Package | License |
|---|---|
| stls_core | AGPL-3.0-or-later OR commercial (this project) |
| fastapi | MIT |
| httpx | BSD-3-Clause |
| jwt (GehirnInc) | Apache-2.0 |
| PyJWT | MIT |
| psycopg2-binary | LGPL-3.0 + OpenSSL exception (see above) |
| python-dotenv | BSD-3-Clause |
| redis | MIT |
| uvicorn | BSD-3-Clause |

Only with the `[google-adk]` extra:

| Package | License |
|---|---|
| google-adk | Apache-2.0 |
| google-genai | Apache-2.0 |
| opentelemetry-api / -sdk | Apache-2.0 |

Only with the `[langchain]` extra:

| Package | License |
|---|---|
| langchain | MIT |
| langchain-community | MIT |
| langchain-openai | MIT |

Only with the `[langgraph]` extra:

| Package | License |
|---|---|
| langgraph | MIT |

Only with the `[semantic-kernel]` extra:

| Package | License |
|---|---|
| semantic-kernel | MIT |

`pybars4` (the transitive LGPL-3.0 dependency noted above) is pulled in
only through the `[semantic-kernel]` extra, not by a plain install.

### `python/stls-mcp`
| Package | License |
|---|---|
| stls_core | AGPL-3.0-or-later OR commercial (this project) |
| mcp | MIT |
| starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| httpx | BSD-3-Clause |
| python-dotenv | BSD-3-Clause |

### `node/stls-core`
| Package | License |
|---|---|
| @noble/curves | MIT |
| @noble/post-quantum | MIT |
| redis | MIT |
| pg (optional) | MIT |
| yaml (optional) | ISC |

### `node/stls-identity`
| Package | License |
|---|---|
| stls-core | AGPL-3.0-or-later OR commercial (this project; local workspace package, not third-party) |
| @noble/curves | MIT |
| bs58 | MIT |
| canonicalize | Apache-2.0 |
| redis | MIT |
| pg (optional) | MIT |

### `node/stls-mcp`
| Package | License |
|---|---|
| stls-core | AGPL-3.0-or-later OR commercial (this project; local workspace package, not third-party) |
| @modelcontextprotocol/sdk | MIT |
| zod | MIT |

## Scan coverage

The tables above cover direct, production dependencies as declared in
each package's `pyproject.toml`/`package.json`. Additionally, a full
transitive scan was run against the `python/stls-a2a` virtual
environment with every optional extra installed (`.[all]`) — its
dependency tree is a superset of `stls-core`'s and `stls-mcp`'s,
through `langchain`/`langgraph`/`google-adk`/`semantic-kernel` — 218
installed packages, inspected via `importlib.metadata`. Result: no
GPL, AGPL, SSPL, or BUSL-licensed package found, beyond the two LGPL
packages already noted above and this project's own AGPL/commercial-
dual-licensed packages. Since issue #65 made those four frameworks
optional extras rather than a default install, a plain
`pip install stls_a2a` pulls in only the "Always installed" subset of
that scanned tree — strictly smaller, so the all-extras scan still
covers it. Node's dependency tree is small enough (9 distinct direct
packages across all three Node packages) that it was reviewed directly
rather than scanned.

This scan was not repeated for `python/stls-identity`, whose dependency
tree (with `httpx` now mandatory as of issue #65, previously dev-only)
is still a strict subset of `stls-a2a`'s "always installed" set.

Dev-only dependencies (`pytest`, `httpx` as a test client, `fakeredis`,
etc. — installed only via each package's `[dev]` extra, never required
to run the software) are excluded from the tables above; all observed
dev dependencies are MIT, Apache-2.0, or BSD-3-Clause.

## Proprietary dependencies (not open source)

The Arqit SKA-Platform™ Python SDK (`arqit-skaplatform-*`, installed
only via the optional `[arqit_py]` extra — see
[`README.md`](README.md) and [`LICENSE-COMMERCIAL.md`](LICENSE-COMMERCIAL.md))
is proprietary, not redistributable, and not covered by this notice.
It is never installed in CI and is not required to use any package in
this repository — see the `pqc` and `base` providers for
dependency-free alternatives.
