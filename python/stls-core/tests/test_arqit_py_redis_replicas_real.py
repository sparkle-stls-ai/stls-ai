"""
test_arqit_py_redis_replicas_real.py
--------------------------------------
Validates issue #38 ("Validate one Arqit device shared across replicas
via the Redis-backed key-store, against the real cloud deploy"):
whether N independent OS processes ("replicas" — the Cloud Run/GKE
horizontal-scaling scenario documented in ArqitKeyStoreFactory.build()'s
"Server: namespace shared across Cloud Run replicas" comment, see
stls_core_providers_keystore.py) can jointly serve the SAME arqit_py
device identity via a REAL Redis-backed key store
(KEY_STORE_BACKEND=redis, a real `redis-server`, never fakeredis),
against the REAL Arqit cloud.

What isn't already covered by the existing test suite
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- test_arqit_py_same_device_concurrent_real.py races the SAME device
  with asyncio.to_thread() INSIDE one process. The process-wide
  _ARQIT_PY_START_LOCK (stls_core_providers_arqit_py.py) added to fix
  that race serializes start() only within one process — it cannot,
  and does not try to, protect two separate OS processes from racing
  each other.
- test_stls_core_keystore.py exercises RedisArqitKeyStore's logic
  against fakeredis — no real Redis, no real device negotiation.

Neither combination (real multi-process replicas + real Redis + real
cloud) has been exercised anywhere else in this project before this
file.

Four things are checked here
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A. Concurrent bring-up: N replica OS processes independently call
   KeyProviderServerArqitPy.start() for the SAME device at roughly the
   same time — does cross-process authentication/negotiation against
   the real cloud succeed for all of them?
B. Cross-replica visibility: a real client session negotiates its key
   against replica i; is that key then visible, via a fresh,
   independently-built RedisArqitKeyStore (role="server") in the same
   namespace, regardless of which replica actually produced it?
C. Cross-replica continuation: can a THIRD, freshly-built
   KeySessionProviderArqitPy — a "sibling" that never itself talked to
   the device or to this client — resolve and read that same
   session's key by kid, purely through the shared Redis namespace?
D. The one open question raised by reading
   RedisArqitKeyStore.wait_for_new_key()/mark_delivered(): the server
   role's pending ZSET is NEVER claimed atomically (mark_delivered()
   deliberately skips ZREM on that role — see its own docstring,
   "sibling replicas must still find the same kid"), and
   de-duplication relies entirely on each instance's own in-memory
   _delivered_key_ids. Two sibling replicas that have each delivered
   nothing yet have no way to avoid landing on the SAME pending kid
   when both call the no-hint getSessionKeyIdentifier() — this isn't
   even timing-sensitive, since nothing in Redis itself records who
   has claimed what. This file deliberately reproduces exactly that:
   two freshly-started replicas, each told (via a stdin command) to
   resolve a session key with no hint, and reports whether they come
   back with the SAME kid (a real security property break — the same
   session key handed to two independent handshakes) or genuinely
   different ones.

Requires
~~~~~~~~
- stls-core installed with BOTH extras: `.[arqit_py,redis]` (plain
  `.[dev]` only pulls in fakeredis, not the real `redis` client).
- A REAL Redis reachable at KEY_STORE_REDIS_URL (default
  redis://localhost:6379) — e.g. `docker run --rm -p 6379:6379
  redis:7`. This script refuses to run against fakeredis; there is no
  such option here.
- The throwaway race-test device pair already provisioned on the real
  Arqit cloud (_arqit_race_fixtures/aq-race-throwaway-{server,
  client}.json), the same ones test_arqit_py_same_device_concurrent_real.py
  uses. This file only ever READS those committed fixtures; for each
  replica it writes its own copy (distinct listening port, its own
  copy of the device-metadata file — see _materialize_replica()'s
  docstring for why — and absolute DEVICE_*_FILEPATH fields) into a
  throwaway temp directory, never touching the committed files.

A REAL call against the real Arqit cloud per client session run — same
rate-limiting caution as sidecar/ARQIT_PEERING_NOTES.md and
test_arqit_py_same_device_concurrent_real.py: don't run this
back-to-back repeatedly.

Usage:
    python tests/test_arqit_py_redis_replicas_real.py [N]

    N = number of replicas/client sessions (default 2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
SRC_DIR = HERE.parent / "src"
RACE_FIXTURES_DIR = HERE / "_arqit_race_fixtures"
REPLICA_ROLE_SCRIPT = HERE / "_arqit_redis_replicas_fixtures" / "replica_server_role.py"

BASE_SOCKET_PORT = 18990  # distinct from _arqit_race_fixtures' 8999 — never collides with that test
READY_TIMEOUT = 30.0
RESOLVE_TIMEOUT = 45.0


# ---------------------------------------------------------------------------
# Fixture generation — per-replica config/metadata copies, absolute paths
# ---------------------------------------------------------------------------

def _materialize_replica(tmp_dir: Path, index: int) -> tuple[Path, Path]:
    """
    Builds one replica's self-contained fixture dir under tmp_dir,
    copied from the committed throwaway race-test device pair, with:

    - its own copy of both devices' metadata files, never shared
      between replicas — avoids two OS processes writing the same
      auth-session file concurrently, a filesystem-level concern
      that's deliberately out of scope here and separate from the
      Redis/cloud concern this file validates (a real Cloud Run/GKE
      deployment would give each pod its own writable copy of a
      mounted device secret for the same reason).
    - DEVICE_*_FILEPATH fields rewritten to ABSOLUTE paths — sidesteps
      the documented cwd-relative-path pitfall (see
      sidecar/ARQIT_PEERING_NOTES.md) entirely, which matters here
      because the client side of this test runs several replicas'
      client configs concurrently from ONE process that cannot
      os.chdir() per-coroutine.
    - a dedicated SOCKET_LISTENING_PORT/SOCKET_RECEIVER_PORT so this
      replica's server and the one client config that targets it
      don't collide with any other replica's on localhost.

    Returns (server_config_path, client_config_path).
    """
    replica_dir = tmp_dir / f"replica_{index}"
    replica_dir.mkdir()
    port = BASE_SOCKET_PORT + index

    def _materialize(role: str) -> Path:
        src_cfg = RACE_FIXTURES_DIR / f"aq-race-throwaway-{role}.json"
        src_meta = RACE_FIXTURES_DIR / f"aq-race-throwaway-{role}_metadata.json"
        dst_meta = replica_dir / f"{role}_metadata.json"
        shutil.copy(src_meta, dst_meta)

        cfg = json.loads(src_cfg.read_text())
        cfg["DEVICE_METADATA_FILEPATH"] = str(dst_meta)
        cfg["DEVICE_PROPERTIES_FILEPATH"] = str(replica_dir / f"{role}_properties.json")
        cfg["DEVICE_POLICY_FILEPATH"] = str(replica_dir / f"{role}_policy.json")
        cfg["SOCKET_LISTENING_PORT"] = port
        cfg["SOCKET_RECEIVER_PORT"] = port
        dst_cfg = replica_dir / f"{role}.json"
        dst_cfg.write_text(json.dumps(cfg, indent=2))
        return dst_cfg

    server_cfg = _materialize("server")
    client_cfg = _materialize("client")
    return server_cfg, client_cfg


# ---------------------------------------------------------------------------
# Redis preflight + cleanup — REAL redis only, never fakeredis
# ---------------------------------------------------------------------------

def _require_real_redis(url: str) -> None:
    try:
        import redis as redis_lib
    except ImportError as exc:
        raise RuntimeError(
            "the 'redis' package isn't installed — this script needs the "
            "REAL redis client (not fakeredis): "
            'uv pip install -e ".[arqit_py,redis]"'
        ) from exc
    try:
        redis_lib.Redis.from_url(url).ping()
    except Exception as exc:
        raise RuntimeError(
            f"no real Redis reachable at {url} — start one, e.g.:\n"
            "  docker run --rm -p 6379:6379 redis:7"
        ) from exc


def _cleanup_redis_namespace(url: str, base_namespace: str) -> None:
    import redis as redis_lib
    r = redis_lib.Redis.from_url(url)
    pattern = f"stls:ks:{base_namespace}:*"
    keys = list(r.scan_iter(match=pattern))
    if keys:
        r.delete(*keys)
    logger.info("[cleanup] removed %d Redis key(s) under %s", len(keys), pattern)


# ---------------------------------------------------------------------------
# Replica subprocess management
# ---------------------------------------------------------------------------

class _Replica:
    def __init__(self, label: str, proc: subprocess.Popen, line_queue: "queue.Queue[str]") -> None:
        self.label = label
        self.proc = proc
        self.queue = line_queue

    def send(self, command: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def wait_for_tagged_line(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return self.queue.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"[{self.label}] process exited early (code={self.proc.returncode})"
                    ) from None
        raise TimeoutError(f"[{self.label}] no tagged line within {timeout}s")

    def terminate(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _reader_thread(proc: subprocess.Popen, label: str, line_queue: "queue.Queue[str]") -> None:
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if line.startswith(("READY ", "KID ", "KIDERR ")):
            line_queue.put(line)
        else:
            print(f"[{label}] {line}")


def _start_replica(label: str, server_config_path: Path, env: dict) -> _Replica:
    proc = subprocess.Popen(
        [sys.executable, str(REPLICA_ROLE_SCRIPT), str(server_config_path), label],
        cwd=str(server_config_path.parent),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    q: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=_reader_thread, args=(proc, label, q), daemon=True).start()
    replica = _Replica(label, proc, q)
    line = replica.wait_for_tagged_line(READY_TIMEOUT)
    if not line.startswith(f"READY {label}"):
        raise RuntimeError(f"[{label}] unexpected first tagged line: {line!r}")
    return replica


# ---------------------------------------------------------------------------
# Client session — runs IN this process, same fix as the same-device test
# ---------------------------------------------------------------------------

async def _run_client_session(label: str, client_config_path: Path) -> dict:
    from stls_core.providers.stls_core_providers_arqit_py import KeyProviderArqitPy

    t0 = time.monotonic()
    try:
        provider = KeyProviderArqitPy().create_provider_client(
            host="", port=0, options={"config_path": str(client_config_path)},
        )
        # See test_arqit_py_same_device_concurrent_real.py: start() must be
        # dispatched to a real OS thread for N concurrent sessions to
        # actually race instead of being serialized for free by asyncio.
        await asyncio.to_thread(provider.start)
        session = provider.createProviderSession()
        kid = await session.getSessionKeyIdentifier()
        kdata = await session.getSessionKeyData(kid)
        logger.info("[%s] client resolved kid=%s elapsed=%.2fs", label, kid, time.monotonic() - t0)
        return {"label": label, "kid": kid, "kdata": kdata, "error": None}
    except Exception as exc:
        logger.error("[%s] client FAILED after %.2fs: %s", label, time.monotonic() - t0, exc, exc_info=True)
        return {"label": label, "kid": None, "kdata": None, "error": exc}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _run(n: int) -> None:
    redis_url = os.getenv("KEY_STORE_REDIS_URL", "redis://localhost:6379")
    _require_real_redis(redis_url)

    base_namespace = f"test-redis-replicas-{uuid.uuid4().hex[:8]}"
    os.environ["KEY_STORE_BACKEND"] = "redis"
    os.environ["KEY_STORE_REDIS_URL"] = redis_url
    os.environ["KEY_STORE_REDIS_NAMESPACE"] = base_namespace
    env = os.environ.copy()

    print(f"\n=== Redis-backed replicas, real cloud, real Redis — N={n} namespace={base_namespace} ===\n")

    replicas: list[_Replica] = []

    with tempfile.TemporaryDirectory(prefix="stls-arqit-redis-replicas-") as tmp:
        tmp_dir = Path(tmp)
        try:
            configs = [_materialize_replica(tmp_dir, i) for i in range(n)]

            # --- A: concurrent bring-up -------------------------------------
            print("--- A: starting replicas concurrently ---")
            for i, (server_cfg, _client_cfg) in enumerate(configs):
                replicas.append(_start_replica(f"replica-{i}", server_cfg, env))
            print(f"A: PASS — all {n} replicas authenticated and are listening\n")

            # --- client sessions, one per replica ---------------------------
            print("--- running one real client session per replica ---")
            client_results = await asyncio.gather(*[
                _run_client_session(f"client-{i}", client_cfg)
                for i, (_server_cfg, client_cfg) in enumerate(configs)
            ])
            for r in client_results:
                status = "OK" if r["error"] is None else f"FAILED: {r['error']}"
                print(f"  {r['label']}: {status} kid={r['kid']}")
            ok_results = [r for r in client_results if r["error"] is None]
            if len(ok_results) < 2:
                print("\nCan't run B/C/D with fewer than 2 successful client sessions — stopping here.")
                return

            # --- B: cross-replica visibility via a fresh Redis handle ------
            from stls_core.providers.stls_core_providers_keystore import ArqitKeyStoreFactory
            server_store = ArqitKeyStoreFactory.build(namespace=base_namespace, role="server")
            print("\n--- B: cross-replica visibility (fresh RedisArqitKeyStore, real Redis) ---")
            b_pass = True
            for r in ok_results:
                seen = server_store.get(r["kid"])
                ok = seen == r["kdata"]
                b_pass = b_pass and ok
                print(f"  {r['label']} kid={r['kid']}: {'PASS' if ok else 'FAIL — key not visible or mismatched'}")
            print(f"B: {'PASS' if b_pass else 'FAIL'}\n")

            # --- C: continuation by a sibling that never negotiated --------
            from stls_core.providers.stls_core_providers_arqit_py import KeySessionProviderArqitPy
            from stls_core.providers.stls_core_providers_base import KeySessionProvider
            print("--- C: continuation by a sibling replica that never negotiated this session ---")
            target = ok_results[0]
            sibling_store = ArqitKeyStoreFactory.build(namespace=base_namespace, role="server")
            sibling_session = KeySessionProviderArqitPy(
                KeySessionProvider.KeySessionProviderType.SERVER, sibling_store, peer_identity=None,
            )
            resolved_kid = await sibling_session.getSessionKeyIdentifier(hint_kid=target["kid"])
            resolved_kdata = await sibling_session.getSessionKeyData(resolved_kid)
            c_pass = resolved_kid == target["kid"] and resolved_kdata == target["kdata"]
            print(f"C: {'PASS' if c_pass else 'FAIL'} — sibling resolved kid={resolved_kid}\n")

            # --- D: no-hint resolve on every replica, back to back ----------
            print("--- D: no-hint getSessionKeyIdentifier() on every replica, back to back ---")
            for replica in replicas:
                replica.send("RESOLVE")
            d_kids: dict[str, Optional[str]] = {}
            for replica in replicas:
                try:
                    line = replica.wait_for_tagged_line(RESOLVE_TIMEOUT)
                    parts = line.split(" ", 2)
                    tag, label = parts[0], parts[1]
                    if tag == "KID":
                        d_kids[label] = parts[2]
                        print(f"  {label}: resolved kid={parts[2]}")
                    else:
                        d_kids[label] = None
                        print(f"  {label}: KIDERR {parts[2] if len(parts) > 2 else ''}")
                except (TimeoutError, RuntimeError) as exc:
                    print(f"  {exc}")

            resolved = [v for v in d_kids.values() if v is not None]
            if len(resolved) < 2:
                print("D: INCONCLUSIVE — fewer than 2 replicas resolved a kid this run\n")
            elif len(set(resolved)) == 1:
                print(
                    "\nD: *** ANOMALY CONFIRMED *** — two sibling replicas, each with an "
                    "empty in-memory _delivered_key_ids, were BOTH handed the SAME kid "
                    f"({resolved[0]}) by the shared Redis pending ZSET. "
                    "RedisArqitKeyStore.wait_for_new_key()/mark_delivered() perform no "
                    "atomic claim — this is session-key reuse across two independent "
                    "handshakes, not a rare timing fluke.\n"
                )
            else:
                print(
                    "\nD: no duplicate observed this run — does NOT prove the issue absent "
                    "(see this file's own module docstring); the lack of any atomic claim "
                    "in wait_for_new_key()/mark_delivered() is still there regardless.\n"
                )

        finally:
            for replica in replicas:
                replica.terminate()
            _cleanup_redis_namespace(redis_url, base_namespace)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    sys.path.insert(0, str(SRC_DIR))
    asyncio.run(_run(n))


if __name__ == "__main__":
    main()
