"""Incremental SSE parser: any byte split of a stream yields the same events."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from llm_guardrail.sse import Event, SSEError, SSEParser, encode_data
from tests.helpers import split_at

STREAM = (
    ": comment\r\n"
    'data: {"x":"é€😀"}\r\n\r\n'
    "event: note\nid: 7\ndata: line1\ndata: line2\n\n"
    "data: cr-only\r\r"
    "data: [DONE]\n\n"
).encode()

EXPECTED = [
    Event(data='{"x":"é€😀"}'),
    Event(data="line1\nline2", event="note", id="7"),
    Event(data="cr-only"),
    Event(data="[DONE]"),
]


def parse(chunks: list[bytes]) -> list[Event]:
    p = SSEParser()
    return [e for c in chunks for e in p.feed(c)]


def test_whole_stream() -> None:
    assert parse([STREAM]) == EXPECTED


def test_every_two_chunk_split_including_inside_characters_and_crlf() -> None:
    for i in range(len(STREAM) + 1):
        assert parse([STREAM[:i], STREAM[i:]]) == EXPECTED, f"split at {i}"


@given(st.lists(st.integers(0, len(STREAM)), max_size=30))
def test_any_chunking(cuts: list[int]) -> None:
    assert parse(split_at(STREAM, cuts)) == EXPECTED


def test_one_byte_at_a_time() -> None:
    assert parse([STREAM[i : i + 1] for i in range(len(STREAM))]) == EXPECTED


def test_oversized_event_fails_closed() -> None:
    p = SSEParser(max_event_bytes=64)
    with pytest.raises(SSEError):
        p.feed(b"data: " + b"x" * 100)


def test_encode_roundtrip() -> None:
    assert parse([encode_data("a\nb")]) == [Event(data="a\nb")]
