"""
Step 2 (server, Python) of the full PQC handshake orchestration test.

Reads the PQCHandshakeRequest written by Node (step 1), runs the real
perform_pqc_handshake() — the same function a Python STLS server would
call from a POST /stls/pqc/handshake route handler — registers the
session in a real InMemorySessionCache, and uses the resulting transport
to encrypt a test message for Node. It then stays alive and polls for
the envelope Node produces in step 3, decrypting it with the SAME
transport instance still held in this process's session cache — this
intentionally avoids needing to reconstruct/serialize the session key
across process boundaries (the cache never exposes raw key material,
by design), matching how a real long-running server actually works.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
sys.path.insert(0, str(REPO_ROOT / "python" / "stls-core" / "src"))

from stls_core.stls_core_handshake_pqc import PQCHandshakeRequest, perform_pqc_handshake
from stls_core.stls_core_session import InMemorySessionCache

with open(HERE / "pqc-handshake-request.json") as f:
    request_data = json.load(f)

request = PQCHandshakeRequest(**request_data)
session_cache = InMemorySessionCache()

response = asyncio.run(perform_pqc_handshake(request, session_cache))
print("Python: perform_pqc_handshake() OK, session_id =", response.session_id, "kid =", response.kid)

transport = session_cache.get(response.session_id)
assert transport is not None, "session not found in cache right after perform_pqc_handshake()"

envelope = transport.encrypt(b"hello-from-python-pqc-handshake")

with open(HERE / "pqc-handshake-response.json", "w") as f:
    json.dump({**response.model_dump(), "envelope_from_python": envelope.to_dict()}, f)
print("Python: envelope for Node written, waiting for Node's envelope...")

node_envelope_path = HERE / "pqc-handshake-node-envelope.json"
deadline = time.monotonic() + 20.0
while not node_envelope_path.exists():
    if time.monotonic() > deadline:
        raise TimeoutError("Node never produced pqc-handshake-node-envelope.json within 20s")
    time.sleep(0.2)

with open(node_envelope_path) as f:
    node_envelope_dict = json.load(f)

from stls_core.stls_core_crypto import StlsEnvelope

node_envelope = StlsEnvelope.from_dict(node_envelope_dict)
plaintext = transport.decrypt(node_envelope)
assert plaintext == b"hello-from-node-pqc-handshake", f"mismatch: {plaintext!r}"
print("Python: decrypted Node's envelope with the SAME session ->", plaintext)
print("ALL PYTHON-SIDE PQC HANDSHAKE CHECKS PASSED")
