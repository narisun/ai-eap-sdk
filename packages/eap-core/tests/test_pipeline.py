import pytest

from eap_core.middleware.pipeline import MiddlewarePipeline
from eap_core.types import Chunk, Context, Message, Request, Response


class RecordingMiddleware:
    """Records the order of on_request and on_response calls."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    async def on_request(self, req: Request, ctx: Context) -> Request:
        self._log.append(f"req:{self.name}")
        return req

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        self._log.append(f"resp:{self.name}")
        return resp

    async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk:
        return chunk

    async def on_stream_end(self, ctx: Context) -> None:
        return None

    async def on_error(self, exc: Exception, ctx: Context) -> None:
        self._log.append(f"err:{self.name}")


async def _terminal(req: Request, ctx: Context) -> Response:
    return Response(text="ok")


async def test_pipeline_runs_request_left_to_right_response_right_to_left():
    log: list[str] = []
    pipe = MiddlewarePipeline(
        [
            RecordingMiddleware("a", log),
            RecordingMiddleware("b", log),
            RecordingMiddleware("c", log),
        ]
    )
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="hi")])
    await pipe.run(req, ctx, _terminal)
    assert log == ["req:a", "req:b", "req:c", "resp:c", "resp:b", "resp:a"]


async def test_pipeline_calls_on_error_in_reverse_for_already_run_middlewares():
    log: list[str] = []

    class Boom(RecordingMiddleware):
        async def on_request(self, req: Request, ctx: Context) -> Request:
            log.append(f"req:{self.name}")
            raise RuntimeError("boom")

    pipe = MiddlewarePipeline(
        [RecordingMiddleware("a", log), Boom("b", log), RecordingMiddleware("c", log)]
    )
    ctx = Context()
    req = Request(model="m", messages=[Message(role="user", content="hi")])
    with pytest.raises(RuntimeError, match="boom"):
        await pipe.run(req, ctx, _terminal)
    assert log == ["req:a", "req:b", "err:b", "err:a"]


async def test_pipeline_streams_chunks_through_each_middleware_in_order():
    chunks_seen: list[str] = []

    class Tagger:
        name = "tag"

        async def on_request(self, req: Request, ctx: Context) -> Request:
            return req

        async def on_response(self, resp: Response, ctx: Context) -> Response:
            return resp

        async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk:
            chunks_seen.append(chunk.text)
            return Chunk(
                index=chunk.index, text=chunk.text + "!", finish_reason=chunk.finish_reason
            )

        async def on_stream_end(self, ctx: Context) -> None:
            return None

        async def on_error(self, exc: Exception, ctx: Context) -> None:
            pass

    pipe = MiddlewarePipeline([Tagger()])

    async def gen():
        for i, t in enumerate(["a", "b", "c"]):
            yield Chunk(index=i, text=t, finish_reason=None)

    ctx = Context()
    out: list[str] = []
    async for c in pipe.run_stream(Request(model="m", messages=[]), ctx, lambda r, c2: gen()):
        out.append(c.text)
    assert out == ["a!", "b!", "c!"]
    assert chunks_seen == ["a", "b", "c"]


async def test_pipeline_run_stream_calls_on_error_on_exception():
    log: list[str] = []

    class BoomStream:
        name = "boomstream"

        async def on_request(self, req: Request, ctx: Context) -> Request:
            log.append("req:boomstream")
            return req

        async def on_response(self, resp: Response, ctx: Context) -> Response:
            return resp

        async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk:
            raise RuntimeError("stream boom")

        async def on_stream_end(self, ctx: Context) -> None:
            return None

        async def on_error(self, exc: Exception, ctx: Context) -> None:
            log.append(f"err:boomstream:{type(exc).__name__}")

    pipe = MiddlewarePipeline([BoomStream()])

    async def gen():
        yield Chunk(index=0, text="a", finish_reason=None)

    ctx = Context()
    with pytest.raises(RuntimeError, match="stream boom"):
        async for _ in pipe.run_stream(Request(model="m", messages=[]), ctx, lambda r, c: gen()):
            pass
    assert any("err:boomstream" in entry for entry in log)
