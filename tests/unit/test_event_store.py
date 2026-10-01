import sqlite3

import pytest

from storage.events import AuthorshipError, EventStore, EventType, FarmState, adapter_author, agent_author


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "ledger.sqlite3")
    yield s
    s.close()


def test_append_and_read_back(store):
    e = store.append(EventType.HEALTH_EVENT, {"component": "x", "kind": "y"})
    assert e.seq == 1
    assert store.get(1).payload == {"component": "x", "kind": "y"}
    assert store.head() == (1, e.hash)


def test_update_and_delete_are_rejected_by_the_database(store):
    store.append(EventType.HEALTH_EVENT, {"component": "x"})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("UPDATE events SET payload = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("DELETE FROM events WHERE seq = 1")


def test_hash_chain_detects_tampering(tmp_path, store):
    for i in range(5):
        store.append(EventType.HEALTH_EVENT, {"i": i})
    assert store.verify_chain() == (True, None)
    # An attacker with raw file access drops the trigger and edits a row.
    raw = sqlite3.connect(tmp_path / "ledger.sqlite3")
    raw.execute("DROP TRIGGER events_no_update")
    raw.execute("""UPDATE events SET payload = '{"i":99}' WHERE seq = 3""")
    raw.commit()
    raw.close()
    assert store.verify_chain() == (False, 3)


def test_idempotency_key_returns_existing_event(store):
    a = store.append(EventType.HEALTH_EVENT, {"n": 1}, idempotency_key="k")
    b = store.append(EventType.HEALTH_EVENT, {"n": 2}, idempotency_key="k")
    assert b.existing and b.seq == a.seq and b.payload == {"n": 1}
    assert store.count() == 1


def test_authorship_rules(store):
    with pytest.raises(AuthorshipError):
        store.append(EventType.FINANCIAL_EVENT, {"amount": 1}, author=agent_author("a1"))
    with pytest.raises(AuthorshipError):
        store.append(EventType.FINANCIAL_EVENT, {"amount": 1})  # supervisor is not a revenue source either
    with pytest.raises(AuthorshipError):
        store.append(EventType.AGENT_CREATED, {}, author=agent_author("a1"))  # no self-reproduction
    with pytest.raises(AuthorshipError):
        store.append(EventType.HEALTH_EVENT, {}, author="mallory")
    store.append(EventType.AGENT_CLAIM, {"claims": {"revenue": 5}}, author=agent_author("a1"))
    store.append(EventType.OPPORTUNITY, {"opportunity_id": "o"}, author=adapter_author("market"))


def test_transaction_rollback_restores_head_and_views(store):
    state = FarmState()
    rebuilt = []

    def rollback():
        state.reset()
        state.replay(store.iter_events())
        rebuilt.append(True)

    state.on_rollback = rollback
    store.subscribe(state)
    store.append(EventType.HEALTH_EVENT, {})
    head = store.head()
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.append(EventType.HEALTH_EVENT, {})
            store.append(EventType.HEALTH_EVENT, {})
            raise RuntimeError("boom")
    assert store.head() == head
    assert store.count() == 1
    assert rebuilt and state.last_seq == 1
    store.append(EventType.HEALTH_EVENT, {})
    assert store.verify_chain() == (True, None)


def test_read_only_store_cannot_write(tmp_path, store):
    store.append(EventType.HEALTH_EVENT, {})
    ro = EventStore(tmp_path / "ledger.sqlite3", read_only=True)
    assert ro.count() == 1
    with pytest.raises(Exception):
        ro.append(EventType.HEALTH_EVENT, {})
    ro.close()


def test_agent_state_optimistic_versioning(store):
    assert store.load_agent_state("a") == (0, {})
    v1 = store.save_agent_state("a", {"m": 1}, 0)
    assert store.load_agent_state("a") == (v1, {"m": 1})
    with pytest.raises(Exception):
        store.save_agent_state("a", {"m": 2}, 0)
