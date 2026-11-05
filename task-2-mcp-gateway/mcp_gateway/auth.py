"""Authentication: resolve the inbound Bearer token to a role, or fail.

The gateway terminates the client's credential here. The token is used for nothing but this
lookup: it is never forwarded downstream and never logged (only a short SHA-256 fingerprint is).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from mcp_gateway.policy import Role

# Scheme is case-insensitive (RFC 9110 §11.1); exactly one space; token is an RFC 6750 b64token.
_BEARER = re.compile(r"(?i:bearer) ([A-Za-z0-9\-._~+/]+=*)")


@dataclass(frozen=True)
class Principal:
    role: Role
    token_fp: str


@dataclass(frozen=True)
class AuthFailure:
    reason: str  # missing_token | multiple_authorization | malformed_authorization | unknown_token
    token_fp: str | None = None


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def authenticate(authorization_values: Sequence[str], tokens: Mapping[str, Role]) -> Principal | AuthFailure:
    if len(authorization_values) == 0:
        return AuthFailure("missing_token")
    if len(authorization_values) > 1:
        return AuthFailure("multiple_authorization")
    match = _BEARER.fullmatch(authorization_values[0])
    if match is None:
        return AuthFailure("malformed_authorization")
    presented = match.group(1).encode("utf-8")

    # Compare against every configured token so timing does not reveal which (if any) matched.
    resolved: Role | None = None
    for token, role in tokens.items():
        if hmac.compare_digest(presented, token.encode("utf-8")):
            resolved = role
    fp = fingerprint(match.group(1))
    if resolved is None:
        return AuthFailure("unknown_token", fp)
    return Principal(role=resolved, token_fp=fp)
