"""Incremental Server-Sent Events parser (bytes in, events out) and serializer.

Bytes are buffered until a full line is available, and lines are decoded only when complete, so a
UTF-8 character split across network reads is never decoded in halves. Handles LF, CRLF and CR line
endings (a CR at the end of a read waits for a possible LF), multi-line ``data:`` fields, and comments.
An event larger than ``max_event_bytes`` raises SSEError: the stream is failed closed, not truncated.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_EVENT_BYTES = 1024 * 1024


class SSEError(ValueError):
    pass


@dataclass(frozen=True)
class Event:
    data: str | None  # None when the event carried no data field
    event: str | None = None
    id: str | None = None


class SSEParser:
    def __init__(self, max_event_bytes: int = MAX_EVENT_BYTES) -> None:
        self._buf = bytearray()
        self._data: list[str] = []
        self._event: str | None = None
        self._id: str | None = None
        self._event_bytes = 0
        self._max = max_event_bytes

    def feed(self, chunk: bytes) -> list[Event]:
        self._buf += chunk
        events: list[Event] = []
        while True:
            line = self._take_line()
            if line is None:
                break
            event = self._process(line)
            if event is not None:
                events.append(event)
        if len(self._buf) + self._event_bytes > self._max:
            raise SSEError("event exceeds the size limit")
        return events

    def _take_line(self) -> bytes | None:
        buf = self._buf
        lf, cr = buf.find(b"\n"), buf.find(b"\r")
        if lf == -1 and cr == -1:
            return None
        if cr == -1 or (lf != -1 and lf < cr):
            line = bytes(buf[:lf])
            del buf[: lf + 1]
            return line
        if cr + 1 == len(buf):
            return None  # a CR at the end of a read: the LF may arrive in the next one
        line = bytes(buf[:cr])
        del buf[: cr + 2 if buf[cr + 1] == 0x0A else cr + 1]
        return line

    def _process(self, raw_line: bytes) -> Event | None:
        if raw_line == b"":
            return self._dispatch()
        self._event_bytes += len(raw_line) + 1
        if self._event_bytes > self._max:
            raise SSEError("event exceeds the size limit")
        line = raw_line.decode("utf-8", errors="replace")
        if line.startswith(":"):
            return None  # comment
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
        elif name == "event":
            self._event = value
        elif name == "id":
            self._id = value
        return None

    def _dispatch(self) -> Event | None:
        if not self._data and self._event is None and self._id is None:
            self._event_bytes = 0
            return None
        event = Event(data="\n".join(self._data) if self._data else None, event=self._event, id=self._id)
        self._data, self._event, self._id, self._event_bytes = [], None, None, 0
        return event


def encode_data(data: str) -> bytes:
    """Serialize one ``data:`` event. Every line of ``data`` becomes its own data line."""
    return b"".join(b"data: " + line.encode("utf-8") + b"\n" for line in data.split("\n")) + b"\n"
