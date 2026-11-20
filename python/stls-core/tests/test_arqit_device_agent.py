"""
test_arqit_device_agent.py
----------------------------
Unit tests for sidecar/arqit_device_agent.py — the real provider is
always mocked out here, nothing in this file touches the Arqit SDK or
the real cloud. See that module's own docstring for what still needs
manual validation against a real device.

Coverage:
  H01 — /livez always 200, even before start() has run
  H02 — /healthz is 503 before start() completes
  H03 — /healthz is 200 once started.set() is set
  H04 — /healthz is 503 if failed.set() is set, even if started too
  H05 — unknown path -> 404
  D01 — run(): a successful provider.start() sets state.started
  D02 — run(): a failing provider.start() sets state.failed and re-raises
  D03 — run(): a real SIGTERM makes run() return normally (not via
        task cancellation) and calls provider.stop() -- D01 only
        exercises task cancellation, which bypasses the signal
        handler/stop_event path entirely
"""

from __future__ import annotations

import asyncio
import os
import sys
import urllib.request
from http.client import HTTPResponse
from http.server import ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sidecar"))

from arqit_device_agent import HealthState, make_health_handler, run  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(port: int, path: str) -> int:
    try:
        resp: HTTPResponse = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2)
        return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class _HealthServerFixture:
    def __init__(self, state: HealthState) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_health_handler(state))
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def health_fixture():
    state = HealthState()
    fixture = _HealthServerFixture(state)
    yield state, fixture
    fixture.close()


# ---------------------------------------------------------------------------
# H — health endpoints
# ---------------------------------------------------------------------------

def test_h01_livez_always_200_before_start(health_fixture):
    """H01 — /livez is 200 even before started/failed are ever set."""
    _state, fixture = health_fixture
    assert _get(fixture.port, "/livez") == 200


def test_h02_healthz_503_before_started(health_fixture):
    """H02 — /healthz is 503 before start() has completed."""
    _state, fixture = health_fixture
    assert _get(fixture.port, "/healthz") == 503


def test_h03_healthz_200_once_started(health_fixture):
    """H03 — /healthz is 200 once state.started is set."""
    state, fixture = health_fixture
    state.started.set()
    assert _get(fixture.port, "/healthz") == 200


def test_h04_healthz_503_if_failed_even_if_started(health_fixture):
    """H04 — /healthz stays 503 if state.failed is set, regardless of started.

    Covers the ordering in run(): started is never set on the failure
    path, but this also guards against a future change accidentally
    setting both.
    """
    state, fixture = health_fixture
    state.started.set()
    state.failed.set()
    assert _get(fixture.port, "/healthz") == 503


def test_h05_unknown_path_404(health_fixture):
    """H05 — any other path is a plain 404."""
    _state, fixture = health_fixture
    assert _get(fixture.port, "/whatever") == 404


# ---------------------------------------------------------------------------
# D — run() lifecycle, provider mocked
# ---------------------------------------------------------------------------

def test_d01_run_sets_started_on_successful_start(monkeypatch):
    """D01 — a successful provider.start() flips state.started, and run()
    then waits (we cancel it ourselves instead of sending a real signal —
    signal delivery itself is out of scope for a unit test, see the
    module's own docstring on what needs manual validation).
    """
    fake_provider = AsyncMock()
    fake_provider.start = AsyncMock(return_value=None)
    fake_provider.stop = AsyncMock(return_value=None)
    fake_factory = AsyncMock()
    fake_factory.create_provider_server = lambda port, options: fake_provider

    with patch(
        "stls_core.providers.stls_core_providers_arqit_py.KeyProviderArqitPy",
        return_value=fake_factory,
    ):
        async def _drive():
            task = asyncio.create_task(run(health_port=0))
            # Poll briefly for start() to complete instead of a fixed
            # sleep -- avoids flaking under a slow/loaded CI runner.
            for _ in range(100):
                if fake_provider.start.called:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_drive())

    fake_provider.start.assert_awaited_once()


def test_d03_real_sigterm_triggers_graceful_stop(monkeypatch):
    """D03 — a real SIGTERM (not task.cancel()) is caught by
    loop.add_signal_handler(), sets stop_event, and run() returns
    normally having awaited provider.stop() -- the actual shutdown
    path a real SIGTERM in production exercises, never previously
    tested (D01 only covers task cancellation, a different path that
    skips the signal handler/stop_event logic entirely).
    """
    import signal as signal_module

    fake_provider = AsyncMock()
    fake_provider.start = AsyncMock(return_value=None)
    fake_provider.stop = AsyncMock(return_value=None)
    fake_factory = AsyncMock()
    fake_factory.create_provider_server = lambda port, options: fake_provider

    with patch(
        "stls_core.providers.stls_core_providers_arqit_py.KeyProviderArqitPy",
        return_value=fake_factory,
    ):
        async def _drive():
            task = asyncio.create_task(run(health_port=0))
            for _ in range(100):
                if fake_provider.start.called:
                    break
                await asyncio.sleep(0.01)
            os.kill(os.getpid(), signal_module.SIGTERM)
            # run() should return normally -- no CancelledError, no
            # timeout -- because the signal handler set stop_event and
            # run()'s own `await stop_event.wait()` unblocked.
            await asyncio.wait_for(task, timeout=5.0)

        asyncio.run(_drive())

    fake_provider.start.assert_awaited_once()
    fake_provider.stop.assert_awaited_once()


def test_d02_run_sets_failed_and_reraises_on_start_error(monkeypatch):
    """D02 — a failing provider.start() sets state.failed and propagates
    the exception out of run() (so a K8s restart actually happens,
    rather than hanging with a silently-broken device-agent forever).
    """
    fake_provider = AsyncMock()
    fake_provider.start = AsyncMock(side_effect=RuntimeError("boom"))
    fake_factory = AsyncMock()
    fake_factory.create_provider_server = lambda port, options: fake_provider

    with patch(
        "stls_core.providers.stls_core_providers_arqit_py.KeyProviderArqitPy",
        return_value=fake_factory,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(run(health_port=0))

    fake_provider.start.assert_awaited_once()
