import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
sys.path.insert(0, str(REPO_ROOT / "python" / "stls-core" / "src"))
from stls_core.stls_core_session import RedisSessionCache

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6399")

cache = RedisSessionCache(redis_url=REDIS_URL, prefix="stls:sc", ttl_seconds=60)
transport = cache.get("node-to-python-session")
assert transport is not None, "session written by Node not found in Redis"

expected_key = bytes(range(32))
recovered = transport.decrypt(transport.encrypt(b"roundtrip-check"))
assert recovered == b"roundtrip-check"
print("OK: Python reads the session written by Node via Redis, session_id =", transport.session_id)

# write a session for the next step (Node reads it back)
cache.put("python-to-node-session", "kid-from-python", expected_key)
print("OK: Python wrote a session to Redis for Node")
