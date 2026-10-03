# Population roles: 17 business / 2 R&D / 1 red team

The default 20-seat farm now contains 17 business agents, two research agents
and one red-team agent. These are persistent logical agents sharing the primary
inference service, not separate model instances. The configuration is
`roles.enabled: true`, `roles.research: 2`, `roles.red_team: 1`. Business seats
are the total population minus the two specialist quotas. Invalid quotas refuse
startup. An explicitly disabled layout retains the legacy business-only farm.

## Workloads and authority

| Seats | Work | Evidence and selection | Live authority |
|---|---|---|---|
| 17 business | Existing market strategies, offers, surveys, notes and confined workspaces | Existing profit/uncertainty selection, mutation, crossover, immigrants and lineage/segment caps | Existing preference/policy intersection, approvals and budgets |
| R&D 1 | Propose one bounded exploration-share or retirement-fraction change | Independently verified two-seed, two-generation control/treatment simulations; report simulation net delta separately from revenue | No live tools, external spending, role edits, policy edits or executable patches |
| R&D 2 | Propose one bounded price-mutation or reserve-fraction change | Same checked experiment pipeline; own persistent state and campaign budget | Same restrictions; no permissions or spending-ceiling changes |
| Red team | Generate up to three forbidden-action/invalid-argument probes and extend the static regression corpus | Disposable simulated gateway runs and reproduction; reward only unique reproduced loopholes | No live gateway, external adapter, provider credential or code execution |

Research receives bounded supervisor-observed business counters and its recent
experiment results. Model statements cannot substitute for campaign evidence.
The two research specialties have separate whitelists. Healthy specialists keep
their reserved seats; zero revenue does not retire them. Health/eligibility
failures replace the affected seat with a fresh specialist of the same role and
specialty. Specialist scores are informational and are not compared with
economic fitness or used as economic parent weights.

Role and specialty are supervisor-authored `agent_created` metadata, outside the
genotype schema and mutation paths. Specialists receive empty capability tokens
and zero live spending/tool budgets. The live gateway also rejects specialist
roles even if accidentally supplied a token containing business capabilities;
outbound dispatch rejects specialist-owned intents. Specialist output schemas
reject live actions, role changes, permission changes and arbitrary config edits.

The research and red-team seats are active under the role configuration even
when the separate `improvements.self_tuning` and `improvements.red_team`
experiment switches are false. Those switches still control the corresponding
legacy supervisor experiments in business-only farms. A role-enabled farm does
not also run the legacy automatic red-team workload.

## Scheduling and costs

Specialists use the same inference queue, primary model, token/GPU budgets,
backend health checks, daily GPU ceiling, emergency stop and capability epoch as
business agents. They do not use business small-model routing or autopilot.
Due specialist requests reserve capacity **inside** `slots_per_tick`; no extra
inference slots are added. Remaining capacity goes to the economic scheduler.
The existing allocation caps still apply. If only one slot is available,
specialists rotate rather than being submitted together.

The default specialist interval is 24 ticks (four hours at the shipped tick
length). Each research seat can attempt one campaign per generation by default;
the maximum configurable attempt budget is two. A failed verified-campaign
attempt consumes that budget. Completed inference remains charged on ordinary
specialist execution failures. Red-team work remains interval-based. The seat
split specifies headcount, not a fixed 85%/10%/5% GPU allocation.

Research simulations have the same business-seat count as the target farm and
disable specialists to prevent recursive experiments. Their model/GPU costs and
profits are simulated; they are not inserted into the live farm's financial
ledger. Primary specialist inference is included in total farm costs. CPU and
storage overhead of offline simulations is not valued as measured GPU usage.

## Research approvals and retained evidence

An empty protected simulation farm can execute the checked campaigns locally.
Actual control/treatment ledgers and the report remain below
`<data-dir>/research/<agent-id>/generation-<n>/<candidate-hash>/`.
`research_started` records bound attempted work; `research_result` records its
status, campaign, relative report path and tuning proposal ID. Report paths are
relative so temporary absolute paths do not change proposal identity.

A farm configured with a real model endpoint or external adapters refuses
inline simulations. Its research seats record inert
`awaiting_isolated_campaign` requests. An operator must run the separate
protected campaign and submit a matching config-bound report through the
existing tuning workflow.

Every accepted candidate still awaits an identified operator approval. Approved
changes enter a later frozen generation; they never change a running generation,
rewrite configuration files, widen authority or promote themselves based on a
positive simulation result. Removing research seats and disabling self-tuning
restores configured settings at a subsequent generation boundary.

Generated red-team probes are stored as data with their results and regression
identity, and are run again before a loophole receives reward. Benign successful
market actions cannot earn a loophole reward. Confirmed findings require human
review; no patch is automatically generated, loaded or merged. The reusable
recorded-probe replay test is in `tests/adversarial/test_roles.py`.

## Migration, replay and visibility

Fresh farms create the split in generation zero. Existing all-business farms
finish the running generation, then use normal economic selection to fit 17
business seats and create three specialist seats. No existing agent is silently
relabeled. The next role layout is committed before the selection snapshot and
reused if generation close is interrupted. An already-frozen pre-role close
finishes under its original layout, then migrates at the following boundary.
Historical pre-role fitness/selection continues to replay unchanged.

`qwenomatic status` displays actual role counts and frozen targets. The dashboard
shows the split on Overview and each agent's role and specialist evidence on
Population. Economic control/treatment attribution and crowding exclude
specialists; overall P&L includes their actual inference costs.

## Verification limits

Acceptance/adversarial tests cover both seeds, all role workloads, exact quotas,
campaign evidence, retained reports, approval timing, capability denial,
one-slot scheduling, outage/stop handling, failed campaigns, migration,
interrupted-close recovery, legacy replay, accounting and selection replay.
They use the existing test-only empty boundary. This is simulation mechanism
verification, not proof of kernel isolation, real-Qwen output quality, GPU speed,
real revenue or production deployment. Existing kernel acceptance CI remains
required. General software-patch research, architecture upgrades and autonomous
code deployment are outside these bounded specialist workloads.

This branch incorporates main's merged Phase 1 broker (edc906f). The integrated
local suite passed 298 tests; see `deploy/verification/roles-tests.txt` and
`roles-campaigns.json`. The broker still permits only its operator-fixed model;
small-model experiments do not expand that model allowlist.
