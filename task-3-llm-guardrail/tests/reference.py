"""An independent, non-incremental reference redactor written against stdlib ``re`` only.

It shares nothing with llm_guardrail.redactor: no ``regex`` module, no partial matching, no POSIX flag,
no streaming, no card run enumeration. It brute-forces the definition directly:

    scan left to right; at each position i, take the longest j such that text[i:j] fully matches some
    pattern body, the lookbehind and lookahead classes hold against the surrounding text, and (for
    cards) the digits pass Luhn; replace text[i:j] with [REDACTED] and continue at j, else emit text[i].

The pattern sources (body, lookaround classes) and luhn() are shared on purpose: this oracle checks the
decision procedure and the streaming machinery, while test_patterns.py checks the patterns themselves.
"""

from __future__ import annotations

import re

from llm_guardrail.patterns import CARD, PATTERNS, TOKEN, luhn

# Characters each pattern can contain; used only to stop extending a candidate early.
_ALPHABET = {
    "email": re.compile(r"[A-Za-z0-9._%+@-]"),
    "card": re.compile(r"[0-9 -]"),
    "ssn": re.compile(r"[0-9-]"),
}


def _valid(pattern, text: str, i: int, j: int) -> bool:
    if re.fullmatch(pattern.body, text[i:j]) is None:
        return False
    if i > 0 and re.fullmatch(pattern.behind, text[i - 1]):
        return False
    if j < len(text) and re.fullmatch(pattern.ahead, text[j]):
        return False
    if pattern is CARD and not luhn(text[i:j].replace(" ", "").replace("-", "")):
        return False
    return True


def _longest(pattern, text: str, i: int) -> int | None:
    alphabet = _ALPHABET[pattern.name]
    stop = i
    limit = min(len(text), i + pattern.max_match)
    while stop < limit and alphabet.fullmatch(text[stop]):
        stop += 1
    for j in range(stop, i, -1):
        if _valid(pattern, text, i, j):
            return j
    return None


def reference_redact(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        best: int | None = None
        for pattern in PATTERNS:
            j = _longest(pattern, text, i)
            if j is not None and (best is None or j > best):
                best = j
        if best is None:
            out.append(text[i])
            i += 1
        else:
            out.append(TOKEN)
            i = best
    return "".join(out)
