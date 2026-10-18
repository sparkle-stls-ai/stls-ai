import sys, json, base64
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
sys.path.insert(0, str(REPO_ROOT / "python" / "stls-core" / "src"))
from stls_core.providers.stls_core_providers_pqc import (
    mlkem_encap, x25519_exchange, x25519_generate_keypair, derive_hybrid_key,
)

with open(HERE / "pqc-vector.json") as f:
    v = json.load(f)

nonce = base64.b64decode(v["nonce"])
client_x25519_pub = base64.b64decode(v["client_x25519_public"])
client_mlkem_pub = base64.b64decode(v["client_mlkem_public"])

server_priv, server_pub = x25519_generate_keypair()
x25519_secret = x25519_exchange(server_priv, client_x25519_pub)
mlkem_ct, mlkem_secret = mlkem_encap(client_mlkem_pub)
session_key = derive_hybrid_key(x25519_secret=x25519_secret, mlkem_secret=mlkem_secret, nonce=nonce)

out = {
    "server_x25519_public": base64.b64encode(server_pub).decode(),
    "mlkem_ciphertext": base64.b64encode(mlkem_ct).decode(),
    "session_key_python": base64.b64encode(session_key).decode(),
}
with open(HERE / "pqc-vector-response.json", "w") as f:
    json.dump(out, f)
print("Python (liboqs + cryptography) server step OK, session_key=", out["session_key_python"])
