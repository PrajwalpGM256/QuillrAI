"""StreamRedactor: chunk-invariant PII redaction over a byte stream.

Invariant (chunk-invariance): for any input bytes B and any split of B into chunks c1..cn,

    feed(c1) + ... + feed(cn) + finish()  ==  redact(B)

byte for byte. redact() is itself feed(B) + finish() on a fresh instance, so streaming and one-shot
redaction share one code path and one decision procedure (decide_at).

Settlement point (the flush rule):
    The settlement point S is the first position, scanning from the cursor, where decide_at returns
    OPEN: the furthest point before which no currently-open match can still grow. Text before S is
    emitted immediately; text from S on is held. MATCH emits [REDACTED] and jumps past the match; NONE
    emits one character.

Bound: decide_at returns OPEN at p only if text[p:] is a prefix of, or equal to, some match, so
len(text) - p <= max match length = MAX_PATTERN_LENGTH - 1 (the window counts the one lookahead
character that is never part of the match). Every match is ASCII, so the held text is ASCII and its
length in characters equals its length in bytes. An incomplete UTF-8 sequence (at most 3 bytes) waits
in the decoder and is bounded separately.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

from llm_guardrail.patterns import LOOKBEHIND_WIDTH, PATTERNS, TOKEN


class Open:
    """More input could change the decision at this position."""


OPEN = Open()


@dataclass(frozen=True)
class Match:
    end: int


Decision = Open | Match | None


def decide_at(text: str, pos: int, eof: bool) -> Decision:
    """The single pattern decision procedure: OPEN, Match(end), or None (no PII starts at ``pos``).

    ``text`` must include the lookbehind context before ``pos``. Leftmost-longest: among valid matches
    starting at ``pos`` the longest wins, ties going to the earlier pattern in PATTERNS.
    """
    if not eof:
        for pattern in PATTERNS:
            if pattern.viable(text, pos):
                return OPEN
    best: int | None = None
    for pattern in PATTERNS:
        end = pattern.longest_end(text, pos)
        if end is not None and (best is None or end > best):
            best = end
    return Match(best) if best is not None else None


class StreamRedactor:
    """Feed bytes, get redacted bytes back as soon as they are settled. Call finish() at end of stream."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._text = ""  # lookbehind context + held text
        self._cursor = 0  # start of held text within self._text
        self._finished = False

    @property
    def held_bytes(self) -> int:
        """Encoded size of the text held at or after the settlement point."""
        return len(self._text[self._cursor :].encode("utf-8"))

    @property
    def held_text(self) -> str:
        return self._text[self._cursor :]

    @property
    def decoder_pending_bytes(self) -> int:
        """Bytes of an incomplete UTF-8 character waiting for its continuation (0 to 3)."""
        return len(self._decoder.getstate()[0])

    def feed(self, chunk: bytes) -> bytes:
        if self._finished:
            raise RuntimeError("feed() after finish()")
        self._text += self._decoder.decode(chunk)
        return self._drain(eof=False)

    def finish(self) -> bytes:
        if self._finished:
            raise RuntimeError("finish() called twice")
        self._finished = True
        self._text += self._decoder.decode(b"", final=True)
        return self._drain(eof=True)

    def _drain(self, *, eof: bool) -> bytes:
        text, pos = self._text, self._cursor
        out: list[str] = []
        plain_start = pos
        while pos < len(text):
            decision = decide_at(text, pos, eof)
            if decision is OPEN:
                break  # pos is the settlement point
            if isinstance(decision, Match):
                out.append(text[plain_start:pos])
                out.append(TOKEN)
                pos = decision.end
                plain_start = pos
            else:
                pos += 1
        out.append(text[plain_start:pos])
        # Keep only the lookbehind context before the settlement point; it is already emitted.
        keep_from = max(0, pos - LOOKBEHIND_WIDTH)
        self._text = text[keep_from:]
        self._cursor = pos - keep_from
        return "".join(out).encode("utf-8")


def redact(data: bytes) -> bytes:
    """One-shot redaction: the same StreamRedactor fed the whole input at once."""
    r = StreamRedactor()
    return r.feed(data) + r.finish()


def redact_text(text: str) -> str:
    return redact(text.encode("utf-8")).decode("utf-8")
