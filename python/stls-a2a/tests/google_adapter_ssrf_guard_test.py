"""
google_adapter_ssrf_guard_test.py
----------------------------------
Regression for a real SSRF found while reviewing GoogleADKAdapter during
issue #45's work (unrelated to identity itself): _to_genai_parts()'s
FileReference fallback path did an unrestricted `httpx.get(uri)` for
any URI that wasn't `gs://` or a youtube.com host — a remote caller
sending a FilePart with e.g. http://169.254.169.254/latest/meta-data/
(cloud instance metadata) or http://localhost:6379 would make this
server fetch it on their behalf and feed the response into the model.

Fixed with _assert_safe_remote_file_url(): resolves the host and
rejects any URI whose resolved address is loopback/link-local/
private/reserved/multicast/unspecified, plus restricting the scheme
to http(s). Same in-process style as the other adapter regression
test here — no network calls, no real DNS needed for the blocked
cases (private/loopback addresses are checked by IP, not by name).
"""

import asyncio
import os
import sys

os.environ.setdefault("KEY_PROVIDER_NAME", "base")
os.environ.setdefault("TASK_STORE_BACKEND", "memory")
os.environ["AUTH_PG_DSN"] = ""

from stls_a2a.stls_a2a_google_adapter import _assert_safe_remote_file_url


def _ok(label: str, detail: str = "") -> None:
    print(f"  ✅ {label}{': ' + detail if detail else ''}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  ❌ {label}{': ' + detail if detail else ''}")


async def _expect_blocked(uri: str) -> bool:
    try:
        await _assert_safe_remote_file_url(uri)
    except ValueError as e:
        _ok(f"blocked {uri!r}", str(e))
        return True
    _fail(f"NOT blocked {uri!r} — should have raised ValueError")
    return False


async def _expect_allowed(uri: str) -> bool:
    try:
        await _assert_safe_remote_file_url(uri)
    except ValueError as e:
        _fail(f"wrongly blocked {uri!r}", str(e))
        return False
    _ok(f"allowed {uri!r}")
    return True


async def test_cloud_metadata_endpoint_blocked() -> bool:
    print("\n── Test 1: cloud instance metadata endpoint blocked ──")
    return await _expect_blocked("http://169.254.169.254/latest/meta-data/")


async def test_loopback_and_private_addresses_blocked() -> bool:
    print("\n── Test 2: loopback/private addresses blocked ──")
    results = [
        await _expect_blocked("http://127.0.0.1:8080/"),
        await _expect_blocked("http://localhost:6379/"),
        await _expect_blocked("http://10.0.0.5/"),
        await _expect_blocked("http://192.168.1.1/"),
    ]
    return all(results)


async def test_non_http_scheme_blocked() -> bool:
    print("\n── Test 3: non-http(s) scheme blocked ──")
    return await _expect_blocked("ftp://example.com/x")


async def test_public_https_url_allowed() -> bool:
    print("\n── Test 4: ordinary public https URL still allowed ──")
    return await _expect_allowed("https://example.com/image.png")


async def main() -> None:
    print("\n" + "=" * 70)
    print("GoogleADKAdapter file-URI SSRF guard")
    print("=" * 70)

    results: dict[str, bool] = {}
    results["1 — cloud metadata blocked"] = await test_cloud_metadata_endpoint_blocked()
    results["2 — loopback/private blocked"] = await test_loopback_and_private_addresses_blocked()
    results["3 — non-http scheme blocked"] = await test_non_http_scheme_blocked()
    results["4 — public https allowed"] = await test_public_https_url_allowed()

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    all_ok = True
    for label, ok in results.items():
        print(f"  {'✅' if ok else '❌'}  Test {label}")
        all_ok = all_ok and ok

    print()
    print("  🎉 All tests passed." if all_ok else "  ⚠️  Some tests failed — check the logs above.")
    print("=" * 70 + "\n")

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
