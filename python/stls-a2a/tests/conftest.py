"""
Test session configuration.

Forces every test in this directory (the real, assert-based pytest
suite — see issue #59) onto in-memory backends and a no-DB key
provider, regardless of what a local `.env` says: `stls_a2a`'s own
modules read these variables at import time, and pytest loads
conftest.py before collecting any sibling test module, so this runs
first. python-dotenv's load_dotenv() (called internally by stls_a2a at
import time) never overrides a variable already present in the
environment, so this sticks.

This suite needs zero real infrastructure (no Redis, no Postgres, no
live key provider/LLM) — that's the whole point of it existing
alongside tests/README.md's manual, real-infra `*_manual.py` scripts.
"""

import os

os.environ.setdefault("KEY_PROVIDER_NAME", "base")
os.environ.setdefault("TASK_STORE_BACKEND", "memory")
os.environ.setdefault("SESSION_CACHE_BACKEND", "memory")
# Force, not setdefault: a local .env pointing AUTH_PG_DSN at a real
# Postgres must never leak into this suite (AgentRegistry would try to
# use it otherwise).
os.environ["AUTH_PG_DSN"] = ""
