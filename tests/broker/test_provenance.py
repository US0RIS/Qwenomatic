import base64
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from broker.protocol import canonical
from storage.events import EventStore, EventType
from storage.events.store import AuthorshipError


@pytest.mark.parametrize('event_type,role', [(EventType.BROKER_EVENT,'broker'),(EventType.INSPECTOR_EVENT,'inspector'),
                                           (EventType.REVENUE_INGEST_EVENT,'revenue_ingest')])
def test_independent_authors_require_correct_external_signature(tmp_path, monkeypatch, event_type, role):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr('storage.events.store.provenance_keys',lambda:{role:key.public_key().public_bytes_raw().hex()})
    record = dict(author=role,kind='test',payload={'receipt':'trusted'})
    payload = dict(record=record,signature=base64.b64encode(key.sign(canonical(record))).decode())
    store = EventStore(tmp_path/'ledger.sqlite')
    try:
        store.append(event_type,payload,author=role)
        with pytest.raises(AuthorshipError): store.append(event_type,payload,author='supervisor')
        payload['record']['payload']['receipt']='forged'
        with pytest.raises(AuthorshipError): store.append(event_type,payload,author=role)
        with pytest.raises(AuthorshipError): store.append(EventType.ACCESS_APPROVED,{},author=role)
    finally:
        store.close()
