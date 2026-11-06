"""PII patterns, their derived match windows, and the Luhn check.

Each pattern is split into a lookbehind class, a body, and a lookahead class so the read window can be
derived from the regex source instead of being guessed:

    window = max width of body + max width of lookahead

The window is the longest string the matcher must read from a start position to decide that position.
MAX_PATTERN_LENGTH is the largest window, and the redactor's held buffer is bounded by
MAX_PATTERN_LENGTH - 1 (see redactor.py). Widths come from the stdlib regex parser; a pattern with an
unbounded maximum is refused at import.

Why regex alone is not enough for cards: [0-9]{13,19} also matches order numbers, timestamps, phone
numbers and account IDs. Every issued card number passes the Luhn mod-10 check, and Luhn rejects about
9 in 10 random digit strings, so the regex finds candidates and Luhn decides.
"""

from __future__ import annotations

import re._parser as sre_parse  # private, stable since 3.11; see test_patterns.py
from dataclasses import dataclass, field

import regex

TOKEN = "[REDACTED]"


def luhn(digits: str) -> bool:
    """True if ``digits`` (ASCII 0-9 only, non-empty) passes the Luhn mod-10 check."""
    if not digits or not digits.isascii() or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def max_width(source: str) -> int:
    """The longest string ``source`` can match, from the stdlib parser. Unbounded patterns raise."""
    _, hi = sre_parse.parse(source).getwidth()
    if hi >= sre_parse.MAXREPEAT:
        raise ValueError(f"pattern has no finite maximum length: {source!r}")
    return hi


@dataclass(frozen=True)
class Pattern:
    """One PII type. ``behind`` and ``ahead`` are character classes used as negative lookarounds."""

    name: str
    behind: str
    body: str
    ahead: str
    compiled: regex.Pattern = field(init=False, repr=False, compare=False)
    longest: regex.Pattern = field(init=False, repr=False, compare=False)
    max_match: int = field(init=False)
    window: int = field(init=False)

    def __post_init__(self) -> None:
        source = f"(?<!{self.behind}){self.body}(?!{self.ahead})"
        object.__setattr__(self, "compiled", regex.compile(source))
        object.__setattr__(self, "longest", regex.compile(source, regex.POSIX))  # leftmost-longest
        object.__setattr__(self, "max_match", max_width(self.body))
        object.__setattr__(self, "window", self.max_match + max_width(self.ahead))

    def viable(self, text: str, pos: int) -> bool:
        """True if text[pos:] is a prefix of, or equal to, some match: more input could change the outcome.

        ``fullmatch(partial=True)`` is required here. ``match(partial=True)`` returns a shorter complete
        match when one exists (``a@b.com`` inside ``a@b.com.x``) although a longer one could still form.
        """
        return self.compiled.fullmatch(text, pos, partial=True) is not None

    def longest_end(self, text: str, pos: int) -> int | None:
        """End of the longest valid match starting at ``pos``, or None. Only called when not viable."""
        m = self.longest.match(text, pos)
        return m.end() if m else None


@dataclass(frozen=True)
class CardPattern(Pattern):
    """Card numbers: every digit end with 13..19 digits is a candidate; the longest Luhn-valid one wins."""

    min_digits: int = 13
    max_digits: int = 19
    run: regex.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        run = regex.compile(rf"(?<!{self.behind})[0-9](?:[ -]?[0-9]){{0,{self.max_digits - 1}}}")
        object.__setattr__(self, "run", run)

    def longest_end(self, text: str, pos: int) -> int | None:
        m = self.run.match(text, pos)
        if m is None:
            return None
        digits: list[str] = []
        ends: list[int] = []  # ends[k-1] = offset just after the k-th digit
        for i in range(pos, m.end()):
            if text[i].isdigit():
                digits.append(text[i])
                ends.append(i + 1)
        for k in range(len(digits), self.min_digits - 1, -1):
            end = ends[k - 1]
            followed_by_digit = end < len(text) and "0" <= text[end] <= "9"
            if not followed_by_digit and luhn("".join(digits[:k])):
                return end
        return None


EMAIL_LOCAL_MAX = 64  # RFC 5321 4.5.3.1.1
EMAIL_LABEL_MAX = 63  # RFC 1035 2.3.4
EMAIL_MAX_LABELS = 4  # labels before the TLD; caps the domain so the window is finite
EMAIL_TLD_MAX = 63

EMAIL = Pattern(
    name="email",
    behind=r"[A-Za-z0-9._%+-]",
    body=(
        rf"[A-Za-z0-9._%+-]{{1,{EMAIL_LOCAL_MAX}}}@"
        rf"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{{0,{EMAIL_LABEL_MAX - 2}}}[A-Za-z0-9])?\.){{1,{EMAIL_MAX_LABELS}}}"
        rf"[A-Za-z]{{2,{EMAIL_TLD_MAX}}}"
    ),
    # A trailing "." may follow the match, so "mail a@b.com." still matches at a sentence end.
    ahead=r"[A-Za-z0-9-]",
)

CARD_MIN_DIGITS = 13  # ISO/IEC 7812 PAN length range
CARD_MAX_DIGITS = 19

CARD = CardPattern(
    name="card",
    behind=r"[0-9]",
    body=rf"[0-9](?:[ -]?[0-9]){{{CARD_MIN_DIGITS - 1},{CARD_MAX_DIGITS - 1}}}",
    ahead=r"[0-9]",
    min_digits=CARD_MIN_DIGITS,
    max_digits=CARD_MAX_DIGITS,
)

SSN = Pattern(
    name="ssn",
    behind=r"[0-9]",
    # SSA never issues area 000, 666 or 900-999, group 00, or serial 0000.
    body=r"(?!000|666|9[0-9]{2})[0-9]{3}-(?!00)[0-9]{2}-(?!0000)[0-9]{4}",
    ahead=r"[0-9]",
)

# Priority order breaks ties between equally long matches at the same start.
PATTERNS: tuple[Pattern, ...] = (EMAIL, CARD, SSN)

MAX_PATTERN_LENGTH = max(p.window for p in PATTERNS)
HELD_BOUND = MAX_PATTERN_LENGTH - 1
LOOKBEHIND_WIDTH = max(max_width(p.behind) for p in PATTERNS)
