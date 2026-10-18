import sys, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
sys.path.insert(0, str(REPO_ROOT / "python" / "stls-core" / "src"))
from stls_core.stls_core_crypto import StlsTransport, StlsEnvelope

session_key = bytes(range(32))
with open(HERE / "node-produced-envelope.json") as f:
    envelope_dict = json.load(f)

transport = StlsTransport(session_key=session_key, session_id="test-session-id")
envelope = StlsEnvelope.from_dict(envelope_dict)
plaintext = transport.decrypt(envelope)
assert plaintext == b"hello stls interop", f"mismatch: {plaintext!r}"
print("OK: Python decrypts Node-produced envelope ->", plaintext)
