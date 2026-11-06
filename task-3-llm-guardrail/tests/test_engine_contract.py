"""The `regex` partial-matching semantics the redactor relies on, pinned against the installed version.

decide_at() treats a position as OPEN when ``fullmatch(text, pos, partial=True)`` is not None, meaning
text[pos:] is a prefix of, or equal to, some match. These tests pin that contract, and also pin why
``match(partial=True)`` is NOT enough (it would break chunk-invariance).
"""

from __future__ import annotations

import pytest

from llm_guardrail.patterns import CARD, EMAIL, SSN


def fm(pattern, text: str, pos: int = 0):
    m = pattern.compiled.fullmatch(text, pos, partial=True)
    return None if m is None else (m.span(), m.partial)


@pytest.mark.parametrize(
    ("pattern", "text", "viable"),
    [
        # complete match at the buffer end: still viable, the lookahead char is unknown
        (SSN, "123-45-6789", True),
        (EMAIL, "john.doe@x.com", True),
        (CARD, "4111 1111 1111 1111", True),
        # proper prefixes are viable
        (SSN, "123-45-678", True),
        (EMAIL, "john.doe", True),
        (EMAIL, "john.doe@", True),
        (EMAIL, "a@b.c", True),
        (CARD, "4111 ", True),
        # a longer match could still form although a shorter complete match exists inside
        (EMAIL, "a@b.com.x", True),
        (EMAIL, "a@b.com-", True),
        # decided: a character past the end of any possible match has been seen
        (SSN, "123-45-6789 ", False),
        (SSN, "123-45-67890", False),
        (SSN, "000-", False),
        (EMAIL, "john.doe@x.com ", False),
        (EMAIL, "john doe", False),
        (EMAIL, "a" * 65, False),
        (CARD, "4" * 20, False),
        (CARD, "4111  ", False),
    ],
)
def test_fullmatch_partial_is_the_open_test(pattern, text: str, viable: bool) -> None:
    assert pattern.viable(text, 0) is viable


def test_lookbehind_reads_before_pos() -> None:
    assert SSN.viable("a123-4", 1) is True
    assert SSN.viable("5123-4", 1) is False  # preceded by a digit
    assert EMAIL.viable(" john", 1) is True
    assert EMAIL.viable("xjohn", 1) is False  # mid-word


def test_match_partial_is_not_enough() -> None:
    """match(partial=True) reports a shorter complete match where a longer one may still form.

    Deciding on it would emit "[REDACTED].x" for buffer "a@b.com.x", but if "y" arrives the full-string
    answer is "[REDACTED]" (a@b.com.xy). This is why viable() uses fullmatch.
    """
    m = EMAIL.compiled.match("a@b.com.x", 0, partial=True)
    assert m is not None and m.span() == (0, 7) and m.partial is False
    assert fm(EMAIL, "a@b.com.x") == ((0, 9), True)


def test_posix_flag_gives_leftmost_longest() -> None:
    assert EMAIL.longest.match("a@b.co.uk z").span() == (0, 9)
    assert EMAIL.longest.match("a@b.cd.ef-").span() == (0, 6)
