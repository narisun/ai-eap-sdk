"""Pipeline error-handling symmetry + httpx-ownership lifecycle (Task 8).

Covers H4 (``run_stream`` must invoke ``on_error`` for a middleware whose
``on_request`` raises — mirroring ``run``), H5 (secondary exceptions from
``on_error`` itself must be logged + chained, not silently swallowed), and
H1 (``EnterpriseLLM.aclose()`` closes IdP-side components it owns).
"""

from __future__ import annotations

import logging

import pytest

from eap_core import EnterpriseLLM, RuntimeConfig
from eap_core.identity.token_exchange import OIDCTokenExchange
from eap_core.middleware.base import PassthroughMiddleware
from eap_core.middleware.pipeline import MiddlewarePipeline
from eap_core.types import Context, Request


@pytest.mark.asyncio
async def test_run_stream_calls_on_error_when_on_request_raises() -> None:
    """H4 — ``run_stream`` must run ``on_error`` on the raising middleware.

    Previously ``run_stream`` appended to ``ran`` AFTER awaiting
    ``on_request``, so a middleware that raised in ``on_request`` never
    received its own ``on_error`` callback (asymmetric with ``run``).
    """
    seen: list[str] = []

    class Raiser(PassthroughMiddleware):
        name = "raiser"

        async def on_request(self, req: Request, ctx: Context) -> Request:
            raise RuntimeError("boom")

        async def on_error(self, exc: Exception, ctx: Context) -> None:
            seen.append("raiser_on_error")

    pipe = MiddlewarePipeline([Raiser()])

    async def terminal(r: Request, c: Context):
        async def gen():
            yield "never"

        async for x in gen():  # pragma: no cover — never reached
            yield x

    with pytest.raises(RuntimeError):
        async for _ in pipe.run_stream(Request(model="x", messages=[]), Context(), terminal):
            pass  # pragma: no cover — terminal never runs

    assert seen == ["raiser_on_error"]


@pytest.mark.asyncio
async def test_on_error_secondary_exception_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """H5 — a middleware whose ``on_error`` itself raises is logged, not swallowed.

    Auditors need failures visible. The previous ``except Exception: pass``
    hid genuine transport/timeout bugs in error-handling paths. We now log
    at WARNING with ``exc_info`` and attach a PEP 678 note to the primary
    so the secondary survives re-raise in the rendered traceback.
    """

    class BrokenErrorHandler(PassthroughMiddleware):
        name = "broken-handler"

        async def on_request(self, req: Request, ctx: Context) -> Request:
            raise RuntimeError("primary")

        async def on_error(self, exc: Exception, ctx: Context) -> None:
            raise ValueError("secondary failure inside on_error")

    pipe = MiddlewarePipeline([BrokenErrorHandler()])

    async def terminal(r: Request, c: Context) -> object:
        raise AssertionError("terminal should never run")  # pragma: no cover

    caplog.set_level(logging.WARNING, logger="eap_core.middleware.pipeline")

    with pytest.raises(RuntimeError) as excinfo:
        await pipe.run(Request(model="x", messages=[]), Context(), terminal)  # type: ignore[arg-type]

    # The secondary failure must be surfaced in the logs (WARNING, with
    # exc_info) and attached to the primary via PEP 678 __notes__.
    assert any(
        rec.levelno == logging.WARNING
        and "broken-handler" in rec.getMessage()
        and "secondary failure inside on_error" in rec.getMessage()
        for rec in caplog.records
    ), (
        f"expected WARNING log for secondary failure, got: {[r.getMessage() for r in caplog.records]}"
    )
    notes = getattr(excinfo.value, "__notes__", [])
    assert any(
        "broken-handler" in n and "ValueError" in n and "secondary failure inside on_error" in n
        for n in notes
    ), f"expected secondary surfaced via __notes__, got: {notes}"


@pytest.mark.asyncio
async def test_on_error_multiple_secondaries_all_surfaced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every middleware whose ``on_error`` raises must be surfaced.

    The previous implementation assigned ``exc.__context__ = secondary``
    inside the loop, so when two middlewares both raised the second
    assignment clobbered the first. With PEP 678 ``__notes__`` and a
    WARNING log per secondary, all failures remain visible to operators.
    """

    class RaiserA(PassthroughMiddleware):
        name = "raiser-a"

        async def on_error(self, exc: Exception, ctx: Context) -> None:
            raise ValueError("from A")

    class RaiserB(PassthroughMiddleware):
        name = "raiser-b"

        async def on_error(self, exc: Exception, ctx: Context) -> None:
            raise RuntimeError("from B")

    # A passthrough that raises in terminal so both A and B have run
    # ``on_request`` and will receive ``on_error``.
    pipe = MiddlewarePipeline([RaiserA(), RaiserB()])

    async def terminal(r: Request, c: Context) -> object:
        raise KeyError("primary")

    caplog.set_level(logging.WARNING, logger="eap_core.middleware.pipeline")

    with pytest.raises(KeyError) as excinfo:
        await pipe.run(Request(model="x", messages=[]), Context(), terminal)  # type: ignore[arg-type]

    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any("raiser-a" in r.getMessage() and "from A" in r.getMessage() for r in warnings), (
        f"expected raiser-a warning, got: {[r.getMessage() for r in warnings]}"
    )
    assert any("raiser-b" in r.getMessage() and "from B" in r.getMessage() for r in warnings), (
        f"expected raiser-b warning, got: {[r.getMessage() for r in warnings]}"
    )

    notes = getattr(excinfo.value, "__notes__", [])
    assert any("raiser-a" in n and "ValueError" in n and "from A" in n for n in notes), (
        f"expected raiser-a note, got: {notes}"
    )
    assert any("raiser-b" in n and "RuntimeError" in n and "from B" in n for n in notes), (
        f"expected raiser-b note, got: {notes}"
    )


@pytest.mark.asyncio
async def test_enterprise_llm_aclose_closes_owned_http_clients() -> None:
    """H1 — ``EnterpriseLLM.aclose()`` closes IdP-side components it owns."""
    closed = {"v": False}

    class StubHttp:
        async def post(self, *a: object, **k: object) -> object:
            raise NotImplementedError

        async def aclose(self) -> None:
            closed["v"] = True

    # ``http=StubHttp()`` is passed in — but ``OIDCTokenExchange`` still
    # registers the http client on self._http; here we want to verify that
    # passing it to ``EnterpriseLLM(token_exchange=...)`` causes the
    # token-exchange object's ``aclose`` to be invoked. The token-exchange
    # ``aclose`` itself only closes the pool when ``_owns_http`` is True;
    # so to exercise the close path we let OIDCTokenExchange own the pool
    # by injecting StubHttp as if we'd just constructed a default one.
    ex = OIDCTokenExchange(token_endpoint="https://idp/token")
    ex._http = StubHttp()  # type: ignore[assignment]
    ex._owns_http = True

    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="x"),
        token_exchange=ex,
    )
    await client.aclose()
    assert closed["v"] is True


@pytest.mark.asyncio
async def test_oidc_token_exchange_does_not_close_borrowed_http() -> None:
    """H1 — when the caller supplies ``http=``, we treat the pool as borrowed.

    Closing a pool the caller still uses elsewhere would break their app;
    only pools we created get closed in ``aclose``.
    """
    closed = {"v": False}

    class BorrowedHttp:
        async def aclose(self) -> None:
            closed["v"] = True

    ex = OIDCTokenExchange(token_endpoint="https://idp/token", http=BorrowedHttp())  # type: ignore[arg-type]
    await ex.aclose()
    assert closed["v"] is False
