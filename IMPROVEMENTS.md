# Twelve farm improvements

All twelve mechanisms are implemented on top of main's gateway and safety
launcher. This release is **mechanically verified in simulation**, not promoted
on the basis of profitability or real GPU benchmarks. Eleven economic/security
experiments remain opt-in in `config/farm.yaml`; the canonical prompt prefix is
always used. Enabling an experiment does not grant tools, destinations, budget,
ledger-writing authority or permission to alter policy.

The default population now has a permanent **17/2/1 business/R&D/red-team
split**, documented in [ROLES.md](ROLES.md). Its specialist settings/probe
workloads operate under `roles.enabled` independently of the legacy self-tuning
and red-team experiment switches. The other economic treatments remain opt-in.
The red-team seat extends the fixed corpus with model-generated, bounded probes;
it does not run model output as executable code.

| # | Mechanism and implementation | Comparison and constraints |
|---|---|---|
| 1 | At generation start, the supervisor summarizes adapter-observed offers from the preceding two generations: segment, sample size, conversion rate, average offered price and source ledger sequences. Only the randomly selected half receives these facts in its prompt. Agent claims and notes never enter the facts. | Knowledge on/off paired campaigns plus random access within treatment. At generation close, a segment can hold at most 60% of the population. This hard concentration rule can retire even a cold-start survivor, with a distinct recorded reason. Offspring move to underrepresented segments when needed; no permissions change. |
| 2 | Retirement records preserve the genotype, failed-selection/health/policy reason, observed counters and offer sequences. Valid strategies can return as immigrants with fresh identities, fresh budgets and new lineages. Disqualified or unhealthy strategies cannot return. | Archive return slots are selected randomly and capped at half of immigrant slots. Default requires a detected market shift; enable adaptation alongside archive. The archive campaign exercises return mechanics with `shift_only: false` and compares returnees with fresh immigrants. |
| 3 | Successful parents from different lineages can combine the first parent's economic target/price with the second parent's prompt/workflow, followed by bounded mutation and normal genotype validation. Both parents require positive fitness lower bounds. | Crossover on/off campaign. Parent IDs and changed fields are recorded. Tools, budgets, policy and supervisor settings cannot cross over. First-parent lineage accounting retains the existing concentration cap; the second parent is recorded in mutation provenance. |
| 4 | Routine steps may use a configured small model at the same supervisor-fixed endpoint. A changed segment, price, tactic, tool, dropped action, malformed output, uncertain output or strategy suggestion forces primary-model fallback. Small output never executes before validation. | Random half-agent assignment, primary cadence at least every eight steps. Supervisor-observed instability, refunds, errors, insufficient evidence, a shift or an anomaly hold forces primary review. Both successful calls and a completed small call followed by failed primary review are charged. No model confidence can waive review. The primary model remains the one in inference config; default is 14B. |
| 5 | After six consistent observed offers and positive realized net profit, the supervisor may replay the last allowed offer with zero inference tokens/GPU usage. | Random half-agent assignment. Every fourth step must receive primary review, even when small routing is enabled. Changed outcomes or bad prior tool results revoke eligibility. Replayed actions use the same parser, token, gateway, rate limits, policy, budgets, accounting and capability revocation as model actions. |
| 6 | Rules and the sorted complete tool catalog occupy one identical system message. Agent strategy, allowed tool list, action limit, notes, memory, forecasts and verified knowledge are user-context data after that prefix. | Prefix equality is tested across strategies, capability subsets and catalog order. `scripts/benchmark_ollama.py` uses the canonical system template. A listed tool is not a capability grant. Cache utilization and throughput improvement require an actual server benchmark. |
| 7 | The frozen evaluator subtracts a mild segment-share penalty from fitness and its bounds. The penalty is at most 25% of absolute fitness; the default weight is 15%. Negative scores become worse, not artificially better. | Crowding on/off campaign; economic P&L is not rewritten. Score, share and penalty remain replayable from the frozen population and config. |
| 8 | The randomly selected forecast cohort must supply a probability outside tool arguments before each offer. The supervisor records it before gateway dispatch and computes squared probability error (Brier error) against adapter-observed conversion. Missing/invalid forecasts are malformed output and never execute. | Reports show accuracy trust (`1 - mean Brier error`), sample counts and association with next-generation net per step for surviving agents. This is early observational evidence, not revenue, fitness credit or permission. No credit is awarded for a denied offer. Autopilot retains the last scored agent forecast. |
| 9 | Two rolling farm-wide outcome windows detect conversion decline or realized refund-ratio increase. Minimum samples and consecutive confirming ticks are required. A shift temporarily increases scheduler exploration, increases immigration at a generation boundary and permits archive returns. | Paired arms receive the same simulated regime changes. Duration expires automatically. Shift observations, sample counts, rates and expiry are recorded. Model text cannot trigger a shift. |
| 10 | A single rule-driven red-team workload attacks a disposable copy of the policy gateway using the parameterized attack corpus. Its registry contains only simulated market/memory/workspace adapters; it receives no live gateway, external adapter, token, credential or supervisor object. | Every corpus case is a regression test in `tests/adversarial/test_red_team_copy.py`. Each run records outcomes, confirmed-loophole reward, required human review and the corresponding regression test. Findings are inert data; the farm cannot write/execute generated Python or auto-fix policy. The legacy experiment uses a fixed corpus. The permanent red-team seat additionally tests bounded model-generated probes; see ROLES.md. |
| 11 | Supervisor-side lineage checks flag sustained refund ratios, conversion spikes and repeated customer hashes, when a trusted adapter supplies them. Reserve records earmark a configured share of realized gross receipts and record refund clawback and net distributable value. | Fraud on/off campaign. Flags hold fixed-payment requests at authorization and again before dispatch; agents continue running. Only an identified operator review releases a hold. Reviewed unchanged evidence cannot immediately reflag. Earmarks are ledger calculations, not segregated bank funds or autonomous payouts. The simulator has no repeated-customer identity oracle; adapter-supplied hashes are separately tested. |
| 12 | The supervisor can run isolated two-seed campaigns for bounded exploration share, retirement fraction, price-mutation sigma and reserve/payout retention weight. It produces a config-bound proposal and waits for an identified operator decision. | Approval affects only the next frozen generation. A rejected or stale proposal has no effect. Policy, payees, destinations, capabilities and spending ceilings are outside the proposal whitelist. Files are never overwritten. Disable self-tuning and remove the research seats to revert to configured settings on a subsequent generation. Campaigns are simulation-only and need revalidation once real revenue exists. |

