# Qwenomatic

Qwenomatic is an experimental local-first evolutionary agent farm: a population of AI agents competes on measurable economic outcomes, receives compute according to demonstrated performance, and evolves through selection, cloning, and controlled mutation.

The project is designed around a simple question:

> Can inexpensive local inference discover and improve legitimate revenue-producing strategies through repeated real-world experimentation?

The initial target machine is a single consumer workstation (RTX 5080, Ryzen 9 9950X3D, 64 GB RAM). Qwenomatic therefore treats an "agent" as persistent state and policy, not as a separately loaded model. A shared inference service serves many logical agents through a scheduler.

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

## Proposed repository layout

```text
qwenomatic/
├── README.md
├── DESIGN.md
├── config/
│   ├── farm.yaml
│   ├── policy.yaml
│   └── fitness.yaml
├── supervisor/
│   ├── scheduler/
│   ├── evaluator/
│   ├── evolution/
│   ├── policy/
│   └── accounting/
├── runtime/
│   ├── agent/
│   ├── inference/
│   └── tools/
├── storage/
│   ├── events/
│   └── migrations/
├── dashboard/
├── tests/
│   ├── acceptance/
│   ├── adversarial/
│   └── simulation/
└── scripts/
```

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

**Design stage.** This README describes the intended system. It does not claim that the farm, scheduler, safety boundary, or autonomous economic loop is currently implemented.
