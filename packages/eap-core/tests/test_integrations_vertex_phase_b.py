"""Tests for Vertex Phase B: Memory Bank + Code Sandbox + Browser Sandbox."""

from __future__ import annotations

import pytest

from eap_core.exceptions import RealRuntimeDisabledError
from eap_core.integrations.vertex import (
    VertexBrowserSandbox,
    VertexCodeSandbox,
    VertexMemoryBankStore,
    register_browser_sandbox_tools,
    register_code_sandbox_tools,
)
from eap_core.memory import MemoryStore
from eap_core.sandbox import BrowserSandbox, CodeSandbox


@pytest.fixture(autouse=True)
def _clear_real_flag(monkeypatch):
    monkeypatch.delenv("EAP_ENABLE_REAL_RUNTIMES", raising=False)


# ---- VertexMemoryBankStore ------------------------------------------------


def test_memory_bank_construction_does_not_hit_google_cloud():
    import sys

    sys.modules.pop("google.cloud.aiplatform_v1beta1", None)
    _ = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")
    assert "google.cloud.aiplatform_v1beta1" not in sys.modules


def test_memory_bank_satisfies_memory_store_protocol():
    """``VertexMemoryBankStore`` must structurally conform to ``MemoryStore``."""
    s = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")
    assert isinstance(s, MemoryStore)
    assert s.name == "vertex_memory_bank"


