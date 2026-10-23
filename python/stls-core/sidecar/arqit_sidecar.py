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

    start            {session_type: "CLIENT"|"SERVER", provider_name?,
                       host?, port?, config_path?} -> {}
    get_session_key_identifier   {}               -> {"kid": str}
    set_session_key_identifier   {kid}             -> {}
    get_session_key_data         {kid}             -> {"session_key_b64": str}
    get_peer_identity            {}                -> {"peer_identity": str|null}
    stop                         {}                -> {}

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


class RpcError(Exception):
    def __init__(self, message: str, code: int = -32603) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class SidecarSession:
    """Connection state = one STLS session (one KeySessionProvider)."""

    def __init__(self) -> None:
        self.provider: KeySessionProvider | None = None

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "start":
            return await self._start(params)
        if method == "get_session_key_identifier":
            # Optional hint_kid (see stls_core_providers_arqit_py.py /
            # stls_core_handshake.py's HandshakeRequest.kid): lets the
            # Node side resolve an EXACT already-delivered key directly
            # instead of plain FIFO, same fix as the Python path.
            hint_kid = params.get("kid")
            return {"kid": await self._provider().getSessionKeyIdentifier(hint_kid=hint_kid)}
        if method == "set_session_key_identifier":
            self._provider().setSessionKeyIdentifier(params["kid"])
            return {}
        if method == "get_session_key_data":
            key = await self._provider().getSessionKeyData(params["kid"])
            return {"session_key_b64": base64.b64encode(bytes(key)).decode("utf-8")}
        if method == "get_peer_identity":
            return {"peer_identity": self._provider().getPeerIdentity()}
        if method == "stop":
            self.provider = None
            return {}
        raise RpcError(f"Unrecognized method: {method}", code=-32601)

    def _provider(self) -> KeySessionProvider:
        if self.provider is None:
            raise RpcError("start has not been called yet on this session", code=-32600)
        return self.provider

    async def _start(self, params: dict[str, Any]) -> dict[str, Any]:
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


async def _handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    session = SidecarSession()
    peer = writer.get_extra_info("peername") or writer.get_extra_info("sockname")
    logger.info("connection opened: %s", peer)

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
                logger.exception("unhandled error in method %s", method)
                response = {"id": request_id, "error": {"message": str(exc), "code": -32603}}

            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
    finally:
        writer.close()
        logger.info("connection closed: %s", peer)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/tmp/stls-arqit-sidecar.sock", help="Path to the Unix domain socket")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    socket_path = Path(args.socket)
    if socket_path.exists():
        socket_path.unlink()

    server = await asyncio.start_unix_server(_handle_connection, path=str(socket_path))
    logger.info("arqit_sidecar listening on %s (KEY_PROVIDER_NAME=%s)", socket_path, os.environ.get("KEY_PROVIDER_NAME", "base"))

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
