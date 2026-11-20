"""
fetch_status_bit / fetch_remote_credential_status (stls_id_status.py) —
the Verifier-side inverse of StatusListManager.publish(), promoted from
samples/server/sofia-admin's tools_identity.py (used there 3x in
production) while scoping issue #15's follow-up (#45).

Uses a real local HTTP server (stdlib http.server, a real socket, real
TCP) rather than a mock — same "real infra over stubs" discipline as
the rest of this package's tests, with zero new test dependencies.
"""

import base64
import datetime as dt
import gzip
import http.server
import json
import threading

import pytest

from stls_identity import (
    CredentialStatus,
    CredentialSubject,
    NetworkAuthorityCredential,
    fetch_remote_credential_status,
    fetch_status_bit,
)


def _bitstring_with_bit_set(index: int, total_bytes: int = 4) -> bytes:
    raw = bytearray(total_bytes)
    raw[index // 8] |= 1 << (7 - index % 8)
    return bytes(raw)


def _encode(raw: bytes) -> str:
    """Same encoding StatusListManager.publish() produces."""
    return base64.urlsafe_b64encode(gzip.compress(raw)).decode("ascii")


class _StatusListHandler(http.server.BaseHTTPRequestHandler):
    encoded_list = ""

    def do_GET(self):
        body = json.dumps({"encodedList": self.encoded_list}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass  # keep test output quiet


@pytest.fixture
def status_list_server():
    servers: list[http.server.HTTPServer] = []

    def _start(encoded_list: str) -> str:
        handler_cls = type("_Handler", (_StatusListHandler,), {"encoded_list": encoded_list})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}/status-list"

    yield _start

    for server in servers:
        server.shutdown()


async def test_fetch_status_bit_true_when_bit_set(status_list_server):
    url = status_list_server(_encode(_bitstring_with_bit_set(index=10)))

    assert await fetch_status_bit(url, 10) is True
    assert await fetch_status_bit(url, 11) is False


async def test_fetch_status_bit_raises_on_missing_encoded_list(status_list_server):
    url = status_list_server("")

    with pytest.raises(ValueError, match="empty or missing"):
        await fetch_status_bit(url, 0)


async def test_fetch_status_bit_raises_on_unreachable_url():
    with pytest.raises(Exception):
        await fetch_status_bit("http://127.0.0.1:1/unreachable", 0)


async def test_fetch_remote_credential_status_reads_url_and_index_from_credential(status_list_server):
    url = status_list_server(_encode(_bitstring_with_bit_set(index=3)))

    credential = NetworkAuthorityCredential(
        id="urn:uuid:1", issuer="did:web:ga.example", valid_from=dt.datetime.now(dt.timezone.utc),
        credential_subject=CredentialSubject(id="did:web:agent", client_id="c1"),
        credential_status=CredentialStatus(
            id=f"{url}#3", status_list_index=3, status_list_credential=url,
        ),
    )

    fetch_time, is_revoked = await fetch_remote_credential_status(credential)

    assert is_revoked is True
    assert (dt.datetime.now(dt.timezone.utc) - fetch_time).total_seconds() < 5
