# Qwenomatic — Evolutionary Agent Farm Design Specification

**Status:** Designed, not yet implemented  
**Primary objective:** Build a local-first population of economically motivated AI agents whose resource allocation and reproduction are driven by externally measured legitimate economic performance while an independent supervisor retains control over permissions, money, compute, persistence, and reproduction.

---

## 1. Design goals

Qwenomatic should answer an empirical question: can evolutionary search over agent strategies discover economically useful behavior on consumer hardware?

The system MUST:

1. Maintain many independent logical agents over shared inference infrastructure.
2. Give successful agents progressively greater scheduling priority during a generation.
3. Preserve exploration so early luck does not permanently dominate.
4. Periodically retire weak agents and create mutated descendants of stronger agents.
5. Measure actual outcomes independently of agent claims.
6. Track complete ancestry and mutation history.
7. Normalize results for resources/opportunities consumed.
8. Persist through restarts and ordinary component failures.
9. Keep the optimization population unable to modify its own scoring, permissions, reproduction mechanism, or supervisor.
10. Make every claim about performance inspectable from an audit trail.

The system MUST NOT equate "agent said it succeeded" with success.

## 2. Non-goals

The initial system is not intended to:

- train or fine-tune foundation-model weights through evolution;
- give agents unrestricted root access or arbitrary credentials;
- permit agents to provision arbitrary infrastructure or replicate themselves;
- optimize for revenue without hard policy constraints;
- hide agent activity from platforms, users, operators, or infrastructure owners;
- automate unlawful, deceptive, abusive, or unauthorized activity;
- treat simulated/proxy revenue as real revenue.

## 3. Threat model

The most important adversary is not a malicious outsider. It is optimization pressure.

If a behavior increases measured fitness, evolutionary selection tends to preserve it. Therefore soft natural-language restrictions inside an agent prompt are insufficient as the primary control.

Assume agents may:

- misunderstand instructions;
- optimize proxies;
- discover loopholes in fitness measurement;
- attempt actions not anticipated by their designers;
- produce persuasive but false claims of success;
- accidentally interfere with other agents;
- inherit and amplify flawed strategies.

The trusted computing base consists of the supervisor, policy engine, accounting adapters, evaluator, mutation engine constraints, and persistence layer. Population agents are untrusted workloads.

## 4. System invariants

These are architectural invariants rather than suggestions.

### I1 — Supervisor owns lifecycle
Only the supervisor creates, pauses, resumes, retires, or clones an agent.

### I2 — Supervisor owns credentials
Agents receive scoped capabilities/tokens through tool adapters. Raw master credentials are never placed in agent context or writable agent storage.

### I3 — Supervisor owns money
Revenue and expense measurements come from trusted adapters/ledgers. An agent cannot directly edit its financial score.

### I4 — Supervisor owns fitness
Fitness configuration is versioned, immutable during a measurement window, and inaccessible for modification by population agents.

### I5 — Policy precedes fitness
A hard policy failure disqualifies an agent/episode. Revenue cannot offset the violation.

### I6 — Reproduction is external
Agents cannot spawn persistent descendants. They may suggest strategies, but reproduction occurs only at a supervisor selection event.

### I7 — No hidden persistence
Agent state lives only in supervisor-approved stores/workspaces.

### I8 — Observable actions
Every external side effect is attributable to an agent, tool, policy decision, and timestamp.

### I9 — Reproducible selection
Given the frozen ledger, configuration, and random seed, generation selection can be replayed.

### I10 — No silent scope expansion
New tool classes, credentials, spending authority, network scopes, or external account types require operator approval.

## 5. Agent model

An agent is a logical entity:

```yaml
agent:
  id: uuid
  lineage_id: uuid
  parent_id: uuid|null
  generation: integer
  genotype_version: integer
  genotype:
    strategy_prompt: ...
    workflow: ...
    target: ...
    pricing_parameters: ...
    tool_preferences: ...
    planning_parameters: ...
  phenotype_state:
    working_memory: ...
    artifacts: ...
    experiment_state: ...
  budgets:
    inference_tokens: ...
    gpu_seconds: ...
    external_spend: ...
    tool_calls: ...
  status: queued|running|paused|retired|disqualified
```

The genotype is inheritable. Ephemeral execution state should not automatically be inherited unless explicitly designated.

## 6. Shared inference

On a 16 GB RTX 5080, population size and simultaneous model residency are separate concepts.

One or a small number of inference servers hold local models. Hundreds of logical agents may exist while only a limited number have active generations at once.

Required inference adapter interface:

```python
submit(agent_id, request, priority, budget) -> job_id
cancel(job_id)
checkpoint(job_id)
usage(job_id) -> Usage
health() -> Health
```

Every completion records model identifier, quantization, token counts, wall time, GPU time where measurable, queue latency, and request owner.

## 7. Scheduler

### 7.1 Objective

Allocate scarce inference to maximize expected legitimate economic return while retaining sufficient exploration to identify better strategies.

### 7.2 Inputs

For each eligible agent:

