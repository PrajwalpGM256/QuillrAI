"""No partial PII is ever emitted under one-character chunking.

Filler never contains digits, "@" or uppercase letters, and every PII value is built only from those
(plus separators), so any digit, "@" or uppercase letter in an emitted chunk is a leaked fragment.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from llm_guardrail.patterns import TOKEN
from llm_guardrail.redactor import StreamRedactor
from tests.helpers import cards, ssns
from tests.reference import reference_redact

FILLER = st.text(alphabet=st.sampled_from(list("abcxyz .,;:!?\néü€中😀")), max_size=10)


@st.composite
def upper_emails(draw) -> str:
    local = draw(st.text(alphabet="ABCXYZ0189._+-", min_size=1, max_size=10))
    labels = draw(st.lists(st.text(alphabet="ABCXYZ09", min_size=1, max_size=6), min_size=1, max_size=3))
    tld = draw(st.text(alphabet="ABCDEFG", min_size=2, max_size=5))
    return f"{local}@{'.'.join(labels)}.{tld}"


def leak(chunk: str) -> bool:
    # The token is emitted whole within one chunk, so removing it leaves only real output characters.
    return any(c.isdigit() or c == "@" or c.isupper() for c in chunk.replace(TOKEN, ""))


@given(st.lists(st.tuples(FILLER, st.one_of(ssns(), cards(), upper_emails())), min_size=1, max_size=6))
def test_no_fragment_in_any_emitted_chunk(parts: list[tuple[str, str]]) -> None:
    # " ; " keeps a card's digit run from absorbing the next value's digits
    text = "".join(f"{filler} ; {pii} ; " for filler, pii in parts)
    r = StreamRedactor()
    emitted = [r.feed(ch.encode("utf-8")).decode("utf-8") for ch in text] + [r.finish().decode("utf-8")]
    for chunk in emitted:
        assert not leak(chunk), f"fragment leaked in {chunk!r}"
    assert "".join(emitted) == reference_redact(text)
    assert "".join(emitted).count("[REDACTED]") == len(parts)
