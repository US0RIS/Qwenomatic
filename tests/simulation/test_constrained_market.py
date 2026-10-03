from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from dashboard.metrics import LedgerView
from helpers import make_supervisor, reopen, run_generations
from runtime.tools.constrained_market import ConstrainedMarket
from runtime.tools.sim_market import build_market, OfferOutcome
from storage.events import EventStore, EventType, digest
from supervisor.accounting import Attribution, Ledger
from supervisor.audit import verify
from supervisor.clock import parse_ts
from supervisor.config import FarmConfig
from supervisor.experiments.market_validity import audit_ok, settle_tail, run_campaign

START = parse_ts('2026-01-01T00:00:00+00:00')
SEGMENT = 'freelancer-templates'


def configuration():
    config = deepcopy(FarmConfig.load().farm['simulation']['market'])
    config['realism'].update(parameter_sigma=0, macro_sigma=0, initial_cash=10000)
    config['segments'][SEGMENT]['realism'].update(prospects_per_day=40, customer_pool=2,
        intent_rate=1, delivery_success=1, labor_minutes=1, support_minutes=0)
    return config


def commit_offer(market, store, number, now, price=.5):
    outcome = market.resolve_offer(f'offer-{number}', SEGMENT, price, now)
    store.append(EventType.OFFER_OBSERVED, {'market_model': market.model,
        'market_details': outcome.details, 'segment': SEGMENT, 'converted': outcome.converted,
        'price': price}, author='adapter:market')
    return outcome


def test_finite_customers_are_shared_and_restart_cannot_replenish_them(tmp_path):
    config = configuration()
    store = EventStore(tmp_path/'market.sqlite3')
    market = ConstrainedMarket(config, 35, START, store)
    now = START+timedelta(hours=23)
    outcomes = [commit_offer(market, store, i, now) for i in range(120)]
    assert 0 < sum(o.converted for o in outcomes) <= 2
    assert sum(o.details['contacted'] for o in outcomes) <= 38
    snapshot = (dict(market.attempts), dict(market.contacts), deepcopy(market.purchases))
    store.close()
    store = EventStore(tmp_path/'market.sqlite3')
    recovered = ConstrainedMarket(config, 35, START, store)
    assert (dict(recovered.attempts), dict(recovered.contacts), recovered.purchases) == snapshot
    assert not commit_offer(recovered, store, 121, now).converted
    store.close()


def test_rollback_restores_demand_and_work(tmp_path):
    store = EventStore(tmp_path/'market.sqlite3')
    market = ConstrainedMarket(configuration(), 35, START, store)
    now = START+timedelta(hours=23)
    with pytest.raises(RuntimeError):
        with store.transaction():
            for i in range(10):
                commit_offer(market, store, i, now)
            raise RuntimeError('interrupted step')
    assert not market.contacts and not market.work and not market.purchases
    assert market.cash == 10000
    assert store.head()[0] == 0
    store.close()


def test_market_draws_ignore_agent_invocation_names_and_surveys_cannot_be_averaged():
    a = ConstrainedMarket(configuration(), 7, START)
    b = ConstrainedMarket(configuration(), 7, START)
    now = START+timedelta(hours=12)
    x = a.resolve_offer('agent-X-renamed', SEGMENT, 15, now)
    y = b.resolve_offer('completely-different-UUID', SEGMENT, 15, now)
    assert x.details == y.details and x.converted == y.converted
    assert a.survey(SEGMENT, now, '1') == a.survey(SEGMENT, now, '2')


def test_capacity_and_working_capital_are_hard_constraints(tmp_path):
    config = configuration()
    config['realism']['capacity_minutes_per_day'] = 0
    market = ConstrainedMarket(config, 1, START)
    assert not market.resolve_offer('one', SEGMENT, .5, START+timedelta(hours=23)).converted
    config['realism']['initial_cash'] = 0
    market = ConstrainedMarket(config, 1, START)
    outcome = market.resolve_offer('one', SEGMENT, .5, START+timedelta(hours=23))
    assert not outcome.converted and not outcome.details['contacted']
    assert outcome.details['acquisition_cost'] == 0
    assert sum(outcome.details['costs'].values()) == 0


def test_competition_and_quality_can_destroy_conversion():
    config = configuration()
    a = ConstrainedMarket(config, 1, START)
    config['realism'].update(competition_multiplier=10, quality_multiplier=.2)
    b = ConstrainedMarket(config, 1, START)
    now = START+timedelta(hours=12)
    assert b.probability(SEGMENT, 20, now) < a.probability(SEGMENT, 20, now)/3


@pytest.mark.parametrize('field,value', [('initial_cash', float('nan')), ('macro_sigma', 9), ('bad_key', 1)])
def test_invalid_assumptions_rejected(field, value):
    config = configuration()
    config['realism'][field] = value
    with pytest.raises(ValueError):
        ConstrainedMarket(config, 1, START)


def test_cannot_rewrite_market_history(tmp_path):
    store = EventStore(tmp_path/'market.sqlite3')
    config = configuration()
    build_market(config, 1, START, store)
    config['realism']['initial_cash'] *= 10
    with pytest.raises(ValueError, match='fresh --data-dir'):
        build_market(config, 1, START, store)
    store.close()


