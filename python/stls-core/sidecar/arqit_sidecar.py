"""
arqit_sidecar.py
-----------------
Sidecar that exposes a KeySessionProvider (stls_core.providers) via a
minimal JSON-RPC protocol over a Unix domain socket, letting a Node.js
process use the Arqit SKA-Platform provider (or any other Python
KeyProvider) without reimplementing the proprietary Arqit SDK in
TypeScript — Arqit does not publish a Node library.

Rationale: stls-core (Node) natively implements the "base" and "pqc"
providers (standard crypto, portable to TypeScript). "arqit_py" instead
requires Arqit's native Python SDK (proprietary wheels, QKD via
SKA-Platform) — there is no Node equivalent. This sidecar is the way to
share that one provider with a Node client/server over local IPC, while
keeping everything else (crypto, handshake, session cache) native on
both sides.

Wire protocol (newline-delimited JSON, one request/response per line):
    -> {"id": <int>, "method": <string>, "params": {...}}
    <- {"id": <int>, "result": ...}  or  {"id": <int>, "error": {"message": ..., "code": ...}}

One connection = one STLS session (the same "Option A" pattern as
KeyProviderClientArqitPy/KeyProviderServerArqitPy: a dedicated
KeyProviderClient/Server per session). Exposed methods:

    authenticate                 {token}           -> {}
    start            {session_type: "CLIENT"|"SERVER", provider_name?,
                       host?, port?, config_path?} -> {}
    get_session_key_identifier   {}               -> {"kid": str}
    set_session_key_identifier   {kid}             -> {}
    get_session_key_data         {kid}             -> {"session_key_b64": str}
    get_peer_identity            {}                -> {"peer_identity": str|null}
    stop                         {}                -> {}

"authenticate" must be the first call on every connection (except
itself) — every other method raises RpcError(code=-32001) until a
matching token has been presented. The expected token is configured via
--token or env ARQIT_SIDECAR_TOKEN (required, no default — the process
refuses to start without one). "start" may only be called once per
connection (RpcError code -32002 on a second call) — a connection's
role/provider/config is fixed for its whole lifetime. See issues
#17 (socket authentication) and #18 (pseudonymous identity / proof
scope / zero key material on teardown).

provider_name default: env KEY_PROVIDER_NAME, then "base". In production
set it to "arqit_py" (requires the Arqit wheels installed — see the
[arqit_py] extra in pyproject.toml).

Usage:
    python sidecar/arqit_sidecar.py --socket /tmp/stls-arqit-sidecar.sock

IMPORTANT — one role per process for provider_name="arqit_py": a SERVER
and a CLIENT session for the same Arqit device pair must NOT live in the
same OS process — doing so makes the real bilocation key exchange fail
with a genuine 401 NotAuthorizedException from Arqit's own backend, not
a local bug (see python/stls-core/sidecar/ARQIT_PEERING_NOTES.md for the
full investigation and A/B proof). This sidecar process can serve
multiple CONNECTIONS over its lifetime, but all of them must request the
same role — a second connection asking for the other role is rejected
immediately (RpcError, code -32000) rather than left to fail mysteriously
30+ seconds later against the real Arqit cloud. Run a separate sidecar
process (different --socket) per role instead — exactly like
vault_server.py/vault_client.py on the Python side, or
tests/interop/arqit-sidecar-real.live.ts on the Node side.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import secrets
import socket
import struct
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stls_core.providers.stls_core_providers_factory import KeySessionProviderFactory
from stls_core.providers.stls_core_providers_base import KeySessionProvider

logger = logging.getLogger("arqit_sidecar")

# Guard for provider_name="arqit_py": tracks which role (SERVER/CLIENT)
# this process has already served, across all connections — see the
# module docstring for why mixing both in one process breaks real Arqit
# bilocation. None of this applies to other providers (base/pqc), which
# have no such constraint.
_active_arqit_py_role: str | None = None

# Shared secret this process requires every connection to present before
# any method other than "authenticate" — set once in main(). Mutual-auth
# layer #2 alongside SO_PEERCRED (see _check_peer_credentials): portable
# to the Node client, which has no OS-level way to verify SO_PEERCRED
# itself. See python/stls-core/README's local-socket-auth notes / issues
# #17-#18.
_EXPECTED_TOKEN: str | None = None

# Detected once at startup (see main()): SO_PEERCRED is Linux-only: not
# available on macOS (LOCAL_PEERCRED, different struct) or Windows.
# When unavailable, authentication falls back to the token alone, which
# is always enforced regardless of this flag.
_SO_PEERCRED_SUPPORTED = hasattr(socket, "SO_PEERCRED")


class RpcError(Exception):
    def __init__(self, message: str, code: int = -32603) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class SidecarSession:
    """Connection state = one STLS session (one KeySessionProvider)."""

    def __init__(self) -> None:
        self.provider: KeySessionProvider | None = None
        self.authenticated = False
        # Proof scope (#18): a connection may call "start" exactly once —
        # freezes its role/provider/config for the connection's whole
        # lifetime, closing the gap where a second "start" with
        # different params could silently re-purpose an already-running
        # session.
        self._started = False
        # Last kid this session resolved or was given (last-write-wins —
        # by protocol design a connection settles on one active kid).
        # Used to evict the key on teardown — see _evict_key().
        self._kid: str | None = None
        self._evicted = False

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "authenticate":
            return self._authenticate(params)
        if method == "stop":
            # Always allowed, even pre-auth: a connection that never
            # authenticated never got a provider either (every other
            # method is gated), so there is nothing sensitive to leak
            # here — just lets a client close cleanly without tripping
            # an extra "not authenticated" error on its way out.
            self._evict_key()
            self.provider = None
            return {}
        if not self.authenticated:
            raise RpcError("not authenticated — call 'authenticate' first", code=-32001)
        if method == "start":
            return await self._start(params)
        if method == "get_session_key_identifier":
            # Optional hint_kid (see stls_core_providers_arqit_py.py /
            # stls_core_handshake.py's HandshakeRequest.kid): lets the
            # Node side resolve an EXACT already-delivered key directly
            # instead of plain FIFO, same fix as the Python path.
            hint_kid = params.get("kid")
            kid = await self._provider().getSessionKeyIdentifier(hint_kid=hint_kid)
            self._kid = kid
            return {"kid": kid}
        if method == "set_session_key_identifier":
            self._provider().setSessionKeyIdentifier(params["kid"])
            self._kid = params["kid"]
            return {}
        if method == "get_session_key_data":
            key = await self._provider().getSessionKeyData(params["kid"])
            return {"session_key_b64": base64.b64encode(bytes(key)).decode("utf-8")}
        if method == "get_peer_identity":
            return {"peer_identity": self._provider().getPeerIdentity()}
        raise RpcError(f"Unrecognized method: {method}", code=-32601)

    def _authenticate(self, params: dict[str, Any]) -> dict[str, Any]:
        token = params.get("token", "")
        # Constant-time comparison: a naive `==` would leak how many
        # leading characters matched via timing, letting a local
        # attacker brute-force the token byte-by-byte.
        if _EXPECTED_TOKEN is None or not secrets.compare_digest(token, _EXPECTED_TOKEN):
            raise RpcError("invalid token", code=-32001)
        self.authenticated = True
        return {}

    def _provider(self) -> KeySessionProvider:
        if self.provider is None:
            raise RpcError("start has not been called yet on this session", code=-32600)
        return self.provider

    def _evict_key(self) -> None:
        """
        Zero-key-material-on-teardown (#18): evicts this session's kid
        from the provider's key store, scoped per store — see
        ArqitKeyStoreBase.remove()'s docstring (no-op for Redis
        server-role, by design). Called from "stop" and from
        _handle_connection's finally block (covers abrupt disconnect).
        Idempotent (self._evicted) since both paths can fire for the
        same connection. Never lets a provider-level failure block
        clean socket teardown.
        """
        if self._evicted or self.provider is None or self._kid is None:
            return
        self._evicted = True
        try:
            self.provider.releaseSessionKeyData(self._kid)
        except Exception:
            logger.exception("error evicting kid=%s on teardown", self._kid)

    async def _start(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._started:
            raise RpcError(
                "'start' already called on this connection — a connection's "
                "role/provider/config is fixed for its whole lifetime (proof scope, #18)",
                code=-32002,
            )
        self._started = True

        session_type_name = params.get("session_type", "CLIENT")
        session_type = KeySessionProvider.KeySessionProviderType[session_type_name]
        provider_name = params.get("provider_name") or os.environ.get("KEY_PROVIDER_NAME", "base")

        if provider_name == "arqit_py":
            global _active_arqit_py_role
            if _active_arqit_py_role is not None and _active_arqit_py_role != session_type_name:
                raise RpcError(
                    f"This sidecar process already serves the {_active_arqit_py_role!r} "
                    f"role for provider_name='arqit_py' — refusing to also start a "
                    f"{session_type_name!r} session here. Mixing both roles of the same "
                    "device pair in one OS process makes the real Arqit bilocation key "
                    "exchange fail with a genuine 401 NotAuthorizedException from "
                    "Arqit's own backend (see "
                    "python/stls-core/sidecar/ARQIT_PEERING_NOTES.md). Run a separate "
                    "sidecar process (different --socket) for each role instead.",
                    code=-32000,
                )
            _active_arqit_py_role = session_type_name

        # config_path (arqit_py only): overrides ARQIT_PY_CLIENT_CONFIG/
        # ARQIT_PY_SERVER_CONFIG for this connection only — lets several
        # connections to this same sidecar process use different device
        # configs, mirroring stls-core's own
        # KeySessionProviderFactory.get_session_provider(options=...)
        # (see stls_core_providers_arqit_py.py). Ignored by other
        # providers, same as the env vars themselves.
        config_path = params.get("config_path")
        options = {"config_path": config_path} if config_path else {}

        factory = KeySessionProviderFactory()
        loop = asyncio.get_event_loop()
        # get_session_provider() is sync (it calls start() sync/async and
        # resolves internally) — run in an executor so it does not block
        # the loop if the provider does blocking I/O (e.g. Arqit peering).
        self.provider = await loop.run_in_executor(
            None,
            lambda: factory.get_session_provider(
                session_type=session_type,
                host=params.get("host"),
                port=params.get("port"),
                name=provider_name,
                options=options,
            ),
        )
        logger.info(
            "session started: type=%s provider=%s%s",
            session_type_name, provider_name,
            f" config_path={config_path}" if config_path else "",
        )
        return {}


def _check_peer_credentials(writer: asyncio.StreamWriter, conn_id: str) -> bool:
    """
    SO_PEERCRED check (#17): verifies the connecting process's uid
    matches this sidecar's own uid — a kernel-supplied fact the remote
    side cannot spoof, unlike anything the client sends itself.

    Returns False (and logs) if the check is unavailable on this
    platform — callers fall back to token-only auth, which is always
    enforced regardless. Returns True if the check passed OR is
    unavailable; False only on an actual uid mismatch (reject).
    """
    if not _SO_PEERCRED_SUPPORTED:
        return True
    sock = writer.get_extra_info("socket")
    if sock is None:
        return True
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
    except OSError:
        logger.warning("conn=%s: SO_PEERCRED lookup failed, allowing (token auth still enforced)", conn_id)
        return True
    if uid != os.getuid():
        logger.warning("conn=%s: rejected — peer uid=%d pid=%d does not match our uid=%d", conn_id, uid, pid, os.getuid())
        return False
    return True


async def _handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    session = SidecarSession()
    # Pseudonymous per-connection id (#18): used in all logging for this
    # connection instead of raw OS-level identifiers (peername/sockname
    # are meaningless for AF_UNIX anyway) or the uid/pid read above.
    conn_id = secrets.token_hex(8)
    logger.info("connection opened: conn=%s", conn_id)

    if not _check_peer_credentials(writer, conn_id):
        writer.close()
        return

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                request = json.loads(line)
                request_id = request.get("id")
                method = request["method"]
                params = request.get("params", {})
            except Exception as exc:
                writer.write((json.dumps({"id": None, "error": {"message": str(exc), "code": -32700}}) + "\n").encode())
                await writer.drain()
                continue

            try:
                result = await session.handle(method, params)
                response = {"id": request_id, "result": result}
            except RpcError as exc:
                response = {"id": request_id, "error": {"message": exc.message, "code": exc.code}}
            except Exception as exc:
                logger.exception("conn=%s: unhandled error in method %s", conn_id, method)
                response = {"id": request_id, "error": {"message": str(exc), "code": -32603}}

            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
    finally:
        # Covers abrupt disconnect without an explicit "stop" call —
        # _evict_key() is idempotent, so this is safe even if "stop"
        # already ran.
        session._evict_key()
        writer.close()
        logger.info("connection closed: conn=%s", conn_id)


async def main() -> None:
    global _EXPECTED_TOKEN

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/tmp/stls-arqit-sidecar.sock", help="Path to the Unix domain socket")
    parser.add_argument("--token", default=None, help="Shared secret clients must present (default: env ARQIT_SIDECAR_TOKEN)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    _EXPECTED_TOKEN = args.token or os.environ.get("ARQIT_SIDECAR_TOKEN")
    if not _EXPECTED_TOKEN:
        parser.error(
            "no auth token configured — pass --token or set ARQIT_SIDECAR_TOKEN. "
            "Required: this socket accepts no connection without it (see issues #17/#18)."
        )

    if not _SO_PEERCRED_SUPPORTED:
        logger.warning(
            "SO_PEERCRED not available on this platform — relying on "
            "token-based authentication only for local-socket auth"
        )

    socket_path = Path(args.socket)
    if socket_path.exists():
        socket_path.unlink()

    server = await asyncio.start_unix_server(_handle_connection, path=str(socket_path))
    # Defense-in-depth (#17): the containing directory's permissions are
    # a deployment concern (should be 0700, owned by this process's
    # user) and can't be fixed from here — but the socket file itself is
    # tightened regardless of the parent dir or the process's umask.
    os.chmod(socket_path, 0o600)
    logger.info("arqit_sidecar listening on %s (KEY_PROVIDER_NAME=%s)", socket_path, os.environ.get("KEY_PROVIDER_NAME", "base"))

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
