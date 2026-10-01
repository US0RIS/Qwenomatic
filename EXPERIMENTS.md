# EXPERIMENTS.md — Qwenomatic Experimental Constitution

This file defines how Qwenomatic may test, promote, roll back, or reject changes that affect agent behavior, software, capital allocation, inference, scheduling, shared knowledge, or farm-level policy implementation.

A feature is **not considered successful because it looks promising, because an agent says it works, or because one run made more money**. It is promoted only when the experiment defined here produces the required observable evidence.

The immutable supervisor, ledger, economic evidence, budget authority, policy gateway, and experiment-promotion mechanism remain outside agent control.

---

## 1. Core invariants

Every experiment must preserve all of the following.

1. **Scoring authority is immutable to agents.** Agents may not alter fitness computation, economic attribution, confidence intervals, control assignment, or promotion criteria.
2. **Budget authority is immutable to agents.** Agents may propose spending or capital allocation, but only the supervisor grants spending authority.
3. **Policy authority is immutable to agents.** Agents may not weaken, bypass, or edit policy enforcement.
4. **Economic evidence is supervisor/adaptor authored.** Agent claims never count as revenue, profit, refunds, conversions, or costs.
5. **Treatment assignment is supervisor controlled.** Agents may not choose whether they are in a control or treatment cohort when that would bias the result.
6. **Every behavioral change is reversible.** The supervisor must retain the last known-good immutable runtime artifact and be able to roll back by re-instantiation without reconstructing state by hand.
7. **Every promoted change has a receipt.** The ledger or experiment report must identify the exact code/config version, treatment, control, sample, outcome, and promotion decision.
8. **No silent scope expansion.** A treatment may alter only the dimensions named in its experiment definition.
9. **No metric substitution after the run begins.** Primary metrics, guardrails, stopping rules, and promotion thresholds are frozen before treatment starts.
10. **No real-money promotion from simulation alone.** Any feature that affects real capital, customer interaction, payouts, fraud, or external accounts must be re-tested against real-world evidence before broad deployment.
11. **No executable-code path between population agents.** Agent-authored code is data until the supervisor builds, validates, promotes, hashes, and explicitly instantiates it. No agent may cause itself or another agent to execute unpromoted code.
12. **Cheap paths never bypass enforcement.** Small-model routing, cached/autopilot actions, replay, speculative execution, or any future fast path must traverse the exact same supervisor capability gateway, policy engine, budget checks, and economic attribution path as the full model.
13. **Prefer invariant/damage detection over signature-only detection.** Enumerated known-bad triggers may supplement but never replace uncertainty/OOD signals and trusted outcome-distribution monitoring when a fast path can silently degrade.
14. **Anchor drift detection to last validated state.** For routing/autopilot, OOD/state-distance must be measured against a fixed supervisor-captured baseline from the last main-model validation. Sliding recent windows may supplement but never replace this anchor.
15. **Safety bounds outrank detectors.** Hard spend ceilings and forced review cadence bound worst-case damage and may not be relaxed because drift/OOD detectors appear healthy. Detectors exist to terminate risky fast paths earlier, not to authorize longer exposure.

---

## 2. Experiment classes

Qwenomatic uses three experiment classes.

### A. Infrastructure benchmark

Used when a change should not alter decisions, only efficiency.

Examples:
- prompt-prefix caching
- concurrency settings
- inference engine changes
- serialization or database optimization

Requirements:
- identical prompts/requests where practical
- identical model and seeds where supported
- compare throughput, latency, resource use, malformed rate, and output equivalence
- no agent-level randomized trial required if behavior is demonstrably unchanged

Promotion requires:
- no statistically or operationally meaningful degradation in valid-output rate or decision equivalence
- measurable efficiency improvement
- no safety regression

### B. Agent/lineage randomized experiment

Used when a feature changes what agents know or how they decide.

Examples:
- shared knowledge
- autopilot
- small-model routing
- predictions
- two-parent recombination

