"""Identifier factory.

With a seed, identifiers are deterministic (uuid5 over a namespace and a
counter), so a simulated farm replays to identical IDs. Without a seed they
are random uuid4s.
"""

from __future__ import annotations

import itertools
import threading
import uuid

_NAMESPACE = uuid.UUID("6f1b0c4e-8a55-4c39-9a57-2b8b1d6c0e11")


def stable_id(*parts: object) -> str:
    return str(uuid.uuid5(_NAMESPACE, ":".join(str(p) for p in parts)))


class IdFactory:
    def __init__(self, seed: int | None = None, session: int = 0) -> None:
        self.seed = seed
        self.session = session
        self._counter = itertools.count()
        self._lock = threading.Lock()

    def new(self, kind: str = "id") -> str:
        if self.seed is None:
            return str(uuid.uuid4())
        with self._lock:
            n = next(self._counter)
        return stable_id(self.seed, self.session, kind, n)

    def start_session(self, session: int) -> None:
        """Each supervisor process start gets its own deterministic ID space."""
        with self._lock:
            self.session = session
            self._counter = itertools.count()
