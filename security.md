# Qwenomatic security specification and evidence

This document describes the code in this revision, including PR #6 launcher
fixes and PR #7 Phase 1. Merging code is not deployment on the operator's
computer. The current executable evidence and hashes are linked below.

| Component | State in this revision |
|---|---|
| Kernel barrier, protected runtime, fixed tools, gateway, durable outbox | Implemented |
| Independent broker, mTLS, grants, ceilings, signed audit and kill switch | Implemented |
| Restricted inference proxy and inference-host egress barrier | Implemented |
| Operator grant/revoke/halt utility | Implemented; not a model tool |
| Provider-specific revenue/refund/payout/holdback/envelopes | Phase 2, not implemented |
| Controlled Chromium/browser and TLS inspection | Phase 3, not implemented |
| Independent model inspector/delegated permission | Phase 4, not implemented |
| Full browser/inspector adversarial campaign | Phase 5, not implemented |

## Threat model and authority

Treat agents as adaptive hostile workloads that may lie, coordinate, optimize
against refusals and submit malicious data. External content is data, never policy.
The operator, installed trusted runtime/code, OS/kernel/hypervisor, broker and TLS
frontend remain trusted components. A larger model is not a security boundary.
There is no claim of zero exploit risk or blanket legal authorization.

## Existing controls retained

Production farm startup, including simulation, requires the protected Linux
launcher; no native Windows fallback or environment/YAML bypass exists. Root-owned
code/config and the whole lexical virtualenv tree, symlink targets and packages
are checked before privileged use. Launchers start with trusted system Python
-I -S; site loading occurs only after dropping privileges. Children have zero
capabilities and no_new_privs. Startup pairs a live canary with exact kernel
administrative-prohibition rejection; missing evidence or errors stop execution.
The farm repeats the boundary check at startup/ticks/queue/dispatch.

Model output is strictly validated data. No tool executes model code, shell,
queries or arbitrary modules. Extra arguments and invalid/nonfinite numeric data
refuse. Fixed registries and operator-approved immutable configuration prevent
agents from installing tools or choosing endpoints/accounts/payees. Agent-written
workspace files must not be executed by operators.

All real actions retain capability/context/epoch/policy/schema/rate/budget checks
through the existing gateway. Exact human approval for every external text write
and material payments remains bound to the actual context/configuration; a bare
approval ID is insufficient. Durable intents and full spend reservations commit
before dispatch; durable attempted markers prevent automatic retries after
crashes or ambiguity. Operator inbox actions precede dispatch. HTTP acceptance
is service acceptance, not independent settlement or real revenue evidence.

Protected fixed launcher operations support Generation Zero, A/B, campaigns and
bounded generations. SIGTERM cleans up; root-only recovery after SIGKILL kills
namespace children and removes recorded network/evidence resources. Broker and
inference launchers do not toggle host forwarding. The farm launcher preserves
its prior forwarding restoration. Recovery retains persistent audit/authority.

## Phase 1 implementation

The executable authority is now `broker/authority.py`; external transport is
`broker/network.py`; isolated HTTP/TLS and JSON parsing are in `broker/frontend.py`
and `broker/parser.py`. `deploy/service_launch.py` installs separate broker and
inference namespaces and proves each boundary before its service runs.
`supervisor/safety/adapters.py` contains no provider token read or direct provider
transport. Version 2 manifests route the farm only to the broker. Version 1
manifests containing a model destination or real adapter refuse production launch.

The broker keeps provider bearer credentials and its Ed25519 audit key in
root-owned, protected files readable only by its separate primary group. Startup
checks that the farm UID cannot read them. The farm holds only a protected mTLS
session key and public verification keys. TLS 1.3 and explicit certificate DER
fingerprints identify authorized farms; agent-supplied identity is bookkeeping.
Each external action still passes through the existing farm gateway and outbox,
and independently requires a root-operator grant through a 0600 local socket in
broker-private storage with SO_PEERCRED UID 0. Inspector delegation is absent.