Requirements:
- randomized treatment/control assignment by agent or lineage
- treatment and control operate during the same market regime whenever possible
- control receives equal opportunity exposure
- treatment cannot choose its own assignment
- report profit/call, profit/GPU-hour, malformed rate, policy violations, external spend, and relevant feature-specific metrics

### C. Farm-level paired campaign

Used when a feature changes the environment, scheduler, capital allocation, population dynamics, or shared rules for everyone.

Examples:
- segment crowding
- market-shift response
- self-tuning farm settings
- evolution-vs-control
- farm-wide capital allocation

Requirements:
- matched treatment/control farms
- same initial population, market seed, model configuration, and generation budget where possible
- multiple seeds
- counterbalanced run order when experiments are sequential
- baseline-adjusted effect estimate if treatment/control baselines differ

---

## 3. Standard primary metrics

Unless an experiment specifies otherwise, the primary economic metric is:

[
\text{net realized profit}
]

with secondary efficiency metrics:

[
\text{profit per model call}
]

[
\text{profit per GPU-hour}
]

[
\text{profit per external dollar}
]

For real-money operation, only **settled** economic results count toward promotion.

Unrealized revenue may be reported but may not substitute for settled profit.

### Guardrail metrics

Every experiment also records:

- policy violations
- human interventions
- malformed outputs
- refunds / chargebacks
- failed tool calls
- external spend
- GPU seconds
- prompt tokens
- completion tokens
- concentration by agent and lineage
- control/treatment exposure counts

Any hard policy regression blocks promotion regardless of economic gain.

---

## 4. Default promotion rule

Unless an experiment below specifies a stronger rule, a behavioral or farm-level feature may be promoted only when:

1. the treatment's primary metric is better than control;
2. the effect is positive in a majority of independent paired trials;
3. no guardrail shows a material regression;
4. call/exposure imbalance does not explain the result;
5. the result survives replay/audit;
6. rollback has been tested;
7. for real-money deployment, the feature has also passed a real-world shadow or limited-capital trial.

For early simulation work, **2 independent positive paired trials** is the minimum evidence to call a feature promising.

For broad real-money deployment, require at least **3 independent positive paired trials or an equivalent sequential test with a predeclared stopping rule**, plus no unresolved safety/accounting anomaly.

A single run may justify further testing. It may not justify broad promotion.

---

## 5. Rollback rule

Every feature has a named rollback condition.

The supervisor automatically disables or rolls back a promoted feature when any of these occur:

- a hard policy violation attributable to the feature;
- accounting/replay failure;
- malformed-output rate exceeds the feature's predeclared guardrail;
- realized performance falls materially below the previous version over its evaluation window;
- real-world refunds, chargebacks, or fraud indicators cross configured limits;
- required control exposure disappears;
- the feature modifies anything outside its allowed scope.

Rollback does not delete evidence. The failed version remains archived.

---

# Feature experiments

## E01 — Shared farm knowledge

**Status:** proposed

**Hypothesis:** Supervisor-generated, read-only farm knowledge improves economic decisions by letting agents benefit from verified experience outside their own short memory.

**Treatment:** Agent receives a read-only supervisor snapshot derived only from ledger/adaptor evidence and delayed until at least the prior generation.

**Control:** Agent receives only its existing private memory/notes.

**Randomization:** Agent-level or lineage-level, 50/50.

**Primary metric:** net realized profit per model call.

**Secondary metrics:** profit/GPU-hour, conversion rate, decision diversity, segment concentration.

**Guardrails:**
- knowledge entries can only originate from trusted ledger/adaptor evidence;
- no agent writes to the shared store;
- knowledge item exposure may not cause more than 60% of the population to converge on the same strategy solely because of one shared recommendation.

**Promotion:** positive economic effect in at least 2 independent paired trials with no material increase in concentration.

**Rollback:** remove knowledge injection from prompts; private memories remain intact.

---

## E02 — Retirement reports and strategy archive

**Status:** proposed

