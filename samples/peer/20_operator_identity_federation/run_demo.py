"""
run_demo.py — 20_operator_identity_federation
-------------------------------------------------
The one thing a person actually runs. Starts Meridian's agent, its MCP
tool server, and Helios's MCP tool server as background processes,
then walks through three guided steps, pausing for Enter before each
one and explaining what is about to happen in plain language. Helios's
own agent (Node, no long-running process — see helios_agent/client.ts)
is invoked once per step as a short-lived subprocess.

Run:
    python bootstrap_identities.py   # once
    python run_demo.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import redis.asyncio as redis
from dotenv import load_dotenv

load_dotenv()

from stls_identity import StatusListManager

HERE = Path(__file__).parent
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
NPX = "npx"

_processes: list[subprocess.Popen] = []


def _pause(message: str) -> None:
    print(f"\n{message}")
    input("Press Enter to continue... ")


def _start(label: str, cwd: Path, cmd: list[str], log_name: str) -> subprocess.Popen:
    """Background service, logs redirected to a file (not PIPE — an
    unread pipe can fill up and block a chatty server mid-demo)."""
    print(f"starting {label}... (log: {log_name})")
    log_file = open(HERE / log_name, "w")
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=log_file, stderr=subprocess.STDOUT)
    _processes.append(proc)
    return proc


async def _wait_http_ready(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                await client.get(url, timeout=1.0)
                return
            except httpx.HTTPError:
                await asyncio.sleep(0.5)
    raise SystemExit(f"timed out waiting for {url} to come up")


def _run_helios_step(credential: str) -> dict:
    """Runs helios_agent/client.ts once, for one credential choice, and
    returns the {outcome, detail, ticketId} it printed."""
    proc = subprocess.run(
        [NPX, "tsx", "--env-file=.env", "client.ts", f"--credential={credential}"],
        cwd=HERE / "helios_agent", capture_output=True, text=True,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
    result_line = next(
        (line for line in proc.stdout.splitlines() if line.startswith("RESULT_JSON:")), None,
    )
    if result_line is None:
        raise SystemExit("helios_agent/client.ts did not print a RESULT_JSON line — see output above")
    return json.loads(result_line[len("RESULT_JSON:"):].strip())


async def _revoke_meridian_identity() -> None:
    meridian_identity = json.loads((HERE / "meridian_identity.json").read_text())
    status_index = meridian_identity["identity_credential"]["credentialStatus"]["statusListIndex"]

    redis_client = redis.from_url(REDIS_URL)
    status = StatusListManager(redis_client, authority_id="governing-authority")
    await status.revoke(status_index)
    await redis_client.aclose()


def main() -> None:
    if not (HERE / "helios_identity.json").exists():
        print("No identities found — running bootstrap_identities.py first...\n")
        subprocess.run([sys.executable, "bootstrap_identities.py"], cwd=HERE, check=True)

    # Node's --env-file flag errors out if the file is simply missing
    # (unlike python-dotenv, which no-ops) — copy the example once so a
    # from-scratch checkout runs without a manual `cp` step.
    for node_dir in ("helios_agent", "helios_mcp_server"):
        env_file = HERE / node_dir / ".env"
        if not env_file.exists():
            env_file.write_text((HERE / node_dir / ".env.example").read_text())

    redis_url_display = REDIS_URL
    print("=" * 70)
    print("Operator identity federation — Meridian <-> Helios")
    print(f"Redis: {redis_url_display}")
    print("=" * 70)

    try:
        _start("Meridian's MCP tool server", HERE / "meridian_mcp_server",
               [sys.executable, "server.py"], "meridian_mcp_server.log")
        _start("Meridian's agent", HERE / "meridian_agent",
               [sys.executable, "server.py"], "meridian_agent.log")
        _start("Helios's MCP tool server", HERE / "helios_mcp_server",
               [NPX, "tsx", "--env-file=.env", "server.ts"], "helios_mcp_server.log")

        asyncio.run(_wait_http_ready("http://localhost:8711/mcp"))
        asyncio.run(_wait_http_ready("http://localhost:8710/agents/meridian/secure-a2a/capabilities"))
        asyncio.run(_wait_http_ready("http://localhost:8721/mcp"))
        print("\nAll services are up.\n")

        _pause(
            "STEP 1 — Helios asks Meridian to reroute traffic on a segment it does not\n"
            "own. Helios presents two things together: the permission slip Meridian\n"
            "gave it earlier (an Operational Credential, scoped to exactly this\n"
            "action), and a copy of Meridian's own ID card — because a permission\n"
            "slip is only worth as much as the standing of whoever signed it.\n"
            "Expected: ACCEPTED."
        )
        _run_helios_step("valid")

        _pause(
            "STEP 2 — Same request, but this time Helios presents a permission slip\n"
            "that has already expired. Meridian's own ID card is unchanged and still\n"
            "perfectly valid — only the specific grant is stale.\n"
            "Expected: REJECTED, before Meridian's own tool is ever called."
        )
        _run_helios_step("expired")

        _pause(
            "STEP 3 (bonus) — Now we revoke Meridian's OWN ID card at the federation\n"
            "level (not the permission slip — the permission slip from step 1 is\n"
            "untouched, still inside its validity window). Helios then presents the\n"
            "EXACT SAME permission slip that worked in step 1.\n"
            "Expected: REJECTED anyway — a permission is only as good as the standing\n"
            "of whoever granted it, checked at verification time, not issuance time."
        )
        asyncio.run(_revoke_meridian_identity())
        _run_helios_step("valid")

        print("\nDemo complete.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        for proc in _processes:
            proc.terminate()
        for proc in _processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
