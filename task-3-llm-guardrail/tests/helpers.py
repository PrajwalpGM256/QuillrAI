"""Shared test helpers: Luhn completion, chunking, and Hypothesis strategies for PII-rich text."""

from __future__ import annotations

from hypothesis import strategies as st

from llm_guardrail.patterns import luhn


def luhn_complete(prefix: str) -> str:
    """Append the check digit that makes ``prefix`` Luhn-valid."""
    for d in "0123456789":
        if luhn(prefix + d):
            return prefix + d
    raise AssertionError("unreachable: one check digit always works")


def split_at(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({c for c in cuts if 0 < c < len(data)})
    edges = [0, *points, len(data)]
    return [data[a:b] for a, b in zip(edges, edges[1:], strict=False)]


# --- strategies --------------------------------------------------------------------------------

DIGIT = "0123456789"


@st.composite
def ssns(draw) -> str:
    area = draw(st.integers(1, 899).filter(lambda a: a != 666))
    group = draw(st.integers(1, 99))
    serial = draw(st.integers(1, 9999))
    return f"{area:03d}-{group:02d}-{serial:04d}"


@st.composite
def cards(draw) -> str:
    n = draw(st.integers(13, 19))
    digits = luhn_complete(draw(st.text(alphabet=DIGIT, min_size=n - 1, max_size=n - 1)))
    sep = draw(st.sampled_from(["", " ", "-", "mixed"]))
    if sep == "mixed":
        return "".join(d + draw(st.sampled_from(["", " ", "-"])) for d in digits[:-1]) + digits[-1]
    group = draw(st.sampled_from([4, 3, 5]))
    chunks = [digits[i : i + group] for i in range(0, len(digits), group)]
    return sep.join(chunks)


@st.composite
def emails(draw) -> str:
    local = draw(st.text(alphabet="abcXYZ019._%+-", min_size=1, max_size=12))
    labels = draw(
        st.lists(
            st.from_regex(r"[a-z0-9](?:[a-z0-9-]{0,6}[a-z0-9])?", fullmatch=True), min_size=1, max_size=3
        )
    )
    tld = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=2, max_size=6))
    return f"{local}@{'.'.join(labels)}.{tld}"


NEAR_MISSES = st.sampled_from(
    [
        "000-12-3456",
        "123-45-67890",
        "4111111111111112",
        "4111 1111 1111 1111 5",
        "a@b.c",
        "a@b.com.",
        "@x.com",
        "123-45-",
        "4111 ",
        "a.b@",
        "1234567890123456789012",
    ]
)

FILLER = st.text(
    alphabet=st.sampled_from(list("abc xyz.,;:!?-@0123456789\n\téü€😀中")),
    max_size=12,
)


def pii_text():
    """Text built from PII, near misses, and filler (including multi-byte characters), joined arbitrarily."""
    piece = st.one_of(ssns(), cards(), emails(), NEAR_MISSES, FILLER)
    sep = st.sampled_from(["", " ", "  ", ", ", "\n", "é", "😀", "x", "1", "-", "."])
    return st.lists(st.tuples(piece, sep), max_size=8).map(lambda xs: "".join(p + s for p, s in xs))


def cut_points(max_len: int = 4096):
    return st.lists(st.integers(0, max_len), max_size=40)