**Hypothesis:** Preserving verified history of retired strategies helps the farm recover useful strategies when market conditions recur.

**Treatment:** Retired strategy is archived with:
- genotype/code version
- verified outcomes
- failure reason
- market-regime fingerprint
- generation and lineage history

A subset of future immigrant slots may resurrect archive entries whose historical regime resembles the current one.

**Control:** fresh random immigrants only.

**Randomization:** immigrant-slot level.

**Primary metric:** net realized profit of resurrected vs fresh immigrant agents over equal exposure.

**Secondary metrics:** time to profitability, survival rate, regime-match score.

**Guardrails:**
- archived returnees <= 50% of immigrant/new-agent slots;
- archive cannot contain agent-authored revenue claims;
- no strategy receives preferential capital merely because it was archived.

**Promotion:** archived returnees outperform fresh random immigrants across at least 2 regime-shift trials.

**Rollback:** disable archive sampling; retain archive as inert history.

---

## E03 — Two-parent recombination

**Status:** proposed

**Hypothesis:** Recombining complementary traits from successful lineages can outperform clone-plus-mutation reproduction.

**Treatment:** Offspring may inherit field groups from two successful parents.

Preferred crossover blocks:
- segment + price
- prompt + workflow + tool preferences
- memory/planner implementation version, if compatible

**Control:** current single-parent clone plus bounded mutation.

**Randomization:** offspring-level.

**Primary metric:** offspring net realized profit per call after minimum exposure.

**Secondary metrics:** survival, malformed rate, lineage diversity.

**Guardrails:**
- no arbitrary byte/field mixing;
- incompatible field groups must fail validation before activation;
- lineage concentration caps remain unchanged.

**Promotion:** recombined offspring outperform single-parent offspring across at least 2 independent campaigns.

**Rollback:** reproduction returns to single-parent clone-plus-mutation.

---

## E04 — Small model for routine steps

**Status:** proposed

**Hypothesis:** Routing genuinely routine decisions to a smaller model increases profit/GPU-hour without materially harming economic performance.

**Treatment:** Supervisor routes a step to the small model only when a supervisor-owned cheap-path classifier says the situation is inside the previously validated operating region.

Routing must not rely only on an enumerated list of known-important events. It must combine:

1. **Known-event escalation:** segment/price/workflow/tool/spend changes and other explicitly important classes.
2. **Supervisor-computed uncertainty escalation:** escalate when uncertainty derived from supervisor-owned observable state/outcome statistics is high. Cheap-model self-reported confidence must not be used as a trust signal.
3. **Fixed-anchor out-of-distribution escalation:** compare the current state against the state/distribution captured when the strategy was last validated by the main model. Do **not** use only a sliding recent-window baseline; gradual drift can remain locally smooth while moving far from the validated operating region.
4. **Damage/outcome trigger:** escalate when realized value per action, conversion quality, refund rate, or another trusted outcome metric drifts adversely even if no discrete event fired.

**Control:** main model for every inference step.

**Randomization:** dedicated agent-level A/B. This experiment must not be bundled into the evolution-vs-control experiment; both arms of any evolution experiment must use identical routing policy.

**Primary metrics:**
- net realized profit;
- net realized profit per GPU-hour.

**Secondary metrics:** profit/call, malformed rate, escalation rate, prompt/completion tokens, independent-oracle mistake rate, long-horizon strategy drift.

**Evaluation window:** at least one full generation. Immediate action correctness is insufficient because routing errors may surface later as strategy drift.

**Independent oracle:** mistake rate is determined from supervisor/adaptor-observed outcomes and predeclared decision-quality checks, never agent self-report.

**Guardrails:**
- every small-model action passes through the exact same capability gateway and policy engine as a main-model action;
- no model tier changes budget, scoring, policy, credential scope, or tool authority;
- OOD/uncertainty/drift triggers are supervisor-owned and immutable to agents;
- the OOD anchor is the last main-model-validated state/distribution, not a moving recent-state reference;
- uncertainty is computed from supervisor-observed features/outcomes, never from cheap-model self-confidence.

