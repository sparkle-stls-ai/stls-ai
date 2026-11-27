"""
stls_mcp_identity_context.py
------------------------------
Issue #74: authorization scope is never propagated from identity
verification to the tool/action executor.

_AuthGateASGI._check_identity_async() (stls_mcp_auth.py) already runs
the same identity_verifier duck type stls-a2a uses, strictly before
the wrapped MCP app sees the request — but until now it only ever
read `.ok`/`.error` off the result and discarded the rest. On success
it now also stashes the verified result object (whatever
identity_verifier.verify_request() returned — same duck type as
stls-a2a, so still `.requestor_did`/`.grantor_did`/`.scope`, see
stls_identity's PresentationVerificationResult) on
`request.state.stls_verified_identity`.

Why this survives all the way to the tool call
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The auth gate is the OUTERMOST ASGI layer around either app
(create_secure_mcp_app() wraps plain_app/stls_app with wrap_with_auth()
before anything else touches the request) — so it's the very first
code to see the raw `scope` dict the ASGI server handed it, and the
first to call `Request(scope, receive=receive)` on it. Starlette's
`Request.state` is backed by `scope.setdefault("state", {})`: setting
an attribute on it mutates that nested dict IN PLACE, not the `scope`
dict itself. Every later ASGI layer between here and the actual tool
call does `inner_scope = dict(scope)` (StlsMcpMiddleware building its
synthetic plaintext request; the SDK's own internals) — a SHALLOW
copy, which copies the top-level `"state"` key's VALUE, i.e. the same
dict object reference, not a fresh one. So by the time the official
SDK builds its own `Request` for the (by-then-decrypted) call and
threads it into `ServerMessageMetadata(request_context=request)` →
`RequestContext.request` (mcp/server/lowlevel/server.py), that
Request's `.state` is the exact same object this module reads back
from inside a running tool via `ctx.request_context.request.state`.

No MCP SDK change, no new header, no new wire format — this is the
existing ASGI `scope["state"]` convention doing exactly the job it was
designed for.

This is deliberately the enforcement surface the issue asks for
("as close as possible to the real effect... ideally the MCP tool
handler itself"): whether the caller is a plain custom client, an ADK
agent, or a LangChain agent, the actual side effect only ever happens
inside the `@mcp.tool()` function running on THIS server, reached over
the real MCP wire protocol — so a tool that calls require_scope()
before acting enforces authorization deterministically, in code,
regardless of which framework or LLM picked the call's arguments. The
model choosing `segment="MER-SEG-7"` is just an argument value; it is
never consulted on whether that value is actually what was delegated.
"""

from __future__ import annotations

from typing import Any

__all__ = ["get_verified_identity", "require_scope", "ScopeMismatchError"]


class ScopeMismatchError(PermissionError):
    """Raised by require_scope() when the verified delegated scope
    doesn't authorize the actual tool-call parameters, or when there
    is no verified delegated scope at all for this call. A subclass of
    the builtin PermissionError (not a bespoke top-level exception)
    because that's exactly what this is — callers that don't care
    about the distinction can catch PermissionError."""


def get_verified_identity(ctx: Any) -> Any | None:
    """
    Returns the identity_verifier's verified result for the request
    backing this tool call (issue #74), or None when identity_verifier
    isn't configured, the call didn't arrive over an identity-checked
    session, or `ctx` carries no HTTP request at all (e.g. stdio
    transport).

    `ctx` is a `mcp.server.fastmcp.Context` — untyped here (`Any`) on
    purpose: nothing in this function actually needs the import, and
    duck-typing the one attribute path it reads keeps this usable even
    against a minimal test double.

    The returned object is whatever identity_verifier.verify_request()
    produced — duck-typed, same as everywhere else this hook is used:
    `.ok`, `.requestor_did`, `.grantor_did`, `.scope` (stls-identity's
    PresentationVerificationResult shape, but stls-mcp never imports
    that type). Only ever present (non-None) when `.ok` was True —
    _AuthGateASGI never stores a failed result.
    """
    try:
        request = ctx.request_context.request
    except (AttributeError, ValueError):
        return None
    if request is None:
        return None
    return getattr(getattr(request, "state", None), "stls_verified_identity", None)


def require_scope(ctx: Any, **expected: Any) -> dict[str, Any]:
    """
    Asserts that this call's own parameters match the caller's
    verified, delegated scope — the deterministic, in-code check the
    issue asks for, meant to run as the FIRST line of a `@mcp.tool()`
    function, before any real effect:

        @mcp.tool()
        def check_and_apply_reroute(segment: str, reason: str, ctx: Context) -> str:
            require_scope(ctx, action="reroute_traffic", segment=segment)
            ...

    Each keyword is a (name, actual-value-from-this-call) pair; it is
    compared against the SAME key in the verified OperationalCredential's
    scope (get_verified_identity(ctx).scope) — never the other way
    around, and the model/caller never gets a vote in what "the same
    key" means, since the tool author is the one who wrote the
    key names being checked.

    Raises:
        ScopeMismatchError: no verified identity at all for this call,
            no delegated scope (a plain identity presentation, not a
            delegation pair, or identity_verifier not configured), or
            at least one key in `expected` doesn't match the delegated
            scope's own value for that key. Fails CLOSED in every one
            of those cases on purpose — a tool that calls this is
            explicitly opting into "deny unless proven otherwise".

    Returns:
        The full verified scope dict, for callers that need to read
        further fields beyond the ones just checked.
    """
    identity = get_verified_identity(ctx)
    if identity is None or not getattr(identity, "ok", False):
        raise ScopeMismatchError(
            "no verified identity for this call — identity_verifier isn't "
            "configured on this server, or this session never completed "
            "the identity challenge/response"
        )

    scope = getattr(identity, "scope", None)
    if scope is None:
        raise ScopeMismatchError(
            "caller presented a plain identity, not a delegated "
            "OperationalCredential — there is no scope to authorize "
            "this call against"
        )

    mismatches = {
        key: {"delegated": scope.get(key), "requested": value}
        for key, value in expected.items()
        if scope.get(key) != value
    }
    if mismatches:
        raise ScopeMismatchError(
            f"call parameters do not match the delegated scope: {mismatches!r}"
        )

    return scope
