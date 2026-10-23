"""
race_test_server_role.py
--------------------------
Minimal standalone script: starts the arqit_py SERVER role for the
throwaway race-test device (aq-race-throwaway-server.json) and stays
alive, printing "READY" on stdout once listening — meant to be
launched as a subprocess by
../test_arqit_py_same_device_concurrent_real.py, never imported
directly. Runs in its own OS process, as required (SERVER and CLIENT
roles for the same arqit_py device pair must never share a process —
see sidecar/ARQIT_PEERING_NOTES.md).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
)


async def main() -> None:
    from stls_core.providers.stls_core_providers_arqit_py import KeyProviderArqitPy

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aq-race-throwaway-server.json")
    provider = KeyProviderArqitPy().create_provider_server(port=0, options={"config_path": config_path})
    result = await provider.start()
    if hasattr(result, "block"):
        pass  # don't actually block synchronously — the caller needs this process's stdout/loop alive
    print("READY", flush=True)
    await asyncio.Event().wait()  # stay alive, serving handshakes, until killed


if __name__ == "__main__":
    asyncio.run(main())