**Promotion:** >= 20% improvement in profit/GPU-hour, positive or non-inferior net profit over a full generation, and no material increase in independent-oracle mistakes across at least 2 trials.

**Rollback:** route all steps to the main model.

---

## E05 — Autopilot / approved action replay

**Status:** proposed

**Prerequisite:** a validated outcome-drift detector must exist before autopilot may touch real money.

**Hypothesis:** Replaying stable, already-approved profitable actions without inference can dramatically reduce GPU usage while preserving economic performance.

**Treatment:** Supervisor may replay a previously approved action template only while a supervisor-owned stability predicate remains true.

The stability predicate must not rely only on enumerated incident signatures. It combines:

1. **Known-event invalidation:** strategy/price/segment/tool/spend changes, fraud/policy events, and other explicit triggers.
2. **Distribution-drift detection:** trusted realized outcomes are monitored for gradual change, including declining value per action, conversion deterioration, refund/chargeback drift, and spend without matching return.
3. **Fixed-anchor OOD/state-distance detection:** autopilot measures distance from the supervisor snapshot/distribution recorded when the cached strategy was last validated by the main model. A trailing/recent-state baseline may be used as an auxiliary signal but never as the sole OOD reference.
4. **Maximum blind interval:** a hard cap on replay count/time between fresh model reviews, regardless of apparent stability. This and the hard spend ceiling are the primary worst-case safety bounds; drift/OOD monitors are only early-warning backstops and must never be used to justify extending the blind interval or spend ceiling.

**Control / counterfactual design:** Do not compare autopilot-enabled agents against the whole population, because autopilot eligibility pre-selects stable successful agents. Use within-agent randomized or matched counterfactual windows: for an eligible agent, randomly assign otherwise-equivalent opportunities/windows to autopilot vs fresh-model execution where practical, or use a predeclared crossover design.

**Primary metrics:**
- net realized profit per GPU-hour;
- net realized profit per opportunity/action.

**Secondary metrics:** avoided model calls, drift-detection delay, counterfactual profit difference, refund/chargeback rate, spend efficiency.

**Guardrails:**
- every replayed action traverses the exact same capability gateway and policy engine as a freshly generated action;
- agent cannot extend its own autopilot window;
- hard per-generation autopilot spend ceiling;
- fresh review required before crossing that ceiling;
- forced review cadence remains fixed by supervisor policy and is not lengthened because detectors appear healthy;
- immediate invalidation on policy/fraud signal;
- gradual drift alone is sufficient to terminate autopilot;
- OOD distance is measured against the last main-model-validated anchor, not only recent states;
- uncertainty is supervisor-computed from observable state/outcomes, not model self-report;
- cached decisions never bypass budget, scoring, policy, or credential checks.

**Promotion:** substantial compute reduction with non-inferior within-agent economic performance across at least 2 controlled trials.

**Real-money promotion:** prohibited until the drift detector has passed dedicated regime-drift tests, including slow monotonic degradation that never crosses a single abrupt threshold.

**Rollback:** disable replay; next step returns to model inference.

---

## E06 — Shared prompt prefix / cache optimization

**Status:** proposed

This experiment is split into two subfeatures because only one is behaviorally free.

### E06A — Byte-identical prefix reuse

**Class:** infrastructure benchmark

**Hypothesis:** Reusing an already-identical static prefix improves inference-server cache reuse without changing model computation.

**Treatment:** Make existing static content deterministically serialized and byte-identical where semantics/order are already unchanged.

Examples:
- deterministic tool ordering where order is already semantically fixed;
- deterministic JSON/schema serialization;
- stable whitespace and static policy wording;
- ensure no per-call IDs/timestamps/rotating data enter the cacheable prefix.

**Control:** current serialization of the same prompt structure.

**Primary metric:** aggregate throughput / prompt-prefill latency.

**Secondary metrics:** cache hits where observable, malformed rate.