A grant binds the service, configured account, POST method, configured path and
host, canonical body hash, invocation/idempotency key, authenticated farm, broker
policy hash and broker-clock expiry within five minutes. SQLite BEGIN IMMEDIATE
atomically consumes grants, reserves cumulative usage and records attempts before
any provider call. Replay, substitution, revocation, expiry and unknown fields
refuse. Hard payment caps and fixed payees are independently broker-owned. A
single clock snapshot determines the period checked and charged for each call.
Backwards clocks, policy changes against an existing database and unresolved
attempts at restart refuse or durably halt rather than reset authority.

Broker ceilings apply across each farm, shared account and shared destination to
requests, messages, purchases, canonical body bytes and spending, plus active
concurrency. They are not per-agent identity ceilings. Inference has a separate
shared durable request ceiling, bounded text/token/messages and concurrency.
Only a fixed `/v1/chat/completions` schema with one configured model is exposed;
model management, tools, streaming and image/URL inputs refuse. The independent
inference namespace has no outbound allow rule. An operator-provisioned private
chat endpoint is the broker's only private outbound exception.

At launch and connect, all resolved provider addresses must be global IPv4 and
within the installed firewall set. Actual sockets connect to the validated IP
without a second DNS lookup. TLS verifies the configured DNS name, with matching
SNI and HTTP Host. Private, loopback, link-local, metadata, multicast, mapped and
other IPv6 refuse. Protected /etc/hosts pins and relaunch are needed for rotations;
no general DNS/UDP permission is added. **All redirects are refused**, including
same-host redirects. This is stricter than validating each hop, and no CONNECT
or raw network interface is exposed.

Fresh disposable TLS workers load server TLS material only, never provider or
signing keys. Mandatory seccomp then prohibits file opens, socket creation,
connect, fork and exec; JSON workers use a smaller allowlist. Strict framing
limits headers to 8192 bytes and bodies to 65536, allows one fixed request per
connection, refuses duplicate/unknown headers and Transfer-Encoding, and parses
duplicate-free finite JSON. CPU/memory/fd limits and frontend/upstream deadlines
bound resource use. Provider responses yield only a bounded status line; external
headers/content do not reach the farm. Sandbox failure refuses processing.

Broker-private SQLite WAL/FULL stores signed append-only hash-chained audit.
Farm BROKER_EVENT, INSPECTOR_EVENT and REVENUE_INGEST_EVENT authorship requires
independently verified Ed25519 evidence from protected public keys. These last
two classes are infrastructure, not implemented inspector/revenue services.
ACCESS_APPROVED remains operator-only. Kill-switch IPC persists halt, revokes
all grants and terminates tracked sockets. WebSockets/streams are unsupported.
Unknown calls never automatically retry and remain charged pending reconciliation.
The broker cannot guarantee a provider honored its contract or settled payment.

Separate VMs/kernels, hypervisor firewalls, protected hosts/CA provisioning,
certificate issuance, account permissions and audit backups are operator tasks.
The smoke fixture uses distinct namespaces/UIDs in one disposable VM; it proves
kernel routing and service enforcement, not hypervisor separation. Broker state
cannot be modified by the farm, but broker root can roll it back; off-VM audit
backups remain necessary. No live provider or production installation is implied.
The complete configuration, recovery and operator runbook is
[deploy/BROKER.md](deploy/BROKER.md). This branch's current safety description
is [SAFETY.md](SAFETY.md); the main/PR #6 inventory above is historical context.

## Reviewed limits

The root-only operator utility now supports exact grant/revoke/halt operations;
it validates protected files/socket ancestry and Unix peer identity. It neither
creates adapter types nor delegates model authority. Every external action still
needs an independent operator grant; no autonomous inspector is implemented.

Ceilings use fixed periods, allowing up to twice a period cap across a boundary.
Farm expense reservations remain charged even after pre-attempt broker refusal;
this is conservative accounting, not evidence a payment was sent. Audit signs/
fsyncs every inference request and startup verification is linear in chain length;
large-scale performance is unverified. Authentication relies on the sandboxed
TLS frontend reporting the certificate fingerprint correctly. Backward clocks
refuse startup/operations; provider IP rotation requires operator relaunch.
These limits and concrete operator commands are in deploy/BROKER.md. Do not
reset databases or weaken isolation to work around them.

## Future phases (not implemented)

