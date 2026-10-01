"""Scoped capability tokens (invariant I2).

The supervisor signs a token per agent per generation listing the tools it
may invoke. Tokens never enter agent context: the runtime holds them on the
agent's behalf. An emergency stop bumps the epoch, invalidating every token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


class InvalidToken(Exception):
    pass


@dataclass(frozen=True)
class Claims:
    token_id: str
    agent_id: str
    generation_id: int
    capabilities: frozenset[str]
    epoch: int


def load_or_create_secret(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes()
    secret = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(secret)
    return secret


class TokenAuthority:
    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def issue(self, *, token_id: str, agent_id: str, generation_id: int, capabilities: list[str], epoch: int) -> str:
        claims = {"tid": token_id, "aid": agent_id, "gen": generation_id, "caps": sorted(capabilities), "ep": epoch}
        body = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True).encode()).decode()
        return f"{body}.{self._sign(body.encode())}"

    def verify(self, token: str, *, current_epoch: int, generation_id: int) -> Claims:
        try:
            body, sig = token.rsplit(".", 1)
        except (ValueError, AttributeError) as exc:
            raise InvalidToken("malformed token") from exc
        if not hmac.compare_digest(sig, self._sign(body.encode())):
            raise InvalidToken("bad signature")
        claims = json.loads(base64.urlsafe_b64decode(body.encode()))
        if claims["ep"] < current_epoch:
            raise InvalidToken("revoked")
        if claims["gen"] != generation_id:
            raise InvalidToken("expired (generation)")
        return Claims(
            token_id=claims["tid"], agent_id=claims["aid"], generation_id=claims["gen"],
            capabilities=frozenset(claims["caps"]), epoch=claims["ep"],
        )