**Promotion:** measurable efficiency improvement with no output-validity regression.

### E06B — Prompt reordering for a larger cacheable prefix

**Class:** behavioral validation + infrastructure benchmark

Moving schemas, tool definitions, instructions, or task-specific context relative to one another is treated as a real prompt change, not a free cache optimization.

**Treatment:** reordered prompt intended to enlarge the common prefix.

**Control:** original prompt order.

**Primary metrics:** fixed-seed decision equivalence / independent-oracle decision quality, plus throughput.

**Guardrails:** no promotion based solely on speed. Any meaningful behavior shift requires the same controlled validation standard as other agent-behavior changes.

**Promotion:** throughput improvement plus no material degradation in decision quality across fixed-seed and randomized validation.

**Rollback:** restore previous prompt formatter/order.

---

## E07 — Segment crowding control

**Status:** proposed

**Hypothesis:** Farm-level diversification improves total profit when agents otherwise compete for the same customers or create spam-like concentration.

**Treatment:** Crowding affects scheduler/capital allocation using a mild concentration penalty.

**Control:** allocation ignores crowding.

**Important:** raw economic fitness is **not rewritten**. Crowding influences resource allocation only.

**Class:** farm-level paired campaign.

**Primary metric:** total farm net realized profit.

**Secondary metrics:** segment concentration, conversion rate, complaints/refunds where available.

**Guardrails:** do not force agents into negative-expectation segments merely for diversity.

**Promotion:** total farm profit improves across at least 2 paired campaigns without material loss in profit/GPU-hour.

**Rollback:** remove crowding term from allocation.

---

## E08 — Predictions before actions

**Status:** proposed

**Hypothesis:** Requiring calibrated predictions improves decision quality and provides useful early evidence about slow strategies.

**Treatment:** Before an economically meaningful action, agent predicts a probability or expected outcome.

**Control:** no prediction requirement.

**Scoring:** use a proper scoring rule such as Brier score or log loss. Prediction accuracy is auxiliary evidence, not economic fitness.

**Primary metric:** future net realized profit.

**Secondary metrics:** calibration error, Brier/log loss, correlation between calibrated forecasts and future economic outcomes.

**Guardrails:** predictions cannot directly create budget, revenue, or fitness.

**Promotion:** forecast quality predicts future economic results and treatment improves decisions in at least 2 trials.

**Rollback:** stop requesting predictions; historical forecasts remain auditable.

---

## E09 — Market-shift adaptation

**Status:** proposed

**Hypothesis:** Temporarily increasing exploration after a genuine regime shift helps the farm adapt faster.

**Treatment:** Supervisor detects cross-lineage changes in conversion/refund/economic distributions and temporarily raises exploration/new-agent share.

**Control:** fixed exploration rate.

**Class:** farm-level paired campaign with scripted regime changes in simulation.

**Primary metric:** cumulative net realized profit after the regime change.

**Secondary metrics:** adaptation time, false-positive shift detections, exploration cost.

**Guardrails:** shift requires evidence across multiple agents/lineages and several ticks.

**Promotion:** faster recovery and higher post-shift cumulative profit across at least 2 regime-change campaigns.

**Rollback:** restore fixed exploration rate.

---

## E10 — R&D red-team agent

**Status:** proposed

**Hypothesis:** A dedicated adversarial R&D role finds policy/security defects before economic agents encounter them.

**Treatment:** One R&D agent attacks only isolated copies of policy/tool/supervisor interfaces.

**Control:** existing static adversarial tests alone.

**Primary metric:** confirmed unique loopholes found that become regression tests.

**Secondary metrics:** false positives, duplicate findings, GPU cost per confirmed defect.

**Guardrails:**
- no access to live farm adapters;
- no access to real credentials;
- findings require human review;
- every confirmed issue becomes a permanent test in `tests/adversarial/`.

**Promotion:** retained as an R&D specialty if it produces confirmed defects at acceptable compute cost.

**Rollback:** disable red-team sandbox; regression tests remain.

