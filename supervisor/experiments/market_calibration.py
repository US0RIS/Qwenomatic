"""Offline observation fitting with chronological, customer-disjoint holdout.

Imports evidence as data, never as authority, code or verified real revenue.
No network access and no simulator-generated observations are included by default.
"""
from __future__ import annotations

import csv
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

from runtime.tools.constrained_market import sigmoid


FIELDS = ('opportunity_id', 'customer_id', 'observed_at', 'segment', 'price',
          'acquisition_cost', 'converted', 'outcome_complete', 'collected', 'refunded',
          'chargeback', 'delivery_success', 'fulfillment_cost', 'labor_minutes',
          'settlement_hours', 'refund_delay_hours', 'delivery_hours')
FLAGS = {'converted', 'outcome_complete', 'collected', 'refunded', 'chargeback', 'delivery_success'}
NUMERIC = set(FIELDS) - FLAGS - {'opportunity_id', 'customer_id', 'observed_at', 'segment'}


def timestamp(value):
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('timestamps require an explicit time zone')
    return date.astimezone(timezone.utc)


def read_observations(path, segments):
    raw = Path(path).read_bytes()
    if len(raw) > 32*1024*1024:
        raise ValueError('observation file exceeds 32 MiB')
    reader = csv.DictReader(raw.decode('utf-8-sig').splitlines())
    if not reader.fieldnames or set(reader.fieldnames) != set(FIELDS) or len(reader.fieldnames) != len(FIELDS):
        raise ValueError('CSV must have exactly the documented observation columns')
    rows, seen = [], set()
    for index, row in enumerate(reader, 2):
        try:
            if any(v is None for v in row.values()) or None in row:
                raise ValueError('invalid column count')
            if not row['opportunity_id'] or row['opportunity_id'] in seen:
                raise ValueError('missing or duplicate opportunity_id')
            if not row['customer_id'] or row['segment'] not in segments:
                raise ValueError('missing customer or unknown segment')
            seen.add(row['opportunity_id'])
            row['observed_at'] = timestamp(row['observed_at'])
            for key in FLAGS:
                if row[key] not in ('0', '1'):
                    raise ValueError(f'{key} must be 0 or 1')
                row[key] = bool(int(row[key]))
            for key in NUMERIC:
                row[key] = float(row[key])
                if not math.isfinite(row[key]) or row[key] < 0:
                    raise ValueError(f'{key} must be finite and non-negative')
            if row['price'] <= 0 or row['price'] > 10000:
                raise ValueError('price outside supported range')
            if not row['converted'] and any(row[k] for k in ('collected', 'refunded', 'chargeback', 'delivery_success', 'fulfillment_cost', 'labor_minutes')):
                raise ValueError('a non-order cannot have payment or fulfillment outcomes')
            if (row['refunded'] or row['chargeback']) and not row['collected']:
                raise ValueError('refunds and chargebacks require a collected payment')
            if row['refunded'] and row['chargeback']:
                raise ValueError('refund and chargeback must not double-count the same loss')
        except (ValueError, TypeError) as exc:
            raise ValueError(f'CSV line {index}: {exc}') from exc
        rows.append(row)
    return sorted(rows, key=lambda r: (r['observed_at'], r['opportunity_id'])), hashlib.sha256(raw).hexdigest()


def wilson(successes, n):
    if not n:
        return [0., 1.]
    z = 1.95996398454
    p = successes/n
    center = (p+z*z/(2*n))/(1+z*z/n)
    half = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return [max(0., center-half), min(1., center+half)]


def fit_curve(rows, reference_price):
    """Small bounded likelihood fit. Pricing association is NOT causal elasticity."""
    ys = [int(r['converted']) for r in rows]
    xs = [math.log(r['price']/reference_price) for r in rows]
    target = (sum(ys)+.5)/(len(ys)+1)
    best = None
    for slope in [0., -.25, -.5, -.75, -1., -1.5, -2., -3., -5.]:
        low, high = -30., 30.
        for _ in range(50):
            intercept = (low+high)/2
            if mean(sigmoid(intercept+slope*x) for x in xs) < target:
                low = intercept
            else:
                high = intercept
        intercept = (low+high)/2
        loss = -sum(y*math.log(sigmoid(intercept+slope*x)) + (1-y)*math.log(1-sigmoid(intercept+slope*x))
                    for x, y in zip(xs, ys)) + .05*slope*slope
        candidate = (loss, intercept, slope)
        if best is None or candidate < best:
            best = candidate
    return {'intercept': best[1], 'slope': best[2]}


