"""
test_arqit_py_real_peering.py
-------------------------------
Test E2E con la cloud Arqit SKA-Platform™ REALE — non un mock, non un
sandbox: richiede un device pair vero, già registrato e provisionato su
un tenant Arqit reale. Per questo è escluso di default (skip automatico,
vedi _real_arqit_env_available() sotto) e non gira in CI — stessa
scelta già documentata per i test E2E arqit_py di stls-mcp.

Perché questo file esiste (il punto sotto test, non solo "funziona"):
-----------------------------------------------------------------------
Investigazione del 2026-10-17 (ARQIT_PEERING_NOTES.md in sidecar/): un
primo tentativo di peering arqit_py reale falliva con
`401 NotAuthorizedException` sulla chiamata di bilocation "final" — sia
chiamando KeyProviderClientArqitPy/KeyProviderServerArqitPy nello stesso
processo Python, sia passando per il sidecar Node (che è comunque un
solo processo Python che serve entrambe le sessioni RPC). Lo stesso
identico device pair, stessa config, stessa cache Redis, ma con SERVER e
CLIENT lanciati come DUE PROCESSI OS SEPARATI (il pattern già usato da
vault_server.py/vault_client.py in stls-mcp) — funziona. Confermato con
un A/B controllato usando solo stls_core, zero stls_mcp.

Conclusione: l'SDK Arqit (o la validazione lato backend) non tollera
avere le sessioni SERVER e CLIENT dello stesso device pair vive nello
stesso processo OS. Non è un problema di stls-core vs stls-mcp, non è
mai stato un problema lato Arqit da riaprire con un ticket — è un
vincolo architetturale da rispettare SEMPRE quando si usa il provider
arqit_py con ruoli SERVER e CLIENT nel medesimo codebase/deployment.

Questo file è quindi anche la documentazione eseguibile di quel
vincolo, per chi userà questo package in futuro (incluso il porting
Node, dove il sidecar arqit_py dovrà rispettare la stessa regola — vedi
ARQIT_PEERING_NOTES.md, sezione "Consequence for the Node port").

Cosa viene verificato, davvero — non solo "il key agreement non fallisce":
---------------------------------------------------------------------------
test_two_separate_processes_succeeds e test_n_sequential_clients_...
non si limitano a verificare che client e server ottengano CIASCUNO una
chiave senza errori. Fanno il giro completo, con primitive reali di
stls_core (non simulate):
  1. key agreement reale con Arqit (come prima)
  2. handshake STLS reale — `perform_handshake()` lato server,
     `verify_handshake_proof()` lato client — che dimostra che le due
     chiavi (ottenute indipendentemente da due provider in due processi
     diversi) sono EFFETTIVAMENTE la stessa chiave, non solo "entrambe
     valide"
  3. un vero round-trip di cifratura `StlsTransport` (AES-256-GCM +
     HMAC-SHA256) sulla sessione appena stabilita: il client cifra un
     payload, il server lo decifra e rimanda un eco cifrato, il client
     lo decifra e verifica il contenuto
Dato che SERVER e CLIENT vivono in due processi OS separati senza un
canale diretto tra loro, è l'orchestratore (il test) a fare da relay
tra gli stdin/stdout dei due, inoltrando le righe JSON del protocollo
handshake/echo nell'ordine giusto — vedi _run_full_roundtrip_client().

Due test "pattern corretto":
  - test_two_separate_processes_succeeds   — il modo CORRETTO di usare
    arqit_py con entrambi i ruoli: due processi `python` indipendenti
    (_arqit_py_real_e2e_server_role.py / _client_role.py), esattamente
    come vault_server.py + vault_client.py. Deve completare l'intero
    giro (key agreement + handshake + encrypt/decrypt), senza alcun 401.
  - test_same_process_reproduces_401       — documenta l'anti-pattern:
    entrambi i ruoli nello STESSO processo pytest. Il key agreement deve
    fallire (il client non ottiene mai una chiave — il vero
    `401 NotAuthorizedException` resta visibile solo nei log del thread
    di negoziazione in background, non nell'eccezione propagata al
    chiamante, che è un TimeoutError del keystore locale — vedi il
    commento sul test). Se un giorno questo test smette di fallire, è un
    segnale che il vincolo è cambiato lato SDK/Arqit — a quel punto va
    aggiornato ARQIT_PEERING_NOTES.md.

Setup richiesto per eseguirlo (altrimenti skip automatico):
  - estra `[arqit_py]` installato (wheel Arqit SKA-Platform™ reali)
  - ARQIT_PY_CLIENT_CONFIG / ARQIT_PY_SERVER_CONFIG → path assoluti ai
    JSON di configurazione di un device pair reale, già provisionato
  - attenzione: i campi DEVICE_METADATA_FILEPATH/DEVICE_PROPERTIES_FILEPATH/
    DEVICE_POLICY_FILEPATH dentro quei JSON sono path RELATIVI, risolti
    contro la cwd del processo (non contro la cartella del JSON) — lancia
    pytest dalla stessa cwd in cui quei file relativi esistono davvero,
    altrimenti l'autenticazione device fallisce prima ancora di arrivare
    al bilocation (FileNotFoundError, non un 401).
  - rilanciare questo file a raffica (molte esecuzioni in pochi minuti)
    può incontrare rate-limiting/flakiness reale lato cloud Arqit —
    osservato durante lo sviluppo di questo test come timeout o
    "Address already in use" apparentemente spurio. Non è un difetto del
    test: se capita, aspetta qualche minuto prima di rilanciare.

Uso:
    cd python/stls-core
    ARQIT_PY_CLIENT_CONFIG=/path/to/client.json \\
    ARQIT_PY_SERVER_CONFIG=/path/to/server.json \\
    pytest tests/test_arqit_py_real_peering.py -v -s
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SERVER_ROLE_SCRIPT = _TESTS_DIR / "_arqit_py_real_e2e_server_role.py"
_CLIENT_ROLE_SCRIPT = _TESTS_DIR / "_arqit_py_real_e2e_client_role.py"


def _receiver_port() -> int:
    """
    Legge SOCKET_LISTENING_PORT dal config JSON del server — serve solo
    per sapere quale porta attendere libera dopo aver terminato il
    processo figlio del ruolo SERVER (vedi _wait_port_free sotto).
    """
    with open(os.environ["ARQIT_PY_SERVER_CONFIG"]) as fh:
        return int(json.load(fh).get("SOCKET_LISTENING_PORT", 8889))


def _wait_port_free(port: int, host: str = "127.0.0.1", timeout: float = 65.0) -> None:
    """
    Attende che `host:port` sia effettivamente BINDABILE — non solo che
    nessuno accetti connessioni lì. `Popen.terminate()` + `.wait()`
    garantisce che il PROCESSO sia uscito, non che il kernel abbia già
    rilasciato il socket in ascolto: un bind() su una porta appena
    liberata da un listening socket può restare in TIME_WAIT e fallire
    anche se un connect() a quella porta darebbe già "connection
    refused" (sono due condizioni diverse). Replica esattamente il
    check che l'SDK Arqit fa in `_check_loopback_port()`
    (`socket_receiver.py`): un bind() nudo, senza SO_REUSEADDR, sullo
    stesso host/porta. Senza questa attesa, il test successivo che
    bindi la stessa porta (nello stesso processo pytest o in un altro
    sottoprocesso) può fallire con "Address already in use" per un
    semplice problema di timing dell'OS, non per il vincolo
    same-process sotto test altrove in questo file.

    Default 65s, non 10-15s come nella prima versione: il TIME_WAIT di
    TCP su Linux per una connessione realmente stabilita e chiusa è
    fisso a ~60s (2×MSL, non riducibile via opzioni socket lato
    "probe") — un timeout più corto fallisce in modo riproducibile ogni
    volta che almeno una connessione reale client→receiver è stata
    aperta e chiusa su questa porta (cioè in ogni run che non sia il
    primissimo). Non è flakiness del cloud Arqit: è il comportamento
    documentato del kernel.
    """
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return  # bind riuscito — porta davvero libera
            except OSError as exc:
                last_error = exc
        time.sleep(0.1)
    raise TimeoutError(
        f"{host}:{port} ancora non bindabile dopo {timeout}s: {last_error}"
    )


def _real_arqit_env_available() -> tuple[bool, str]:
    """
    True se questo ambiente ha tutto il necessario per un peering arqit_py
    reale: wheel SDK installati + config client/server puntate a file
    esistenti. Restituisce anche il motivo dello skip, per leggibilità.
    """
    try:
        import uk.arqit.device.common  # noqa: F401
    except ImportError:
        return False, "wheel Arqit SKA-Platform™ non installati (estra [arqit_py])"

    client_cfg = os.getenv("ARQIT_PY_CLIENT_CONFIG")
    server_cfg = os.getenv("ARQIT_PY_SERVER_CONFIG")
    if not client_cfg or not server_cfg:
        return False, "ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG non impostate"
    if not Path(client_cfg).is_file() or not Path(server_cfg).is_file():
        return False, "ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG puntano a file inesistenti"

    return True, ""


_available, _skip_reason = _real_arqit_env_available()
pytestmark = pytest.mark.skipif(not _available, reason=_skip_reason)


# ---------------------------------------------------------------------------
# Relay dell'orchestratore tra i due processi — handshake + encrypt/decrypt
# ---------------------------------------------------------------------------

def _start_server_process() -> subprocess.Popen:
    """
    Avvia il ruolo SERVER con stdin in pipe (per inviargli comandi
    handshake/echo) e stderr su file temporaneo — con stdin/stdout in
    pipe per il dialogo col test, lo stderr (molto verboso: log
    DEBUG/INFO dell'intero SDK Arqit) NON può restare anch'esso in
    `PIPE` senza che qualcuno lo consumi: il buffer del pipe si riempie
    e il processo figlio si blocca sulla write(), appendendo l'intero
    test. Un file è letto solo a posteriori, se serve per debug.
    """
    stderr_file = tempfile.TemporaryFile(mode="w+")
    proc = subprocess.Popen(
        [sys.executable, str(_SERVER_ROLE_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_file,
        text=True,
        bufsize=1,
    )
    proc._stderr_file = stderr_file  # type: ignore[attr-defined]
    return proc


def _stop_server_process(server_proc: subprocess.Popen) -> None:
    try:
        if server_proc.stdin and not server_proc.stdin.closed:
            server_proc.stdin.write(json.dumps({"cmd": "stop"}) + "\n")
            server_proc.stdin.flush()
            server_proc.stdin.close()
        server_proc.wait(timeout=5)
    except Exception:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait(timeout=5)
    finally:
        stderr_file = getattr(server_proc, "_stderr_file", None)
        if stderr_file is not None:
            stderr_file.close()


def _run_full_roundtrip_client(server_proc: subprocess.Popen, label: str) -> dict[str, str]:
    """
    Avvia il ruolo CLIENT come sottoprocesso interattivo e fa da relay,
    riga per riga, tra lui e `server_proc` (già vivo e pronto) per
    l'intero protocollo handshake + echo cifrato descritto nel
    docstring del modulo. I due ruoli non si parlano mai direttamente —
    solo questa funzione ha gli handle di entrambi gli stdin/stdout.

    Asserisce il successo di ogni fase (key agreement, handshake,
    round-trip di cifratura); solleva AssertionError con l'intero
    output catturato se qualcosa fallisce o arriva fuori sequenza.

    Ritorna {"kid": ..., "session_id": ...} — usato da chi chiama per
    verificare che handshake distinti (es. client diversi contro lo
    stesso server) ottengano identificatori distinti.
    """
    client_stderr_file = tempfile.TemporaryFile(mode="w+")
    client_proc = subprocess.Popen(
        [sys.executable, str(_CLIENT_ROLE_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=client_stderr_file,
        text=True,
        bufsize=1,
    )

    def _client_stderr_tail() -> str:
        client_stderr_file.seek(0)
        return client_stderr_file.read()[-4000:]

    try:
        assert server_proc.stdin is not None and server_proc.stdout is not None
        assert client_proc.stdin is not None and client_proc.stdout is not None

        key_ok_line = client_proc.stdout.readline()
        assert key_ok_line.startswith("KEY_OK"), (
            f"[{label}] key agreement fallito: {key_ok_line!r}\n"
            f"client stderr (coda):\n{_client_stderr_tail()}"
        )

        handshake_req_line = client_proc.stdout.readline()
        req_msg = json.loads(handshake_req_line)
        assert req_msg.get("cmd") == "handshake", f"[{label}] riga inattesa: {handshake_req_line!r}"

        server_proc.stdin.write(json.dumps(req_msg) + "\n")
        server_proc.stdin.flush()
        handshake_resp_line = server_proc.stdout.readline()
        resp_msg = json.loads(handshake_resp_line)
        assert resp_msg.get("cmd") == "handshake_response", (
            f"[{label}] risposta server inattesa: {handshake_resp_line!r}"
        )

        client_proc.stdin.write(json.dumps(resp_msg) + "\n")
        client_proc.stdin.flush()

        handshake_status_line = client_proc.stdout.readline()
        assert handshake_status_line.startswith("HANDSHAKE_OK"), (
            f"[{label}] handshake (proof-of-key-possession) fallito: "
            f"{handshake_status_line!r} — client e server NON condividono "
            f"la stessa chiave, o il proof è errato.\n"
            f"client stderr (coda):\n{_client_stderr_tail()}"
        )

        echo_req_line = client_proc.stdout.readline()
        echo_req_msg = json.loads(echo_req_line)
        assert echo_req_msg.get("cmd") == "echo", f"[{label}] riga inattesa: {echo_req_line!r}"

        server_proc.stdin.write(json.dumps(echo_req_msg) + "\n")
        server_proc.stdin.flush()
        echo_resp_line = server_proc.stdout.readline()
        echo_resp_msg = json.loads(echo_resp_line)
        assert echo_resp_msg.get("cmd") == "echo_response", (
            f"[{label}] risposta server inattesa: {echo_resp_line!r}"
        )

        client_proc.stdin.write(json.dumps(echo_resp_msg) + "\n")
        client_proc.stdin.flush()

        final_line = client_proc.stdout.readline()
        print(f"[{label}] {key_ok_line.strip()} | {handshake_status_line.strip()} | {final_line.strip()}")

        assert "roundtrip=ok" in final_line and final_line.startswith("CLIENT_PASSED"), (
            f"[{label}] round-trip di cifratura reale fallito: {final_line!r}\n"
            f"client stderr (coda):\n{_client_stderr_tail()}"
        )

        client_proc.stdin.close()
        returncode = client_proc.wait(timeout=10)
        assert returncode == 0, f"[{label}] client terminato con exit code {returncode}"

        kid = key_ok_line.split("kid=", 1)[1].split()[0]
        session_id = handshake_status_line.split("session_id=", 1)[1].split()[0]
        return {"kid": kid, "session_id": session_id}
    finally:
        if client_proc.poll() is None:
            client_proc.kill()
            client_proc.wait(timeout=5)
        client_stderr_file.close()


# ---------------------------------------------------------------------------
# Pattern corretto: due processi OS separati
# ---------------------------------------------------------------------------

def test_two_separate_processes_succeeds():
    """
    SERVER e CLIENT in due processi `python` indipendenti — lo stesso
    pattern di vault_server.py/vault_client.py. Deve completare l'intero
    giro: key agreement reale con Arqit, handshake STLS (proof che le
    due chiavi coincidono davvero), e un vero round-trip di cifratura
    AES-256-GCM — senza alcun 401.
    """
    server_proc = _start_server_process()
    try:
        assert server_proc.stdout is not None
        ready_line = server_proc.stdout.readline()
        assert "SERVER_READY" in ready_line, (
            f"server role non pronto, output: {ready_line!r}"
        )

        _run_full_roundtrip_client(server_proc, label="single-client")
    finally:
        _stop_server_process(server_proc)
        _wait_port_free(_receiver_port())


# ---------------------------------------------------------------------------
# N client sequenziali contro UN server long-running
# ---------------------------------------------------------------------------

_N_CLIENTS = 3


def test_n_sequential_clients_against_one_server_succeeds():
    """
    UN SOLO processo SERVER, lasciato vivo, contro cui girano N client
    SEQUENZIALI (stesso identity di device, N negoziazioni separate) —
    lo scenario di produzione reale: un server stls-mcp/stls-a2a resta
    attivo a lungo, e riceve handshake da client diversi (o lo stesso
    client più volte) nel tempo, non un solo client e poi shutdown.

    Prima di questo test, l'unico scenario mai verificato empiricamente
    era 1 server + 1 client, una volta. Non era garantito che il
    provider SERVER (istanza KeySessionProviderArqitPy long-running,
    vedi il suo docstring in stls_core_providers_arqit_py.py) servisse
    correttamente un SECONDO handshake dopo il primo — il meccanismo
    `_delivered_key_ids` + `wait_for_new_key(seen_key_ids=...)` è
    progettato per questo, ma solo testato qui per la prima volta contro
    la cloud Arqit reale.

    Verifica, per ciascuno degli N client, l'intero giro (key agreement
    + handshake + encrypt/decrypt reale — vedi _run_full_roundtrip_client)
    contro lo STESSO server senza riavviarlo, e che kid/session_id siano
    tutti DIVERSI tra loro — se il server riconsegnasse lo stesso kid del
    primo handshake a tutti i successivi, sarebbe un bug di sicurezza
    silenzioso (sessioni STLS diverse con la stessa chiave), non solo un
    errore funzionale.
    """
    server_proc = _start_server_process()
    results: list[dict[str, str]] = []
    try:
        assert server_proc.stdout is not None
        ready_line = server_proc.stdout.readline()
        assert "SERVER_READY" in ready_line, (
            f"server role non pronto, output: {ready_line!r}"
        )

        for i in range(_N_CLIENTS):
            result = _run_full_roundtrip_client(server_proc, label=f"client-{i + 1}/{_N_CLIENTS}")
            results.append(result)
    finally:
        _stop_server_process(server_proc)
        # Usa il default della funzione (65s): dopo N negoziazioni reali,
        # l'ultima connessione chiusa sulla porta del receiver è comunque
        # soggetta allo stesso TIME_WAIT fisso di ~60s di una singola
        # negoziazione — non serve un timeout diverso, serve solo che sia
        # abbastanza lungo in assoluto (vedi _wait_port_free).
        _wait_port_free(_receiver_port())

    assert len(results) == _N_CLIENTS
    kids = [r["kid"] for r in results]
    session_ids = [r["session_id"] for r in results]
    assert len(set(kids)) == _N_CLIENTS, (
        f"kid duplicati tra client diversi — stesso server avrebbe "
        f"consegnato la stessa chiave a sessioni STLS diverse: {kids!r}"
    )
    assert len(set(session_ids)) == _N_CLIENTS, (
        f"session_id duplicati tra handshake diversi: {session_ids!r}"
    )


# ---------------------------------------------------------------------------
# Anti-pattern: stesso processo — documenta il vincolo
# ---------------------------------------------------------------------------

def test_same_process_reproduces_401():
    """
    ANTI-PATTERN, intenzionale: SERVER e CLIENT nello stesso processo
    pytest. Riproduce il 401 reale lato Arqit documentato in
    ARQIT_PEERING_NOTES.md — NON copiare questo pattern in codice di
    produzione (vedi test sopra per il pattern corretto).

    Se questo test smette di fallire (cioè `pytest.raises` non intercetta
    più alcuna eccezione), significa che Arqit o il loro SDK hanno
    rimosso il vincolo same-process — a quel punto va aggiornato
    ARQIT_PEERING_NOTES.md e rivista la necessità dell'isolamento di
    processo nel sidecar Node (arqit-sidecar.ts).

    Nota sulla forma dell'eccezione osservata: il thread di negoziazione
    del client (`_negotiate`, avviato in background) è quello che riceve
    il vero `401 NotAuthorizedException` dalla chiamata di bilocation —
    ma lo logga (ERROR) senza propagarlo al chiamante. Il codice in
    foreground (`getSessionKeyIdentifier`/`getSessionKeyData`) vede solo
    la conseguenza: nessuna chiave mai consegnata, quindi un
    `TimeoutError` dal keystore locale. È questo `TimeoutError` che il
    test intercetta con `pytest.raises` — il `401`/`NotAuthorizedException`
    reale resta visibile solo nei log (stderr, con `-s`/`--debug`), non
    nel testo dell'eccezione stessa. Per questo l'assert sotto non cerca
    quelle stringhe nell'eccezione: verifica solo che il key agreement
    non sia mai riuscito, che è l'impatto osservabile dal chiamante.
    """
    import asyncio

    from stls_core.providers.stls_core_providers_factory import KeyProviderFactory

    key_provider = KeyProviderFactory().get_provider("arqit_py")

    # Bypass deliberato di KeySessionProviderFactory: qui serve un handle
    # diretto a server/client (non solo alla "sessione") per poterli
    # fermare in teardown — altrimenti il thread receiver del server
    # resterebbe in ascolto sulla porta per il resto del processo pytest,
    # rompendo qualunque test successivo che tenti di bindare la stessa
    # porta (è esattamente il tipo di problema di pulizia che questo file
    # deve evitare di introdurre lui stesso — vedi _wait_port_free sopra).
    server = key_provider.create_provider_server(port=None, options={})
    asyncio.run(server.start())
    client = key_provider.create_provider_client(host="", port=0, options={})
    client.start()
    client_provider = client.createProviderSession()

    try:
        # getSessionKeyIdentifier/getSessionKeyData sono async.
        async def _fetch():
            kid = await client_provider.getSessionKeyIdentifier()
            return await client_provider.getSessionKeyData(kid)

        with pytest.raises(Exception) as exc_info:
            asyncio.run(_fetch())

        print(f"key agreement failed as expected: {exc_info.value!r}")
    finally:
        client.stop()
        asyncio.run(server.stop())
        _wait_port_free(_receiver_port())