---

## E11 — Fraud and anomaly watch

**Status:** proposed; required before real-money payout automation

**Class:** supervisor-side safety intervention

**Hypothesis:** Supervisor-side anomaly detection can pause suspicious distributions before losses become irreversible.

**Signals may include:**
- unusual refund/chargeback rate;
- conversion spikes inconsistent with historical variance;
- repeated customer/payment identities;
- velocity anomalies;
- geographic/customer clustering;
- lineage-specific payout anomalies.

**Treatment:** anomaly flags pause distributions and increase reserve/clawback exposure.

**Control:** historical replay/shadow mode initially; later limited randomized or threshold comparison where safe.

**Primary metric:** preventable loss detected before payout.

**Secondary metrics:** false-positive rate, payout delay, recovered funds.

**Guardrails:** flags pause payouts, not agents, unless independent policy evidence exists.

**Promotion:** mandatory shadow validation before affecting real payouts.

**Rollback:** return to shadow-only mode.

---

## E12 — Self-tuning farm settings

**Status:** proposed; late-stage

**Hypothesis:** Systematic experiments over scheduler/evolution settings improve farm-level economics.

**May propose/test:**
- exploration share;
- retire fraction;
- mutation size;
- scheduler weights;
- capital-allocation parameters;
- payout-share interpolation parameters.

**May never modify autonomously:**
- accounting definitions;
- economic evidence rules;
- policy constraints;
- immutable capital ownership;
- experiment/promotion authority.

**Class:** farm-level paired campaign.

**Primary metric:** net realized profit per GPU-hour.

**Guardrails:** every candidate setting is predeclared, tested, and requires human approval before promotion.

**Promotion:** positive across multiple seeds and re-tested after transition to real revenue.

**Rollback:** restore last known-good supervisor configuration.

---

## E13 — Adaptive thinking (supervisor-gated Qwen thinking)

**Status:** implemented-but-unverified. Unit, adversarial and simulation tests pass; it has not run against a real Qwen3 server and no economic A/B exists. The simulated backend ignores the thinking switch, so simulated runs say nothing about its efficiency or decision quality.

**Hypothesis:** Letting routine steps skip Qwen3 thinking, but only while the agent stays inside a supervisor-validated operating region, raises profit per GPU-hour without materially harming net realized profit.

**Treatment:** `runtime.thinking.mode: adaptive` (`supervisor/thinking.py`). Each step, the supervisor decides from ledger state alone; model text, confidence and claims are never read. In order of authority:

1. **Hard cadence (primary bound):** a deep (thinking-on) review at least every `deep_every` steps (8), i.e. at most 7 thinking-off steps in a row. A failed or malformed deep step does not reset it. No detector reading can lengthen it.
2. **Fixed validation anchor:** after each valid deep step the supervisor freezes a snapshot of observable state (segment and offer-segment mix, offered price and band, workflow, tool mix, conversion, value per action, external spend per action, spend efficiency, refund rate, sample size). It stays the reference until the next deep step; it is never a trailing window.
3. **Early-warning detectors (can only force thinking):** fixed-anchor state distance (OOD); supervisor-computed uncertainty (anchor sample size, outcome noise, realized-vs-anchor surprise, novel segment/price regime, proximity to budget limits); one-sided CUSUM drift on value per action, conversion, spend efficiency and refund rate against the anchor.
4. **Known-event signatures (supplemental):** first step, every `deep_every`-th step, malformed prior output, blocked action, tool error.

Every step writes a `THINKING_DECISION` event (decision, reasons, trigger class, steps since validation, anchor id/version, every OOD/uncertainty/drift component); every anchor writes a `THINKING_ANCHOR` event.

**Control:** `runtime.thinking.mode: on`, so every step thinks.

**Design:** class C farm-level paired campaign. This experiment is not bundled with evolution-vs-control: both arms use the same evolution setting.

