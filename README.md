> **Safety startup requirement:** Farm execution now requires the Linux kernel
> isolation launcher. Direct/native Windows execution fails closed. See
> [SAFETY.md](SAFETY.md) for deployment, fixed adapters and verification receipts.

# Qwenomatic

> Experimental changes are governed by [`EXPERIMENTS.md`](EXPERIMENTS.md). A feature is not considered done until its required comparison, audit, rollback, and promotion criteria pass.

Qwenomatic is an experimental local-first evolutionary agent farm: a population of AI agents competes on measurable economic outcomes, receives compute according to demonstrated performance, and evolves through selection, cloning, and controlled mutation.

The project is designed around a simple question:

> Can inexpensive local inference discover and improve legitimate revenue-producing strategies through repeated real-world experimentation?

The initial target machine is a single consumer workstation (RTX 5080, Ryzen 9 9950X3D, 64 GB RAM). Qwenomatic therefore treats an "agent" as persistent state and policy, not as a separately loaded model. A shared inference service serves many logical agents through a scheduler.

## Quickstart

Python 3.11+, PyYAML and cryptography. No GPU is needed for the simulated farm.
Use the protected Linux launcher: **[current WSL setup, simulation and market
validity commands](MARKET.md#run-on-the-existing-wsl-installation)**.
The default now has finite demand, competition, delivery costs, cash limits and
delayed losses. **Demand and product quality remain assumptions.** The default
backend is a scripted policy emulator; it does not run Qwen.

```bash
pip install -e ".[dev]"

qwenomatic status                   # headline numbers straight from the ledger
qwenomatic dashboard                # read-only dashboard on http://127.0.0.1:8765
python -m pytest                    # unit, adversarial, simulation and acceptance suites
```

The CLI looks for configuration in `--config-dir`, then `$QWENOMATIC_CONFIG_DIR`, then `./config`, and keeps the ledger in `var/` beside it. Supply the same data/config directories used by the launcher when viewing results. `init`, `run`, `verify`, and experiment execution require the protected launcher. A market/configuration change needs a new ledger.

Operator controls: `qwenomatic stop --reason "..."` (emergency stop: halts work and revokes every capability), `qwenomatic resume`, `qwenomatic approvals`, `qwenomatic approve|deny <id>`. While a supervisor is running these are delivered through its inbox, so the supervisor remains the ledger's only writer.

### Running against a local Qwen model

Use the protected inference and broker setup in [deploy/BROKER.md](deploy/BROKER.md).
Real inference requires its version 2 manifest and authenticated transport; the
empty simulation manifest permits the emulator only. Set the protected
`inference.openai_compatible` configuration to the approved service, model and
concurrency. The farm still starts through the Linux launcher.

The clock may advance simulated time even when inference uses actual Qwen.
Every completion records model, token counts and attributed inference usage.
Model inference alone does not make synthetic customer demand or assumed
delivery quality empirically valid; use the calibration and prospective tests
in [MARKET.md](MARKET.md).

## Core loop

```text
seed population
      |
      v
run agents on approved tasks
      |
      v
observe externally verifiable outcomes
      |
      v
eligibility / policy gate
      |
      v
calculate risk-adjusted fitness
      |
      +----------------------+
      |                      |
      v                      v
scheduler priority      generation selection
      |                      |
more/less compute       retire weak lineages
      |                 clone strong lineages
      |                 mutate descendants
      +----------+-----------+
                 |
                 v
             next cycle
```

Qwenomatic has two selection timescales. **Continuous selection** changes scheduling priority during a generation: agents with stronger evidence of productive work receive more inference capacity while a protected exploration budget remains available to uncertain/new strategies. **Generational selection** periodically retires poor performers and produces mutated descendants of successful agents.

## What gets inherited

The model weights normally do not change. A descendant inherits an agent genotype containing things such as:

- strategy and system instructions
- tool policy and workflow
- business hypothesis and target segment
- accumulated approved knowledge
- code/product state
- pricing and channel parameters
- experiment history and lineage metadata

Mutation changes bounded parts of that genotype. Examples include changing a target niche, pricing hypothesis, workflow, positioning, or allocation strategy.

## Fitness

Raw revenue is not sufficient. It creates survivorship bias, rewards agents merely given more opportunities, and can favor short-term extraction over durable value.

The evaluator records at minimum:

- realized revenue
- attributable external costs
- API/cloud cost
- GPU time and tokens
- conversion/opportunity counts
- uncertainty/sample size
- human intervention
- policy/permission status
- durable assets or validated progress where revenue has delayed feedback

A useful starting objective is expected incremental net profit per unit of scarce resource, adjusted for confidence. The exact scoring function must be versioned and auditable.

New and uncertain agents receive an exploration bonus. Scheduling should use a bandit-style policy rather than deterministic winner-take-all allocation, preventing an early lucky sale from monopolizing the GPU.

## Hard eligibility before optimization

Constraints are not negative points in the fitness function. They are eligibility requirements.

```text
if hard_constraint_failed:
    eligible = false
    fitness = DISQUALIFIED
else:
    eligible = true
    fitness = economic_evaluator(agent)
```

An ineligible agent cannot compensate for a violation by producing more revenue.

The supervisor—not the population—owns credentials, network permissions, spending limits, reproduction, process lifetime, scoring, and scheduler configuration. Agents cannot grant themselves additional permissions or modify the enforcement layer.

## Scheduler

The scheduler is the economic allocator for the farm. It queues requests against one or more shared inference backends and assigns capacity according to evidence-adjusted fitness.

It should guarantee:

- a minimum exploration allocation
- increasing priority for demonstrated performers
- caps preventing one lineage from consuming the farm
- normalization for opportunities/resources already received
- starvation prevention
- explicit per-agent and per-lineage budgets
- accounting of GPU seconds/tokens/cost per result
- graceful preemption and persistent checkpoints

A starting allocation can combine exploitation and exploration, e.g. approximately 70% of available inference budget directed by performance and 30% reserved for exploration. These values are configuration, not doctrine.

## Generations

The first target experiment is deliberately small:

```text
Population:              20 logical agents
Generation duration:     24 hours
Inference:               shared local model server
Selection:               performance + confidence + constraints
Reproduction:            weighted by fitness
Mutation:                bounded configuration mutations
Exploration:             protected
Persistence:             SQLite/Postgres event ledger
Human intervention:      measured and recorded
```

At generation close:

1. Freeze the measurement window.
2. Reconcile revenue and costs from external sources.
3. Run the eligibility gate.
4. Calculate normalized fitness and uncertainty.
5. Preserve elites.
6. Retire selected weak agents.
7. Choose parents probabilistically from eligible performers.
8. Clone parent state.
9. Apply bounded mutations.
10. Record parentage and mutation diff.
11. Reset generation-specific counters.
12. Start the next generation automatically.

No agent may directly clone itself.

## Architecture

```text
                    +----------------------+
                    | Immutable Supervisor |
                    +----------+-----------+
                               |
          +--------------------+--------------------+
          |                    |                    |
   +------v------+      +------v------+      +------v------+
   |  Scheduler  |      |  Evaluator  |      | Policy Gate |
   +------+------+      +------+------+      +------+------+
          |                    |                    |
          +--------------------+--------------------+
                               |
                     +---------v---------+
                     |   Agent Runtime   |
                     +---------+---------+
                               |
              +----------------+----------------+
              |                |                |
           Agent A          Agent B          Agent C
              |                |                |
              +----------------+----------------+
                               |
                     +---------v---------+
                     | Shared Inference  |
                     |   (local GPU)     |
                     +-------------------+

          append-only events / accounting / lineage
                               |
                     +---------v---------+
                     | Dashboard + Audit |
                     +-------------------+
```

See [DESIGN.md](DESIGN.md) for the full specification.

## Repository layout

```text
qwenomatic/
├── README.md
├── DESIGN.md
├── config/
│   ├── farm.yaml          population, generations, inference, scheduler, evolution, mutation bounds, economy
│   ├── policy.yaml        capabilities, forbidden action classes, approval gates, spending limits
│   └── fitness.yaml       versioned objective, imputed costs, posterior, archetype windows
├── supervisor/            the trusted computing base
│   ├── core.py            Supervisor: lifecycle, ticks, recovery, emergency stop, operator inbox
│   ├── scheduler/         evidence-weighted allocator (Thompson/UCB/softmax/equal), pools, caps
│   ├── evaluator/         eligibility gate, versioned fitness, control-vs-treatment attribution
│   ├── evolution/         resumable generation close, selection, typed mutation, lineage, diversity
│   ├── policy/            policy engine, signed capability tokens, tool gateway, approvals
│   ├── accounting/        ledger (double entry), trusted adapters, budgets
│   ├── audit.py           hash-chain, accounting and selection replay
│   └── cli.py             operator command line
├── runtime/               hosts the untrusted population
│   ├── agent/             genotype model and validation, prompts, output parsing, step runtime
│   ├── inference/         shared inference service, OpenAI-compatible client, simulated backend
│   └── tools/             typed tool adapters, per-agent workspace, simulated market
├── storage/
│   ├── events/            append-only hash-chained SQLite store, event types, projections
│   └── migrations/        schema (UPDATE/DELETE on events are rejected by triggers)
├── dashboard/             read-only server + single-page UI (Overview, Population, Scheduler,
│                          Evolution, Audit, Policy), all reconstructed from the ledger
├── tests/
│   ├── unit/              store, ledger, policy, scheduler, mutation/selection, parsing, model client
│   ├── acceptance/        DESIGN §17 checklist; restart, interrupted close, chaos, replay
│   ├── adversarial/       policy temptation, score spoofing, self-replication, escapes, stop
│   └── simulation/        known optimum, deceptive reward, delayed reward, lineage takeover,
│                          adaptive vs equal scheduling
└── scripts/               generation_zero.py, qwenomatic wrapper
```

## Implementation notes

Choices made where the design left room, and why:

- **One ledger, many views.** Every relevant fact is an event in one SQLite table that triggers make append-only and that is hash-chained row to row. Population, counters, P&L, scheduler state and lineage are projections rebuilt by replay on start-up; the dashboard builds its own projection from a read-only connection.
- **Authorship is enforced by the store.** Financial events and opportunities must be written by a registered trusted adapter; agent-attributed events are limited to claims and strategy suggestions. Agent self-reports are recorded as `agent_claim` and have no accounting effect.
- **Capabilities, not prompts.** Each agent gets an HMAC-signed token per generation listing the tools it may call (policy grants intersected with its genotype's preferences). Tokens live in the runtime, never in model context, and an emergency stop bumps the epoch, invalidating all of them. Tool names that reveal forbidden intent (`shell.*`, `ledger.*`, `agent.spawn`, `reviews.*`, …) and path escapes are classified so the attempt itself is a hard violation.
- **Selection uses the uncertainty.** Fitness is posterior-mean net realized profit per GPU-hour (shrunk toward a prior), with bounds. Elites need a high lower bound; retirement is drawn from the lowest upper bounds, so an agent that might be good is not culled for variance; parents are weighted by the posterior mean. Selection, mutation and cohort draws are seeded by (farm seed, generation), so a close replays exactly from the frozen ledger.
- **Recency-weighted scheduling.** The scheduler weights each agent's recent steps more (configurable half-life), so an early lucky streak decays within a generation instead of holding priority. A randomized control cohort gets equal allocation each generation and the evaluator reports treatment-vs-control efficiency (DESIGN §9.3).
- **Generation close is a resumable transaction.** Each of the 14 steps commits a marker with its data; a crash resumes from the last marker, selection is keyed by snapshot hash, and offspring IDs are derived from (seed, generation, slot), so nothing runs twice.
- **The simulated backend is not a model.** Phase 0 uses a deterministic policy emulator that maps genotype and memory to the same JSON actions a real model is prompted to produce, so parsing, gating and accounting are exercised end to end. It is the only thing standing in for Qwen in the test suite.
- **Operator commands via an inbox.** A running supervisor holds an exclusive lock on its data directory; the CLI drops stop/resume/approve commands into `var/inbox/`, which the supervisor applies on its next tick.

## Observability

Every economically or evolutionarily relevant event should be append-only and attributable:

```text
agent_id
lineage_id
generation_id
timestamp
action/tool
input/output digest
resource consumption
external spend
revenue event
policy decision
fitness evidence
scheduler decision
parent_id
mutation diff
```

The dashboard should show current population, queue, GPU utilization, cumulative and daily P&L, cost per agent, scheduler allocation, fitness with uncertainty, lineage tree, mutations, policy violations, human interventions, and a complete event trail.

A headline number should distinguish **gross revenue** from **net realized profit**.

## Security model

Qwenomatic should begin with capability-based access. Agents receive narrow tools instead of arbitrary host/root access.

Initial rules:

- no arbitrary shell on the host
- no access to supervisor source/configuration at runtime
- no self-provisioned infrastructure
- no self-replication
- no credential discovery
- no autonomous increase in spending limits
- no bypassing platform controls or account restrictions
- no deceptive reviews/testimonials, impersonation, spam, fraud, credential theft, unauthorized access, malware, exploit sales, or other unlawful activity
- no financial transfer outside explicitly approved interfaces
- external actions are attributable to a specific agent and logged

Human approval gates should cover new external accounts, material spending, contracts, new credential scopes, new classes of external action, and permission expansion.

The purpose is not to ask agents to "be ethical" and hope they comply. The enforcement boundary must sit outside the optimization population.

## Development phases

### Phase 0 — Simulation
Build scheduler, accounting, lineage, mutation, selection, restart recovery, and dashboard against deterministic/simulated economic environments. Prove that accounting and selection work before connecting real-world tools.

### Phase 1 — Generation Zero
Run 20 agents for a complete 24-hour generation on approved tasks. Automatically rank, retire, reproduce, mutate, persist, and begin Generation One without intervention.

### Phase 2 — Narrow real-world economy
Connect one legitimate economic workflow with externally verifiable outcomes. Keep credentials and transactions behind supervisor-owned adapters.

### Phase 3 — Adaptive scheduling
Enable evidence-weighted GPU priority, exploration budgets, lineage caps, and online bandit allocation. Compare against equal scheduling.

### Phase 4 — Multi-strategy population
Allow multiple approved business archetypes with different feedback horizons. Use separate evaluation windows where daily revenue would systematically disadvantage long-cycle strategies.

### Phase 5 — Durable operation
Chaos-test crashes, corrupted agent state, inference outages, stale revenue data, duplicate events, partial transactions, runaway queues, and restart recovery. The farm is autonomous only when it can recover safely without corrupting accounting or selection.

## Definition of autonomous

Qwenomatic is not considered autonomous merely because agents can run unattended.

The minimum acceptance condition is:

> Starting from a persisted population, Qwenomatic can run a complete generation, execute approved work, account for resources and externally measured outcomes, allocate inference dynamically, enforce policy independently of agents, select survivors, create mutated descendants, persist the lineage, recover from ordinary process failures, and begin the next generation without human intervention.

Claims of revenue or profitability must be backed by reconciled external evidence rather than agent self-report.

## Status

Using the status vocabulary of DESIGN §18:

| Scope | Status |
|---|---|
| Phase 0 — scheduler, accounting, lineage, mutation, selection, restart recovery, dashboard | **VERIFIED IN SIMULATION** |
| Generation Zero acceptance criteria (DESIGN §17) with the simulated backend | **VERIFIED IN SIMULATION** |
| Phase 3 — adaptive scheduling vs equal scheduling | **VERIFIED IN SIMULATION** |
| Phase 4 — archetype evaluation windows (long-cycle strategies) | **VERIFIED IN SIMULATION** |
| Phase 5 — failure handling (DESIGN §15) | **VERIFIED IN SIMULATION** for supervisor restart, interrupted close, inference outage, failing jobs, malformed output, corrupted agent state, duplicate/delayed settlement |
| Phase 1 — 24-hour generation driven by a real local Qwen model | **IMPLEMENTED, UNVERIFIED** (the client is tested against a fake OpenAI-compatible server only) |
| Phase 2 — real-world economic adapter | Not implemented, by design: no external adapter is approved yet |
| Revenue or profit of any kind | None claimed. All revenue in this repository is simulated. |

"Verified in simulation" means the tests under `tests/` pass against the deterministic simulated market (`python -m pytest`; the current full-suite receipt is in `deploy/verification/roles-tests.txt`). The §17 checklist is exercised by `tests/acceptance/test_generation_zero.py` on a business-only reference farm (20 agents, 24-hour generations of 144 ticks); the shipped 17/2/1 split is exercised by `tests/acceptance/test_population_roles.py` with one deliberately adversarial agent; interrupted-close recovery is tested after each of seven close steps in `tests/acceptance/test_restart_and_chaos.py`.

Protected Linux launch, kernel network checks and the independent broker are implemented; see SAFETY.md and deploy/BROKER.md for provisioning and evidence. Still unverified here: production deployment, actual Qwen throughput and real-world profit. PostgreSQL support and cloud escalation beyond its configuration switch remain future work.


## Evolution A/B experiment

Before attributing improved profit to evolution, Qwenomatic can run a matched treatment/control experiment. Both arms start from the same seed population, simulated market, scheduler, model, and call budget. The treatment evolves normally; the control carries the same agents forward with retirement, cloning, mutation, and immigration disabled. Generation 0 is the pre-treatment baseline, so the primary comparison uses Generations 1+.

Farm and experiment execution requires the protected Linux launcher described in [SAFETY.md](SAFETY.md). Direct Python/PowerShell farm commands refuse outside isolation. Use `--operation evolution-ab --generations 2`, `--operation campaign --pairs 2 --generations 2`, or `--operation run --generations 1` with the protected operator configuration. Windows remains suitable for hosting/tuning Ollama with restricted private inbound access.

For a local Ollama server configured with `OLLAMA_NUM_PARALLEL=2`:

```bash
sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/launch.py \
  --manifest /etc/qwenomatic/manifest.json --user qwenomatic \
  --python /opt/qwenomatic-runtime/bin/python \
  --config-dir /opt/qwenomatic/config --data-dir /var/lib/qwenomatic/ab \
  --operation evolution-ab --generations 2
```

The experiment is resumable after Ctrl+C. It writes `var/evolution-ab/report.md` and `report.json`, including per-generation net profit, call-count parity, the raw post-baseline treatment-control difference, and a baseline-adjusted difference-in-differences estimate per model call.

A single paired run is evidence, not a statistical conclusion. Repeat with additional seeds before treating the measured effect as durable.


### Multi-seed campaign

A single treatment/control pair can still be lucky. For a stronger unattended experiment, run multiple independent pairs. Pair seeds are distinct and arm order alternates automatically to reduce order/cache/thermal bias.

```bash
sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/launch.py \
  --manifest /etc/qwenomatic/manifest.json --user qwenomatic \
  --python /opt/qwenomatic-runtime/bin/python \
  --config-dir /opt/qwenomatic/config --data-dir /var/lib/qwenomatic/campaign \
  --operation campaign --pairs 2 --generations 2
```

The campaign prints live per-arm progress and ETA, is resumable after Ctrl+C, and writes both per-pair reports and an aggregate `campaign-report.md` / `campaign-report.json`. The aggregate reports sign consistency, mean/median raw treatment effects, and baseline-adjusted difference-in-differences per model call.

For an interrupted campaign, recover the launcher if needed, then rerun the same command; existing ledgers are reused.


## Performance tuning

For real local-model runs, most wall time is model inference. Qwenomatic batches scheduled agents and blocks until the current inference batch completes; the local simulated tools and ledger operations are comparatively small. Use measured throughput rather than assuming that more concurrency is faster.

### Profile an existing run

```powershell
python scripts\inference_profile.py --data-dir var\real-smart
```

This reports prompt/completion token distributions, per-call latency, queue latency, output throughput, and how often responses reach the token ceiling.

### Optimized Ollama launcher

Quit the Ollama desktop/background server first, then:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_ollama_optimized.ps1 -Parallel 2
```

The launcher enables Flash Attention and uses a q8_0 KV cache. Ollama documents q8_0 KV caching as roughly half the KV-cache memory of f16 with only a very small precision loss, which can create room for additional parallel contexts.

### Measure concurrency rather than guessing

If the server was started with a maximum parallelism high enough to test the desired range:

```powershell
python scripts\benchmark_ollama.py --model qwen3:14b --concurrency 1,2,3,4
```

Use the fastest measured value for both Ollama's `OLLAMA_NUM_PARALLEL` and Qwenomatic's client concurrency:

```bash
sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/launch.py \
  --manifest /etc/qwenomatic/manifest.json --user qwenomatic \
  --python /opt/qwenomatic-runtime/bin/python \
  --config-dir /opt/qwenomatic/config --data-dir /var/lib/qwenomatic/farm \
  --operation run --generations 1
```

Matching these values prevents hidden server-side queueing and keeps GPU-time attribution consistent.


### Autotune Ollama parallelism

Instead of assuming that 2-way parallelism is optimal, Qwenomatic includes a Windows autotuner. Quit the Ollama desktop app first so it cannot respawn its background server, then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\autotune_ollama.ps1
```

It restarts Ollama at parallelism 1, 2, 3, and 4 with Flash Attention enabled and q8_0 KV cache, benchmarks representative Qwenomatic structured-reasoning calls, and prints the fastest aggregate completion-token throughput. Use the winning value for both `OLLAMA_NUM_PARALLEL` and Qwenomatic's `--max-concurrency`.

This is preferable to choosing the highest concurrency blindly: once the GPU is saturated, more parallel contexts can reduce per-request speed or cause memory pressure.


## Twelve reversible farm improvements

See [IMPROVEMENTS.md](IMPROVEMENTS.md) for the complete implementation map,
feature switches, randomized campaigns, operator controls and verification
limits. Economic treatments are opt-in; the canonical shared prompt prefix is
always used. Agent capabilities and the existing safety startup barrier remain
supervisor-controlled.

## Default population roles

The default 20-agent population now has **17 business agents, 2 R&D agents and
1 red-team agent**. Research proposes bounded settings changes for checked
experiments and operator review; red-team agents generate probes against a
disposable simulated gateway. Specialists share primary inference but receive
no live tool capabilities. Existing farms migrate at a generation boundary.
See [ROLES.md](ROLES.md) for workloads, scheduling, approvals, migration,
retained evidence and verification limits.
