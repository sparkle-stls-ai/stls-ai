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
to http(s). No network calls needed for the blocked cases
(private/loopback addresses are checked by IP, not by name); the
"allowed" case resolves a real public hostname via DNS.
"""

import pytest

from stls_a2a.stls_a2a_google_adapter import _assert_safe_remote_file_url


async def test_cloud_metadata_endpoint_blocked():
    with pytest.raises(ValueError):
        await _assert_safe_remote_file_url("http://169.254.169.254/latest/meta-data/")


@pytest.mark.parametrize(
    "uri",
    [
        "http://127.0.0.1:8080/",
        "http://localhost:6379/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
    ],
)
async def test_loopback_and_private_addresses_blocked(uri):
    with pytest.raises(ValueError):
        await _assert_safe_remote_file_url(uri)


async def test_non_http_scheme_blocked():
    with pytest.raises(ValueError):
        await _assert_safe_remote_file_url("ftp://example.com/x")


async def test_public_https_url_allowed():
    await _assert_safe_remote_file_url("https://example.com/image.png")