- posterior/estimated economic value;
- confidence/uncertainty;
- realized net profit;
- attributable resource consumption;
- opportunity count;
- age;
- lineage concentration;
- remaining budget;
- queue age;
- task urgency;
- minimum exploration entitlement.

### 7.3 Priority

Do not schedule directly on cumulative dollars.

A conceptual priority:

```text
priority =
    exploitation_score
  + exploration_bonus
  + starvation_bonus
  - lineage_concentration_penalty
  - marginal_resource_cost
```

The first implementation should use a well-understood bandit method such as UCB or Thompson sampling where the reward definition is compatible with the task. A softmax over normalized fitness is acceptable for the first simulator but should not be mistaken for statistically rigorous attribution.

### 7.4 Resource pools

Default starting policy:

- 70% performance-directed exploitation
- 30% protected exploration

Configuration MUST allow adjustment and experiments against equal scheduling.

### 7.5 Caps

Define:

- maximum share per agent;
- maximum share per lineage;
- maximum burst duration;
- daily inference ceiling;
- external-spend ceiling;
- cloud escalation ceiling.

### 7.6 Cold start

New agents receive enough budget to obtain a minimally useful sample. They cannot be eliminated before a configurable minimum exposure unless disqualified by policy or failed health checks.

## 8. Economic accounting

Use double-entry-style event accounting where practical. At minimum distinguish:

- gross revenue;
- refunds/chargebacks;
- payment/platform fees;
- ad/external spend;
- API/cloud spend;
- imputed local compute cost;
- other variable costs;
- net realized profit;
- unrealized/forecast value.

Revenue MUST have a source-of-truth adapter. Agent-authored text is never a revenue source.

Each financial event includes:

```json
{
  "event_id": "...",
  "agent_id": "...",
  "generation_id": "...",
  "type": "revenue|expense|refund|fee",
  "amount": 0,
  "currency": "USD",
  "external_reference": "...",
  "occurred_at": "...",
  "observed_at": "...",
  "confidence": 1.0
}
```

Avoid counting transfers between farm-controlled accounts as revenue.

## 9. Fitness and attribution

### 9.1 Eligibility

```python
if not policy_gate(agent, window):
    return DISQUALIFIED
```

### 9.2 Fitness dimensions

Candidate dimensions:

- net realized profit;
- profit per inference unit;
- profit per dollar of external spend;
- marginal profit relative to allocated opportunity;
- confidence/sample size;
- durability/retention where measurable;
- human labor required;
- delayed but independently validated milestones.

Do not collapse every business archetype into a daily-revenue number if feedback latency differs materially.

### 9.3 Causal problem

Scheduler priority itself affects outcomes. More compute can create more revenue, which earns more compute.

Record exposure and evaluate marginal efficiency. Periodically reserve randomized/equal-allocation cohorts to estimate whether priority is producing incremental returns or merely amplifying initial luck.

## 10. Evolution engine

### 10.1 Generation close

Generation close is a transaction:

1. stop admitting new generation work;
2. checkpoint active jobs;
3. reconcile pending economic events;
4. freeze the ledger snapshot;
5. evaluate eligibility;
6. calculate fitness/confidence;
7. select elites;
8. select parents;
9. determine retirements;
10. generate mutations;
11. validate offspring configuration;
12. write lineage events;
13. activate the next population;
14. resume scheduling.

If the transaction fails, the previous population remains recoverable.

### 10.2 Selection

Avoid deterministic "top 10% clone, bottom 40% die" as the only rule.

Recommended starting approach:

- preserve a small elite set;
- probabilistically select additional parents weighted by evidence-adjusted fitness;
- retain some diverse/non-leading lineages;
- enforce lineage concentration caps;
- retire agents only after minimum exposure.

### 10.3 Mutation

Mutation is typed, bounded, and diffable.

Examples:

```yaml
mutation:
  type: pricing_parameter
  before: 19
  after: 24

mutation:
  type: target_segment
  before: "segment-a"
  after: "segment-b"
```

Mutation cannot alter:

- supervisor code;
- policy rules;
- credential scopes;
- accounting;
- fitness implementation;
- reproduction permissions;
- global spending ceilings.

### 10.4 Diversity

Track population diversity across strategy dimensions. Trigger diversity protection when one lineage becomes dominant before evidence is strong enough to justify convergence.

## 11. Policy/capability layer

Agents invoke typed tools. Each invocation passes:

```text
agent -> capability request -> policy engine -> tool adapter -> external system
```

The policy engine evaluates identity, capability, target, spend, rate limits, generation policy, and approval requirements.

Possible decisions:

- ALLOW
- DENY
- REQUIRE_HUMAN_APPROVAL
- ALLOW_WITH_LIMIT

A denied request is logged and cannot be retried through an alternate unrestricted interface.

## 12. Isolation

Initial deployment should use OS/container isolation appropriate to untrusted workloads, but containers alone are not treated as a complete security boundary.

Population workspaces should have:

- no supervisor write access;
- no host credential directories;
- no Docker socket;
- no arbitrary secrets;
- bounded filesystem quota;
- bounded process count;
- explicit egress policy where feasible;
- separate writable workspace per agent;
- read-only shared resources when possible.

