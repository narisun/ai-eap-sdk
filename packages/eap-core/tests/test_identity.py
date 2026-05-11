import asyncio
import time
import warnings

import jwt
import pytest

from eap_core.identity.local_idp import LocalIdPStub
from eap_core.identity.nhi import NonHumanIdentity, resolve_token


async def test_nhi_caches_token_until_ttl_elapses(monkeypatch):
    idp = LocalIdPStub(for_testing=True)
    nhi = NonHumanIdentity(client_id="agent-1", idp=idp, roles=["operator"])
    t = await nhi.get_token(audience="api.bank", scope="accounts:read")
    assert isinstance(t, str)
    cached = await nhi.get_token(audience="api.bank", scope="accounts:read")
    assert cached == t


async def test_nhi_returns_new_token_after_expiry():
    idp = LocalIdPStub(token_ttl=0, for_testing=True)
    nhi = NonHumanIdentity(client_id="agent-1", idp=idp, roles=["operator"])
    t1 = await nhi.get_token(audience="api.bank", scope="accounts:read")
    # NHI's cache uses ``time.time()`` (wall clock) so the IdP-issued
    # ``expires_at`` and the cache check share a clock domain. With
    # ``token_ttl=0`` and the v0.6.0 default 30s buffer, every call should
    # re-mint (issued expires_at is ~now+1s, buffer-adjusted check
    # ``expires_at - 30 > now`` is always false → always cache-miss).
    time_to_expire = time.time() + 0.01
    while time.time() < time_to_expire:
        pass
    t2 = await nhi.get_token(audience="api.bank", scope="accounts:read")
    assert t1 != t2


async def test_nhi_default_cache_buffer_matches_jwt_verifier_skew():
    """N-N1: NHI cache_buffer_seconds default must match the inbound
    verifier's clock_skew_seconds (30) so an agent whose JWT exp is 30s
    ahead of the server's clock cannot observe a cached-then-rejected
    token."""
    nhi = NonHumanIdentity(client_id="a", idp=LocalIdPStub(for_testing=True))
    assert nhi.cache_buffer_seconds == 30


async def test_nhi_concurrent_get_token_does_not_double_issue():
    """20 concurrent get_token calls for the same key issue only once.

    H2: without the ``asyncio.Lock`` around the cache-miss path, N
    concurrent callers all see an empty cache, all call ``idp.issue``,
    and N writes race — doubling cost against a paid / rate-limited IdP.
    The lock serializes the miss path; only the first awaiter calls
    ``issue`` and the rest read the freshly-populated cache.
    """
    issued: list[str] = []

    class CountingIdP:
        def issue(self, *, client_id, audience, scope, roles=None):
            issued.append(audience)
            return f"tok-{len(issued)}", time.time() + 300

    nhi = NonHumanIdentity(client_id="a", idp=CountingIdP(), default_audience="b")
    tokens = await asyncio.gather(*[nhi.get_token() for _ in range(20)])
    assert len(set(tokens)) == 1
    assert len(issued) == 1


def test_local_idp_issues_jwt_with_expected_claims():
    idp = LocalIdPStub(for_testing=True)
    token, expires_at = idp.issue(
        client_id="agent-1", audience="api.bank", scope="x", roles=["operator"]
    )
    assert expires_at > time.time()
    payload = idp.verify(token, expected_audience="api.bank")
    assert payload["sub"] == "agent-1"
    assert payload["aud"] == "api.bank"
    assert payload["scope"] == "x"
    assert "operator" in payload["roles"]


def test_local_idp_rejects_tampered_token():
    idp = LocalIdPStub(for_testing=True)
    token, _ = idp.issue(client_id="x", audience="y", scope="z")
    tampered = token[:-2] + "AA"
    with pytest.raises(Exception):
        idp.verify(tampered, expected_audience="y")


def test_local_idp_verify_rejects_wrong_audience():
    idp = LocalIdPStub(for_testing=True)
    token, _ = idp.issue(client_id="a", audience="aud-1", scope="x")
    with pytest.raises(jwt.InvalidAudienceError):
        idp.verify(token, expected_audience="aud-2")


def test_local_idp_warns_when_not_marked_for_testing():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        LocalIdPStub()  # no for_testing kwarg
    assert any(
        issubclass(rec.category, RuntimeWarning) and "not for production" in str(rec.message)
        for rec in w
    )


