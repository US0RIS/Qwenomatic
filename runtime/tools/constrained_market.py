"""Finite, competing customer cohorts with auditable resource constraints.

This is a structural model, not an empirically validated economy. Unknown
parameters are explicit assumptions. Randomness uses world/day/contact keys,
never agent, job or invocation IDs. Committed events are the entire state;
restart and rollback cannot replenish customers, cash, or delivery capacity.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import math

from storage.events import EventType
from .sim_market import SimulatedMarket, OfferOutcome


GLOBAL_DEFAULTS = {
    'initial_cash': 2000., 'overhead_per_day': 5., 'labor_hourly': 46.89,
    'capacity_minutes_per_day': 480., 'annual_capital_rate': .12,
    'macro_sigma': .25, 'parameter_sigma': .35, 'survey_lag_hours': 24.,
    'demand_multiplier': 1., 'competition_multiplier': 1.,
    'cost_multiplier': 1., 'quality_multiplier': 1., 'delay_multiplier': 1.,
}
SEGMENT_DEFAULTS = {
    'prospects_per_day': 30., 'customer_pool': 900, 'intent_rate': .15,
    'elasticity': 1.5, 'budget_sigma': .6, 'competitors': 3.,
    'competitor_price_ratio': .95, 'competitor_response': .25,
    'quality': .65, 'delivery_success': .85, 'unit_cost': 1.,
    'labor_minutes': 10., 'support_minutes': 3., 'delivery_hours': 24.,
    'collection_hours': 48., 'refund_window_hours': 336.,
    'repeat_after_days': 30., 'retention': .35, 'default_rate': .02,
    'chargeback_rate': .005, 'chargeback_fee': 15., 'weekend_multiplier': .7,
}


def sigmoid(x):
    return 1 / (1 + math.exp(-max(-35., min(35., x))))


def validate(config):
    raw = config.get('realism', {})
    if not isinstance(raw, dict) or set(raw) - set(GLOBAL_DEFAULTS):
        raise ValueError('unknown market realism setting')
    global_cfg = {**GLOBAL_DEFAULTS, **raw}
    specs = {}
    for name, segment in config['segments'].items():
        values = segment.get('realism', {})
        if not isinstance(values, dict) or set(values) - (set(SEGMENT_DEFAULTS) | {'empirical_curve'}):
            raise ValueError(f'unknown realism parameter in {name}')
        specs[name] = {**SEGMENT_DEFAULTS, **values}
        curve = specs[name].get('empirical_curve')
        if curve is not None:
            if (set(curve) != {'intercept', 'slope', 'data_sha256', 'train_n', 'holdout_n', 'status'}
                    or curve['status'] != 'observational_fit_unverified_origin'
                    or not isinstance(curve['data_sha256'], str) or len(curve['data_sha256']) != 64
                    or any(type(curve[k]) not in (int, float) or not math.isfinite(curve[k])
                           for k in ('intercept', 'slope', 'train_n', 'holdout_n'))
                    or not -5 <= curve['slope'] <= 0 or abs(curve['intercept']) > 35
                    or curve['train_n'] < 1 or curve['holdout_n'] < 1):
                raise ValueError(f'invalid empirical curve in {name}')
    for values in [global_cfg, *specs.values()]:
        for key, value in values.items():
            if key == 'empirical_curve':
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e9:
                raise ValueError(f'{key} must be finite and non-negative')
    rates = ('intent_rate', 'quality', 'delivery_success', 'retention', 'default_rate',
             'chargeback_rate', 'competitor_response')
    for name, spec in specs.items():
        if any(spec[k] > 1 for k in rates):
            raise ValueError(f'probabilities must be <= 1 in {name}')
        if type(spec['customer_pool']) is not int or not 1 <= spec['customer_pool'] <= 10_000_000:
            raise ValueError('customer_pool must be a bounded positive integer')
        if spec['competitor_price_ratio'] <= 0 or spec['budget_sigma'] <= 0:
            raise ValueError('positive price ratio and budget dispersion required')
        base = config['segments'][name]
        for key in ('reference_price', 'ad_cost', 'fee_rate', 'fee_fixed', 'settlement_delay_hours',
                    'refund_rate', 'refund_delay_hours', 'base_conversion'):
            v = base[key]
            if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
                raise ValueError(f'invalid {key} in {name}')
        if base['reference_price'] <= 0 or base['refund_rate'] > 1 or base['fee_rate'] > 1:
            raise ValueError(f'invalid price or rate in {name}')
    if max(global_cfg['macro_sigma'], global_cfg['parameter_sigma']) > 2:
        raise ValueError('uncertainty sigma must be <= 2')
    return global_cfg, specs


class ConstrainedMarket(SimulatedMarket):
    model = 'constrained_v1'

    def __init__(self, config, seed, start, store=None):
        super().__init__(config, seed, start)
        self.settings, self.specs = validate(config)
        self.store = store
        self.reset()
        if store is not None:
            self.on_rollback()
            store.subscribe(self)

    def reset(self):
        self.attempts = defaultdict(int)
        self.contacts = defaultdict(int)
        self.work = defaultdict(float)
        self.purchases = {}
        self.orders = []
        self.prices = defaultdict(list)
        self.cash = self.settings['initial_cash']
        self.minimum_cash = self.cash

    def on_rollback(self):
        self.reset()
        for event in self.store.iter_events(types=[EventType.OFFER_OBSERVED, EventType.FINANCIAL_EVENT]):
            self.apply(event)

    def apply(self, event):
        p = event.payload
        if (event.type is EventType.FINANCIAL_EVENT and p['status'] == 'realized'
                and p.get('category') != 'compute_imputed'):
            self.cash += p['amount'] * (1 if p['type'] == 'revenue' else -1)
            self.minimum_cash = min(self.minimum_cash, self.cash)
        if event.type is EventType.OFFER_OBSERVED and p.get('market_model') == self.model:
            d = p['market_details']
            key = (p['segment'], d['day'])
            self.attempts[key] += 1
            self.contacts[key] += int(d['contacted'])
            if d['contacted']:
                self.prices[key].append(p['price'])
            if p['converted']:
                self.work[d['day']] += d['labor_minutes']
                self.purchases[(p['segment'], d['customer'])] = d
                self.orders.append((p['segment'], d))

    def day(self, now):
        return max(0, int((now - self.start).total_seconds() // 86400))

    def world_factor(self, segment):
        sigma = self.settings['parameter_sigma']
        return max(.1, min(3., self._rng('parameters', segment).lognormvariate(-sigma*sigma/2, sigma)))

    def demand(self, segment, now):
        day = self.day(now)
        sigma = self.settings['macro_sigma']
        # Shared market shocks, serially correlated over a week, plus sector noise.
        shock = .65*self._rng('macro-week', day//7).gauss(0, sigma)
        shock += .35*self._rng('macro-day', day).gauss(0, sigma)
        shock += self._rng('sector-day', segment, day).gauss(0, sigma/2)
        spec = self.specs[segment]
        weekend = spec['weekend_multiplier'] if now.weekday() >= 5 else 1.
        factor = math.exp(shock) * self.world_factor(segment) * weekend
        return max(0, int(spec['prospects_per_day'] * factor * self.settings['demand_multiplier']
                          * self.decay_factor(self.segments[segment], now)))

    def acquisition_cost(self, segment, now):
        spec, base = self.specs[segment], self.segments[segment]
        saturation = self.attempts[(segment, self.day(now))] / max(1, self.demand(segment, now))
        auction = 1 + .5*min(4., saturation) + .1*spec['competitors']*self.settings['competition_multiplier']
        return round(base.ad_cost * self.settings['cost_multiplier'] * auction, 6)

    def max_delivery_cost(self, segment):
        spec, cfg = self.specs[segment], self.settings
        # Upper bound for the truncated work/cost draws used below.
        return 2*cfg['cost_multiplier'] * (spec['unit_cost'] +
               (spec['labor_minutes'] + spec['support_minutes'])*cfg['labor_hourly']/60)

    def competitor_price(self, segment, now):
        spec = self.specs[segment]
        base = self.segments[segment].reference_price * spec['competitor_price_ratio']
        yesterday = self.prices.get((segment, self.day(now)-1), [])
        if yesterday:
            response = min(1.3, max(.7, sum(yesterday)/len(yesterday)/base))
            return base * (1 + spec['competitor_response']*(response-1))
        return base

    def probability(self, segment, price, now):
        s, cfg = self.specs[segment], self.settings
        base = self.segments[segment]
        # Reputation changes only when delivery AND the refund observation window
        # have elapsed. New lineages inherit the farm brand, never reset it.
        matured = [d for seg, d in self.orders if seg == segment and datetime.fromisoformat(d['matures_at']) <= now]
        good = sum(d['successful_delivery'] and not d['lost_payment'] for d in matured)
        reputation = (good + 2)/(len(matured) + 4)
        quality = min(1., s['quality'] * cfg['quality_multiplier'])
        own = math.exp(2*(quality-.65) + .8*(reputation-.5)) * (price/base.reference_price)**(-s['elasticity'])
        rival = s['competitors']*cfg['competition_multiplier'] * (self.competitor_price(segment, now)/base.reference_price)**(-s['elasticity'])
        share = own/(1 + own + rival)  # outside option is an explicit alternative
        curve = s.get('empirical_curve')
        if curve:
            # Observed purchase curves already include willingness to pay and the
            # reference competitive context; apply only relative scenario changes.
            nominal_own = (price/base.reference_price)**(-s['elasticity'])
            nominal_share = nominal_own/(1 + nominal_own + s['competitors']*s['competitor_price_ratio']**(-s['elasticity']))
            p = sigmoid(curve['intercept'] + curve['slope']*math.log(price/base.reference_price)) * share/nominal_share
        else:
            willing = .5 * math.erfc(math.log(price/base.reference_price)/(s['budget_sigma']*math.sqrt(2)))
            p = s['intent_rate'] * willing * share
        p *= self.world_factor(segment) * self.regime_values(segment, now)['conversion_multiplier']
        return min(.99, max(0., p))

    def resolve_offer(self, opportunity_id, segment, price, now, tactic=None):
        spec, base, cfg = self.specs[segment], self.segments[segment], self.settings
        day = self.day(now)
        ordinal = self.attempts[(segment, day)]
        rng = self._rng('contact', segment, day, ordinal)
        elapsed = max(0., (now-self.start).total_seconds()/86400 - day)
        released = int(self.demand(segment, now)*elapsed)
        affordable = self.cash >= self.acquisition_cost(segment, now) + self.max_delivery_cost(segment)
        contacted = affordable and self.contacts[(segment, day)] < released
        customer = rng.randrange(spec['customer_pool'])
        previous = self.purchases.get((segment, customer))
        repeat = previous is not None
        willing_repeat = not previous or (
            now >= datetime.fromisoformat(previous['matures_at']) + timedelta(days=spec['repeat_after_days'])
            and previous['successful_delivery'] and not previous['lost_payment']
            and rng.random() < spec['retention'])
        work = min(2., max(.5, rng.lognormvariate(-.045, .3))) * (spec['labor_minutes']+spec['support_minutes'])
        capacity = self.work[day] + work <= cfg['capacity_minutes_per_day']
        prob = self.probability(segment, price, now) if contacted and willing_repeat and capacity else 0.
        converted = rng.random() < prob
        delivered = rng.random() < min(1., spec['delivery_success']*cfg['quality_multiplier'])
        default = converted and rng.random() < spec['default_rate']
        chargeback = converted and not default and rng.random() < spec['chargeback_rate']
        refund = converted and not default and (not delivered or chargeback or rng.random() < self.regime_values(segment, now)['refund_rate'])
        lag = min(2160., (spec['collection_hours'] + spec['delivery_hours'])*cfg['delay_multiplier']*rng.lognormvariate(0, .4))
        refund_lag = min(2160., spec['refund_window_hours']*cfg['delay_multiplier']*rng.lognormvariate(0, .4))
        costs = {'fulfillment': spec['unit_cost']*cfg['cost_multiplier'] if converted else 0.,
                 'delivery_labor': work/60*cfg['labor_hourly']*cfg['cost_multiplier'] if converted else 0.}
        d = {'day': day, 'customer': customer, 'contacted': contacted, 'repeat_customer': repeat,
             'reason': 'offer' if prob else ('capital' if not affordable else 'demand_or_capacity'),
             'labor_minutes': work if converted else 0., 'successful_delivery': delivered if converted else False,
             'payment_failed': default, 'chargeback': chargeback, 'chargeback_fee': spec['chargeback_fee'] if chargeback else 0.,
             'lost_payment': bool(default or refund),
             'acquisition_cost': self.acquisition_cost(segment, now) if affordable else 0.,
             'costs': {k: round(v, 6) for k, v in costs.items()},
             'matures_at': (now+timedelta(hours=lag+refund_lag)).isoformat()}
        return OfferOutcome(opportunity_id, segment, round(price, 2), converted,
                            round(base.fee_rate*price+base.fee_fixed, 6) if converted else 0.,
                            now+timedelta(hours=lag), refund, refund_lag, prob, d)

    def survey(self, segment, now, query_id):
        # Lagged, imperfect observations. Repeated requests in the same hour
        # see the same noisy sample; unlimited queries cannot reveal true values.
        past = max(self.start, now-timedelta(hours=self.settings['survey_lag_hours']))
        rng = self._rng('survey-cohort', segment, int((now-self.start).total_seconds()//3600))
        noise = lambda: rng.lognormvariate(0, self.survey_noise)
        return {'segment': segment, 'typical_price': round(self.competitor_price(segment, past)*noise(), 2),
                'interest_level': round(min(1., self.specs[segment]['intent_rate']*noise()), 3),
                'acquisition_cost': round(self.acquisition_cost(segment, past)*noise(), 2),
                'payment_delay_hours': self.specs[segment]['collection_hours'],
                'as_of': past.isoformat(), 'evidence': 'noisy synthetic survey'}
