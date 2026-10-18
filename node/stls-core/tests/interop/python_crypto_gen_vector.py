import sys, os, base64, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
sys.path.insert(0, str(REPO_ROOT / "python" / "stls-core" / "src"))

import stls_core.stls_core_crypto as crypto_mod

FIXED_IV = bytes(range(12))  # 00 01 02 ... 0b
crypto_mod.os.urandom = lambda n: FIXED_IV

from stls_core.stls_core_crypto import StlsTransport

session_key = bytes(range(32))  # 00 01 ... 1f
plaintext = b"hello stls interop"

transport = StlsTransport(session_key=session_key, session_id="test-session-id")
envelope = transport.encrypt(plaintext)
d = envelope.to_dict()
print(json.dumps(d))

# also verify decrypt round trip
recovered = transport.decrypt(envelope)
assert recovered == plaintext, "python self round-trip failed"
