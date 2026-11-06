"""Every two-chunk split of text containing each PII type, at the redactor level.

The end-to-end version, through the proxy and the adversarial mock provider, is in test_proxy.py.
"""

from __future__ import annotations

import pytest

from llm_guardrail.redactor import StreamRedactor, redact

SAMPLES = {
    "ssn": ("é SSN 123-45-6789 😀", "é SSN [REDACTED] 😀"),
    "card": ("€ card 4111 1111 1111 1111 中", "€ card [REDACTED] 中"),
    "email": ("ü mail john.doe@example.com. ok", "ü mail [REDACTED]. ok"),
    "all": (
        "Hi é, SSN 123-45-6789, card 4111-1111-1111-1111, mail first.last+x@mail.example.co.uk 😀",
        "Hi é, SSN [REDACTED], card [REDACTED], mail [REDACTED] 😀",
    ),
}


@pytest.mark.parametrize("name", list(SAMPLES))
def test_every_two_chunk_split(name: str) -> None:
    text, expected = SAMPLES[name]
    raw = text.encode("utf-8")
    assert redact(raw) == expected.encode("utf-8")
    for i in range(len(raw) + 1):  # every byte offset, including inside multi-byte characters
        r = StreamRedactor()
        out = r.feed(raw[:i]) + r.feed(raw[i:]) + r.finish()
        assert out == expected.encode("utf-8"), f"split at byte {i}"
        assert "�" not in out.decode("utf-8")


@pytest.mark.parametrize("name", ["ssn", "card", "email"])
def test_every_three_chunk_split_across_the_pii(name: str) -> None:
    text, expected = SAMPLES[name]
    raw = text.encode("utf-8")
    for i in range(len(raw) + 1):
        for j in range(i, len(raw) + 1):
            r = StreamRedactor()
            out = r.feed(raw[:i]) + r.feed(raw[i:j]) + r.feed(raw[j:]) + r.finish()
            assert out == expected.encode("utf-8"), f"splits at {i}, {j}"