@pytest.mark.asyncio
async def test_memory_bank_remember_gated_by_env_flag():
    s = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")
    with pytest.raises(RealRuntimeDisabledError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await s.remember("session1", "k", "v")


@pytest.mark.asyncio
async def test_memory_bank_recall_gated_by_env_flag():
    s = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")
    with pytest.raises(RealRuntimeDisabledError):
        await s.recall("session1", "k")


@pytest.mark.asyncio
async def test_memory_bank_list_keys_gated():
    s = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")
    with pytest.raises(RealRuntimeDisabledError):
        await s.list_keys("s")


@pytest.mark.asyncio
async def test_memory_bank_forget_gated():
    s = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")
    with pytest.raises(RealRuntimeDisabledError):
        await s.forget("s", "k")


@pytest.mark.asyncio
async def test_memory_bank_clear_gated():
    s = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")
    with pytest.raises(RealRuntimeDisabledError):
        await s.clear("s")


def test_memory_bank_parent_path_format():
    s = VertexMemoryBankStore(project_id="p1", location="europe-west1", memory_bank_id="mb-xx")
    assert s._parent() == "projects/p1/locations/europe-west1/memoryBanks/mb-xx"


# ---- VertexMemoryBankStore.recall — narrowed exception handling (H16) ----
#
# These tests need ``google.api_core.exceptions`` to import the NotFound /
# PermissionDenied types. The [gcp] extra is not installed in the default
# test-core matrix; ``pytest.importorskip`` gates the tests so they skip
# cleanly when the extra is missing — they still document the contract.


@pytest.mark.asyncio
async def test_memory_bank_recall_returns_none_on_not_found(monkeypatch):
    """``NotFound`` from the Vertex client maps to ``None`` (cache miss)."""
    gax_exceptions = pytest.importorskip("google.api_core.exceptions")
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    store = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")

    class _NotFoundClient:
        def get_memory(self, **_kwargs):
            raise gax_exceptions.NotFound("memory record absent")

    monkeypatch.setattr(store, "_client", lambda: _NotFoundClient())

    assert await store.recall("session-1", "key-1") is None


@pytest.mark.asyncio
async def test_memory_bank_recall_propagates_credentials_error(monkeypatch):
    """Auth errors must propagate — NOT be silently masked as cache miss (H16)."""
    gax_exceptions = pytest.importorskip("google.api_core.exceptions")
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    store = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")

    class _DeniedClient:
        def get_memory(self, **_kwargs):
            raise gax_exceptions.PermissionDenied("403 forbidden")

    monkeypatch.setattr(store, "_client", lambda: _DeniedClient())

    with pytest.raises(gax_exceptions.PermissionDenied):
        await store.recall("session-1", "key-1")


@pytest.mark.asyncio
async def test_memory_bank_recall_propagates_throttle_error(monkeypatch):
    """Throttling / quota errors must propagate (H16)."""
    gax_exceptions = pytest.importorskip("google.api_core.exceptions")
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    store = VertexMemoryBankStore(project_id="p", memory_bank_id="mb1")

    class _ThrottleClient:
        def get_memory(self, **_kwargs):
            raise gax_exceptions.ResourceExhausted("429 quota exceeded")

    monkeypatch.setattr(store, "_client", lambda: _ThrottleClient())

    with pytest.raises(gax_exceptions.ResourceExhausted):
        await store.recall("session-1", "key-1")


# ---- VertexCodeSandbox ----------------------------------------------------


def test_code_sandbox_construction_lazy():
    import sys

    sys.modules.pop("google.cloud.aiplatform_v1beta1", None)
    _ = VertexCodeSandbox(project_id="p")
    assert "google.cloud.aiplatform_v1beta1" not in sys.modules


def test_code_sandbox_satisfies_code_sandbox_protocol():
    s = VertexCodeSandbox(project_id="p")
    assert isinstance(s, CodeSandbox)
    assert s.name == "vertex_code_sandbox"


@pytest.mark.asyncio
async def test_code_sandbox_execute_gated():
    s = VertexCodeSandbox(project_id="p")
    with pytest.raises(RealRuntimeDisabledError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await s.execute("python", "print(1)")


def test_register_code_sandbox_tools_adds_three_tools():
    """Tools register on a McpToolRegistry without hitting GCP."""
    from eap_core.mcp.registry import McpToolRegistry

    reg = McpToolRegistry()
    register_code_sandbox_tools(reg, project_id="p")
    names = {t.name for t in reg.list_tools()}
    assert {"execute_python", "execute_javascript", "execute_typescript"} <= names


@pytest.mark.asyncio
async def test_registered_code_tool_raises_without_env_flag():
    """The registered tool should raise RealRuntimeDisabledError when invoked."""
    from eap_core.mcp.registry import McpToolRegistry

    reg = McpToolRegistry()
    register_code_sandbox_tools(reg, project_id="p")
    spec = next(t for t in reg.list_tools() if t.name == "execute_python")
    with pytest.raises(RealRuntimeDisabledError):
        await spec.fn(code="print(1)")


# ---- VertexBrowserSandbox -------------------------------------------------


def test_browser_sandbox_satisfies_browser_sandbox_protocol():
    b = VertexBrowserSandbox(project_id="p")
    assert isinstance(b, BrowserSandbox)
    assert b.name == "vertex_browser_sandbox"


@pytest.mark.asyncio
async def test_browser_navigate_gated():
    b = VertexBrowserSandbox(project_id="p")
    with pytest.raises(RealRuntimeDisabledError):
        await b.navigate("https://example.com")


@pytest.mark.asyncio
async def test_browser_click_gated():
    b = VertexBrowserSandbox(project_id="p")
    with pytest.raises(RealRuntimeDisabledError):
        await b.click("#button")


@pytest.mark.asyncio
async def test_browser_fill_gated():
    b = VertexBrowserSandbox(project_id="p")
    with pytest.raises(RealRuntimeDisabledError):
        await b.fill("#input", "value")


@pytest.mark.asyncio
async def test_browser_extract_text_gated():
    b = VertexBrowserSandbox(project_id="p")
    with pytest.raises(RealRuntimeDisabledError):
        await b.extract_text()


@pytest.mark.asyncio
async def test_browser_screenshot_gated():
    b = VertexBrowserSandbox(project_id="p")
    with pytest.raises(RealRuntimeDisabledError):
        await b.screenshot()


def test_register_browser_sandbox_tools_adds_five_tools():
    from eap_core.mcp.registry import McpToolRegistry

    reg = McpToolRegistry()
    register_browser_sandbox_tools(reg, project_id="p")
    names = {t.name for t in reg.list_tools()}
    expected = {
        "browser_navigate",
        "browser_click",
        "browser_fill",
        "browser_extract_text",
        "browser_screenshot",
    }
    assert expected <= names
