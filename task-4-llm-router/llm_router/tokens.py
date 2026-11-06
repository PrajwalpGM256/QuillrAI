"""Input-token estimate used for the admission reservation.

estimate = sum over messages of ceil(utf8_bytes(content) / 3) + 4

Deliberately conservative: English averages about 4 characters per token, so bytes / 3 over-estimates
it by roughly a quarter, and CJK (3 UTF-8 bytes per character, about 1 token per character) comes out
about even. Over-estimating reserves a little too much at admission; settlement replaces the
reservation with the provider's actual usage.total_tokens. No tokenizer dependency, deterministic,
offline.
"""

from __future__ import annotations

import json
from typing import Any

BYTES_PER_TOKEN = 3
PER_MESSAGE_OVERHEAD = 4


class InvalidMessages(ValueError):
    pass


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content parts: [{"type": "text", "text": "..."}, ...]
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
        return "".join(parts)
    return json.dumps(content, ensure_ascii=False)


def estimate_tokens(messages: Any) -> int:
    if not isinstance(messages, list) or not messages:
        raise InvalidMessages("messages must be a non-empty list")
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            raise InvalidMessages("each message must be an object")
        size = len(_content_text(message.get("content")).encode("utf-8"))
        total += -(-size // BYTES_PER_TOKEN) + PER_MESSAGE_OVERHEAD
    return total
