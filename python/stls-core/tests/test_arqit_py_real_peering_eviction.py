"""
test_arqit_py_real_peering_eviction.py
----------------------------------------
Issue #34: the one check that neither #17 nor #18's own verification
covered — chaining teardown-triggered key eviction to a real,
cloud-delivered Arqit key, in a single test, instead of leaving the two
halves (the teardown call path, and the key store's remove()/get()
behavior) verified only independently.

Companion to test_arqit_py_real_peering.py: same real-cloud
prerequisites (skip gate, two-separate-OS-processes constraint — see
that file's module docstring and ARQIT_PEERING_NOTES.md for the full
"why" on process isolation), same server-role helper script. The only
difference is the CLIENT role script used: this file drives
_arqit_py_real_e2e_client_role_eviction.py (not the plain
_arqit_py_real_e2e_client_role.py), which performs the real round trip
and then, on the SAME provider/key-store instance that resolved the
real kid:

  1. calls `releaseSessionKeyData(kid)` — the exact call the Arqit
     sidecar's "stop" RPC / connection-close path makes (see
     sidecar/arqit_sidecar.py's `_evict_key()`).
  2. re-fetches that same kid and asserts it is gone (KeyError).

What was already verified elsewhere, and is deliberately NOT
re-proven here:
  - that the sidecar's teardown path actually calls
    releaseSessionKeyData() (issue #18's own test coverage).
  - that InMemoryArqitKeyStore.remove()/RedisArqitKeyStore.remove()
    (client role) evict an entry, against fixture key bytes (issue
    #18's own unit-level coverage).
This test's only job is the missing link: proving those two already-true
things compose correctly against a real key, not a fixture.

Setup required to run it (otherwise automatic skip — see
_real_arqit_env_available() in test_arqit_py_real_peering.py, mirrored
here):
  - the `[arqit_py]` extra installed (real Arqit SKA-Platform(tm) wheels)
  - ARQIT_PY_CLIENT_CONFIG / ARQIT_PY_SERVER_CONFIG -> absolute paths to
    a real, already-provisioned device pair's JSON configs
  - run pytest from the same cwd those configs' relative
    DEVICE_METADATA_FILEPATH/etc. paths resolve against (see
    test_arqit_py_real_peering.py's docstring for the exact pitfall)

Usage:
    cd python/stls-core
    ARQIT_PY_CLIENT_CONFIG=/path/to/client.json \\
    ARQIT_PY_SERVER_CONFIG=/path/to/server.json \\
    pytest tests/test_arqit_py_real_peering_eviction.py -v -s
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
_CLIENT_ROLE_EVICTION_SCRIPT = _TESTS_DIR / "_arqit_py_real_e2e_client_role_eviction.py"


def _receiver_port() -> int:
    with open(os.environ["ARQIT_PY_SERVER_CONFIG"]) as fh:
        return int(json.load(fh).get("SOCKET_LISTENING_PORT", 8889))


def _wait_port_free(port: int, host: str = "127.0.0.1", timeout: float = 65.0) -> None:
    """
    See test_arqit_py_real_peering.py's own _wait_port_free for the full
    rationale (TCP TIME_WAIT on a real, closed connection is a fixed
    ~60s on Linux — not flakiness, not tunable from here).
    """
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return
            except OSError as exc:
                last_error = exc
        time.sleep(0.1)
    raise TimeoutError(f"{host}:{port} still not bindable after {timeout}s: {last_error}")


def _real_arqit_env_available() -> tuple[bool, str]:
    try:
        import uk.arqit.device.common  # noqa: F401
    except ImportError:
        return False, "Arqit SKA-Platform(tm) wheels not installed (extra [arqit_py])"

    client_cfg = os.getenv("ARQIT_PY_CLIENT_CONFIG")
    server_cfg = os.getenv("ARQIT_PY_SERVER_CONFIG")
    if not client_cfg or not server_cfg:
        return False, "ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG not set"
    if not Path(client_cfg).is_file() or not Path(server_cfg).is_file():
        return False, "ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG point to missing files"

    return True, ""


_available, _skip_reason = _real_arqit_env_available()
pytestmark = pytest.mark.skipif(not _available, reason=_skip_reason)


# ---------------------------------------------------------------------------
# Orchestrator relay between the two processes
# ---------------------------------------------------------------------------

def _start_server_process() -> subprocess.Popen:
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


def _run_roundtrip_and_eviction_probe(server_proc: subprocess.Popen) -> dict[str, str]:
    """
    Drives _arqit_py_real_e2e_client_role_eviction.py against the given
    (already-ready) server process: relays the same handshake/echo
    protocol as test_arqit_py_real_peering.py's own helper, then reads
    one extra line for the eviction probe's result.

    Raises AssertionError, with the full captured output, on any failure
    at any stage.
    """
    client_stderr_file = tempfile.TemporaryFile(mode="w+")
    client_proc = subprocess.Popen(
        [sys.executable, str(_CLIENT_ROLE_EVICTION_SCRIPT)],
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
            f"key agreement failed: {key_ok_line!r}\n"
            f"client stderr (tail):\n{_client_stderr_tail()}"
        )

        handshake_req_line = client_proc.stdout.readline()
        req_msg = json.loads(handshake_req_line)
        assert req_msg.get("cmd") == "handshake", f"unexpected line: {handshake_req_line!r}"

        server_proc.stdin.write(json.dumps(req_msg) + "\n")
        server_proc.stdin.flush()
        handshake_resp_line = server_proc.stdout.readline()
        resp_msg = json.loads(handshake_resp_line)
        assert resp_msg.get("cmd") == "handshake_response", (
            f"unexpected server response: {handshake_resp_line!r}"
        )

        client_proc.stdin.write(json.dumps(resp_msg) + "\n")
        client_proc.stdin.flush()

        handshake_status_line = client_proc.stdout.readline()
        assert handshake_status_line.startswith("HANDSHAKE_OK"), (
            f"handshake (proof-of-key-possession) failed: {handshake_status_line!r}\n"
            f"client stderr (tail):\n{_client_stderr_tail()}"
        )

        echo_req_line = client_proc.stdout.readline()
        echo_req_msg = json.loads(echo_req_line)
        assert echo_req_msg.get("cmd") == "echo", f"unexpected line: {echo_req_line!r}"

        server_proc.stdin.write(json.dumps(echo_req_msg) + "\n")
        server_proc.stdin.flush()
        echo_resp_line = server_proc.stdout.readline()
        echo_resp_msg = json.loads(echo_resp_line)
        assert echo_resp_msg.get("cmd") == "echo_response", (
            f"unexpected server response: {echo_resp_line!r}"
        )

        client_proc.stdin.write(json.dumps(echo_resp_msg) + "\n")
        client_proc.stdin.flush()

        passed_line = client_proc.stdout.readline()
        assert "roundtrip=ok" in passed_line and passed_line.startswith("CLIENT_PASSED"), (
            f"real encrypt/decrypt round trip failed: {passed_line!r}\n"
            f"client stderr (tail):\n{_client_stderr_tail()}"
        )

        # The one new assertion this issue is about: eviction chained
        # against this exact, cloud-delivered kid.
        eviction_line = client_proc.stdout.readline()
        assert eviction_line.startswith("EVICTION_OK"), (
            f"key eviction after teardown did not take effect: {eviction_line!r}\n"
            f"client stderr (tail):\n{_client_stderr_tail()}"
        )

        print(f"{key_ok_line.strip()} | {handshake_status_line.strip()} | "
              f"{passed_line.strip()} | {eviction_line.strip()}")

        client_proc.stdin.close()
        returncode = client_proc.wait(timeout=10)
        assert returncode == 0, f"client exited with code {returncode}"

        kid = key_ok_line.split("kid=", 1)[1].split()[0]
        session_id = handshake_status_line.split("session_id=", 1)[1].split()[0]
        return {"kid": kid, "session_id": session_id}
    finally:
        if client_proc.poll() is None:
            client_proc.kill()
            client_proc.wait(timeout=5)
        client_stderr_file.close()


def test_client_key_evicted_after_release_against_real_cloud_key():
    """
    Full real round trip (device auth + bilocation key agreement + STLS
    handshake proof + AES-256-GCM encrypt/decrypt) against the live
    Arqit cloud, then: releaseSessionKeyData(kid) on the resolving
    provider, then a re-fetch of that same kid that must raise KeyError.

    This is the missing link flagged when #18 was closed: the teardown
    call path and the key store's eviction logic were each correct in
    isolation, but never chained together against one real,
    cloud-delivered key in a single test.
    """
    server_proc = _start_server_process()
    try:
        assert server_proc.stdout is not None
        ready_line = server_proc.stdout.readline()
        assert "SERVER_READY" in ready_line, f"server role not ready, output: {ready_line!r}"

        _run_roundtrip_and_eviction_probe(server_proc)
    finally:
        _stop_server_process(server_proc)
        _wait_port_free(_receiver_port())
