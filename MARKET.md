# Market model and validity of the experiment

The default market is `constrained_v1`. It is a reproducible structural
simulation with explicitly uncertain assumptions. **Real-world validity is
unestablished.** More mechanisms do not make assumed demand into measured demand.
The previous `toy_v1` model remains available for regression tests.

The default decision backend is a **scripted policy emulator**, not Qwen.
Its GPU seconds and inference costs are emulated. A profitable simulation
cannot demonstrate Qwen's ability to produce valuable work, attract customers,
or operate a business.

## What now affects results

| Mechanism | Implementation and constraint |
|---|---|
| Finite demand | Each segment has a shared daily prospect limit. Prospects arrive through the day. Twenty agents cannot each claim the whole market. |
| Customer differences | Finite customer identities, a dispersed willingness-to-pay distribution and a probabilistic purchase decision. |
| Competition | Explicit alternatives, including buying nothing; competitor prices respond to the farm's previous-day prices. |
| Saturation | Repeated acquisition attempts raise the cost per attempt and eventually exhaust reachable prospects. Failed advertising still costs money. |
| Demand changes | Weekend effects, correlated weekly/daily market shocks, sector shocks, and the existing trend decay and configurable regimes. |
| Parameter uncertainty | Each world draws a demand/conversion multiplier before any decisions. Independent seeds explore different assumptions and customer outcomes. |
| Fulfillment | Purchased inputs, delivery and support labor, and a farm-wide daily limit on work minutes. Costs apply even if the customer never pays. |
| Quality | Configurable delivery failure probability. Failure can cause a full refund; assumed quality also affects customer choice. No agent can declare its own quality. |
| Reputation | The farm brand changes only after the delivery/refund observation window. Replacing an agent does not erase reputation. |
| Repeat customers | A customer cannot immediately buy again. Repeat purchases require an elapsed cooldown, satisfactory prior delivery, and retention. |
| Cash | A finite starting budget and a conservative acquisition/delivery reserve before accepting an order. Unpaid receivables cannot fund new work. Imputed compute affects profit but does not withdraw cash. |
| Costs | Acquisition, fulfillment inputs, labor, payment fees, dispute fees, operating overhead, imputed compute, and financing in the campaign runoff. |
| Collections | Variable collection/delivery lags, defaulted invoices, delayed refunds and chargebacks. Original processing fees survive a refund. |
| Surveys | Lagged noisy observations; repeating a survey within an hour returns the same sample. |
| Restart and crash | Demand, customer histories, work and cash reconstruct from committed ledger events. Rollbacks restore them. |
| Historical integrity | The market configuration, seed and start time are frozen for a ledger. A changed market requires a fresh data directory. |

The economic unit is one attempted acquisition/contact, not an impression,
qualified lead, or guaranteed order. Daily limits and the customer pool are
**addressable to this experiment**, not estimates of total market size. Customer
and delivery models are aggregates; no real product is created or graded here.
Reputation and retained customers belong to the farm, not an individual agent.
Tactics such as "premium" have no magic conversion bonus without changing price
or measured economics.

## Evidence and assumptions

Sources checked October 3, 2026:

