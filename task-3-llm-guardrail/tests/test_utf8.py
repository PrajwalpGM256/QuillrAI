"""UTF-8 boundary safety: chunks that end inside a multi-byte character are deferred, not corrupted."""

from __future__ import annotations

import pytest

from llm_guardrail.redactor import StreamRedactor, redact

# 2-, 3- and 4-byte characters directly adjacent to each PII type
TEXT = "é123-45-6789€4111111111111111中a@b.co😀"
EXPECTED = "é[REDACTED]€[REDACTED]中[REDACTED]😀"


def test_one_shot_baseline() -> None:
    assert redact(TEXT.encode()) == EXPECTED.encode()


@pytest.mark.parametrize("char", ["é", "€", "中", "😀"])
def test_split_inside_each_multibyte_character(char: str) -> None:
    raw = TEXT.encode()
    start = raw.index(char.encode())
    width = len(char.encode())
    assert width >= 2
    for k in range(1, width):  # every interior offset of the character
        cut = start + k
        r = StreamRedactor()
        first = r.feed(raw[:cut])
        assert r.decoder_pending_bytes == k  # the incomplete character is deferred
        first.decode("utf-8")  # emitted bytes are always complete UTF-8
        out = first + r.feed(raw[cut:]) + r.finish()
        assert out == EXPECTED.encode()
        assert "�" not in out.decode()


def test_every_byte_alone() -> None:
    raw = TEXT.encode()
    r = StreamRedactor()
    pieces = [r.feed(raw[i : i + 1]) for i in range(len(raw))] + [r.finish()]
    for piece in pieces:
        piece.decode("utf-8")  # never a partial character on the wire
    assert b"".join(pieces) == EXPECTED.encode()


def test_truncated_character_at_end_of_stream_matches_one_shot() -> None:
    raw = "123-45-6789 é".encode()[:-1]  # stream ends mid-character
    r = StreamRedactor()
    out = r.feed(raw) + r.finish()
    assert out == redact(raw) == "[REDACTED] �".encode()