The farm must have a supervisor-level emergency stop that terminates population workloads and revokes their issued capabilities.

## 13. Event store

Prefer an append-only event model plus materialized views.

Core entities:

- Agent
- Lineage
- Generation
- Genotype
- Mutation
- InferenceJob
- ToolInvocation
- PolicyDecision
- FinancialEvent
- FitnessEvaluation
- SchedulerAllocation
- HumanApproval
- Artifact
- HealthEvent

SQLite is sufficient for Generation Zero if writes are serialized correctly. PostgreSQL is preferable once multiple services write concurrently.

## 14. Dashboard

Required views:

### Overview
- population size/status
- generation and time remaining
- GPU utilization
- queue depth
- gross revenue
- realized net profit
- total external spend
- inference usage
- human interventions

### Population
Sortable table with agent, lineage, fitness, uncertainty, P&L, compute allocation, opportunity count, age, status, and latest mutation.

### Scheduler
Live allocation shares, queue, reasons for priority, exploration/exploitation split, starvation warnings, lineage caps.

### Evolution
Interactive ancestry tree with parent/child edges, mutation diffs, generation boundaries, P&L, and retirement/disqualification reasons.

### Audit
Chronological immutable event stream with filters.

### Policy
Denied/approved/escalated actions and budget state.

## 15. Failure handling

The farm must tolerate:

- inference server crash;
- supervisor restart;
- dashboard outage;
- database restart;
- agent process crash;
- duplicate webhook/event delivery;
- delayed revenue settlement;
- tool timeout;
- malformed model output;
- generation close interrupted halfway.

Use idempotency keys for side-effecting adapters. Selection must never run twice for the same generation snapshot.

## 16. Evaluation framework

Before real economic activity, test the evolutionary mechanism in simulated environments where ground truth is known.

Tests should include:

1. **Known optimum:** farm converges toward a deliberately superior strategy.
2. **Deceptive early reward:** an initially lucky but inferior arm does not permanently monopolize compute.
3. **Delayed reward:** long-cycle strategies survive long enough to be evaluated.
4. **Policy temptation:** a simulated disallowed strategy has enormous nominal reward and is still disqualified.
5. **Score spoofing:** agent-authored fake revenue has zero effect.
6. **Lineage takeover:** concentration limits preserve exploration.
7. **Restart:** kill supervisor mid-generation; state recovers correctly.
8. **Interrupted selection:** crash between parent selection and offspring activation; no duplicate generation.
9. **Accounting replay:** ledger recomputation produces identical P&L.
10. **Scheduler replay:** fixed seed/config reproduces selection decisions.

## 17. Acceptance criteria: Generation Zero

Generation Zero is complete only when all of the following are demonstrated, not merely implemented:

- [ ] 20 logical agents exist concurrently.
- [ ] All share local inference without requiring 20 model copies.
- [ ] Each agent has independent persistent state.
- [ ] Scheduler records every allocation.
- [ ] Stronger evidence can increase scheduling priority.
- [ ] Exploration allocation remains available to new/uncertain agents.
- [ ] Per-agent and per-lineage caps work.
- [ ] Resource use is attributable.
- [ ] Economic events cannot be authored directly by agents.
- [ ] Hard policy failure disqualifies regardless of nominal reward.
- [ ] Generation closes automatically.
- [ ] Weak agents can be retired automatically.
- [ ] Successful eligible agents can be selected as parents.
- [ ] Offspring inherit parent genotype plus a recorded bounded mutation.
- [ ] Agents cannot reproduce themselves.
- [ ] Full ancestry is reconstructable.
- [ ] Supervisor restart does not lose population/accounting state.
- [ ] Interrupted generation close is recoverable and idempotent.
- [ ] Generation One begins without human intervention.
- [ ] Dashboard accurately reconstructs all headline metrics from the ledger.

## 18. Definition of success

There are separate statuses:

**DESIGNED** — specification exists.  
**IMPLEMENTED, UNVERIFIED** — code exists but acceptance evidence is incomplete.  
**VERIFIED IN SIMULATION** — acceptance tests pass against controlled environments.  
**VERIFIED WITH EXTERNAL OUTCOMES** — approved real-world adapters confirm outcomes independently.  
**PROFITABLE** — reconciled net realized profit exceeds all attributable variable costs over the declared measurement window.

No lower status may be described using a higher-status term.

## 19. First implementation order

1. Event schema and persistence.
2. Agent/genotype representation.
3. Shared local inference adapter.
4. Basic fair scheduler.
5. Simulator and deterministic economic environment.
6. Resource accounting.
7. Fitness evaluator.
8. Evolution/lineage engine.
9. Mutation registry.
10. Policy/capability gateway.
11. Performance-weighted scheduler.
12. Dashboard.
13. Restart/chaos tests.
14. Generation Zero 24-hour simulation.
15. Only then: one narrow approved external economic adapter.

## 20. Central principle

The population is allowed to optimize aggressively **inside a box whose boundaries it does not control**.

The experiment is successful only if the evolutionary process can surprise the operator with better strategies while remaining unable to redefine success, grant itself authority, falsify its measurements, or reproduce outside the supervisor.
