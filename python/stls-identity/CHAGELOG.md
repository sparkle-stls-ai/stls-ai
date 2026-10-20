# Changelog

Tutte le modifiche rilevanti a questo package, in ordine cronologico
inverso.

## [0.1.0] — prima versione

### Aggiunto

**Fase 1 — Identità**
- DID Document (`did:web`), bootstrap della chiave di firma Ed25519,
  keystore dedicato persistente senza TTL (non `ArqitKeyStoreBase`,
  semantica diversa: quello è per chiavi di sessione effimere)
- Identity Credential (`NetworkAuthorityCredential`), emessa
  parallelamente all'API key esistente, mai in sostituzione
- Canonicalizzazione RFC 8785 (JCS) reale, non un'approssimazione
  `sort_keys` — necessaria per far convergere bridge implementati
  indipendentemente sugli stessi byte firmati

**Fase 2 — Delega e federazione**
- Operational Credential — delega Grantor → Requestor, depth-limit 1
  imposto a runtime (controllo `isinstance`, non solo convenzione)
- Revocation via status list — bitstring su Redis, stesso pattern
  Lua-atomico di `stls:revoked`, MRI a 5 minuti, verifica fail-closed
- Identity Bridge HTTP (DID Document + status list), backed da Postgres
- Verifiable Presentation — bundle di una o due credenziali con holder
  binding e protezione replay via challenge
- Trust Anchor Registry — Authority riconosciute (Opzione A,
  centralizzato), con storia delle chiavi: una rotazione non invalida
  le credenziali firmate prima della rotazione
- Aggancio live del registro al `Sentinel`: `verify_signature()` tenta
  la risoluzione storica quando una chiave non è nella cache corrente,
  prima di rifiutare

**Allineamento W185** (risposta al contributo dei colleghi)
- `verificationMethod.controller` corretto: l'agente stesso, non la
  Governing Authority
- `proof.type` → `DataIntegrityProof` / `cryptosuite: eddsa-jcs-2022`
  (era `Ed25519Signature2020`, canonicalizzazione implicita sbagliata)
- `schemaVersion`, `principalRef`, `keyStorageTier` come metadata
  dichiarativi sulla Identity Credential
- Campo `roles` + controllo esplicito in `DelegationManager`: avere
  un'identità valida non basta più per fare da Grantor

### Note di rilascio

- Richiede `stls-core` come dipendenza locale (`[tool.uv.sources]`,
  path relativo `../stls-core`) — **il nome esatto del progetto va
  confermato** contro `[project.name]` del `pyproject.toml` reale di
  `stls-core` prima della pubblicazione su un index privato
- 67 test, contro Postgres/Redis/crypto reali dove pertinente
- Non ancora incluso: formato wire del bundle VP in `stls-a2a`,
  rotazione chiave per singolo agente, keystore hardware-backed,
  governance Opzione B/C, PQC ibrido per la firma, selective disclosure