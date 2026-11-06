"""The admission estimate: exact formula, monotonic, and conservative."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from llm_router.tokens import InvalidMessages, estimate_tokens


def msg(text) -> dict:
    return {"role": "user", "content": text}


def test_formula() -> None:
    assert estimate_tokens([msg("")]) == 4
    assert estimate_tokens([msg("abc")]) == 5
    assert estimate_tokens([msg("abcd")]) == 6
    assert estimate_tokens([msg("é")]) == 5  # 2 UTF-8 bytes
    assert estimate_tokens([msg("hi"), msg("there")]) == (1 + 4) + (2 + 4)
    parts = [{"type": "text", "text": "abc"}, {"type": "text", "text": "def"}]
    assert estimate_tokens([msg(parts)]) == 2 + 4


@pytest.mark.parametrize("bad", [None, [], "hi", [1], [None], {"role": "user"}])
def test_invalid_messages(bad) -> None:
    with pytest.raises(InvalidMessages):
        estimate_tokens(bad)


@given(st.text(), st.text())
def test_monotonic_in_content(a: str, b: str) -> None:
    assert estimate_tokens([msg(a + b)]) >= estimate_tokens([msg(a)])


@given(st.text(alphabet=st.characters(codec="ascii"), min_size=1))
def test_conservative_for_ascii(text: str) -> None:
    # At least the common ~4 characters per token rule, before per-message overhead.
    assert estimate_tokens([msg(text)]) - 4 >= math.ceil(len(text) / 4)


@given(st.text(alphabet=st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF), min_size=1))
def test_at_least_one_token_per_cjk_character(text: str) -> None:
    assert estimate_tokens([msg(text)]) - 4 >= len(text)