| Input | Source | How used | Limits |
|---|---|---|---|
| 2.9% + $0.30 | [Stripe US standard pricing](https://stripe.com/pricing) | Domestic card fee assumption for the configured segments | Specific to eligible domestic card transactions. Other rails, currencies, platforms and contracts differ. |
| Original payment fees retained on refunds | [Stripe pricing, refund FAQ](https://stripe.com/pricing) | Fees remain expenses after a refunded sale | Does not estimate the refund probability. |
| $15 received-dispute fee | [Stripe dispute pricing](https://stripe.com/pricing) | Charged when a simulated chargeback settles | Dispute handling/win rates and additional network or contest fees are not modeled. |
| $46.89 per labor hour | [BLS Employer Costs for Employee Compensation, June 2026](https://www.bls.gov/news.release/ecec.nr0.htm) | Broad US private-industry compensation anchor | Not the price of the user's time or a quote for bookkeeping/audit labor. Override with measured opportunity cost. |
| Landing-page conversion benchmarks | [Unbounce 2024 report](https://unbounce.com/conversion-benchmark-report/) | Context only; deliberately not imported as paid-order probability | Its conversion events include outcomes other than purchases. Selected platform customers are not representative of this business. |

Prospect counts, customer pools, purchase intent, price response, delivery
quality, labor minutes, fulfillment cost, retention, capital, overhead, and
scenario multipliers are **unvalidated assumptions**. None has a claimed
"accuracy percentage". The fees and labor reference alone do not calibrate an
economy. Sensitivity results do not assign real-world probabilities to scenarios.

Remaining omissions include actual product/content quality, organic discovery,
platform ranking and account restrictions, customer negotiation, geography,
taxes, licensing, legal liability and endogenous competitors that can launch
new products. Profit is an incremental pre-tax model quantity. The simulation
cannot decide whether an AI-generated professional service is permissible or
competent. Product quality must be measured on actual delivered work.

## Run on the existing WSL installation

Use the `codex/market-validity` branch, which includes the 17/2/1 implementation.
Update the protected checkout and installed package:

```bash
sudo git -C /opt/qwenomatic fetch origin
sudo git -C /opt/qwenomatic switch --track origin/codex/market-validity
sudo /opt/qwenomatic-runtime/bin/pip install /opt/qwenomatic
sudo chmod -R go-w /opt/qwenomatic /opt/qwenomatic-runtime
```

On a later update, use `sudo git -C /opt/qwenomatic pull --ff-only` while on that
branch, then reinstall the package. Keep the original `sim-17-2-1` ledger for
comparison. The existing simulation-only manifest remains sufficient.

Start with two generations in a **new** directory:

```bash
sudo -u qwenomatic mkdir -p /var/lib/qwenomatic/market-v1
sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/launch.py \
  --python /opt/qwenomatic-runtime/bin/python \
  --user qwenomatic \
  --manifest /etc/qwenomatic/simulation.json \
  --config-dir /opt/qwenomatic/config \
  --data-dir /var/lib/qwenomatic/market-v1 \
  --operation generation-zero --generations 2
```

Long collection/refund windows mean two generations are only a functional
check. It is normal to have unsettled orders. The normal farm run continues
the existing ledger; it does not fast-forward future obligations.

Dashboard (keep this running in WSL, then open `http://localhost:8765` in Windows):

```bash
sudo -u qwenomatic /opt/qwenomatic-runtime/bin/python -I -m supervisor \
  --config-dir /opt/qwenomatic/config \
  --data-dir /var/lib/qwenomatic/market-v1 dashboard
```

## Test whether evolution adds value under the assumptions

This campaign keeps the configured population roles, including specialist
workloads. Both arms start with identical genotypes. Treatment evolves;
control keeps its original population. A changed control invalidates the pair.
Each arm receives independent copies of the same world, with random draws keyed
by market day/segment/contact rather than agent IDs. Once behavior differs, the
sequence of contacts may differ too; this is variance reduction, not an exact
customer-level causal experiment.

Run all five predeclared scenarios (baseline, demand slump, more competition,
acquisition shock, delivery stress):

```bash
sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/launch.py \
  --python /opt/qwenomatic-runtime/bin/python \
  --user qwenomatic \
  --manifest /etc/qwenomatic/simulation.json \
  --config-dir /opt/qwenomatic/config \
  --data-dir /var/lib/qwenomatic/market-campaign-01 \
  --operation market-validity --pairs 3 --generations 7
```

The campaign directory must not already exist. Its parent must be writable by
`qwenomatic`. This is much larger than a two-generation run: 30 arms, including
the R&D agents' internal experiments. Progress is printed each generation.
Use a different output directory for each campaign. Ten or more seed pairs
are required for the preliminary simulation screen; three is exploratory.

`plan.json` exists before the first outcome. `report.json`, `report.md`, every
pair result, and every underlying ledger are retained, including losing runs.
Interrupted or invalid campaigns produce `incomplete.json`, not a success
claim. The primary endpoint is the post-baseline difference in net profit.
Total profit is also compared to doing nothing (zero incremental profit).

At the end, new sales stop and the simulator settles **every** outstanding
payment and refund. Delivery/support labor was already expensed at order time.
The active window and runoff include financing costs for outstanding receivables, but no further
operating overhead beyond the active window. The report shows both the pre-runoff
and fully settled results. Completed campaign ledgers cannot resume trading after
seeing future settlements. Inspect these assumptions when estimating cash needs.

Bootstrap intervals use independent seed pairs, not individual agents or
generations. With a few seeds they are unstable. Even a positive screen means
only "promising under tested assumptions". It never becomes a real-world
validation badge. Losses under arbitrary assumptions also do not prove that
the real idea must fail.

## Calibrate with actual observations

Use deidentified data for **every acquisition attempt**, including non-buyers,
failed payments and incomplete outcomes. A buyers-only export cannot estimate
conversion or customer acquisition cost. Keep the source export, sampling rules
and account scope. Never upload customer names, contact information or payment
credentials to this repository.

CSV header (an empty template is at `examples/market_observations.csv`):

```text
opportunity_id,customer_id,observed_at,segment,price,acquisition_cost,converted,outcome_complete,collected,refunded,chargeback,delivery_success,fulfillment_cost,labor_minutes,settlement_hours,refund_delay_hours,delivery_hours
```

IDs must be stable pseudonyms. `observed_at` is the acquisition timestamp with
a timezone. Boolean fields are `0`/`1`. `converted` means accepted order;
`collected` means money was received. Costs are USD; labor is minutes and lags
are hours. Settlement time is from contact until payment; refund delay is from
collection until loss. `labor_minutes` includes support. `delivery_success`
requires an independent quality check, not an agent's self-report.
`outcome_complete=1` requires the observation window to have matured; missing
refunds must not be silently coded as zero. All fields are required; unused
numeric values are zero. Do not combine a refund and chargeback for the same loss.

Choose a chronological cutoff before evaluating the results. Later customers
who appeared in training are removed from holdout, including across segments.
Incomplete outcomes are counted and excluded, which can still create bias.
Price fits are associations, not causal elasticity, unless the experiment
randomized prices independently of customer characteristics.

```bash
/opt/qwenomatic-runtime/bin/python /opt/qwenomatic/scripts/calibrate_market.py \
  --config-dir /opt/qwenomatic/config \
  --observations ./observations.csv \
  --holdout-after '2026-11-01T00:00:00Z' \
  --source-description 'Describe the actual source, sampling and maturity window' \
  --output-config ./calibrated-profile
```

The date above is illustrative: choose a cutoff appropriate to your dataset.
The output directory must be new. The report retains the data SHA-256, sample
sizes, censored records, customer exclusions, price support, prediction error,
and a constant-rate baseline. It fits on training rows only. At least 30 training
and 20 holdout rows per segment are needed to fit; the stronger diagnostic needs
100 holdout rows and 10 orders with no censoring or price extrapolation. These
are screening thresholds, not a guarantee of statistical power.

The profile is labeled `observational_fit_unverified_origin`: importing a CSV
does not prove that the observations are real. It estimates a purchase curve
and selected observed costs/lags; it does not identify demand limits, retention,
or competitive response. Review the report and install the profile as a
root-owned protected configuration before passing it to the launcher. Do not
edit the configuration of an existing simulation ledger.

## What would establish useful real-world evidence

1. Select a single product and channel; measure actual Qwen output against a
   fixed independent quality rubric and record all delivery/support time.
2. Collect a representative sequence of acquisition attempts, including zero
   sales, with observable prices, spend and mature outcomes.
3. Freeze the model and evaluation rule before using later customers as holdout.
   Compare calibration, full costs and profits with a simple fixed workflow.
4. Run a limited prospective pilot with explicit spend limits and independently
   verified purchases and refunds. Real-world traffic still requires the
   protected broker, approved adapters and account-specific authorization.

This change launches no real advertising, sends no customer messages and makes
no external payments. The existing network and broker protections still apply.

## Verification receipts

`tests/simulation/test_constrained_market.py` checks finite demand, cash/capacity,
crash/restart replay, delayed refunds and disputes, defaults, immutable market
history and complete campaign accounting. `tests/unit/test_market_calibration.py`
checks time/customer separation, holdout non-interference, missing outcomes,
and malformed/duplicate evidence.

The `market-evidence` CI job runs the full default 17/2/1 population for two
generations in both evolving and frozen arms. Its downloadable artifact retains
the plan, report and original ledgers for 30 days. This job uses the simulated
test boundary; the separate `kernel-safety` job verifies Linux isolation and
broker enforcement. Neither job tests real customers or actual Qwen work.
