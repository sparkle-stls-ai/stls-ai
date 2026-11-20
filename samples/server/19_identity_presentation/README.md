# 19_identity_presentation — DID/VC identity over a secure A2A call

Demonstrates issue #15 gap #1: `stls-identity`'s DID/Verifiable-Credential
layer attached to a real `stls-a2a` secure call — the piece that was
missing (`stls-identity` had no connection to `stls-a2a` at all before
this). Two processes, zero external infra (no Postgres/Redis — the
Governing Authority and its status list are both in-memory, same
pattern as `stls-identity`'s own `tests/test_identity_e2e.py`).

## What's actually on the wire

A Requestor presenting a plain identity (not a delegated one, see the
PR/issue for that shape) sends one extra HTTP header,
`X-STLS-Identity-Presentation`, holding base64 of:

```jsonc
{
  "@context": ["https://www.w3.org/ns/credentials/v2"],
  "type": ["VerifiablePresentation"],
  "holder": "did:web:requestor.example.com:agents:requestor",
  "verifiableCredential": [{
    "type": ["VerifiableCredential", "NetworkAuthorityCredential"],
    "issuer": "did:web:sample.sparkle.example.com",
    "credentialSubject": { "id": "did:web:requestor.example.com:agents:requestor", "...": "..." },
    "proof": { "...": "signed by the Governing Authority" }
  }],
  "proof": {
    "proofPurpose": "authentication",
    "challenge": "<server-issued, single-use>",
    "proofValue": "<signed by the Requestor's OWN key>"
  }
}
```

The outer `proof` is what's new here: it binds this specific
presentation to a challenge the Verifier agent minted moments earlier
— reusing it, or forging a different `challenge` value into a captured
presentation, both fail (the fix that came out of building this
gap: `challenge` is now part of what's signed, not appended after).

## Flow

1. `bootstrap_identity.py` plays Governing Authority for a moment:
   creates its own keypair and the Requestor's, issues the Requestor a
   `NetworkAuthorityCredential`, writes `authority_public.json` (for
   the Verifier) and `requestor_identity.json` (for the Requestor).
2. `server.py` is the Verifier agent — a trivial "whoami" A2A agent
   with `identity_verifier=RequestPresentationVerifier(...)` wired
   into `create_secure_a2a_endpoint()`.
3. `client.py` is the Requestor — opens a normal PQC-secured session,
   sends `messages/send`. First attempt gets back
   `ERR_IDENTITY_CHALLENGE_REQUIRED` (-32005) with a fresh challenge;
   `STLSHttpxTransport` calls `IdentityPresenter.on_challenge()`
   automatically and retries **exactly once** — the caller never sees
   that round trip, it's transparent, same as the existing
   re-handshake-on-session-expiry behavior.

## Running it

```bash
cd samples/server/19_identity_presentation
python bootstrap_identity.py
python server.py &          # terminal 1 (or background)
python client.py            # terminal 2
```

### Three things to try

1. **Default behavior is unchanged.** Stop the server, restart it with
   `IDENTITY_ENABLED=false python server.py`, run `python client.py`
   again (even `--no-identity`) — it succeeds immediately, no
   challenge, no header. This is the off-by-default guarantee:
   `identity_verifier=None` means zero behavior change for every
   existing agent/sample.
2. **With identity enabled, a valid presentation works** — restart the
   server without the env var (`python server.py`), run
   `python client.py`. You'll see the automatic retry happen
   transparently and the call succeed.
3. **A tampered credential is rejected.** `python client.py --tamper`
   mutates the credential after it was signed — the signature no
   longer matches, the server rejects it. `python client.py
   --no-identity` against the identity-enabled server shows the other
   rejection path: no presentation offered at all, stuck at
   `ERR_IDENTITY_CHALLENGE_REQUIRED`.

## Known limitation (by design, not a bug)

`server.py`'s `status_resolver` is a trivial "never revoked" stub —
`stls-identity` has no remote status-list fetcher yet for a verifying
bridge (only the issuing side publishes one). A real deployment must
supply a `status_resolver` that actually fetches and checks the
issuing Authority's published `BitstringStatusList`; see
`RequestPresentationVerifier`'s docstring (`stls_id_request_verifier.py`).
