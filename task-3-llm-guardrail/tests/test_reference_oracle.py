"""redact() agrees with an independent brute-force reference implementation.

test_invariance.py compares the redactor with itself under different chunkings, so a systematic bug in
decide_at() would pass it. This file compares the redactor with tests/reference.py, which implements
leftmost-longest-valid from the definition with stdlib re and no streaming.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from llm_guardrail.redactor import redact, redact_text
from tests.helpers import luhn_complete, pii_text
from tests.reference import reference_redact


@given(pii_text())
def test_agrees_on_pii_rich_text(text: str) -> None:
    assert redact_text(text) == reference_redact(text)


@given(st.text())
def test_agrees_on_arbitrary_text(text: str) -> None:
    assert redact_text(text) == reference_redact(text)


@given(st.binary())
def test_agrees_on_arbitrary_bytes(data: bytes) -> None:
    assert redact(data) == reference_redact(data.decode("utf-8", errors="replace")).encode("utf-8")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ssn 123-45-6789.", "ssn [REDACTED]."),
        ("card 4111 1111 1111 1111 5 end", "card [REDACTED] 5 end"),
        ("mail a@b.com.", "mail [REDACTED]."),
        ("a@b.com.xy", "[REDACTED]"),
        ("123-45-67890", "123-45-67890"),
        ("not 4111111111111112", "not 4111111111111112"),
        ("x123-45-6789", "x[REDACTED]"),  # a letter is not a digit boundary
        ("é123-45-6789😀", "é[REDACTED]😀"),
        ("123-45-6789 1234", "[REDACTED] 1234"),  # 13 digits but not Luhn-valid as a card, so SSN wins
    ],
)
def test_known_examples(text: str, expected: str) -> None:
    assert redact_text(text) == expected
    assert reference_redact(text) == expected


@given(pii_text())
def test_pattern_order_only_breaks_ties(text: str) -> None:
    """Leftmost-longest is order independent: reversing PATTERNS (used only for ties) changes nothing.

    This pins "longest wins": with SSN checked first, a first-match rule would pick an SSN over a longer
    card starting at the same position.
    """
    from llm_guardrail import patterns, redactor

    forward = redact_text(text)
    original = redactor.PATTERNS
    redactor.PATTERNS = tuple(reversed(patterns.PATTERNS))
    try:
        assert redact_text(text) == forward
    finally:
        redactor.PATTERNS = original


def test_longest_beats_order_on_a_concrete_case() -> None:
    """A 13-digit Luhn-valid card whose first 11 characters are SSN-shaped: the whole card is redacted,
    even with SSN checked first."""
    from llm_guardrail import patterns, redactor

    card = "123-45-6789-" + luhn_complete("1234567890" + "00")[-4:]
    assert patterns.luhn(card.replace("-", "")) and len(card.replace("-", "")) == 13
    assert patterns.SSN.longest_end(card, 0) == 11  # the SSN alone would also match here

    original = redactor.PATTERNS
    redactor.PATTERNS = (patterns.SSN, patterns.CARD, patterns.EMAIL)
    try:
        assert redact_text(card) == "[REDACTED]"  # not "[REDACTED]-xxxx"
    finally:
        redactor.PATTERNS = original
    assert reference_redact(card) == "[REDACTED]"