def test_payment_tail_keeps_original_fees_and_charges_late_dispute_and_labor(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'simulation': {'market': {'model': 'constrained_v1'}}}})
    try:
        a = sup.state.active_agents()[0]
        attr = Attribution(a.id, a.lineage_id, 0, 'step', 0, sup.clock.now_dt())
        sup.market_costs.charge(attr, 'order', {'delivery_labor': 8, 'fulfillment': 2})
        outcome = OfferOutcome('disputed', SEGMENT, 100, True, 3.2,
            sup.clock.now_dt()+timedelta(days=30), True, 720, .1,
            {'chargeback': True, 'chargeback_fee': 15})
        sup.payments.on_sale(attr, outcome)
        assert len(sup.state.pending_settlements) == 1
        tail = settle_tail(sup)
        assert tail['pending'] == 0 and not sup.state.pending_settlements
        pnl = Ledger.pnl(sup.store.iter_events())
        assert pnl['gross_revenue'] == 100 and pnl['refunds'] == 100
        assert pnl['net_realized_profit'] < -28.2  # retained fee + labor + inputs + dispute + finance
        balance = Ledger.trial_balance(sup.store.iter_events())
        assert abs(balance.get('asset:receivable_pending', 0)) < 1e-8
        assert abs(balance.get('income:unrealized', 0)) < 1e-8
        before = sup.store.head()
        settle_tail(sup)
        assert sup.store.head() == before
        assert verify(sup)['accounting']['ok']
    finally:
        sup.close()


def test_defaulted_invoice_pays_no_revenue_and_keeps_costs(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'simulation': {'market': {'model': 'constrained_v1',
        'realism': {'annual_capital_rate': 0}}}}})
    try:
        a = sup.state.active_agents()[0]
        attr = Attribution(a.id, a.lineage_id, 0, 'step', 0, sup.clock.now_dt())
        sup.market_costs.charge(attr, 'order', {'delivery_labor': 20})
        sup.payments.on_sale(attr, OfferOutcome('default', SEGMENT, 100, True, 3.2,
            sup.clock.now_dt()+timedelta(days=30), False, 0, .1, {'payment_failed': True}))
        assert len(sup.state.pending_settlements) == 1
        settle_tail(sup)
        assert not sup.state.pending_settlements
        assert Ledger.pnl(sup.store.iter_events())['net_realized_profit'] == -20
        assert abs(Ledger.trial_balance(sup.store.iter_events()).get('asset:receivable_pending', 0)) < 1e-8
    finally:
        sup.close()


def test_realistic_farm_runs_roles_audits_and_persists_evidence(tmp_path):
    cfg = {'farm': {'roles': {'enabled': True, 'every_ticks': 2},
                   'simulation': {'market': {'model': 'constrained_v1'}},
                   'generation': {'duration_hours': 4, 'tick_seconds': 3600},
                   'inference': {'simulated': {'malformed_rate': 0}}}}
    sup = make_supervisor(tmp_path, cfg)
    try:
        run_generations(sup, 2)
        assert audit_ok(verify(sup))
        assert not [e for e in sup.store.iter_events(types=[EventType.HEALTH_EVENT])
                    if e.payload.get('kind') in ('step_exception', 'tool_exception')]
        view = LedgerView(sup.store)
        view.refresh()
        evidence = view.overview()
        assert evidence['role_counts'] == {'business': 17, 'research': 2, 'red_team': 1}
        assert evidence['market']['model'] == 'constrained_v1'
        assert evidence['market']['real_world_validity'] == 'unestablished'
        assert 'Qwen is not running' in evidence['market']['decision_maker']
        initial_cash = sup.market.cash
        initial_customers = deepcopy(sup.market.purchases)
        sup = reopen(sup)
        assert sup.market.cash == initial_cash and sup.market.purchases == initial_customers
    finally:
        sup.close()


def test_paired_campaign_retains_losing_worlds_and_declares_no_validation(tmp_path):
    report = run_campaign(None, tmp_path/'campaign', seeds=(12, 29), generations=2,
        scenarios=('baseline', 'delivery_stress'), progress=lambda *a, **kw: None,
        overrides={'farm': {'roles': {'enabled': False},
            'generation': {'duration_hours': 2, 'tick_seconds': 1800},
            'inference': {'simulated': {'malformed_rate': 0}}}, 'fitness': {'min_exposure_steps': 1}})
    assert len(report['pairs']) == 4
    assert report['real_world_validity'] == 'unestablished'
    assert not report['enough_seeds_for_screen']
    assert report['plan_digest'] == digest(report['plan'])
    assert not (tmp_path/'campaign/incomplete.json').exists()
    assert (tmp_path/'campaign/plan.json').is_file()
    for pair in report['pairs']:
        for name in ('treatment', 'control'):
            assert audit_ok(pair[name]['audit'])
            assert (tmp_path/'campaign'/pair[name]['ledger']).is_file()


def test_imputed_compute_changes_profit_but_not_cash(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'simulation': {'market': {'model': 'constrained_v1'}}}})
    try:
        a = sup.state.active_agents()[0]
        initial = sup.market.cash
        sup.compute.charge(Attribution(a.id, a.lineage_id, 0, 'step', 0, sup.clock.now_dt()), 'gpu', 3600)
        assert sup.market.cash == initial
        assert Ledger.pnl(sup.store.iter_events())['net_realized_profit'] < 0
    finally:
        sup.close()


def test_protected_launcher_forwards_validity_operation():
    pytest.importorskip('fcntl')
    from deploy.launch import operation_command
    args = SimpleNamespace(operation='market-validity', config_dir='/cfg', data_dir='/data', generations=7, pairs=10)
    command = operation_command(args, '/protected')
    assert 'scripts.market_validity' in command and "'--pairs', '10'" in command
