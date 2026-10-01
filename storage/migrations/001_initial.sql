-- Qwenomatic event ledger (DESIGN §13).
--
-- `events` is append-only and hash-chained: every row commits to the hash of
-- the previous row, and triggers reject UPDATE and DELETE outright.

CREATE TABLE IF NOT EXISTS events (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT    NOT NULL UNIQUE,
    type             TEXT    NOT NULL,
    author           TEXT    NOT NULL,
    agent_id         TEXT,
    lineage_id       TEXT,
    generation_id    INTEGER,
    occurred_at      TEXT    NOT NULL,
    recorded_at      TEXT    NOT NULL,
    idempotency_key  TEXT    UNIQUE,
    payload          TEXT    NOT NULL,
    prev_hash        TEXT    NOT NULL,
    hash             TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS events_type_idx       ON events(type);
CREATE INDEX IF NOT EXISTS events_agent_idx      ON events(agent_id);
CREATE INDEX IF NOT EXISTS events_generation_idx ON events(generation_id);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

-- Supervisor-approved store for per-agent phenotype state (working memory,
-- experiment state). Mutable, but every write is versioned and its digest is
-- recorded in the ledger with the step that produced it.
CREATE TABLE IF NOT EXISTS agent_state (
    agent_id    TEXT    PRIMARY KEY,
    version     INTEGER NOT NULL,
    state       TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
