from eap_core.testing.fixtures import (
    assert_pii_round_trip,
    capture_traces,
    make_test_client,
)
from eap_core.testing.responses import canned_responses


async def test_make_test_client_runs_end_to_end():
    client = make_test_client()
    resp = await client.generate_text("hello")
    assert "[local-runtime]" in resp.text


async def test_capture_traces_collects_metadata_attributes():
    client = make_test_client()
    with capture_traces() as traces:
        await client.generate_text("hello")
    assert any(t["gen_ai.request.model"] for t in traces)


def test_assert_pii_round_trip_helper_matches_email():
    text = "ping me at sundar@example.com"
    processed_with_token = "ping me at <EMAIL_abc123>"
    vault = {"<EMAIL_abc123>": "sundar@example.com"}
    assert_pii_round_trip(text, processed_with_token, vault)


async def test_canned_responses_cm_serves_yaml_responses():
    entries = [{"match": "capital of Spain", "text": "Madrid."}]
    with canned_responses(entries) as td:
        assert (td / "responses.yaml").exists()
        client = make_test_client()
        resp = await client.generate_text("capital of Spain")
        assert resp.text == "Madrid."


async def test_make_test_client_with_extra_middlewares():
    from eap_core.middleware.base import PassthroughMiddleware

    class NullMiddleware(PassthroughMiddleware):
        name = "null"

    client = make_test_client(extra_middlewares=[NullMiddleware()])
    resp = await client.generate_text("hi")
    assert isinstance(resp.text, str)
