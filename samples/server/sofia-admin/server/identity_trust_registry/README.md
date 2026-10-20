# Trust Anchor Registry — README

Server HTTP che pubblica pubblicamente **quali Governing Authority
sono riconosciute dalla federazione** e le loro chiavi correnti.
Sparkle gioca il ruolo di **anchor operator** (Opzione A dal design
originale: registro centralizzato, operato da un membro anchor) — è
il pezzo che permette a un'altra piattaforma (Operatore B, C, ...) di
sapere se una terza piattaforma è legittima, senza dover negoziare
fiducia bilateralmente con ognuna.

File: `run_trust_registry.py` — indipendente sia da `agent_identity.py`
sia da `run_identity_bridge.py` (propria configurazione, proprio
processo). Importa solo l'API pubblica di `stls_identity`, non ne fa
parte.

## ✅ Stato di verifica

`create_trust_registry_router()` è confermato funzionante per davvero
— il router si costruisce, FastAPI lo monta, ed espone endpoint reali
(verificato tramite `/docs`, la documentazione interattiva che FastAPI
genera automaticamente). L'unico errore iniziale era nei **path**
assunti (avevo ipotizzato `/trust-anchors`, quelli veri sono sotto,
con prefisso `/trust-registry/`) — corretto dopo verifica diretta con
`/openapi.json`, non più a memoria.

## Perché tre processi separati, non due

`agent_identity.py` è conversazionale — un operatore chiede "registra
Operatore B come Authority riconosciuta". `run_identity_bridge.py`
serve identità *interne* a Sparkle verso l'esterno. Questo terzo
processo serve una cosa concettualmente diversa: non "chi sono i
nostri agenti", ma "di chi ci fidiamo come federazione" — stesso
principio di separazione avuto per il bridge, applicato a un dominio
diverso (governance della federazione, non identità operativa).

## Perché è più semplice del bridge dell'identità

Il bridge dell'identità ha bisogno di un task periodico perché la
status list è una bitstring compressa che va ricalcolata e
ripubblicata. Qui non c'è nulla da comprimere: ogni richiesta legge
Postgres direttamente. Nessun refresher, nessun intervallo da tarare
contro l'MRI — solo lettura diretta.

## Architettura in breve

```
                    ┌─────────────────────────┐
   onboard/revoke   │   agent_identity.py      │
   Authority ───────▶│   (tools_identity.py)    │
                    └──────────┬───────────────┘
                               │  scrive
                    ┌──────────▼───────────────┐
                    │  Postgres                 │
                    │  sf_identity_trust_anchors│
                    └──────────┬───────────────┘
                               │  legge, ad ogni richiesta
                    ┌──────────▼───────────────┐
                    │ run_trust_registry.py     │
                    │  - GET /trust-registry/anchors
                    │  - GET /trust-registry/anchors/{id}
                    └──────────┬───────────────┘
                               │
                     altre piattaforme federate
                    (Operatore B, C, ...) — e Sparkle
                    stessa, internamente, per verificare
                    credenziali esterne in futuro
```

Sola lettura da parte di questo processo — non scrive mai. La
scrittura (onboard/revoke) resta esclusivamente nell'agente.

## Configurazione — `.env.trust_registry`

Copiare `.env.trust_registry.example` in `.env.trust_registry` e
compilare. Variabile critica da tenere **coerente con il `.env`
dell'agente**:

| Variabile | Deve combaciare con | Perché |
|---|---|---|
| `TRUST_REGISTRY_PG_TABLE` | stessa var in `.env.identity_agent` | è la tabella che `onboard_trust_anchor`/`revoke_trust_anchor` scrivono — se diversa, questo server pubblica un registro vuoto o sbagliato |
| `IDENTITY_PG_DSN` | stessa var in `.env.identity_agent` | stesso database "sofia" |

## Avvio

```bash
pip install fastapi uvicorn python-dotenv stls-identity
python3 run_trust_registry.py
# oppure, con reload per sviluppo:
uvicorn run_trust_registry:app --reload --port 8100
```

Porta di default **8100** — diversa da quella del bridge (8000):
sono due servizi HTTP distinti, entrambi esposti verso l'esterno.

All'avvio: crea la tabella se non esiste
(`CREATE TABLE IF NOT EXISTS`, stesso self-bootstrap degli altri due
processi, nessuna migrazione manuale).

## Endpoint esposti (confermati via `/docs`)

| Endpoint | Cosa restituisce |
|---|---|
| `GET /trust-registry/anchors` | Elenco delle Authority attualmente riconosciute (generazione di chiave corrente, non revocata) |
| `GET /trust-registry/anchors/{authority_id}` | Una specifica Authority — 404 se sconosciuta o revocata |

## Come si onboarda un'Authority, in pratica

Non tramite questo processo — solo tramite `agent_identity.py`
(`onboard_trust_anchor`), conversazionalmente. I quattro campi si
copiano **esattamente** dal DID Document che l'altra piattaforma
pubblica sul proprio bridge:

```
"Registra Operatore B come Authority riconosciuta:
 - authority_id: operatore-b
 - did: did:web:b.example.com
 - verification_method_id: did:web:b.example.com#key-1
 - public_key_multibase: z6Mk..."
```

Nessuna chiamata di rete automatica per andare a prendere questi dati
— l'onboarding è un'azione amministrativa deliberata (Opzione A: è
Sparkle, come anchor operator, a decidere chi entra in federazione),
non un processo automatico che si fida di qualunque DID Document
trovi in giro.

## Cosa NON fa (deliberatamente, per ora)

**Non verifica credenziali emesse da un'Authority esterna.** Questo
registro dice solo "quale chiave ha attualmente Operatore B" — non
"questa specifica credenziale emessa da Operatore B è ancora valida".
Per quello servirebbe anche contattare il bridge *di quella
piattaforma* per la sua status list (loro `run_identity_bridge.py`
equivalente), un pezzo a sé, costruito quando servirà davvero,
appoggiandosi a questo registro come primo passo (risoluzione della
chiave) ma non fermandosi qui.

## Testare il componente

Non esiste ancora uno script di test live dedicato (a differenza del
bridge dell'identità, che ha `test_identity_bridge_live.py`) — dato
che questo processo è sola lettura e più semplice, il modo più diretto
per ora è: onboardare un'Authority via `agent_identity.py`, poi
controllare con `curl`:

```bash
curl http://localhost:8100/trust-registry/anchors
curl http://localhost:8100/trust-registry/anchors/operatore-b
```

e verificare a mano che il contenuto combaci con quello passato a
`onboard_trust_anchor`. Se vuoi, posso scrivere l'equivalente
automatizzato di `test_identity_bridge_live.py` per questo componente.

## Troubleshooting

**`GET /trust-registry/anchors` risponde vuoto anche dopo un onboarding**
Verificare che `TRUST_REGISTRY_PG_TABLE` in `.env.trust_registry`
combaci esattamente con quella in `.env.identity_agent` — è l'errore
di configurazione più probabile con due `.env` separati, stesso
schema di problema già capitato con il bridge dell'identità.

**`GET /trust-registry/anchors/{authority_id}` risponde 404 per un'Authority appena onboardata**
Controllare di aver passato lo stesso `authority_id` esatto (case-sensitive)
usato in `onboard_trust_anchor` — è un id breve, non il DID.

**Un'Authority revocata compare ancora**
`revoke_trust_anchor` chiude *tutte* le generazioni di chiave
(`revoke_authority`, non `revoke_key`) — se compare ancora, verificare
di aver chiamato `revoke_trust_anchor` con l'`authority_id` giusto, e
non — per errore — `verification_method_id` o `did`.