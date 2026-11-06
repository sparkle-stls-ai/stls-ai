# stls-identity

DID + Verifiable Credential identity layer per l'ecosistema STLS-AI.

Quarto package del monorepo, sopra `stls-core`: aggiunge un'identità
persistente e verificabile da terzi agli agenti — separata
dall'autorizzazione — senza toccare `stls-core`, `stls-a2a` o
`stls-mcp`. `ApiKeyManager`/`KeyRecord` restano pienamente operativi in
parallelo per tutta la migrazione.

## Cosa fa

| Capacità | Modulo | Stato |
|---|---|---|
| DID Document (`did:web`), bootstrap chiave Ed25519 | `stls_id_did.py` | ✅ testato |
| Identity Credential (`NetworkAuthorityCredential`) | `stls_id_issuer.py` | ✅ testato |
| Operational Credential — delega Grantor → Requestor, depth-limit 1 | `stls_id_delegation.py` | ✅ testato |
| Revocation via status list (bitstring su Redis, MRI 5 min) | `stls_id_status.py` | ✅ testato (Redis reale) |
| Verifiable Presentation (bundle Identity + Operational) | `stls_id_presentation.py` | ✅ testato |
| Identity Bridge (HTTP: DID Document + status list) | `stls_id_bridge.py`, `stls_id_postgres_store.py` | ✅ testato (Postgres reale) |
| Trust Anchor Registry — Authority riconosciute + storia delle chiavi | `stls_id_trust_registry_*.py` | ✅ testato (Postgres reale), agganciato live al Sentinel |
| Verifica (`Sentinel`) — firma, validità, revoca, catena di delega | `stls_id_verifier.py` | ✅ testato |

Canonicalizzazione: RFC 8785 (JCS) reale via libreria `rfc8785`, non
un'approssimazione — necessaria perché bridge implementati
indipendentemente su piattaforme diverse convergano sugli stessi byte
firmati. Firma: Ed25519, proof type `DataIntegrityProof` /
`cryptosuite: eddsa-jcs-2022`.

## Cosa NON fa (ancora)

Elencato esplicitamente per non dare l'impressione che sia dimenticato:

- **Formato wire del bundle VP dentro `stls-a2a`** — il modello
  (`VerifiablePresentation`) e la logica di verifica esistono e sono
  testati; il modo in cui viaggia in una vera richiesta A2A (header?
  campo dedicato?) non è ancora specificato in `stls-a2a`.
- **Rotazione della chiave di un singolo agente** — la storia delle
  chiavi esiste solo per le Governing Authority nel Trust Anchor
  Registry. Un agente che ruota la propria chiave non ha lo stesso
  meccanismo (i suoi DID Document non hanno storia).
- **Keystore hardware-backed** — `IdentityKeystoreBase` ha solo backend
  software (`memory`, `redis`); ogni credenziale dichiara onestamente
  `keyStorageTier: TIER_1_SOFTWARE`, mai un tier superiore.
- **Governance del registry oltre l'Opzione A** — solo il registro
  centralizzato (operato da un anchor member) è implementato. Quorum
  (Opzione B) e transparency log (Opzione C) restano solo nel design.
- **PQC per la firma delle identità** — le credenziali sono firmate
  solo Ed25519 (classico). L'ibrido Ed25519+ML-DSA discusso per
  l'allineamento con l'handshake PQC di `stls-core` non è implementato.
- **Selective disclosure / ZKP, DIDComm receipts** — esplicitamente
  Fase 5/Full, mai state nello scope di questa versione.

## Installazione

Nel monorepo, con `uv`:

```bash
cd python/stls-identity
uv venv
uv sync --extra dev
```

`[tool.uv.sources]` risolve `stls-core` dalla cartella sorella
(`../stls-core`) in modalità editable — nessun passaggio manuale
aggiuntivo.

## Uso rapido

```python
from stls_identity import (
    IdentityManager, InMemoryIdentityKeystore,
    bootstrap_signing_key, build_did_document,
)

keystore = InMemoryIdentityKeystore()  # in produzione: RedisIdentityKeystore
key, pub, _ = bootstrap_signing_key("my-agent", keystore)
doc = build_did_document(
    host="sofia.sparkle.example.com",
    agent_path_segments=["agents", "my-agent"],
    governing_authority_did="did:web:sofia.sparkle.example.com",
    signing_public_key=pub,
)
```

Tre esempi completi, eseguibili, in `examples/`:

- `basic_identity_and_delegation.py` — DID, Identity Credential,
  Operational Credential, tutto in un unico flusso
- `federated_grantor_external_authority.py` — un Grantor esterno
  (Operatore C), con e senza onboarding nel Trust Anchor Registry
- `internal_grantor_no_registry_needed.py` — un Grantor interno a
  Sparkle: nessun bridge esterno, nessun registry coinvolto

```bash
uv run python examples/basic_identity_and_delegation.py
```

## Test

```bash
uv run pytest -v
```

Serve Redis e Postgres raggiungibili per i test che li usano davvero
(`test_status_list_redis.py`, `test_postgres_store.py`,
`test_trust_registry.py`, `test_bridge.py`) — vedi `.env.example` per
le variabili (`IDENTITY_TEST_PG_DSN`, `STATUS_LIST_TEST_REDIS_URL`,
caricate automaticamente da `tests/conftest.py`). I test che non
toccano infrastruttura esterna girano comunque senza nulla configurato.

67 test alla 0.1.0, tutti contro crypto/DB/cache reali dove pertinente
— mai contro stub Python per le parti che contano (firma, canonicalizzazione,
persistenza, revoca).

## Struttura

```
src/stls_identity/
    stls_id_models.py              modelli Pydantic (DID, VC, VP, TrustAnchorEntry)
    stls_id_did.py                 DID Document, bootstrap chiave, encode/decode multibase
    stls_id_keystore.py            persistenza chiave di firma (no-TTL, non ArqitKeyStoreBase)
    stls_id_issuer.py              IdentityManager — emissione Identity Credential
    stls_id_delegation.py          DelegationManager — Operational Credential, depth-1
    stls_id_status.py              status list (bitstring Redis, MRI)
    stls_id_presentation.py        costruzione/firma della Verifiable Presentation
    stls_id_verifier.py            Sentinel — tutte le verifiche
    stls_id_postgres_store.py      DID Document + status list cache (Postgres)
    stls_id_bridge.py              endpoint HTTP pubblici (FastAPI)
    stls_id_trust_registry_store.py    Trust Anchor Registry, storia delle chiavi (Postgres)
    stls_id_trust_registry_client.py   cache/TTL, fallback storico per il Sentinel
    stls_id_trust_registry_bridge.py   endpoint HTTP di sola lettura del registry
examples/    tre script eseguibili, dati reali generati dal codice
tests/       67 test, stessa disciplina "contro infrastruttura vera" del resto del monorepo
```

## Licenza

Doppia licenza: [GNU AGPLv3](LICENSE) oppure [licenza commerciale](LICENSE-COMMERCIAL.md),
coerente con `stls-core`, `stls-a2a` e `stls-mcp`.