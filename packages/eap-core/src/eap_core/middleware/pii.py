"""PII masking middleware.

Default behavior uses regex patterns and an in-context vault for
re-identification. The Presidio path is enabled when ``pii`` extra is
installed and ``engine="presidio"`` is passed.

Default regex coverage (H10): email, US SSN, US phone (with or without
country code), credit card (generic 16-digit), Amex 15-digit, IPv4, and
international phone (``+`` prefix). Presidio remains the recommended
engine for production — the regex set is a safety net for the default
install where the ``pii`` extra is not present.

Note: IBAN detection is deferred to Presidio (install the ``[pii]``
extra). A regex-only IBAN matcher has poor precision — the shape
``LL##XXX...`` (2 letters + 2 digits + 11+ alphanumerics) catches any
sufficiently long mixed identifier, producing false positives on
tracking IDs and test fixtures.

Streaming notes (H12)
---------------------

``on_stream_chunk`` buffers across chunk boundaries when a partial vault
token straddles the split (e.g. ``<EMAIL_aaaa`` in chunk N, ``bbbbbbbbbbbbbbbb>``
in chunk N+1). The buffer is keyed on the active ``Context``: per-context
state lives in ``ctx.metadata["pii._stream_buffer"]`` so concurrent streams
do not interfere. The trade-off is small added latency until the closing
``>`` arrives. If a stream genuinely contains a stray ``<`` that never
closes, it is flushed when the producer signals end-of-stream by sending
a chunk with ``finish_reason`` set.

This implementation is best-effort: callers who need a hard guarantee
should terminate PII masking before stream emission and apply unmask
post-hoc on the final assembled text.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from collections.abc import Iterator
from typing import Any, Literal

from eap_core.middleware.base import PassthroughMiddleware
from eap_core.types import Chunk, Context, Message, Request, Response

_LOG = logging.getLogger(__name__)

# P1-6: labels that require a Luhn modulus-10 check to qualify as PII.
# The CREDIT_CARD regex matches any 16-digit-with-separators sequence;
# Luhn filters out false positives like phone numbers or arbitrary
# numeric IDs that happen to fit the shape. Amex (15-digit, 3[47]
# prefix) is checked the same way once it matches the prefix regex.
_LUHN_LABELS = frozenset({"CREDIT_CARD", "AMEX"})

_DEFAULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Amex 15-digit (3[47] prefix) — checked BEFORE the generic 16-digit
    # credit-card pattern so the AMEX label sticks.
    ("AMEX", re.compile(r"\b3[47]\d{13}\b")),
    ("CREDIT_CARD", re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")),
    # International phone — leading "+", optional separators (spaces/dashes)
    # between segments, 7-15 digits total. Anchored by the "+" so it does
    # not collide with US PHONE.
    ("PHONE_INTL", re.compile(r"\+\d{1,3}(?:[\s-]?\(?\d{1,4}\)?){1,4}\d{1,4}")),
    # US phone — country code group is fully optional so bare formats
    # like ``(415) 555-1234`` and ``415-555-1234`` are matched alongside
    # ``+1 415 555 1234``. The leading/trailing ``\b`` keeps it from
    # firing on arbitrarily long digit strings.
    ("PHONE_US", re.compile(r"\b(?:\+?\d{1,3}[\s-]?)?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}\b")),
    # IBAN detection deferred to Presidio (install the [pii] extra).
    # A regex-only IBAN matcher has poor precision (matches 15+ char
    # alphanumeric IDs of shape LL##XXX...).
    # IPv4 with simple range check baked into the regex.
    (
        "IPV4",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"),
    ),
)

# Metadata key that buffers partial-token text across streaming chunks.
_STREAM_BUFFER_KEY = "pii._stream_buffer"

# Maximum width of a vault token: ``<LABEL_<16hex>>`` where the longest
# label is ``CREDIT_CARD`` (11 chars). 11 + 2 (``<``, ``_``) + 16 + 1
# (``>``) = 30; pad to 32 for headroom. Used by streaming buffer lookback
# so a stray ``<`` cannot trigger unbounded buffer growth (Issue #2).
_MAX_TOKEN_LEN = 32


def _passes_luhn(digits_str: str) -> bool:
    """Validate a card number using the Luhn algorithm (P1-6).

    Returns True if the digit sequence is a valid Luhn checksum, False
    otherwise. Used to filter false-positive credit-card regex matches
    (e.g., 16-digit phone numbers or arbitrary numeric sequences).

    Standard card lengths: 13 (rare), 14, 15 (Amex), 16 (most), 19 (some).
    Separators (``-``/space) are ignored — pass either the raw match or
    a normalized digits-only string.
    """
    digits = [int(c) for c in digits_str if c.isdigit()]
    if len(digits) not in (13, 14, 15, 16, 19):
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _replace_in_text(
    text: str, vault: dict[str, str], patterns: tuple[tuple[str, re.Pattern[str]], ...]
) -> str:
    out = text
    for label, pat in patterns:
        # P1-6: card-shaped labels gate masking on Luhn validity so we
        # don't redact 16-digit phone numbers or arbitrary numeric IDs.
        requires_luhn = label in _LUHN_LABELS

        def _sub(m: re.Match[str], _label: str = label, _check_luhn: bool = requires_luhn) -> str:
            value = m.group(0)
            if _check_luhn and not _passes_luhn(value):
                # Regex matched the shape but the digits fail Luhn —
                # leave the original text in place.
                return value
            # Token width: 16 hex chars (was 8) — lower collision probability
            # in long sessions and across overlapping vaults (H11).
            token = f"<{_label}_{secrets.token_hex(8)}>"
            vault[token] = value
            return token

        out = pat.sub(_sub, out)
    return out


def _content_iter(content: str | list[dict[str, object]]) -> Iterator[str]:  # pragma: no cover
    if isinstance(content, str):
        yield content
    else:
        for part in content:
            if isinstance(part, dict) and "text" in part:
                yield str(part["text"])


class PiiMaskingMiddleware(PassthroughMiddleware):
    name = "pii_masking"

    def __init__(
        self,
        engine: Literal["regex", "presidio"] = "regex",
        patterns: tuple[tuple[str, re.Pattern[str]], ...] | None = None,
    ) -> None:
        self._engine = engine
        self._patterns = patterns or _DEFAULT_PATTERNS
        self._presidio: Any = None
        if engine == "presidio":
            self._init_presidio()

    def _init_presidio(self) -> None:  # pragma: no cover  # exercised in extras tests
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
        except ImportError as e:
            raise ImportError(
                "engine='presidio' requires the [pii] extra: pip install eap-core[pii]"
            ) from e
        self._presidio = (AnalyzerEngine(), AnonymizerEngine())  # type: ignore[no-untyped-call,unused-ignore]

    def _mask_text(self, text: str, vault: dict[str, str]) -> str:
        if self._engine == "regex":
            return _replace_in_text(text, vault, self._patterns)
        # Presidio path — exercised in tests/extras/test_pii_presidio.py
        analyzer, anonymizer = self._presidio  # pragma: no cover
        results = analyzer.analyze(text=text, language="en")  # pragma: no cover
        if not results:  # pragma: no cover
            return text
        from presidio_anonymizer.entities import OperatorConfig  # pragma: no cover

        resolved = anonymizer.anonymize(  # pragma: no cover
            text=text,
            analyzer_results=results,
            operators={"DEFAULT": OperatorConfig("replace", {"new_value": "<<PII>>"})},
        )
        out: str = resolved.text  # pragma: no cover
        for item in sorted(resolved.items, key=lambda i: i.start):  # pragma: no cover
            token = f"<{item.entity_type}_{uuid.uuid4().hex[:16]}>"
            original = text[item.start : item.end]
            vault[token] = original
            out = out.replace("<<PII>>", token, 1)
        return out  # pragma: no cover

    def _mask_message(self, msg: Message, vault: dict[str, str]) -> Message:
        if isinstance(msg.content, str):
            return msg.model_copy(update={"content": self._mask_text(msg.content, vault)})
        new_parts: list[dict[str, object]] = []
        for part in msg.content:
            if isinstance(part, dict) and "text" in part:
                new_parts.append({**part, "text": self._mask_text(part["text"], vault)})
            else:
                new_parts.append(part)
        return msg.model_copy(update={"content": new_parts})

    # ------------------------------------------------------------------ unmask
    @staticmethod
    def _unmask(text: str, *, vault: dict[str, str], ctx: Context | None = None) -> str:
        """Replace every vault token in ``text`` with its original value.

        Uses a single regex alternation, longest-token-first, so a token
        that is a prefix of another (e.g. ``<EMAIL_aa>`` vs ``<EMAIL_aabb>``)
        cannot be partially matched (H11). Token order in ``vault`` does
        not affect the result.

        When ``ctx`` is provided the compiled alternation is cached on
        ``ctx.metadata`` keyed by vault size. On the streaming path this
        fires per chunk; rebuilding the alternation each call dominated
        the cost for large vaults.
        """
        if not vault:
            return text
        cache_key = f"pii._unmask_cache_{len(vault)}"
        cached: re.Pattern[str] | None = None
        if ctx is not None:
            # Drop any stale cache entries (vault grew since last call)
            # — keeps the per-context cache bounded to one entry (M-N6).
            stale_keys = [
                k
                for k in list(ctx.metadata)
                if k.startswith("pii._unmask_cache_") and k != cache_key
            ]
            for k in stale_keys:
                del ctx.metadata[k]
            cached = ctx.metadata.get(cache_key)
        if cached is None:
            tokens = sorted(vault.keys(), key=len, reverse=True)
            cached = re.compile("|".join(re.escape(t) for t in tokens))
            if ctx is not None:
                ctx.metadata[cache_key] = cached
        return cached.sub(lambda m: vault[m.group(0)], text)

    # --------------------------------------------------------------- lifecycle
    async def on_request(self, req: Request, ctx: Context) -> Request:
        count_before = len(ctx.vault)
        new_msgs = [self._mask_message(m, ctx.vault) for m in req.messages]
        # Dev-guide §3.7 names this key; impl previously never wrote it (H11).
        ctx.metadata["pii.masked_count"] = len(ctx.vault) - count_before
        return req.model_copy(update={"messages": new_msgs})

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        if not ctx.vault:
            return resp
        return resp.model_copy(update={"text": self._unmask(resp.text, vault=ctx.vault, ctx=ctx)})

    async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk:
        """Unmask vault tokens, buffering partial tokens across chunks.

        A token of the form ``<LABEL_<hex>>`` may be split across two
        chunks. We hold any trailing text starting at an unmatched ``<``
        in ``ctx.metadata[_STREAM_BUFFER_KEY]`` until either:

        - the next chunk supplies the closing ``>``, or
        - the producer signals end-of-stream (``finish_reason`` is set),
          at which point the buffer is flushed verbatim.
        """
        buffer: str = ctx.metadata.get(_STREAM_BUFFER_KEY, "")
        combined = buffer + chunk.text

        # If a "<" is open without a closing ">", retain from there for
        # the next chunk. Skip the search entirely on the final chunk so
        # any stray "<" is emitted verbatim instead of buffered forever.
        #
        # Lookback is bounded to ``_MAX_TOKEN_LEN`` chars (Issue #2): any
        # ``<`` earlier than ``len(combined) - _MAX_TOKEN_LEN`` cannot be
        # the opener of a vault token still in flight, so holding past
        # that point would just buffer arbitrary content forever (stray
        # ``<`` in code/text emitted by the LLM).
        emit: str
        held: str
        if chunk.finish_reason is None:
            search_start = max(0, len(combined) - _MAX_TOKEN_LEN)
            open_idx = combined.rfind("<", search_start)
            close_idx = combined.rfind(">", search_start)
            if open_idx != -1 and open_idx > close_idx:
                emit = combined[:open_idx]
                held = combined[open_idx:]
            else:
                emit = combined
                held = ""
        else:
            emit = combined
            held = ""

        ctx.metadata[_STREAM_BUFFER_KEY] = held

        if not ctx.vault:
            return chunk.model_copy(update={"text": emit})
        return chunk.model_copy(update={"text": self._unmask(emit, vault=ctx.vault, ctx=ctx)})

    async def on_stream_end(self, ctx: Context) -> None:
        """Flush + clear the per-context partial-token buffer.

        A non-empty buffer at ``on_stream_end`` means the upstream
        finished without emitting a chunk with ``finish_reason`` set --
        likely an abrupt stop or mid-stream exception (which fires
        ``on_stream_end`` via the pipeline's ``finally`` block). The
        buffer content is dropped rather than re-emitted: the Protocol
        does not allow yielding from ``on_stream_end``, and silently
        re-emitting partial content is a correctness/security
        liability. We log a WARNING so operators can investigate.

        Closes v1.6.2 follow-up #1 (PII stream buffer leak).
        """
        buffer = ctx.metadata.pop(_STREAM_BUFFER_KEY, None)
        if buffer:
            _LOG.warning(
                "PII stream buffer non-empty at on_stream_end (%d chars); "
                "upstream finished without a finish_reason chunk. Held text "
                "is being dropped to prevent state leak across requests.",
                len(buffer),
            )

    async def on_call_end(self, ctx: Context) -> None:
        """Clear the per-context vault + stream buffer.

        The vault (``ctx.vault``) holds ``<LABEL_xxxx>`` -> original-PII
        mappings populated in ``on_request`` and consumed in
        ``on_response``. Today the vault relies on ctx GC; patterns that
        retain ctx for background work leak the mapping. ``on_call_end``
        always fires (T1: v1.7 ``finally`` block) so this is the right
        place to clear.

        Stream buffer is cleared defensively even though
        ``on_stream_end`` normally handles it -- defense-in-depth for
        paths that bypass ``on_stream_end`` somehow.

        Closes v1.6.2 follow-up #2 (PII vault leak symmetry).
        """
        ctx.vault.clear()
        ctx.metadata.pop(_STREAM_BUFFER_KEY, None)