def test_local_idp_silent_when_marked_for_testing():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        LocalIdPStub(for_testing=True)
    assert not w


async def test_resolve_token_handles_sync_and_async():
    """``resolve_token`` awaits async identities and passes sync ones through.

    Both gateway clients (AgentCore, Vertex) call this helper so the same
    shim covers ``NonHumanIdentity`` (async since v0.5.0) and sync
    identities like ``VertexAgentIdentityToken`` (wraps sync google-auth).
    """

    class SyncIdentity:
        def get_token(self, *, audience=None, scope=""):
            return "sync-tok"

    class AsyncIdentity:
        async def get_token(self, *, audience=None, scope=""):
            return "async-tok"

    assert await resolve_token(SyncIdentity(), audience="a") == "sync-tok"
    assert await resolve_token(AsyncIdentity(), audience="a") == "async-tok"


async def test_nhi_per_key_locking_does_not_serialize_distinct_audiences():
    """Structurally verify the M-N2 refactor: distinct (audience, scope)
    keys produce distinct entries in ``_locks``, and the legacy single
    ``_lock`` attribute is gone.

    This is a structural test rather than a concurrency-timing test:
    ``IdentityProvider.issue`` is sync, and ``asyncio.gather`` of two
    sync ``time.sleep`` calls would serialize regardless of locking
    design. A true parallelism assertion would require an async IdP
    shape (out of scope for the v0.6.0 cleanup release).
    """
    issued: list[str] = []

    class FastIdP:
        def issue(self, *, client_id, audience, scope, roles=None):
            issued.append(audience)
            return f"tok-{audience}", time.time() + 300

    nhi = NonHumanIdentity(client_id="a", idp=FastIdP())
    a, b = await asyncio.gather(
        nhi.get_token(audience="x"),
        nhi.get_token(audience="y"),
    )
    # Both keys produced distinct tokens — neither was overwritten under
    # a shared cache lookup.
    assert a != b
    assert set(issued) == {"x", "y"}
    # Structural assertion of the refactor: per-key locks created (and
    # cached) ONE lock per distinct key — observable as two distinct
    # entries in ``_locks`` after the calls. The two-level locking
    # design also keeps ``_locks_mutex`` as a separate ``asyncio.Lock``.
    assert len(nhi._locks) == 2
    assert ("x", "") in nhi._locks
    assert ("y", "") in nhi._locks
    assert isinstance(nhi._locks_mutex, asyncio.Lock)
    assert isinstance(nhi._locks[("x", "")], asyncio.Lock)
    # And the legacy single-lock attribute is gone — code that touched
    # ``nhi._lock`` directly must migrate to the per-key map.
    assert not hasattr(nhi, "_lock")


def test_enterprise_llm_identity_accepts_vertex_agent_identity_token():
    """v0.5.1 reviewer follow-up: ``VertexAgentIdentityToken`` must
    satisfy the ``IdentityToken`` Protocol. ``EnterpriseLLM(identity=)``
    accepts both ``NonHumanIdentity`` and Vertex's sync token impl
    under one structural type — mypy-strict no longer needs ignore
    comments at the Vertex call sites.
    """
    from eap_core import EnterpriseLLM
    from eap_core.config import RuntimeConfig
    from eap_core.identity import IdentityToken
    from eap_core.integrations.vertex import VertexAgentIdentityToken

    vat = VertexAgentIdentityToken()
    assert isinstance(vat, IdentityToken)
    client = EnterpriseLLM(RuntimeConfig(provider="local", model="echo-1"), identity=vat)
    assert client.identity is vat


def test_local_idp_issue_returns_token_and_expires_at():
    """Step 8.3: ``IdentityProvider.issue`` returns ``(token, expires_at)``.

    The expiry is wall-clock seconds so callers can compare it directly to
    the JWT ``exp`` claim — no more probing ``idp._ttl`` from ``NonHumanIdentity``.
    """
    stub = LocalIdPStub(for_testing=True)
    result = stub.issue(client_id="a", audience="b", scope="r")
    assert isinstance(result, tuple)
    token, expires_at = result
    assert isinstance(token, str)
    assert isinstance(expires_at, float)
    assert expires_at > time.time()
