"""
test_stls_mcp_identity_context.py
------------------------------------
Issue #74: authorization scope propagation. Two things are proven here:

  1. get_verified_identity()/require_scope() against a minimal duck-typed
     Context double — the actual authorization LOGIC (scope matching,
     fail-closed behavior), independent of the real MCP SDK.
  2. The ASGI mechanism require_scope() depends on in production: that
     _AuthGateASGI setting `request.state.stls_verified_identity` on
     its OWN Request survives a later `dict(scope)` shallow copy (the
     exact pattern StlsMcpMiddleware/the official SDK's internals use
     between the auth gate and the tool call) — a second
     starlette.requests.Request built from the copied scope must see
     the same state. This is the load-bearing claim in
     stls_mcp_identity_context.py's module docstring; it's tested
     directly here rather than only asserted in a comment.

Does NOT stand up a real FastMCP server/ClientSession — that full round
trip (real identity_verifier, real tool call, real Context injection)
is exercised live in samples/peer/20_operator_identity_federation.
"""

from __future__ import annotations

import pytest

from stls_mcp.stls_mcp_identity_context import (
    ScopeMismatchError,
    get_verified_identity,
    require_scope,
)


class _Result:
    def __init__(self, ok=True, requestor_did=None, grantor_did=None, scope=None):
        self.ok = ok
        self.requestor_did = requestor_did
        self.grantor_did = grantor_did
        self.scope = scope


class _State:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Request:
    def __init__(self, state=None):
        self.state = state


class _RequestContext:
    def __init__(self, request=None, raise_unavailable=False):
        self._request = request
        self._raise = raise_unavailable

    @property
    def request(self):
        if self._raise:
            raise ValueError("Context is not available outside of a request")
        return self._request


class _Ctx:
    def __init__(self, request_context):
        self.request_context = request_context


# ---------------------------------------------------------------------------
# get_verified_identity()
# ---------------------------------------------------------------------------

def test_get_verified_identity_absent_when_no_request():
    ctx = _Ctx(_RequestContext(request=None))
    assert get_verified_identity(ctx) is None


def test_get_verified_identity_absent_outside_a_request():
    ctx = _Ctx(_RequestContext(raise_unavailable=True))
    assert get_verified_identity(ctx) is None


def test_get_verified_identity_absent_when_identity_verifier_not_configured():
    ctx = _Ctx(_RequestContext(request=_Request(state=_State())))
    assert get_verified_identity(ctx) is None


def test_get_verified_identity_returns_the_stashed_result():
    result = _Result(ok=True, requestor_did="did:web:b:agents:042", scope={"action": "x"})
    ctx = _Ctx(_RequestContext(request=_Request(state=_State(stls_verified_identity=result))))
    assert get_verified_identity(ctx) is result


# ---------------------------------------------------------------------------
# require_scope()
# ---------------------------------------------------------------------------

def _ctx_with(result) -> _Ctx:
    return _Ctx(_RequestContext(request=_Request(state=_State(stls_verified_identity=result))))


def test_require_scope_fails_closed_with_no_verified_identity():
    ctx = _Ctx(_RequestContext(request=_Request(state=_State())))
    with pytest.raises(ScopeMismatchError, match="no verified identity"):
        require_scope(ctx, action="reroute_traffic")


def test_require_scope_fails_closed_on_plain_identity_no_delegation():
    ctx = _ctx_with(_Result(ok=True, scope=None))
    with pytest.raises(ScopeMismatchError, match="plain identity"):
        require_scope(ctx, action="reroute_traffic")


def test_require_scope_accepts_matching_parameters():
    ctx = _ctx_with(_Result(
        ok=True, scope={"action": "reroute_traffic", "segment": "MER-SEG-7"},
    ))
    scope = require_scope(ctx, action="reroute_traffic", segment="MER-SEG-7")
    assert scope == {"action": "reroute_traffic", "segment": "MER-SEG-7"}


def test_require_scope_rejects_mismatched_segment():
    """The exact failure the issue describes: a credential scoped to
    MER-SEG-7 must not authorize a call for a different segment."""
    ctx = _ctx_with(_Result(
        ok=True, scope={"action": "reroute_traffic", "segment": "MER-SEG-7"},
    ))
    with pytest.raises(ScopeMismatchError, match="MER-SEG-9"):
        require_scope(ctx, action="reroute_traffic", segment="MER-SEG-9")


def test_require_scope_rejects_mismatched_action():
    ctx = _ctx_with(_Result(ok=True, scope={"action": "reroute_traffic"}))
    with pytest.raises(ScopeMismatchError):
        require_scope(ctx, action="delete_segment")


def test_require_scope_extra_scope_keys_not_checked_are_ignored():
    """require_scope only checks the keys the TOOL cares about — a
    scope with additional keys the tool doesn't assert on isn't itself
    a mismatch."""
    ctx = _ctx_with(_Result(
        ok=True, scope={"action": "reroute_traffic", "segment": "MER-SEG-7", "extra": "whatever"},
    ))
    scope = require_scope(ctx, action="reroute_traffic", segment="MER-SEG-7")
    assert scope["extra"] == "whatever"


# ---------------------------------------------------------------------------
# The ASGI mechanism require_scope() depends on in production: state set
# on a Request built from `scope` must survive a later shallow copy of
# that same `scope` dict (dict(scope)), and be visible from a SECOND
# Request built from the copy.
# ---------------------------------------------------------------------------

def test_request_state_survives_shallow_scope_copy():
    from starlette.requests import Request as StarletteRequest

    scope = {"type": "http", "method": "POST", "path": "/mcp-stls", "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    outer = StarletteRequest(scope, receive)
    outer.state.stls_verified_identity = _Result(ok=True, scope={"segment": "MER-SEG-7"})

    # Exactly what StlsMcpMiddleware/the SDK's own internals do between
    # the auth gate and the tool call.
    inner_scope = dict(scope)
    inner = StarletteRequest(inner_scope, receive)

    assert inner.state.stls_verified_identity.scope == {"segment": "MER-SEG-7"}
