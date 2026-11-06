"""Chunk-invariance: any chunking of the input yields exactly the one-shot redaction, byte for byte."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from llm_guardrail.redactor import StreamRedactor, redact
from tests.helpers import pii_text, split_at


def stream(chunks: list[bytes]) -> bytes:
    r = StreamRedactor()
    return b"".join(r.feed(c) for c in chunks) + r.finish()


def chunkings(data: bytes):
    return st.lists(st.integers(0, len(data)), max_size=max(1, len(data))).map(
        lambda cuts: split_at(data, cuts)
    )


@given(pii_text(), st.data())
def test_pii_text_any_chunking(text: str, data) -> None:
    raw = text.encode("utf-8")
    chunks = data.draw(chunkings(raw))
    assert stream(chunks) == redact(raw)


@given(st.binary(max_size=300), st.data())
def test_arbitrary_bytes_any_chunking(raw: bytes, data) -> None:
    """Includes invalid UTF-8: the incremental decoder must replace exactly as a one-shot decode does."""
    chunks = data.draw(chunkings(raw))
    assert stream(chunks) == redact(raw)


@given(pii_text())
def test_one_byte_chunks(text: str) -> None:
    raw = text.encode("utf-8")
    assert stream([raw[i : i + 1] for i in range(len(raw))]) == redact(raw)


@given(pii_text(), st.data())
def test_empty_chunks_are_harmless(text: str, data) -> None:
    raw = text.encode("utf-8")
    chunks = data.draw(chunkings(raw))
    padded = [piece for c in chunks for piece in (b"", c, b"")]
    assert stream(padded) == redact(raw)


def test_lookbehind_context_survives_a_fully_emitted_chunk() -> None:
    """The character before a chunk boundary still counts as lookbehind for the next chunk.

    A 65-char local part is too long for an email, so it is emitted in full; "y@b.com" that follows is
    then mid-word and must not match. Likewise a 20-digit run is emitted, and the SSN-shaped tail that
    follows is preceded by a digit.
    """
    for first, second in [(b"x" * 65, b"y@b.com done"), (b"1" * 20, b"23-45-6789 done")]:
        r = StreamRedactor()
        out = r.feed(first) + r.feed(second) + r.finish()
        assert out == redact(first + second) == first + second
