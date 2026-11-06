"""The held buffer never exceeds MAX_PATTERN_LENGTH - 1 bytes, and the bound is reached.

held_bytes counts encoded bytes of the text at or after the settlement point. The incomplete UTF-8
tail in the decoder is a separate variable with its own bound of 3 bytes.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from llm_guardrail.patterns import (
    EMAIL_LABEL_MAX,
    EMAIL_LOCAL_MAX,
    EMAIL_MAX_LABELS,
    EMAIL_TLD_MAX,
    HELD_BOUND,
    MAX_PATTERN_LENGTH,
)
from llm_guardrail.redactor import OPEN, StreamRedactor, decide_at
from tests.helpers import pii_text, split_at


@st.composite
def long_emails(draw) -> str:
    """Emails up to the full RFC-bounded size, to push the held buffer toward its limit."""
    local = draw(st.text(alphabet="ab9._+-", min_size=1, max_size=EMAIL_LOCAL_MAX))
    n = draw(st.integers(1, EMAIL_MAX_LABELS))
    labels = ["x" + draw(st.text(alphabet="xy7-", max_size=EMAIL_LABEL_MAX - 2)) + "z" for _ in range(n)]
    tld = draw(st.text(alphabet="abc", min_size=2, max_size=EMAIL_TLD_MAX))
    return f"{local}@{'.'.join(labels)}.{tld}"


def assert_bounded_run(raw: bytes, chunks: list[bytes]) -> int:
    r = StreamRedactor()
    peak = 0
    for chunk in chunks:
        r.feed(chunk)
        assert r.held_bytes <= HELD_BOUND == MAX_PATTERN_LENGTH - 1
        assert r.decoder_pending_bytes <= 3
        assert r.held_text.isascii()
        if r.held_text:
            # the held text starts exactly at an OPEN position: the settlement point
            context = r._text  # noqa: SLF001 - white-box check of the settlement point
            assert decide_at(context, r._cursor, eof=False) is OPEN  # noqa: SLF001
        peak = max(peak, r.held_bytes)
    r.finish()
    assert r.held_bytes == 0 and r.decoder_pending_bytes == 0
    return peak


@given(pii_text(), st.data())
def test_bound_on_pii_text(text: str, data) -> None:
    raw = text.encode("utf-8")
    cuts = data.draw(st.lists(st.integers(0, len(raw)), max_size=max(1, len(raw))))
    assert_bounded_run(raw, split_at(raw, cuts))


@given(st.lists(long_emails(), min_size=1, max_size=3), st.data())
def test_bound_on_long_emails(addresses: list[str], data) -> None:
    raw = " ".join(addresses).encode("ascii")
    cuts = data.draw(st.lists(st.integers(0, len(raw)), max_size=60))
    assert_bounded_run(raw, split_at(raw, cuts))


@given(st.binary(max_size=400), st.data())
def test_bound_on_arbitrary_bytes(raw: bytes, data) -> None:
    cuts = data.draw(st.lists(st.integers(0, len(raw)), max_size=max(1, len(raw))))
    assert_bounded_run(raw, split_at(raw, cuts))


def test_bound_is_reached_exactly() -> None:
    """A maximum-length email with its lookahead character still unseen holds exactly HELD_BOUND bytes."""
    witness = (
        "a" * EMAIL_LOCAL_MAX + "@" + ("b" * EMAIL_LABEL_MAX + ".") * EMAIL_MAX_LABELS + "c" * EMAIL_TLD_MAX
    )
    r = StreamRedactor()
    assert r.feed(witness.encode()) == b""
    assert r.held_bytes == HELD_BOUND == 384
    assert r.feed(b" ") == b"[REDACTED] "
    assert r.held_bytes == 0


def test_byte_at_a_time_never_exceeds_bound() -> None:
    witness = "a" * 64 + "@" + ("b" * 63 + ".") * 4 + "c" * 63 + "x"  # one char longer than any match
    r = StreamRedactor()
    for b in witness.encode():
        r.feed(bytes([b]))
        assert r.held_bytes <= HELD_BOUND


def test_decoder_tail_is_separate_from_held_text() -> None:
    r = StreamRedactor()
    r.feed(b"123-45-678")
    assert r.held_bytes == 10 and r.decoder_pending_bytes == 0
    r.feed("9😀".encode()[:3])  # '9' plus the first 2 bytes of a 4-byte character
    assert r.held_bytes == 11 and r.decoder_pending_bytes == 2
    assert r.feed("😀".encode()[2:]) == "[REDACTED]😀".encode()
