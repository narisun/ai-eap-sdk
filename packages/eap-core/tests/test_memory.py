"""Tests for the MemoryStore Protocol and InMemoryStore default."""

from __future__ import annotations

from eap_core.memory import InMemoryStore, MemoryStore


def test_in_memory_store_satisfies_protocol():
    store = InMemoryStore()
    assert isinstance(store, MemoryStore)


async def test_remember_and_recall_round_trip():
    store = InMemoryStore()
    await store.remember("session-1", "favorite_color", "blue")
    assert await store.recall("session-1", "favorite_color") == "blue"


async def test_recall_missing_returns_none():
    store = InMemoryStore()
    assert await store.recall("nothing-here", "key") is None
    await store.remember("session-1", "k", "v")
    assert await store.recall("session-1", "different_key") is None


async def test_sessions_are_isolated():
    store = InMemoryStore()
    await store.remember("alice", "color", "blue")
    await store.remember("bob", "color", "red")
    assert await store.recall("alice", "color") == "blue"
    assert await store.recall("bob", "color") == "red"


async def test_list_keys_returns_only_session_keys():
    store = InMemoryStore()
    await store.remember("session-1", "k1", "v1")
    await store.remember("session-1", "k2", "v2")
    await store.remember("session-2", "k3", "v3")
    keys = list(await store.list_keys("session-1"))
    assert set(keys) == {"k1", "k2"}


async def test_forget_removes_key():
    store = InMemoryStore()
    await store.remember("session-1", "k", "v")
    await store.forget("session-1", "k")
    assert await store.recall("session-1", "k") is None


async def test_forget_is_no_op_on_missing():
    store = InMemoryStore()
    await store.forget("nothing", "k")  # must not raise


async def test_clear_removes_all_session_keys():
    store = InMemoryStore()
    await store.remember("session-1", "k1", "v1")
    await store.remember("session-1", "k2", "v2")
    await store.clear("session-1")
    assert list(await store.list_keys("session-1")) == []


async def test_context_carries_memory_store():
    """The Context dataclass exposes a memory_store field."""
    from eap_core.types import Context

    store = InMemoryStore()
    ctx = Context(memory_store=store, session_id="s1")
    assert ctx.memory_store is store
    assert ctx.session_id == "s1"


async def test_context_memory_store_default_is_none():
    from eap_core.types import Context

    ctx = Context()
    assert ctx.memory_store is None
    assert ctx.session_id == ""


def test_in_memory_store_named():
    """Backends expose a `name` attr for tracing/observability."""
    assert InMemoryStore.name == "in_memory"


async def test_in_memory_store_handles_unicode():
    store = InMemoryStore()
    await store.remember("session", "key", "café 🤖")
    assert await store.recall("session", "key") == "café 🤖"


async def test_remembering_same_key_overwrites():
    store = InMemoryStore()
    await store.remember("session", "k", "v1")
    await store.remember("session", "k", "v2")
    assert await store.recall("session", "k") == "v2"


def test_protocol_runtime_check_rejects_wrong_shape():
    # Runtime Protocol check uses duck-typing on method names.
    # Strict structural typing isn't enforced for missing methods, so
    # we just confirm a totally unrelated object isn't classified as a
    # MemoryStore. InMemoryStore matching the Protocol is asserted in
    # test_in_memory_store_satisfies_protocol above.
    assert not isinstance(object(), MemoryStore)


# ---- MemoryStore Protocol default-body contract -----------------------------
#
# Each ``async def`` in the ``MemoryStore`` Protocol has an ``...`` body so
# the runtime-checkable protocol can declare an interface without a forced
# raise. The tests below pin the default-body behavior: invoking the unbound
# class method on an arbitrary instance returns ``None``. If a future change
# alters a Protocol body to raise ``NotImplementedError`` (a common
# alternative shape), these tests catch it and force the maintainer to
# update the docs / contract. They are not tautological — they exercise the
# bodies of the five ``...`` lines (memory.py:40, 44, 48, 52, 56) and lock
# the "Protocol default is a no-op coroutine" decision.


class _Sentinel:
    """Bare object that doesn't implement any MemoryStore method itself.

    We pass this as ``self`` to the unbound Protocol methods so the only
    code that runs is the Protocol's ``...`` body — no override gets in
    the way.
    """


async def test_memorystore_protocol_remember_default_body_is_async_noop() -> None:
    # The Protocol body is ``...``; the resulting coroutine awaits to None
    # (no raise). The contract being pinned: subclasses are free to add
    # behavior, but the bare Protocol's default never raises.
    result = await MemoryStore.remember(_Sentinel(), "s", "k", "v")  # type: ignore[arg-type,func-returns-value]
    assert result is None


async def test_memorystore_protocol_recall_default_body_is_async_noop() -> None:
    result = await MemoryStore.recall(_Sentinel(), "s", "k")  # type: ignore[arg-type]
    assert result is None


async def test_memorystore_protocol_list_keys_default_body_is_async_noop() -> None:
    result = await MemoryStore.list_keys(_Sentinel(), "s")  # type: ignore[arg-type]
    assert result is None


async def test_memorystore_protocol_forget_default_body_is_async_noop() -> None:
    result = await MemoryStore.forget(_Sentinel(), "s", "k")  # type: ignore[arg-type,func-returns-value]
    assert result is None


async def test_memorystore_protocol_clear_default_body_is_async_noop() -> None:
    result = await MemoryStore.clear(_Sentinel(), "s")  # type: ignore[arg-type,func-returns-value]
    assert result is None
