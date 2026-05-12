"""Regression tests for LocalRuntimeAdapter responses caching (P2-9).

Default behavior: ``_load_responses`` is invoked exactly once at adapter
construction and the parsed list is reused across requests. The opt-in
``options.reload_responses=True`` flag preserves the old per-request
reload for dev workflows that want "edit responses.yaml, see next
request pick it up".
"""

from __future__ import annotations

from unittest.mock import patch

from eap_core.config import RuntimeConfig
from eap_core.runtimes.local import LocalRuntimeAdapter
from eap_core.types import Message, Request


async def test_caches_responses_yaml_by_default() -> None:
    config = RuntimeConfig(provider="local", model="echo")
    with patch("eap_core.runtimes.local._load_responses") as mock_load:
        mock_load.return_value = []
        adapter = LocalRuntimeAdapter(config)
        req = Request(model="echo", messages=[Message(role="user", content="hi")])
        await adapter.generate(req)
        await adapter.generate(req)
        await adapter.generate(req)
    # Loaded exactly once (at init); subsequent generate() calls hit the cache.
    assert mock_load.call_count == 1


async def test_reloads_when_reload_responses_option_set() -> None:
    config = RuntimeConfig(
        provider="local",
        model="echo",
        options={"reload_responses": True},
    )
    with patch("eap_core.runtimes.local._load_responses") as mock_load:
        mock_load.return_value = []
        adapter = LocalRuntimeAdapter(config)
        req = Request(model="echo", messages=[Message(role="user", content="hi")])
        await adapter.generate(req)
        await adapter.generate(req)
    # In reload mode, init does NOT load and every generate() reloads.
    assert mock_load.call_count == 2


async def test_no_init_cache_in_reload_mode() -> None:
    config = RuntimeConfig(
        provider="local",
        model="echo",
        options={"reload_responses": True},
    )
    with patch("eap_core.runtimes.local._load_responses") as mock_load:
        mock_load.return_value = []
        LocalRuntimeAdapter(config)
    assert mock_load.call_count == 0


async def test_cached_responses_still_match(tmp_path, monkeypatch) -> None:
    """End-to-end: cached responses.yaml is still consulted on generate()."""
    yaml_file = tmp_path / "responses.yaml"
    yaml_file.write_text("responses:\n  - match: 'cached marker'\n    text: 'cached hit'\n")
    monkeypatch.chdir(tmp_path)

    adapter = LocalRuntimeAdapter(RuntimeConfig(provider="local", model="echo"))
    # Mutate the file after construction; cached path should ignore the change.
    yaml_file.write_text("responses:\n  - match: 'cached marker'\n    text: 'different'\n")

    resp = await adapter.generate(
        Request(
            model="echo",
            messages=[Message(role="user", content="please cached marker text")],
        )
    )
    assert resp.text == "cached hit"  # served from init-time cache, not re-read


async def test_reload_responses_picks_up_edits(tmp_path, monkeypatch) -> None:
    """opt-in reload reads the file on every generate()."""
    yaml_file = tmp_path / "responses.yaml"
    yaml_file.write_text("responses:\n  - match: 'edit marker'\n    text: 'first'\n")
    monkeypatch.chdir(tmp_path)

    adapter = LocalRuntimeAdapter(
        RuntimeConfig(
            provider="local",
            model="echo",
            options={"reload_responses": True},
        )
    )
    resp1 = await adapter.generate(
        Request(model="echo", messages=[Message(role="user", content="edit marker now")])
    )
    assert resp1.text == "first"

    yaml_file.write_text("responses:\n  - match: 'edit marker'\n    text: 'second'\n")
    resp2 = await adapter.generate(
        Request(model="echo", messages=[Message(role="user", content="edit marker now")])
    )
    assert resp2.text == "second"