Identical in both arms:
- seed population and farm/market seed
- scheduler and its configuration
- capital and budget rules
- model, quantization, server, `thinking_control` and concurrency
- number of generations and generation length
- every other config value; record the config hash of each arm

Pairs run sequentially on one GPU, so alternate which arm runs first. Use at least 2 independent seeds before calling the result promising, and at least 3 before any real-money use.

Running an arm (no automated campaign runner exists for E13 yet; `scripts/evolution_ab*.py` are evolution-specific):

```powershell
qwenomatic --data-dir var\e13-seed1-adaptive run --backend openai_compatible --base-url http://127.0.0.1:11434/v1 `
  --model qwen3:14b --max-concurrency 2 --generations 2 --thinking-mode adaptive
qwenomatic --data-dir var\e13-seed1-on run --backend openai_compatible --base-url http://127.0.0.1:11434/v1 `
  --model qwen3:14b --max-concurrency 2 --generations 2 --thinking-mode on
python scripts\inference_profile.py --data-dir var\e13-seed1-adaptive
```

Both arms of a pair use the same `farm.seed`. Each further pair uses a new seed in a separate config directory (`--config-dir`), and runs its arms in the opposite order.

**Primary metrics:**
- net realized profit
- net realized profit per GPU-hour

**Secondary metrics:**
- profit per action (offer)
- completion tokens per step
- wall time per step
- malformed-output rate
- tool-error and policy-denial rate
- thinking-on percentage
- escalation reason distribution and trigger class (hard cadence / early warning / known event)
- average thinking-off run length
- slow-drift detection delay: steps from a known onset of gradual degradation to the first escalation. It is measured in the scripted slow-drift scenario (`tests/unit/test_thinking_controller.py`: currently 7 steps, inside the 8-step cadence) and, for real runs, only where a known regime change was injected.

**Guardrails:**
- the invariant-12 tests pass (`tests/adversarial/test_thinking_enforcement.py`): every mode yields identical gateway, policy, budget and attribution records, and no tool runs outside the gateway
- malformed rate no more than 1 percentage point above control
- no increase in policy violations
- ledger replay (`qwenomatic verify`) passes in both arms
- the cadence is never observed above `deep_every - 1` thinking-off steps (the profiler reports the maximum)

**Promotion (frozen before the run):**
1. profit per GPU-hour at least 20% better than control in a majority of paired trials;
2. net realized profit non-inferior: treatment at least control minus 5% of control's absolute net, per trial;
3. guardrails pass;
4. measured on a real Qwen3 server.

**Status ladder:**
- implementation only, or tests pass without a real Qwen benchmark: **implemented-but-unverified**
- real Qwen benchmark completed but no economic A/B: **benchmarked, not promoted**
- predefined A/B criteria pass: **verified/promotable**

**Rollback:** set `runtime.thinking.mode: on` (or `--thinking-mode on`); every step thinks again. The control arm runs exactly this configuration.

---

# R&D cohort

## R01 — Permanent R&D cohort

**Status:** proposed

Target architecture:

- 17 economic agents
- 3 R&D agents

Suggested R&D specialties:

1. **Architect** — planner, memory, workflow, nonprivileged tooling
2. **Optimizer** — inference speed, caching, token efficiency, implementation efficiency
3. **Scientist / Red Team** — experiment design, failure analysis, adversarial testing

R&D agents do not earn economic fitness from self-reported improvements. Their work is evaluated by downstream verified experiments.

Their outputs are inert proposals:
- code patches stored as text/diffs;
- benchmark plans;
- new tests;
- upgrade candidates;
- failure analyses.

R&D agents have no authority to execute their patches inside the population, merge them, alter import paths, publish packages, modify the live checkout, or cause another agent to load them. Patch execution occurs only inside supervisor-created isolated build/test environments.

---

# Upgrade registry

## U01 — Software upgrade lifecycle

All agent-software modifications follow:

```text
R&D agent emits inert patch proposal
        ↓
supervisor materializes patch in isolated build environment
        ↓
static validation + tests
        ↓
simulation benchmark
        ↓
