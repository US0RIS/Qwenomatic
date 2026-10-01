import pytest

from storage.events import EventStore, EventType
from supervisor.accounting import Ledger, LedgerError, TrustedAdapter, UntrustedSourceError


class Pay(TrustedAdapter):
    name = "payments"


@pytest.fixture
def ledger(tmp_path):
    store = EventStore(tmp_path / "l.sqlite3")
    led = Ledger(store, farm_controlled_accounts=["farm-treasury"])
    adapter = Pay()
    led.register_adapter(adapter)
    yield led, adapter, store
    store.close()


def rec(led, adapter, ref, type="revenue", category="gross_revenue", amount=10.0, **kw):
    return led.record(adapter, agent_id="a", lineage_id="l", generation_id=0, type=type, category=category,
                      amount=amount, external_reference=ref, occurred_at="t", observed_at="t", **kw)


def test_unregistered_adapter_is_rejected(ledger):
    led, _, _ = ledger

    class Imposter(TrustedAdapter):
        name = "payments"  # same name, different object

    with pytest.raises(UntrustedSourceError):
        rec(led, Imposter(), "r1")


def test_validation(ledger):
    led, a, _ = ledger
    with pytest.raises(LedgerError):
        rec(led, a, "r", amount=-1)
    with pytest.raises(LedgerError):
        rec(led, a, "r", amount=float("nan"))
    with pytest.raises(LedgerError):
        rec(led, a, "r", type="expense", category="gross_revenue")


def test_duplicate_delivery_is_idempotent(ledger):
    led, a, store = ledger
    rec(led, a, "sale-1")
    rec(led, a, "sale-1")  # duplicate webhook
    assert Ledger.pnl(store.iter_events())["gross_revenue"] == 10.0


def test_internal_transfer_is_not_revenue(ledger):
    led, a, store = ledger
    assert rec(led, a, "t1", counterparty="farm-treasury") is None
    assert Ledger.pnl(store.iter_events()).get("gross_revenue", 0) == 0
    assert store.iter_events(types=[EventType.FINANCIAL_REJECTED])[0].payload["reason"] == "internal_transfer"


def test_pnl_and_double_entry(ledger):
    led, a, store = ledger
    rec(led, a, "s1", amount=100)
    rec(led, a, "f1", type="fee", category="payment_fee", amount=3)
    rec(led, a, "r1", type="refund", category="refund", amount=10)
    rec(led, a, "ad1", type="expense", category="external_spend", amount=20)
    rec(led, a, "gpu1", type="expense", category="compute_imputed", amount=0.5)
    rec(led, a, "p1", amount=50, status="unrealized")
    pnl = Ledger.pnl(store.iter_events())
    assert pnl["gross_revenue"] == 100
    assert pnl["net_realized_profit"] == pytest.approx(100 - 3 - 10 - 20 - 0.5)
    assert pnl["unrealized"] == 50
    rec(led, a, "p1:settled", amount=50, settles_reference="p1")
    pnl = Ledger.pnl(store.iter_events())
    assert pnl["unrealized"] == 0 and pnl["gross_revenue"] == 150
    assert abs(sum(Ledger.trial_balance(store.iter_events()).values())) < 1e-9
