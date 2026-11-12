"""
test_arqit_py_concurrent_devices_real.py
-------------------------------------------
Deliberately forces the race that every other test in this project
(and every sample under samples/client/10_client_ms,
samples/client/11_client_mcp_ms) has carefully avoided: TWO DIFFERENT
arqit_py client device identities, started CONCURRENTLY — not
sequentially — in ONE Python process, via asyncio.gather.

Why this exists
~~~~~~~~~~~~~~~~
stls_core.providers.stls_core_providers_session_resolver.KeySessionResolver
(internal — not part of stls_core's public API since issue #5,
"Restrict stls-core's public API surface"; used via
establish_session()/resolve_transport() now, and the stls-a2a/stls-mcp
managers built on them) already support one
process holding several arqit_py device configs via
options["config_path"] — but every real validation so far (see
samples/client/10_client_ms/README.md, samples/client/11_client_mcp_ms/README.md)
used it SEQUENTIALLY: one device's full session setup (authenticate +
bilocation + P2P negotiation thread) completes before the next one
starts. That sequencing is exactly what SecureSessionManager's/
SecureToolsetManager's own locking already does internally — it was
never actually proven safe for two devices racing each other.

The concrete suspect: `uk.arqit.device.common.device_metadata.DeviceMetadata`
is a process-wide singleton whose `.config` attribute is silently
overwritten on every `get_instance(cfg)` call, with NO guard against a
second, different device's call arriving while the first device's
background negotiation thread (started by
KeyProviderClientArqitPy.start(), see stls_core_providers_arqit_py.py)
is still running and may itself read `self._dev_metadata.config.*`
(ratchet handling, provisioning_state — see
uk/arqit/device/common/authentication/authentication.py) at any point
after the second device's setup has already swapped it. If this race
is real, the symptom would be one device's ratchet/metadata read or
written against the WRONG device's file, or a 401 from Arqit's backend
analogous to the already-documented same-process SERVER+CLIENT
constraint (see sidecar/ARQIT_PEERING_NOTES.md) but for CLIENT+CLIENT
instead of SERVER+CLIENT.

**Update (2026-10-23, same day): credible evidence this race is
real, found via a DIFFERENT path than this file.** Every earlier
"concurrent" validation in this project that stayed in pure Python
(this file included, before the fix below) called
KeyProviderClientArqitPy.start() — a plain, blocking SYNCHRONOUS
method — directly from a coroutine passed to asyncio.gather(). Since
asyncio only switches coroutines at `await` points, and .start() has
none before DeviceMetadata.get_instance() runs inside it, those tests
never actually raced at the critical point — asyncio serialized them
for free, then the real work "looked concurrent" only in the
already-returned background negotiation threads. This is why
`_resolve_one_device()` below now wraps `provider.start()` in
`asyncio.to_thread()` — without it, this file would not test what it
claims to.

Separately, the Node port of this project's multi-session sample
(samples/client/12_client_mcp_ms/client_multi_session.ts) DOES create
real OS-thread concurrency at this exact point: the Python sidecar
(arqit_sidecar.py) dispatches each RPC "start" call through
`loop.run_in_executor(None, ...)` — a real ThreadPoolExecutor thread
per connection. Three concurrent Node sessions against the SAME
device (not even two DIFFERENT devices — see the "same-device"
variant this implies, beyond what this file currently tests) failed
with `No new key available within 30s (3 already delivered
previously)` after only one of three succeeded, then hung on the next
round. That is consistent with real concurrent corruption of
DeviceMetadata's shared ratchet/auth state under genuine OS-thread
parallelism — not yet proven, but now considerably more credible than
before this fix, and not well explained by ordinary rate-limiting
alone (see samples/server/12_server_mcp_ms/README.md for the full
account). This file should eventually be extended with a same-device
(not just different-device) concurrent variant to probe that
specific failure directly in pure Python, now that `asyncio.to_thread`
makes a real test possible.

*** EXPLICIT RISK — READ BEFORE RUNNING ***
This test is intentionally adversarial. A failed run is not a "test
failure" in the usual sense — it may leave one or both real Arqit
devices' ratchet state corrupted on Arqit's cloud, possibly requiring
re-provisioning (see device_provisioning.py in
python/stls-a2a/arqit_utility/) before they work again. Do not run
this against devices you care about without accepting that risk, and
do not chase a second run immediately after a failure — a corrupted
ratchet does not self-heal, and Arqit-side rate limiting can also
produce a false "failure" indistinguishable from a real one (see the
rate-limiting note in sidecar/ARQIT_PEERING_NOTES.md's follow-up
section). This is why it is NOT wired into any CI-safe pytest marker
and is excluded by default the same way test_arqit_py_real_peering.py
is — via a hard environment-variable gate, not just missing wheels.

Setup (otherwise this whole module is skipped):
    ARQIT_PY_CLIENT_CONFIG_1=/path/to/device1-client.json
    ARQIT_PY_CLIENT_CONFIG_2=/path/to/device2-client.json
    ARQIT_PY_ALLOW_CONCURRENT_DEVICE_RACE_TEST=yes   <- explicit opt-in,
        separate from the two config vars above, so this can never run
        by accident just because the env happens to have arqit configs
        set for an unrelated test/sample.

Each device needs its own already-running, already-provisioned SERVER
counterpart listening (e.g. two instances of
samples/server/11_server_mcp_ms/server_mcp_ms_{1,2}/server.py with
KEY_PROVIDER_NAME=arqit_py, or any other already-provisioned pair —
the two client configs just need to each resolve a real device whose
server-side peer is reachable).

Usage:
    ARQIT_PY_CLIENT_CONFIG_1=.../aq-sf-mcp-client-ms-1.json \\
    ARQIT_PY_CLIENT_CONFIG_2=.../aq-sf-mcp-client-ms-2.json \\
    ARQIT_PY_ALLOW_CONCURRENT_DEVICE_RACE_TEST=yes \\
    uv run pytest tests/test_arqit_py_concurrent_devices_real.py -v -s
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import pytest

logger = logging.getLogger(__name__)


def _real_arqit_env_available() -> tuple[bool, str]:
    """
    True only with the explicit opt-in flag AND both wheels AND both
    client configs present as real files — see the module docstring
    for why the opt-in flag is separate from (and in addition to) the
    config env vars.
    """
    try:
        import uk.arqit.device.common  # noqa: F401
    except ImportError:
        return False, "Arqit SKA-Platform(TM) wheels not installed (extra [arqit_py])"

    if os.getenv("ARQIT_PY_ALLOW_CONCURRENT_DEVICE_RACE_TEST", "").strip().lower() not in (
        "1", "true", "yes",
    ):
        return False, (
            "ARQIT_PY_ALLOW_CONCURRENT_DEVICE_RACE_TEST not set — this test deliberately "
            "races two real Arqit device identities against each other and can corrupt "
            "device ratchet state on failure; see the module docstring before opting in."
        )

    config_1 = os.getenv("ARQIT_PY_CLIENT_CONFIG_1")
    config_2 = os.getenv("ARQIT_PY_CLIENT_CONFIG_2")
    if not config_1 or not config_2:
        return False, "ARQIT_PY_CLIENT_CONFIG_1/ARQIT_PY_CLIENT_CONFIG_2 not set"
    if not Path(config_1).is_file() or not Path(config_2).is_file():
        return False, "ARQIT_PY_CLIENT_CONFIG_1/ARQIT_PY_CLIENT_CONFIG_2 point at nonexistent files"

    return True, ""


_available, _skip_reason = _real_arqit_env_available()
pytestmark = pytest.mark.skipif(not _available, reason=_skip_reason)


async def _resolve_one_device(config_path: str, label: str) -> dict[str, Any]:
    """
    Starts a single arqit_py CLIENT device identity and resolves its
    session key — same primitive SecureSessionManager/KeySessionResolver
    use internally, called directly here (no lock, no sequencing)
    specifically so this coroutine races freely against the other one
    when both are passed to asyncio.gather() below.

    CRITICAL: KeyProviderClientArqitPy.start() is a plain, blocking,
    SYNCHRONOUS method (no `await` inside it) — if called directly
    from a coroutine, asyncio's single-threaded cooperative scheduler
    would run it to completion before ever switching to the other
    coroutine, so asyncio.gather() alone would NOT create a real race
    at the DeviceMetadata.get_instance() call site inside it (this was
    a real bug in an earlier version of this file, caught after a
    concurrent arqit_py session test over the Node sidecar — which
    DOES run start() in a real OS thread via loop.run_in_executor(),
    see arqit_sidecar.py's _start() — failed with symptoms consistent
    with this exact race, while every pure-Python asyncio.gather()
    test in this project, including this one before the fix, never
    actually exercised it and passed vacuously). `asyncio.to_thread()`
    below dispatches `.start()` to the default ThreadPoolExecutor,
    mirroring the sidecar's real concurrency model so this test
    actually races the way production traffic through the sidecar
    does.
    """
    import time

    from stls_core.providers.stls_core_providers_arqit_py import KeyProviderArqitPy

    t0 = time.monotonic()
    try:
        provider = KeyProviderArqitPy().create_provider_client(
            host="", port=0, options={"config_path": config_path},
        )
        await asyncio.to_thread(provider.start)
        session = provider.createProviderSession()
        kid = await session.getSessionKeyIdentifier()
        kdata = await session.getSessionKeyData(kid)
        elapsed = time.monotonic() - t0
        logger.info("[%s] resolved kid=%s key_len=%d elapsed=%.2fs", label, kid, len(kdata), elapsed)
        return {"label": label, "config_path": config_path, "kid": kid, "kdata": kdata, "error": None}
    except Exception as exc:  # noqa: BLE001 — we want the raw exception for diagnosis, not a clean failure
        elapsed = time.monotonic() - t0
        logger.error("[%s] failed after %.2fs: %s", label, elapsed, exc, exc_info=True)
        return {"label": label, "config_path": config_path, "kid": None, "kdata": None, "error": exc}


async def test_concurrent_different_devices_same_process_real() -> None:
    """
    THE test. Starts device 1 and device 2 CONCURRENTLY
    (asyncio.gather, not awaited one after the other) in this one
    process, then checks:
      1. Did both resolve without raising?
      2. Are the two kids/keys genuinely distinct (not a corrupted
         cross-read of each other's state)?
      3. Does each kid trace back to ITS OWN device (sanity: a
         same-process race could in principle still produce two
         different-looking but wrong kids, not just an exception —
         this only catches the "it blew up" case, not silent
         misattribution; read the elapsed/log output by eye too).

    A failure here (exception, identical kids, or one side silently
    getting the other's config) is evidence the DeviceMetadata
    singleton race described in the module docstring is real, not
    just theoretical. A clean pass is evidence it ISN'T a problem for
    this SDK version/transport combination — but see the module
    docstring's warning about not treating one clean run as proof
    either way (rate-limiting flakiness can mask a real race, and vice
    versa a lucky scheduling order can mask a real one too — if this
    ever needs to be trusted as a real "no" answer, run it several
    times on different days, not just once).
    """
    config_1 = os.environ["ARQIT_PY_CLIENT_CONFIG_1"]
    config_2 = os.environ["ARQIT_PY_CLIENT_CONFIG_2"]

    result_1, result_2 = await asyncio.gather(
        _resolve_one_device(config_1, "device-1"),
        _resolve_one_device(config_2, "device-2"),
    )

    if result_1["error"] is not None:
        pytest.fail(f"device-1 raised during concurrent resolution: {result_1['error']!r}")
    if result_2["error"] is not None:
        pytest.fail(f"device-2 raised during concurrent resolution: {result_2['error']!r}")

    assert result_1["kid"] != result_2["kid"], (
        "device-1 and device-2 resolved to the SAME kid — strong evidence of the "
        "DeviceMetadata singleton race described in the module docstring: one "
        "device's identity bled into the other's."
    )
    assert result_1["kdata"] != result_2["kdata"], (
        "device-1 and device-2 resolved to the SAME key material — same concern as above."
    )

    logger.info(
        "PASS: two different arqit_py devices resolved CONCURRENTLY in one process, "
        "distinct kid/key each (device-1=%s, device-2=%s)",
        result_1["kid"], result_2["kid"],
    )