def scores(rows, curve, reference_price, constant):
    ps = [sigmoid(curve['intercept']+curve['slope']*math.log(r['price']/reference_price)) for r in rows]
    ys = [int(r['converted']) for r in rows]
    return {'n': len(rows), 'orders': sum(ys), 'observed_rate': mean(ys),
            'observed_rate_wilson_95': wilson(sum(ys), len(ys)), 'predicted_rate': mean(ps),
            'brier': mean((p-y)**2 for p, y in zip(ps, ys)),
            'constant_brier': mean((constant-y)**2 for y in ys),
            'log_loss': -mean(y*math.log(p)+(1-y)*math.log(1-p) for p, y in zip(ps, ys))}


def calibrate(path, market_config, holdout_after, source_description):
    if not source_description.strip():
        raise ValueError('an observation source description is required')
    cutoff = timestamp(holdout_after)
    rows, sha = read_observations(path, market_config['segments'])
    # Purge identities seen in ANY earlier segment, not just the same segment.
    prior_customers = {r['customer_id'] for r in rows if r['observed_at'] < cutoff}
    reports, patches = {}, {}
    for segment, cfg in market_config['segments'].items():
        all_rows = [r for r in rows if r['segment'] == segment]
        complete = [r for r in all_rows if r['outcome_complete']]
        train = [r for r in complete if r['observed_at'] < cutoff]
        test = [r for r in complete if r['observed_at'] >= cutoff and r['customer_id'] not in prior_customers]
        info = {'total': len(all_rows), 'censored': len(all_rows)-len(complete),
                'train_n': len(train), 'holdout_n': len(test),
                'purged_holdout_customers': len({r['customer_id'] for r in all_rows
                    if r['observed_at'] >= cutoff and r['customer_id'] in prior_customers})}
        reports[segment] = info
        if len(train) < 30 or len(test) < 20:
            info['status'] = 'insufficient_data; no parameters promoted'
            continue
        curve = fit_curve(train, cfg['reference_price'])
        prior = (sum(r['converted'] for r in train)+.5)/(len(train)+1)
        info['holdout'] = scores(test, curve, cfg['reference_price'], prior)
        info['train'] = scores(train, curve, cfg['reference_price'], prior)
        info['price_range'] = [min(r['price'] for r in train), max(r['price'] for r in train)]
        info['holdout_outside_price_support'] = sum(not info['price_range'][0] <= r['price'] <= info['price_range'][1] for r in test)
        info['status'] = 'observational_fit_unverified_origin'
        info['sufficient_for_predictive_screen'] = (len(test) >= 100 and info['holdout']['orders'] >= 10
            and info['censored'] == 0 and info['holdout_outside_price_support'] == 0)
        empirical = {**curve, 'data_sha256': sha, 'train_n': len(train), 'holdout_n': len(test), 'status': info['status']}
        patch = {'ad_cost': mean(r['acquisition_cost'] for r in train), 'realism': {'empirical_curve': empirical}}
        orders = [r for r in train if r['converted']]
        collected = [r for r in orders if r['collected']]
        if orders:
            patch['realism'].update(unit_cost=mean(r['fulfillment_cost'] for r in orders),
                labor_minutes=mean(r['labor_minutes'] for r in orders), support_minutes=0.,
                delivery_success=(sum(r['delivery_success'] for r in orders)+.5)/(len(orders)+1),
                default_rate=(sum(not r['collected'] for r in orders)+.5)/(len(orders)+1),
                delivery_hours=median(r['delivery_hours'] for r in orders))
        if collected:
            # Refunds due to failed delivery are accounted separately by the model.
            delivered = [r for r in collected if r['delivery_success'] and not r['chargeback']]
            patch['refund_rate'] = (sum(r['refunded'] for r in delivered)+.5)/(len(delivered)+1) if delivered else 0.
            patch['realism']['chargeback_rate'] = (sum(r['chargeback'] for r in collected)+.5)/(len(collected)+1)
            patch['realism']['collection_hours'] = max(0., median(r['settlement_hours'] for r in collected)-patch['realism']['delivery_hours'])
            losses = [r for r in collected if r['refunded'] or r['chargeback']]
            if losses:
                patch['realism']['refund_window_hours'] = median(r['refund_delay_hours'] for r in losses)
        patches[segment] = patch
    return {'schema_version': 1, 'data_sha256': sha, 'source_description': source_description,
            'source_verification': 'operator supplied; origin not independently verified',
            'holdout_after': cutoff.isoformat(), 'split': 'time ordered; new customers only',
            'segments': reports, 'parameter_patch': patches,
            'real_world_validity': 'unestablished',
            'limitations': ['Prices were not randomized: estimated price associations are not causal.',
                'Incomplete outcomes are excluded and counted; missingness may be biased.',
                'These tests validate only the purchase curve on observed prices, not the full dynamic economy.',
                'Demand capacity, retention, competition and quality remain assumptions without separate evidence.']}
