import time
import warnings

import jwt
import pytest

from eap_core.identity.local_idp import LocalIdPStub
from eap_core.identity.nhi import NonHumanIdentity


def test_nhi_caches_token_until_ttl_elapses(monkeypatch):
    idp = LocalIdPStub(for_testing=True)
    nhi = NonHumanIdentity(client_id="agent-1", idp=idp, roles=["operator"])
    t = nhi.get_token(audience="api.bank", scope="accounts:read")
    assert isinstance(t, str)
    cached = nhi.get_token(audience="api.bank", scope="accounts:read")
    assert cached == t


def test_nhi_returns_new_token_after_expiry():
    idp = LocalIdPStub(token_ttl=0, for_testing=True)
    nhi = NonHumanIdentity(client_id="agent-1", idp=idp, roles=["operator"])
    t1 = nhi.get_token(audience="api.bank", scope="accounts:read")
    # NHI's cache now uses ``time.time()`` (wall clock) so the IdP-issued
    # ``expires_at`` and the cache check share a clock domain. With
    # ``token_ttl=0`` and the default 5s buffer, every call should re-mint.
    time_to_expire = time.time() + 0.01
    while time.time() < time_to_expire:
        pass
    t2 = nhi.get_token(audience="api.bank", scope="accounts:read")
    assert t1 != t2


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