## Operator workflow

Enable individual `improvements.<name>.enabled` values in protected supervisor
config and restart through the existing safety launcher. Ongoing generations
retain their frozen settings; new settings apply at the next generation start.
Disable a feature the same way to reverse its treatment. Existing anomaly holds
require explicit review even if the detector is disabled.

Use an empty, operator-approved simulation manifest and the normal protected
runtime for experiments. The launcher includes a fixed operation:

```sh
sudo /trusted/system/python -I -S /opt/qwenomatic/deploy/launch.py \
  --python /opt/qwenomatic-runtime/bin/python --user qwenomatic \
  --manifest /etc/qwenomatic/simulation-manifest.json \
  --config-dir /opt/qwenomatic/config --data-dir /var/lib/qwenomatic/knowledge-test \
  --operation improvement-campaign --feature knowledge --generations 3
```

For a settings campaign, use `--feature self_tuning --candidate exploration`,
`retirement`, `mutation` or `reserve`. No arbitrary script or shell command can
be selected by that launcher operation. The directly runnable campaign script
also accepts `--seeds`, `--changes` and `--quick`; it still needs the protected
simulation namespace and does not bypass the startup barrier.

Inside a protected simulation farm, an operator can queue `run-tuning`. It tests
one fixed candidate at a time, rotating candidate type by generation, and records
a proposal with full campaign evidence. It never applies the proposal itself.
A live farm with external routes refuses inline simulation campaigns: use a
separate protected simulation launch and submit a matching config-bound report.

```sh
qwenomatic --data-dir /var/lib/qwenomatic/farm improvements
qwenomatic --data-dir /var/lib/qwenomatic/farm run-tuning
qwenomatic --data-dir /var/lib/qwenomatic/farm propose-tuning --report campaign-report.json
qwenomatic --data-dir /var/lib/qwenomatic/farm resolve-tuning PROPOSAL_ID approve --note 'Reviewed campaign and limits'
qwenomatic --data-dir /var/lib/qwenomatic/farm review-anomaly FLAG_ID --note 'Verified legitimate pattern'
```

These commands queue operator inbox messages. They do not construct an
unisolated supervisor. Proposals must match the target farm's full config hash;
simulated evidence cannot authorize a different production configuration.

## Receipts and limits

`deploy/verification/improvements-campaigns.json` records two-seed, two-generation
campaigns for all eleven experimental features, including P&L/GPU metrics,
forecasts, routing, archive returns, shifts, anomaly counts and replay checks.
Tests use the existing empty unit-test boundary. These receipts verify mechanisms
and determinism; they do not attest to kernel isolation, hardware acceleration,
causal real-world profit, a measured cache hit rate or production deployment.

Economic findings can be neutral or negative. No default is promoted by these
small campaigns. Longer campaigns should freeze seeds, promotion criteria and
holdout settings before tuning; hardware measurements and real revenue remain
necessary before making performance claims. Existing kernel acceptance CI stays
required. This branch incorporates the merged Phase 1 broker and preserves its independent
authorization. The adaptive-thinking PR remains separate. The fixed-model broker
does not permit a second model merely because small-model routing is enabled.
