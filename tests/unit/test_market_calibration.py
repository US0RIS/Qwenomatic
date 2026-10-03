import csv
from datetime import datetime, timedelta, timezone

import pytest

from supervisor.config import FarmConfig
from supervisor.experiments.market_calibration import FIELDS, calibrate, read_observations


def observations():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(600):
        converted = i % 4 == 0
        row = {k: 0 for k in FIELDS}
        row.update(opportunity_id=f'op-{i}', customer_id=f'customer-{i}',
            observed_at=(start+timedelta(hours=i)).isoformat(), segment='freelancer-templates',
            price=10 if i % 3 else 30, acquisition_cost=.5, converted=int(converted),
            outcome_complete=1, collected=int(converted), delivery_success=int(converted),
            fulfillment_cost=1 if converted else 0, labor_minutes=2 if converted else 0,
            settlement_hours=48, delivery_hours=2)
        rows.append(row)
    return rows


def write(path, rows):
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_holdout_does_not_change_fit_and_origin_is_never_claimed_verified(tmp_path):
    path = tmp_path/'observations.csv'
    rows = observations()
    cutoff = rows[400]['observed_at']
    cfg = FarmConfig.load().farm['simulation']['market']
    write(path, rows)
    first = calibrate(path, cfg, cutoff, 'synthetic test fixture')
    for row in rows[400:]:
        row.update(converted=0, collected=0, delivery_success=0, fulfillment_cost=0, labor_minutes=0)
    write(path, rows)
    second = calibrate(path, cfg, cutoff, 'synthetic test fixture')
    a = first['parameter_patch']['freelancer-templates']['realism']['empirical_curve']
    b = second['parameter_patch']['freelancer-templates']['realism']['empirical_curve']
    assert (a['intercept'], a['slope']) == (b['intercept'], b['slope'])
    assert first['segments']['freelancer-templates']['holdout']['observed_rate'] > 0
    assert second['segments']['freelancer-templates']['holdout']['observed_rate'] == 0
    assert a['status'] == 'observational_fit_unverified_origin'
    assert first['real_world_validity'] == 'unestablished'


def test_customer_overlap_and_censoring_are_counted_not_silent_zeros(tmp_path):
    rows = observations()
    cutoff = rows[400]['observed_at']
    rows[400]['customer_id'] = rows[0]['customer_id']
    rows[401]['outcome_complete'] = 0
    path = tmp_path/'observations.csv'
    write(path, rows)
    report = calibrate(path, FarmConfig.load().farm['simulation']['market'], cutoff, 'synthetic fixture')
    metrics = report['segments']['freelancer-templates']
    assert metrics['holdout_n'] == 198 and metrics['censored'] == 1
    assert metrics['purged_holdout_customers'] == 1
    assert not metrics['sufficient_for_predictive_screen']


@pytest.mark.parametrize('field,value', [('price', 'nan'), ('converted', 'yes'), ('observed_at', '2026-01-01'),
                                        ('segment', 'invented'), ('acquisition_cost', -1)])
def test_bad_evidence_rejected(tmp_path, field, value):
    rows = observations()[:3]
    rows[0][field] = value
    path = tmp_path/'observations.csv'
    write(path, rows)
    with pytest.raises(ValueError):
        read_observations(path, FarmConfig.load().segments)


def test_duplicate_sales_rejected(tmp_path):
    rows = observations()[:3]
    rows.append(rows[0])
    path = tmp_path/'observations.csv'
    write(path, rows)
    with pytest.raises(ValueError, match='duplicate'):
        read_observations(path, FarmConfig.load().segments)
