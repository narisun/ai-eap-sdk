import time

import pytest

from eap_core.identity.local_idp import LocalIdPStub
from eap_core.identity.nhi import NonHumanIdentity


def test_nhi_caches_token_until_ttl_elapses(monkeypatch):
    idp = LocalIdPStub()
    nhi = NonHumanIdentity(client_id="agent-1", idp=idp, roles=["operator"])
    t = nhi.get_token(audience="api.bank", scope="accounts:read")
    assert isinstance(t, str)
    cached = nhi.get_token(audience="api.bank", scope="accounts:read")
    assert cached == t


def test_nhi_returns_new_token_after_expiry():
    idp = LocalIdPStub(token_ttl=0)
    nhi = NonHumanIdentity(client_id="agent-1", idp=idp, roles=["operator"])
    t1 = nhi.get_token(audience="api.bank", scope="accounts:read")
    time_to_expire = time.monotonic() + 0.01
    while time.monotonic() < time_to_expire:
        pass
    t2 = nhi.get_token(audience="api.bank", scope="accounts:read")
    assert t1 != t2


def test_local_idp_issues_jwt_with_expected_claims():
    idp = LocalIdPStub()
    token = idp.issue(client_id="agent-1", audience="api.bank", scope="x", roles=["operator"])
    payload = idp.verify(token)
    assert payload["sub"] == "agent-1"
    assert payload["aud"] == "api.bank"
    assert payload["scope"] == "x"
    assert "operator" in payload["roles"]


def test_local_idp_rejects_tampered_token():
    idp = LocalIdPStub()
    token = idp.issue(client_id="x", audience="y", scope="z")
    tampered = token[:-2] + "AA"
    with pytest.raises(Exception):
        idp.verify(tampered)