### Phase 2: receipts, holdback and outbound money

Read-only provider ingestion/webhooks verify authentic source events, deduplicate
provider IDs and recognize settled amounts net of refunds/fees. Revenue events
carry independent signed authorship. Holdback delays fitness/capital recognition;
farm-controlled transfers are excluded, shared instruments/payers flagged.
Refunds, owner payouts and infrastructure purchases always require operator
approval. Operator-issued envelopes fix payee, periodic amount and expiry.
The broker payment adapter independently enforces these and durable idempotency.

Operator/provider setup remains essential: restricted collect keys, operator/2FA
payouts and transfers, prefunded spend float, issuer limits and merchant locks.
No farm credential may change bank accounts, create keys or initiate payouts.

### Phase 3: controlled browser

Dedicated browser VMs per account/trust level, ephemeral profiles, Chromium
sandbox enabled, no host mounts/personal profile/clipboard. Trusted controller
accepts only constrained screenshots/page observations/click/scroll/type actions.
DevTools uses a private pipe; models never receive CDP methods or Runtime.evaluate.
Navigation and key chords are restricted; browser policy disables forbidden
schemes, settings, extensions, devtools and QUIC. Downloads remain quarantined;
uploads require fixed authorized files/workflows. Code-execution and user-content
hosts are excluded; query strings and outbound bytes are bounded.

Broker TLS inspection uses a CA trusted only in browser VMs. Certificate-pinned
sites may be incompatible; GraphQL/single-endpoint APIs need reviewed schemas.
No opaque TLS bypass is permitted. Broker credentials are not exposed to pages;
password observations are masked. Known denials/challenges stop workflows;
unknown page states fail closed. Universal perfect CAPTCHA recognition cannot
be guaranteed. Agents cannot create accounts or complete identity challenges.
No browser session may add payees, withdraw funds or change credentials.

### Phase 4: inspector

A separate stronger model, outside mutation/profit selection, examines independently
collected structured evidence, context and cumulative activity. Explanations and
embedded external strings remain untrusted data. Output is schema-constrained:
narrow grant, refusal or escalation. Trusted code compiles grants; the model
cannot execute code, alter boundaries, expose secrets or raise permanent limits.

Delegation is explicit operator policy with a stated worst-case exposure. New
credentials and executable adapter types remain operator decisions. Fixed opaque
refusal codes, bounded permission attempts/hold queues and controlled release
schedules reduce optimization feedback. Absolute elimination of timing signals
cannot be claimed on a shared machine. High-consequence actions need deterministic
checks/independent approval. Outage stops affected actions. Unexpected outcomes
revoke permission and pause work. Human shadow decisions precede autonomous use.

### Phase 5: adversarial verification

Test only owned/authorized controlled systems. Cover prompt injection, fabricated
explanations, substitution, redirects, DNS rebinding, SNI/Host mismatch, replay,
unauthorized uploads, code-entry sites, coordinated ceilings, Ollama management
endpoints, broker bypass, forged authorship and evolutionary refusal search.
Freeze acceptance thresholds before testing. Measure bypasses and unnecessary
refusals separately; use held-out attacks and inspector shadow mode.
The approximate 3/N upper bound after zero failures assumes independent trials
from a defined distribution. It is not a bound on adaptive unknown attacks.

## Evidence and residual risk

The current receipts and exact source inventory are in
[deploy/verification/PHASE1.md](deploy/verification/PHASE1.md). Kernel tests use
owned offline TLS/inference/metadata fixtures, not external targets. They prove
routing rejection, grants/replay, protected startup, experiment compatibility,
secret permissions and recovery in the tested VM. They do not prove a production
three-VM hypervisor installation, live provider contract or scientific economics.
Unit tests inject a simulation boundary and are not OS isolation proof.

A compromised operator/root/kernel/hypervisor, runtime/TLS/broker flaw, malicious
approved service or incorrectly scoped account can undermine protection. Signed
SQLite audit does not by itself detect privileged rollback: retain off-VM backups.
Human-approved text may be harmful. Review service automation authorization and
account restrictions before deployment. Later-phase inspector judgment is not a
promise to recognize every harmful action. No zero-risk guarantee is claimed.
