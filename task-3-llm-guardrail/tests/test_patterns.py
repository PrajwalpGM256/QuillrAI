"""Pattern definitions: derived widths, tight witnesses, detection edges, and Luhn."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from llm_guardrail import patterns
from llm_guardrail.patterns import (
    CARD,
    CARD_MAX_DIGITS,
    EMAIL,
    EMAIL_LABEL_MAX,
    EMAIL_LOCAL_MAX,
    EMAIL_MAX_LABELS,
    EMAIL_TLD_MAX,
    HELD_BOUND,
    MAX_PATTERN_LENGTH,
    PATTERNS,
    SSN,
    luhn,
    max_width,
)
from tests.helpers import luhn_complete

# --- width derivation -------------------------------------------------------------------------


def test_private_parser_api_still_present() -> None:
    import re._parser as p

    assert callable(p.parse) and hasattr(p.parse("a").__class__, "getwidth") and p.MAXREPEAT > 10**9


def test_unbounded_pattern_is_refused() -> None:
    with pytest.raises(ValueError, match="no finite maximum"):
        max_width(r"[a-z]+@x")


def test_email_max_is_the_rfc_arithmetic() -> None:
    # local part, "@", each label plus its dot, then the TLD
    expected = EMAIL_LOCAL_MAX + 1 + EMAIL_MAX_LABELS * (EMAIL_LABEL_MAX + 1) + EMAIL_TLD_MAX
    assert expected == 64 + 1 + 4 * (63 + 1) + 63 == 384
    assert EMAIL.max_match == expected


def test_card_and_ssn_max_are_the_arithmetic() -> None:
    assert (
        CARD.max_match == CARD_MAX_DIGITS + (CARD_MAX_DIGITS - 1) == 37
    )  # digits plus one separator between each
    assert SSN.max_match == len("123-45-6789") == 11


def test_window_is_max_match_plus_lookahead() -> None:
    for p in PATTERNS:
        assert p.window == p.max_match + 1  # every lookahead is a single character class
    assert MAX_PATTERN_LENGTH == max(p.window for p in PATTERNS) == 385
    assert HELD_BOUND == MAX_PATTERN_LENGTH - 1 == 384


def _witnesses() -> dict[str, str]:
    digits = luhn_complete("4" * (CARD_MAX_DIGITS - 1))
    return {
        "ssn": "123-45-6789",
        "card": " ".join(digits),  # 19 digits, 18 separators
        "email": "a" * EMAIL_LOCAL_MAX
        + "@"
        + ("b" * EMAIL_LABEL_MAX + ".") * EMAIL_MAX_LABELS
        + "c" * EMAIL_TLD_MAX,
    }


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.name)
def test_witness_reaches_the_derived_max(pattern) -> None:
    """The bound is tight: a real match of exactly max_match characters exists, built from the limits."""
    witness = _witnesses()[pattern.name]
    assert len(witness) == pattern.max_match
    assert re.fullmatch(pattern.body, witness)  # stdlib re, independent of the regex module
    assert pattern.longest_end(witness, 0) == len(witness)


# --- detection edges --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "matches"),
    [
        ("123-45-6789", True),
        ("078-05-1120", True),
        ("000-12-3456", False),
        ("666-12-3456", False),
        ("900-12-3456", False),
        ("123-00-4567", False),
        ("123-45-0000", False),
        ("123456789", False),  # dashes only
        ("123 45 6789", False),
    ],
)
def test_ssn_shapes(text: str, matches: bool) -> None:
    assert (SSN.longest_end(text, 0) == len(text)) is matches


@pytest.mark.parametrize(
    ("text", "end"),
    [
        ("4111111111111111", 16),
        ("4111 1111 1111 1111", 19),
        ("4111-1111-1111-1111", 19),
        ("3782 822463 10005", 17),  # Amex grouping, 15 digits
        ("4111111111111112", None),  # fails Luhn
        ("4111 1111 1111 1111 5", 19),  # longest Luhn-valid end wins; the stray digit is not a card digit
        ("41111111111111115", None),  # 17 contiguous digits: a shorter end is followed by a digit
        ("4111  1111 1111 1111", None),  # double separator breaks the run below 13 digits
        ("411111111111", None),  # 12 digits
        ("4" * 20, None),
    ],
)
def test_card_longest_luhn_valid(text: str, end: int | None) -> None:
    assert CARD.longest_end(text, 0) == end


@pytest.mark.parametrize(
    ("text", "end"),
    [
        ("a@b.co", 6),
        ("first.last+tag@mail.example.co.uk", 33),
        ("a@b.com.", 7),  # sentence-ending dot is not part of the address
        ("a@b.c", None),  # TLD needs 2 letters
        ("a@-b.com", None),
        ("@b.com", None),
    ],
)
def test_email_shapes(text: str, end: int | None) -> None:
    assert EMAIL.longest_end(text, 0) == end


# --- Luhn -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "number",
    ["4111111111111111", "5555555555554444", "378282246310005", "6011111111111117", "79927398713"],
)
def test_luhn_accepts_published_test_numbers(number: str) -> None:
    assert luhn(number)


@pytest.mark.parametrize("bad", ["79927398710", "4111111111111112", "", "41a1", "٤١١١"])
def test_luhn_rejects(bad: str) -> None:
    assert not luhn(bad)


@given(st.text(alphabet="0123456789", min_size=1, max_size=18), st.data())
def test_luhn_detects_every_single_digit_change(prefix: str, data) -> None:
    number = luhn_complete(prefix)
    assert luhn(number)
    i = data.draw(st.integers(0, len(number) - 1))
    other = data.draw(st.sampled_from([d for d in "0123456789" if d != number[i]]))
    assert not luhn(number[:i] + other + number[i + 1 :])


def test_module_exports_token() -> None:
    assert patterns.TOKEN == "[REDACTED]"
