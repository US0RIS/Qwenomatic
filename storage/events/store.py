"""SQLite append-only, hash-chained event store (DESIGN §13).

Writes are serialized through one lock and one connection. Readers in other
processes (the dashboard) open the database read-only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

from .canonical import canonical_json, digest
from .types import ADAPTER_ONLY, AGENT_AUTHORABLE, AUTHOR_SUPERVISOR, Event, EventType

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
GENESIS_HASH = "0" * 64


class EventStoreError(Exception):
    pass


class AuthorshipError(EventStoreError):
    """Raised when an author is not permitted to write an event type."""


class Listener(Protocol):
    def apply(self, event: Event) -> None: ...

    def on_rollback(self) -> None: ...


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_hash(fields: dict[str, Any]) -> str:
    return digest(fields)


def check_authorship(event_type: EventType, author: str) -> None:
    if author.startswith("agent:"):
        if event_type not in AGENT_AUTHORABLE:
            raise AuthorshipError(f"agent-authored {event_type.value} events are not permitted")
        return
    if event_type in ADAPTER_ONLY and not author.startswith("adapter:"):
        raise AuthorshipError(f"{event_type.value} must be authored by a trusted adapter, not {author!r}")
    if not (author == AUTHOR_SUPERVISOR or author.startswith("adapter:") or author.startswith("operator:")):
        raise AuthorshipError(f"unknown author {author!r}")


class EventStore:
    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], str] | None = None,
        new_id: Callable[[], str] | None = None,
        read_only: bool = False,
    ) -> None:
        self.path = str(path)
        self._now = now or _utcnow
        self._new_id = new_id or (lambda: str(uuid.uuid4()))
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._depth = 0
        self.read_only = read_only
        if read_only:
            uri = f"file:{Path(self.path).resolve()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._migrate()
        self._conn.row_factory = sqlite3.Row
        self._head_seq, self._head_hash = self._load_head()

    # ------------------------------------------------------------------ setup
    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {r[0] for r in self._conn.execute("SELECT name FROM schema_migrations")}
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if sql_file.name in applied:
                continue
            self._conn.execute("BEGIN")
            try:
                for statement in _split_sql(sql_file.read_text()):
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)", (sql_file.name, _utcnow())
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _load_head(self) -> tuple[int, str]:
        row = self._conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (0, GENESIS_HASH)

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------- transactions
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group appends atomically. Nested calls join the outer transaction."""
        with self._lock:
            if self._depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("ROLLBACK")
                    self._head_seq, self._head_hash = self._load_head()
                    for listener in self._listeners:
                        listener.on_rollback()
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("COMMIT")

    # ---------------------------------------------------------------- writes
    def append(
        self,
        type: EventType,
        payload: dict[str, Any] | None = None,
        *,
        author: str = AUTHOR_SUPERVISOR,
        agent_id: str | None = None,
        lineage_id: str | None = None,
        generation_id: int | None = None,
        occurred_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> Event:
        if self.read_only:
            raise EventStoreError("store opened read-only")
        check_authorship(type, author)
        payload = payload or {}
        # Round-trip through JSON so the in-memory payload equals the stored one.
        payload = json.loads(canonical_json(payload))
        with self._lock:
            if idempotency_key is not None:
                existing = self.get_by_idempotency(idempotency_key)
                if existing is not None:
                    return Event(**{**existing.__dict__, "existing": True})
            recorded_at = self._now()
            fields = {
                "seq": self._head_seq + 1,
                "event_id": self._new_id(),
                "type": type.value,
                "author": author,
                "agent_id": agent_id,
                "lineage_id": lineage_id,
                "generation_id": generation_id,
                "occurred_at": occurred_at or recorded_at,
                "recorded_at": recorded_at,
                "idempotency_key": idempotency_key,
                "payload": payload,
                "prev_hash": self._head_hash,
            }
            h = _event_hash(fields)
            own_txn = self._depth == 0
            if own_txn:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO events(seq, event_id, type, author, agent_id, lineage_id, generation_id,"
                    " occurred_at, recorded_at, idempotency_key, payload, prev_hash, hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        fields["seq"], fields["event_id"], fields["type"], author, agent_id, lineage_id,
                        generation_id, fields["occurred_at"], recorded_at, idempotency_key,
                        canonical_json(payload), fields["prev_hash"], h,
                    ),
                )
                if own_txn:
                    self._conn.execute("COMMIT")
            except BaseException:
                if own_txn:
                    self._conn.execute("ROLLBACK")
                raise
            self._head_seq, self._head_hash = fields["seq"], h
            event = Event(
                seq=fields["seq"], event_id=fields["event_id"], type=type, author=author, agent_id=agent_id,
                lineage_id=lineage_id, generation_id=generation_id, occurred_at=fields["occurred_at"],
                recorded_at=recorded_at, idempotency_key=idempotency_key, payload=payload,
                prev_hash=fields["prev_hash"], hash=h,
            )
            for listener in self._listeners:
                listener.apply(event)
            return event

    # ----------------------------------------------------------------- reads
    def head(self) -> tuple[int, str]:
        with self._lock:
            if self.read_only:
                self._head_seq, self._head_hash = self._load_head()
            return self._head_seq, self._head_hash

    def get_by_idempotency(self, key: str) -> Event | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE idempotency_key = ?", (key,)).fetchone()
        return _row_to_event(row) if row else None

    def get(self, seq: int) -> Event | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        return _row_to_event(row) if row else None

    def iter_events(
        self,
        *,
        after_seq: int = 0,
        upto_seq: int | None = None,
        types: Iterable[EventType] | None = None,
        agent_id: str | None = None,
        generation_ids: Iterable[int] | None = None,
        limit: int | None = None,
        descending: bool = False,
    ) -> list[Event]:
        clauses, params = ["seq > ?"], [after_seq]
        if upto_seq is not None:
            clauses.append("seq <= ?")
            params.append(upto_seq)
        if types is not None:
            types = list(types)
            clauses.append(f"type IN ({','.join('?' * len(types))})")
            params.extend(t.value for t in types)
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if generation_ids is not None:
            gens = list(generation_ids)
            clauses.append(f"generation_id IN ({','.join('?' * len(gens))})")
            params.extend(gens)
        sql = f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY seq {'DESC' if descending else 'ASC'}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def verify_chain(self, upto_seq: int | None = None) -> tuple[bool, int | None]:
        """Recompute every hash. Returns (ok, first_bad_seq)."""
        prev = GENESIS_HASH
        expected_seq = 1
        with self._lock:
            sql = "SELECT * FROM events" + (" WHERE seq <= ?" if upto_seq else "") + " ORDER BY seq"
            cursor = self._conn.execute(sql, (upto_seq,) if upto_seq else ())
            for row in cursor:
                fields = {
                    "seq": row["seq"], "event_id": row["event_id"], "type": row["type"], "author": row["author"],
                    "agent_id": row["agent_id"], "lineage_id": row["lineage_id"],
                    "generation_id": row["generation_id"], "occurred_at": row["occurred_at"],
                    "recorded_at": row["recorded_at"], "idempotency_key": row["idempotency_key"],
                    "payload": json.loads(row["payload"]), "prev_hash": row["prev_hash"],
                }
                if row["seq"] != expected_seq or row["prev_hash"] != prev or _event_hash(fields) != row["hash"]:
                    return False, row["seq"]
                prev = row["hash"]
                expected_seq += 1
        return True, None

    # ----------------------------------------------------- agent state store
    def load_agent_state(self, agent_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT version, state FROM agent_state WHERE agent_id = ?", (agent_id,)).fetchone()
        if not row:
            return 0, {}
        try:
            state = json.loads(row[1])
        except json.JSONDecodeError:
            return row[0], {"_corrupted": True}
        return row[0], state if isinstance(state, dict) else {"_corrupted": True}

    def save_agent_state(self, agent_id: str, state: dict[str, Any], expected_version: int) -> int:
        """Optimistic write; returns the new version."""
        new_version = expected_version + 1
        with self._lock:
            own_txn = self._depth == 0
            if own_txn:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE agent_state SET version = ?, state = ?, updated_at = ? WHERE agent_id = ? AND version = ?",
                    (new_version, canonical_json(state), self._now(), agent_id, expected_version),
                )
                if cur.rowcount == 0:
                    if expected_version != 0:
                        raise EventStoreError(f"agent_state version conflict for {agent_id}")
                    self._conn.execute(
                        "INSERT INTO agent_state(agent_id, version, state, updated_at) VALUES (?,?,?,?)",
                        (agent_id, new_version, canonical_json(state), self._now()),
                    )
                if own_txn:
                    self._conn.execute("COMMIT")
            except BaseException:
                if own_txn:
                    self._conn.execute("ROLLBACK")
                raise
        return new_version


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        seq=row["seq"], event_id=row["event_id"], type=EventType(row["type"]), author=row["author"],
        agent_id=row["agent_id"], lineage_id=row["lineage_id"], generation_id=row["generation_id"],
        occurred_at=row["occurred_at"], recorded_at=row["recorded_at"], idempotency_key=row["idempotency_key"],
        payload=json.loads(row["payload"]), prev_hash=row["prev_hash"], hash=row["hash"],
    )


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies intact."""
    statements, buf, in_trigger = [], [], False
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper == "END;":
                statements.append("\n".join(buf))
                buf, in_trigger = [], False
        elif stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements
