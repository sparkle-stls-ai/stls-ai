# Identity Bridge — README

Server HTTP che pubblica pubblicamente due cose per la Governing
Authority di questa piattaforma: i **DID Document** dei suoi agenti e
la **status list** di revoca. È il pezzo che permette a un
verificatore esterno (un'altra piattaforma federata) di controllare
un'identità **senza mai ricontattare direttamente** l'agente o
l'Authority in tempo reale — legge solo questi due endpoint pubblici.

File: `run_identity_bridge.py` — indipendente da `agent_identity.py`
(propria configurazione, proprio processo). Entrambi importano
`stls_identity` come dipendenza esterna, nessuno dei due vive dentro
il package.

## ⚠️ Stato di verifica

La firma di `create_identity_bridge_router()`, `StatusListManager.publish()`
e `StatusListCredential` in questo file sono ricostruite dalla
documentazione di progetto — il sorgente reale (`stls_id_bridge.py`)
non è stato riverificato in questa sessione (perso in un reset del
sandbox). Il codice compila ed è internamente coerente, ma **non è
stato eseguito con successo contro il package reale**. Da confermare
riga per riga la prima volta che gira per davvero.

## Perché due processi separati (bridge + agente)

`agent_identity.py` è conversazionale, pensato per un operatore umano
che chiede "crea un'identità per l'agente X". `run_identity_bridge.py`
è un server HTTP che deve stare sempre acceso, servire richieste
esterne, e tenersi aggiornato da solo — due cicli di vita diversi,
due processi diversi, ognuno col proprio `.env`.

## Architettura in breve

```
                    ┌─────────────────────────┐
   scrive           │   agent_identity.py      │
   DID + revoche ──▶│   (tools_identity.py)    │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  Postgres (DID store)     │
                    │  Redis (bitstring revoca) │
                    └──────────┬───────────────┘
                               │  legge + ripubblica
                               │  ogni N secondi
                    ┌──────────▼───────────────┐
                    │ run_identity_bridge.py    │
                    │  - GET /agents/{id}/did.json
                    │  - GET /identity/status-list
                    └──────────┬───────────────┘
                               │
                        verificatore esterno
                     (altra piattaforma federata)
```

Il bridge non scrive mai nulla — legge quello che l'agente ha già
scritto (DID Document via Postgres, stato di revoca via Redis) e lo
ripubblica in formato pubblico/cacheabile.

## Il task periodico — perché esiste

`IdentityManager.revoke()` aggiorna **solo il bit su Redis**. Non
pubblica nulla verso l'esterno da solo — sono due passi
deliberatamente separati (revocare è istantaneo e locale, pubblicare
è un'operazione di rete verso una cache condivisa). Se nessuno chiama
mai `StatusListManager.publish()` + scrive il risultato nella cache
Postgres, `/identity/status-list` risponde:

```json
{"detail":"status list not published yet — refresher task has not run"}
```

`run_identity_bridge.py` risolve questo con un task in background,
avviato a `startup`:

1. **Pubblica una volta subito** — altrimenti l'errore sopra resta
   finché non scatta il primo giro del timer
2. Poi ogni `STATUS_LIST_REFRESH_INTERVAL_SECONDS` (default 60s)

L'intervallo **deve restare sotto l'MRI** (Maximum Revocation
Interval, 5 minuti / 300s nel codice di `verifier.py`) — un
verificatore esterno rifiuta una credenziale se lo snapshot della
status list che ha in cache è più vecchio dell'MRI. Pubblicare ogni
60s lascia ampio margine.

## Configurazione — `.env.identity_bridge`

Copiare `.env.identity_bridge.example` in `.env.identity_bridge` e
compilare. Variabili critiche da tenere **coerenti con il `.env`
dell'agente**:

| Variabile | Deve combaciare con | Perché |
|---|---|---|
| `IDENTITY_AUTHORITY_ID` | stessa var nel `.env` dell'agente | è la bitstring che `IdentityManager.revoke()` aggiorna — se diversa, il bridge pubblica lo stato di revoca di un'Authority sbagliata |
| `STATUS_LIST_PUBLISH_URL` | stessa var nel `.env` dell'agente | va dentro ogni `CredentialStatus.statusListCredential` emesso — se diverso, punta a un URL che questo bridge non serve |
| `IDENTITY_PG_DSN` / `IDENTITY_PG_DID_TABLE` | stesse var nel `.env` dell'agente | il bridge legge i DID Document che l'agente ha scritto — tabelle diverse = bridge vuoto |
| `STATUS_LIST_REDIS_URL` | stessa var nel `.env` dell'agente | stessa bitstring, stesso Redis |
| `IDENTITY_KEYSTORE_BACKEND` / `IDENTITY_KEYSTORE_REDIS_URL` / `IDENTITY_KEYSTORE_REDIS_NAMESPACE` | stesse var nel `.env` dell'agente | il bridge risolve (mai rigenera) la chiave dell'Authority per pubblicare il suo DID Document — valori diversi = chiave diversa da quella con cui l'Authority firma davvero |

Tutte le altre variabili sono documentate inline in
`.env.identity_bridge.example`.

## Avvio

```bash
pip install fastapi uvicorn python-dotenv redis stls-identity
python3 run_identity_bridge.py
# oppure, con reload per sviluppo:
uvicorn run_identity_bridge:app --reload --port 8000
```

All'avvio, in ordine:
1. Crea le tabelle Postgres se non esistono (`CREATE TABLE IF NOT EXISTS`, nessuna migrazione manuale)
2. Pubblica la status list una prima volta
3. Avvia il task periodico

## Endpoint esposti

| Endpoint | Cosa restituisce |
|---|---|
| `GET /agents/{agent_id}/did.json` | Il DID Document dell'agente, se pubblicato (`bootstrap_agent_identity` lo scrive) |
| `GET /identity/status-list` | Lo `StatusListCredential` corrente — bitstring compressa, chi è revocato |

## Testare il componente

Vedi `test_identity_bridge_live.py` — script standalone, non pytest,
pensato per essere lanciato manualmente contro un bridge già in
esecuzione (`python3 run_identity_bridge.py` in un altro terminale).

```bash
python3 test_identity_bridge_live.py
```

Copre, in sequenza, contro HTTP reale:
1. Bootstrap + issue di un'identità (via `tools_identity.py`, non via HTTP — è l'agente che scrive, il bridge solo legge)
2. `GET /agents/{id}/did.json` — deve rispondere con il documento appena creato
3. `GET /identity/status-list` — deve rispondere (non più l'errore "not published yet", grazie alla pubblicazione immediata a startup)
4. Revoca della credenziale
5. Attesa di un ciclo di refresh (`STATUS_LIST_REFRESH_INTERVAL_SECONDS` + margine)
6. Ri-lettura di `/identity/status-list` — la bitstring deve riflettere la revoca

## Troubleshooting

**`{"detail":"authority DID Document not bootstrapped yet"}`**
`GET /.well-known/did.json` — risolto: fino a poco fa questo processo
non pubblicava mai il DID Document dell'Authority stessa (solo quello
degli agenti, via `agent_id`). Ora lo fa da solo allo `startup`
(`agent_path_segments=[]`, `controller` auto-referenziale — diverso
dal DID Document di un agente qualunque). Se lo vedi ancora dopo aver
aggiornato il file, controlla che `IDENTITY_KEYSTORE_BACKEND` e le
altre variabili keystore in `.env.identity_bridge` combacino
esattamente con `.env.identity_agent` (vedi tabella sopra) — altrimenti
`bootstrap_signing_key()` qui genera/risolve una chiave diversa, e la
`put()` verso Postgres può fallire silenziosamente per altri motivi di
connessione.

**`{"detail":"status list not published yet — refresher task has not run"}`**
Il bridge non è mai riuscito a pubblicare. Controllare i log
all'avvio: se Redis o Postgres non sono raggiungibili, `_publish_once()`
fallisce silenziosamente nel task periodico (loggato come eccezione,
non fa crashare il processo) — controllare `IDENTITY_PG_DSN` e
`STATUS_LIST_REDIS_URL`.

**`GET /agents/{id}/did.json` risponde 404 anche dopo il bootstrap**
Verificare che `IDENTITY_PG_DSN`/`IDENTITY_PG_DID_TABLE` nel `.env`
del bridge puntino allo **stesso** database/tabella dell'agente — è
l'errore di configurazione più probabile con due `.env` separati.

**La revoca non si riflette in `/identity/status-list` per un po'**
Atteso, non un bug: il bridge la pubblica al **prossimo giro** del
refresher, non istantaneamente. Il ritardo massimo è
`STATUS_LIST_REFRESH_INTERVAL_SECONDS`.