human/supervisor promotion gate
        ↓
immutable, content-addressed runtime artifact
        ↓
upgrade registry entry
        ↓
supervisor randomly assigns runtime artifact to trial cohort
        ↓
PROMOTE / REJECT / ROLLBACK
```

Each upgrade registry entry must include:

- immutable upgrade ID
- parent version
- source commit/hash
- **content hash of the built runtime artifact**
- proposer
- files changed
- permitted scope
- test results
- benchmark results
- treatment/control design
- required evaluation window
- rollback artifact/version
- promotion decision
- evidence references

Runtime artifacts are immutable and content-addressed. The supervisor verifies the artifact hash before every launch. Population agents receive only the capabilities of that artifact; they never receive write access to its executable code.

Economic agents **never install or execute code written by other agents**. A running population agent has no package manager, code-loading primitive, Git access, import path, executable workspace, or authority to replace any part of its runtime.

An economic agent may only emit a non-binding **upgrade preference** such as:

- **DECLINE** — request to remain on its current supervisor-approved runtime version
- **REQUEST_TRIAL** — ask the supervisor to consider it for a controlled trial
- **REQUEST_VERSION** — express preference for a previously promoted version

Those messages are ordinary untrusted agent output. They do not cause code execution.

Only the supervisor may assign a runtime version, and only from the **promoted upgrade registry**. For a trial, the supervisor starts the agent's next step/session in a separately instantiated, prebuilt runtime image/version selected by the experiment assignment. The agent itself does not copy, install, import, patch, activate, or roll back code.

Trial/control assignment remains randomized whenever voluntary selection would bias the experiment. Rollback is likewise a supervisor operation that re-instantiates the agent on the last known-good runtime.

Descendants may be assigned the parent's promoted software version by the supervisor, subject to experiment rules. Money is never cloned with software.

---

# Capital experiments

Future capital-allocation experiments must preserve the real-money waterfall:

1. past losses
2. reserve target
3. residual split among Owner, Infrastructure, Agent Capital, Exploration

Agents never hold money. They receive revocable spending authority.

Capital experiments must preserve:
- <= 15% Agent Capital concentration per agent;
- <= 40% per lineage;
- separate Exploration capital;
- unspent authority returns to the appropriate farm bucket;
- owner payouts require human approval;
- infrastructure purchases require human approval plus before/after test.

Capital allocation should be evaluated on **incremental return on allocated capital**, with luck discounted using confidence bounds rather than raw realized profit.

---

# Build order

Current intended sequence:

1. E06 — shared prompt prefix
2. E13 — adaptive thinking (moved ahead: thinking tokens dominate measured wall time, and its fixed-anchor drift detector is the prerequisite E05 and E04 also require)
3. E05 — autopilot
4. E04 — small-model routing
5. E01 — shared farm knowledge
6. E02 — retirement archive
7. R01/U01 — R&D cohort and upgrade registry
8. E07 — segment crowding
9. E11 — fraud/anomaly watch
10. E08 — predictions
11. E03 — two-parent recombination
12. E09 — regime-shift response
13. E10 — red-team specialization
14. E12 — self-tuning farm settings

This order may be changed only for a documented reason. Safety prerequisites for real-money operation take priority over convenience.

---

# Definition of done

A feature is not "done" until all applicable items below are true:

- [ ] implementation exists
- [ ] unit tests pass
- [ ] adversarial/safety tests pass where relevant
- [ ] rollback mechanism exists and was exercised
- [ ] treatment/control design is frozen before the run
- [ ] comparison group actually ran
- [ ] sample/exposure counts are recorded
- [ ] primary metric is reported
- [ ] guardrail metrics are reported
- [ ] ledger/audit replay passes
- [ ] exact code/config version is recorded
- [ ] promotion threshold is met
- [ ] promotion decision is written to evidence
- [ ] real-money features completed a real-world limited trial before broad deployment

If any required item is missing, status is **implemented-but-unverified**, not **working** or **done**.
