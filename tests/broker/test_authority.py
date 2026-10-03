import copy
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from broker.authority import Authority, wire_request
from broker.protocol import Rejected, digest


def test_explicit_tls_policy_migration_retains_audit_and_usage(tmp_path):
    from scripts.provision_local_qwen import local_policy
    key = Ed25519PrivateKey.generate().private_bytes_raw()
    path = tmp_path / 'broker.sqlite3'
    old, new = local_policy('a' * 64), local_policy('b' * 64)
    prior = Authority(path, old, key)
    prior.db.execute("INSERT INTO usage VALUES('farm:farm',0,'requests',9)")
    prior.db.close()
    assert Authority.migrate_local_tls_policy(path, new, key)
    assert not Authority.migrate_local_tls_policy(path, new, key)
    restarted = Authority(path, new, key)
    restarted.verify_audit()
    assert restarted.db.execute("SELECT value FROM usage WHERE scope='farm:farm'").fetchone() == (9,)
    restarted.db.close()


def test_tls_policy_migration_refuses_unresolved_attempt(tmp_path):
    from scripts.provision_local_qwen import local_policy
    key = Ed25519PrivateKey.generate().private_bytes_raw()
    path = tmp_path / 'broker.sqlite3'
    prior = Authority(path, local_policy('a' * 64), key)
    prior.db.execute("INSERT INTO attempts VALUES('farm','x','h','d','attempted')")
    prior.db.close()
    with pytest.raises(Rejected, match='unresolved attempted'):
        Authority.migrate_local_tls_policy(path, local_policy('b' * 64), key)


def policy():
    bounds = dict(requests=3, messages=3, purchases=3, bytes=10000, spend_cents=100,
                  concurrency=2, period_seconds=3600)
    return dict(version='v1', operator='owner', approval_reference='test-only',
                farms={'farm': {'limits': dict(bounds), 'certificates': ['1' * 64]},
                       'other': {'limits': dict(bounds), 'certificates': ['2' * 64]}},
                services={'text': dict(kind='fixed_json', endpoint='https://example.com/send', account='shared',
                    credential_file=str(Path('/etc/broker/token').resolve()), payee=None, hard_cap_cents=0,
                    limits=dict(bounds), farms=['farm', 'other'])})


@pytest.fixture
def authority(tmp_path):
    now = [100.0]
    value = Authority(tmp_path / 'authority.sqlite', policy(), Ed25519PrivateKey.generate().private_bytes_raw(), clock=lambda: now[0])
    value.now = now
    yield value
    value.db.close()


def request(iid='action', text='hello'):
    return dict(invocation_id=iid, service='text', args={'text': text})


def authorize(a, r, farm='farm'):
    a.grant(farm, r, a.now[0] + 30, 'owner')


def test_without_independent_permission(authority):
    with pytest.raises(Rejected):
        authority.reserve('farm', request())
    assert authority.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


def test_canonical_request_atomic_replay_and_receipt(authority):
    r = request()
    authorize(authority, r)
    mutated = request(text='different')
    with pytest.raises(Rejected):
        authority.reserve('farm', mutated)
    _, body, scopes = authority.reserve('farm', r)
    assert body == {'text': 'hello'}
    with pytest.raises(Rejected):
        authority.reserve('farm', r)
    receipt = authority.finish('farm', r['invocation_id'], scopes, True)
    assert receipt['record']['payload']['request_digest'] == digest(r)
    assert receipt['record']['payload']['status'] == 'accepted'
    authority.verify_audit()


def test_racing_replays_send_once(authority):
    r = request()
    authorize(authority, r)
    def attempt(_):
        try:
            return authority.reserve('farm', r)
        except Rejected:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert sum(r is not None for r in results) == 1
    assert authority.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1


def test_shared_destination_account_not_claimed_agent(authority):
    for n, farm in enumerate(['farm', 'other', 'farm']):
        r = request(str(n))
        authorize(authority, r, farm)
        _, _, scopes = authority.reserve(farm, r)
        authority.finish(farm, str(n), scopes, True)
    r = request('fourth')
    authorize(authority, r, 'other')
    with pytest.raises(Rejected, match='aggregate'):
        authority.reserve('other', r)
    assert authority.db.execute("SELECT consumed FROM grants WHERE iid='fourth'").fetchone()[0] == 0
    with pytest.raises(Rejected):
        authority.grant('farm', {**request('claim'), 'agent_id': 'new'}, 130, 'owner')


def test_concurrency_unknown_still_counts_budget(authority):
    active = []
    for n in range(2):
        r = request(str(n)); authorize(authority, r)
        active.append(authority.reserve('farm', r)[2])
    r = request('third'); authorize(authority, r)
    with pytest.raises(Rejected, match='concurrency'):
        authority.reserve('farm', r)
    authority.finish('farm', '0', active[0], False)
    authority.reserve('farm', r)
    assert authority.db.execute("SELECT value FROM usage WHERE scope='account:shared' AND dimension='requests'").fetchone()[0] == 3


def test_expiry_revoke_and_halt(authority):
    r = request(); authorize(authority, r)
    authority.now[0] = 131
    with pytest.raises(Rejected): authority.reserve('farm', r)
    r = request('new'); authorize(authority, r)
    authority.revoke('farm', 'new', 'owner')
    with pytest.raises(Rejected): authority.reserve('farm', r)
    r = request('halted'); authorize(authority, r)
    authority.halt('owner')
    with pytest.raises(Rejected, match='halted'): authority.reserve('farm', r)


def test_crash_restart_halts_and_policy_change_refuses(authority):
    r = request(); authorize(authority, r); authority.reserve('farm', r)
    path = authority.db.execute('PRAGMA database_list').fetchone()[2]
    key = authority.key.private_bytes_raw()
    restarted = Authority(path, policy(), key, clock=lambda: 100)
    assert restarted.db.execute("SELECT value FROM settings WHERE name='halted'").fetchone()
    with pytest.raises(Rejected): restarted.reserve('farm', r)
    restarted.db.close()
    changed = policy(); changed['version'] = 'v2'
    with pytest.raises(Rejected, match='migration'): Authority(path, changed, key, clock=lambda: 100)


def test_clock_rollback_cannot_reset_expiry_or_period(authority):
    r = request(); authorize(authority, r)
    authority.now[0] = 90
    with pytest.raises(Rejected, match='backwards'): authority.reserve('farm', r)


def test_protected_append_only_audit_and_signature(authority):
    with pytest.raises(sqlite3.IntegrityError): authority.db.execute('DELETE FROM audit')
    with pytest.raises(sqlite3.IntegrityError): authority.db.execute("UPDATE audit SET signature='forged'")
    authority.db.execute('DROP TRIGGER no_audit_update')
    authority.db.execute("UPDATE audit SET record=? WHERE seq=1", (b'{"forged":true}',))
    with pytest.raises((Rejected, KeyError)): authority.verify_audit()


@pytest.mark.parametrize('field,value', [('method','GET'), ('url','https://evil.test'), ('payee','attacker'), ('command','sh')])
def test_unrecognized_arguments(authority, field, value):
    r = request(); r['args'][field] = value
    with pytest.raises(Rejected): authorize(authority, r)


def test_payment_payee_cap_and_integer_schema():
    p = policy(); s = p['services']['text']; s.update(kind='fixed_payment', payee='fixed-owner', hard_cap_cents=50)
    for amount in (True, 0, 51, 1.0):
        with pytest.raises(Rejected): wire_request(p, dict(invocation_id='id', service='text', args={'amount_cents': amount}))
    _, body, cost, _ = wire_request(p, dict(invocation_id='id', service='text', args={'amount_cents': 50}))
    assert body == dict(amount_cents=50, payee='fixed-owner', currency='USD')
    assert cost['spend_cents'] == 50


def test_identity_is_mtls_certificate(authority):
    assert authority.authenticate('1' * 64) == 'farm'
    with pytest.raises(Rejected): authority.authenticate('3' * 64)


def test_reservation_uses_one_period_snapshot(authority):
    authority.now[0] = 3590
    r = request('rollover')
    authorize(authority, r)
    clock = iter(range(3599, 3700))
    authority.clock = lambda: next(clock)
    authority.reserve('farm', r)
    # The wall clock can cross a boundary while auditing. All ceilings checked
    # and charged for this single reservation must use the same snapshot.
    assert authority.db.execute('SELECT DISTINCT period FROM usage').fetchall() == [(0,)]
