# Qwenomatic security specification and evidence

This document is an inventory of enforcement, authority and evidence. It is not
an assertion that arbitrary autonomous browsing is safe. Model output, agent
explanations and externally supplied content are untrusted. The farm's economic
selection pressure is part of the threat model, not a substitute for security.

## Version and status

At publication, main is based on commit
110e49b2ebc0fa581de712ab467fabb655054859 (PR #5). PR #6
https://github.com/US0RIS/Qwenomatic/pull/6 is unmerged; its head is
a51db52a12147e026cb39c4a171bea9820b12647. Features below explicitly distinguish
those revisions. Future changes must update this inventory when merged. A draft
PR or design is not enforcement on main or on the operator's machine.

| Component | Status at publication |
|---|---|
| Kernel network boundary, strict fixed adapters, gateway and durable outbox | Implemented on main; tested in an isolated Linux VM |
| Complete venv checks, isolated experiments, mandatory external text approval, SIGKILL recovery | Implemented in unmerged PR #6; tested in a Linux VM |
| Independent external-access broker and inference proxy | Phase 1 approved for implementation; not present on main |
| Real revenue ingestion, holdback, independent refund/payout controls and budget envelopes | Phase 2 specification; not implemented |
| Controlled Chromium worker/controller and TLS inspection | Phase 3 specification; not implemented |
| Independent security inspector and delegated permissions | Phase 4 specification; not implemented |
| Full hostile-browser/broker/inspector acceptance campaign | Phase 5 specification; not implemented |

## Threat model and trust

Assume an agent may produce malicious requests, lie about its intentions, exploit
shared context, coordinate with other agents and optimize against refusals. Also
consider malicious websites, prompt injection, replay, credential theft, DNS
rebinding, request splitting, circular revenue and failures during dispatch.

On main the trusted computing base includes the OS/kernel, launcher, installed
Python/runtime, supervisor, adapters, gateway, policy engine and ledger. It does
**not** withstand a compromised supervisor: the supervisor currently holds
provider credentials and can issue arbitrary traffic to an allowed IP/port.
An append-only SQLite API does not prevent a process that owns the database
from replacing it. Moving these authorities outside the farm is the next phase.

An operator/root compromise, kernel/hypervisor exploit, vulnerable permitted
service, incorrect provider configuration or human approval of harmful conduct
can undermine controls. None of the test receipts establishes zero risk.

## Enforcement on main

### 1. Kernel egress isolation

`deploy/launch.py` creates a Linux network namespace and veth interface with a
default route. Namespace nftables input/output/forward policies drop traffic;
only configured literal IPv4/TCP address-port pairs are permitted. IPv6, UDP,
DNS and unapproved loopback destinations have no allow rules. A narrow ICMP
administrative-prohibition allowance delivers rejection evidence to the socket.

The child runs under a dedicated non-root UID with cleared groups, zero Linux
capabilities (including the bounding set), and no_new_privs. Agent output cannot
rewrite firewall rules or elevate privileges through setuid binaries.

The launcher temporarily changes host IPv4 forwarding and adds NAT. A dedicated
Linux VM is required by the deployment runbook to limit host-wide effects.
Native Windows farm execution, including simulation, refuses initialization.
Windows Ollama requires an explicitly approved private address reachable from
Linux, with separately enforced inbound firewall restrictions.

Limit: address-port filtering does not constrain HTTP methods, paths or payloads.
Direct access to Ollama's port exposes more than the chat API, including model
management endpoints. It must not be represented as application-layer isolation.

### 2. Protected operator configuration

The manifest has an explicit operator identity, approval reference, model URL
and adapter list. Missing/extra fields, unknown adapter kinds, implicit defaults,
invalid payment limits and inline endpoint credentials are rejected. Files and
ancestors must be root-owned, non-writable by group/others and non-symlinks.
The loaded manifest digest and namespace/UID must match root-created evidence.

Configured inference must match the approved local endpoint. Inline inference
API keys and cloud escalation are prohibited. New destinations, credentials or
compiled adapter kinds require an operator decision and relaunch. ACCESS_APPROVED
records operator identity, decision reference, contract, destinations and
credential fingerprints; tokens themselves must never be logged.

Limit: the main launcher resolves venv/bin/python and can miss writable venv
packages. PR #6 fixes this; main must not be described as checking a whole venv.

### 3. Live rejection proof and fail-closed behavior

The launcher creates a TCP canary, temporarily allows it and confirms a real
connection from the farm namespace. It reinstalls the final rules atomically.
NetworkBoundary then requires failure with EACCES/EPERM or Linux EHOSTUNREACH
accompanied by the socket error queue's ICMP origin, type 3, code 13.
Timeout, connection refusal and generic no-route are not accepted as proof.

Initialization, each tick, adapter queueing and outbound dispatch check the
boundary. Missing evidence, configuration mismatches and check errors stop the
farm. There is no production bypass flag. Unit tests deliberately substitute a
fixture; kernel tests run separately without that fixture.

Limit: a canary checks an actual blocked endpoint, not every possible rule or
protocol. It is evidence alongside privileged rule installation, not a complete
formal proof of the kernel's correctness.

### 4. Model output is data

The runtime parses bounded JSON output; duplicate keys and explicit non-finite
JSON constants are rejected. Tool validation rejects malformed/extra arguments
and non-finite spend. Models have no shell/script/query execution tool and cannot
load dynamic adapter implementations. Registries freeze after initialization.
Workspace outputs remain data; operators must not execute agent-written files.

This rule forbids executing model-generated programs. Future browsers may run
site JavaScript inside Chromium's sandbox; that does not authorize agent-authored
scripts, CDP calls, developer consoles or code-execution websites.

### 5. Existing gateway remains mandatory

ToolGateway verifies signed capability claims, agent/context binding, generation,
current capability epoch, policy, tool existence, schemas, rate limits and spend.
Denied requests do not reach adapters. Hard policy violations disqualify regardless
of potential profit. Real-world adapters use this same path, not a parallel tool API.

Human approvals must be granted, unused and bound to the exact agent, generation,
tool, arguments, step, epoch, policy digest and real adapter digest. Changes to
payee/config invalidate approval. Approval IDs alone confer no permission.

Limit: capability keys and policy checks live inside the trusted supervisor.
They do not protect against compromise of that process. Independent broker
permissions must not trust supervisor claims as authoritative identities.

### 6. Fixed real-world adapters

`fixed_json` accepts only text, capped at 4096 UTF-8 bytes. `fixed_payment` accepts
only positive integer amount_cents, under a configured hard cap; payee and USD
are fixed by trusted configuration. No tool accepts URL, host, account, payee,
command, provider headers or query expressions. HTTPS transport fixes the endpoint,
disables redirects/proxies and verifies the certificate against the literal IP.

Material payments (including equality with the approval threshold) require human
approval even if the policy's configurable approval list omits the class.
Threshold zero means every payment requires approval.

On main, external.fixed_write is not intrinsically approval-required. PR #6 makes
every text submission require exact-content approval independent of policy defaults.
Text can be harmful even without executable syntax; schema validation is not
content authorization or assurance about the receiving service.

### 7. Money reservations and durable outbox

Allowed invocations commit an outbound intent and full payment-spend reservation
before dispatch. No transport executes inside an uncommitted ledger transaction.
The dispatcher matches the committed TOOL_INVOKED receipt, arguments digest,
authorship, policy/epoch, manifest, current generation and active agent state.
Operator inbox commands are applied before dispatch on the next tick.

OUTBOUND_ATTEMPTED commits before network transmission. No automatic retry occurs
after crashes/timeouts/ambiguous outcomes. Reservations stay charged even if
cancelled, rejected or unknown. HTTP acceptance is not bank-settlement evidence.
An operator reconciles provider outcomes using the invocation/idempotency key.

Limit: this controls farm intent. An external relay/provider must independently
restrict payees, accounts, caps, approval and idempotency. No ready-made bank or
provider relay is implemented on main. No live provider was used in verification.

### 8. Economic evidence and ledger

Agents cannot author financial events. Trusted accounting adapters record
attributed revenue, expenses, fees and refunds; fitness derives from the ledger,
not agent claims. Event authorship checks distinguish agents, adapters, operator
and supervisor. SQLite transactions, idempotency keys, hash chaining and replay
provide integrity checks and restart recovery within the current trust model.

The shipped economy is simulated. No real settled-revenue ingestion adapter exists.
Simulated results must not be presented as externally verified revenue or profit.

## PR #6 additional enforcement (pending merge)

* Runtime checks inspect the lexical venv path, its entire tree and symlink
  targets before privileged use; system-site-package inheritance is refused.
* The launcher starts with trusted system Python -I -S. Privileged probes disable
  site loading. Farm Python loads protected site packages after privilege drop.
* Fixed launcher operations host Generation Zero, A/B and campaign scripts and
  propagate operator config and --generations; no arbitrary script/module input.
* Every external.fixed_write requires exact-request, single-use human approval.
* A root-only recovery record precedes host mutation. SIGTERM invokes cleanup.
  After SIGKILL, --cleanup takes the same host lock, kills namespace children,
  removes recorded namespace/interface/NAT/evidence, restores forwarding and
  removes the record last. Failed cleanup retains the record for retry.

## Approved phased architecture (not enforcement on main)

### Phase 1: separate broker, credentials and inference proxy

Broker: separate UID and namespace, deployed in a separate VM/kernel with a
hypervisor firewall. The farm/browser can reach only a fixed broker endpoint;
provider credentials, policy and signing keys are unreadable by the farm.
The broker checks service/account/method/path/field schemas, canonical wire
content hashes, broker-clock expiry, policy version, revocation and atomic
single-use consumption. Farm/account/destination ceilings cover requests,
concurrency, messages, purchases, bytes and spend. Claimed agent identity is
bookkeeping; authenticated farm/session identities enforce limits.

DNS is resolved by the broker and the actual connection is pinned to a validated
public IP. Private, loopback, link-local, multicast, metadata and prohibited IPv6
addresses are rejected. Host and SNI must agree. Each redirect hop requires
scope validation; no unrestricted CONNECT tunnel is allowed. Parsers need
isolation, strict framing, body/header limits and timeouts. An independent
hash-chained signed audit and kill switch revoke permissions and stop in-flight
connections/streams. A restricted inference proxy exposes only validated chat;
the inference server has independently blocked outbound access.

Acceptance requires positive controls followed by kernel-proven rejections at
farm, broker and inference boundaries. Proxy path tests complement kernel tests.

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

PR #5: 169 local tests and a successful real Linux kernel VM smoke. PR #6: 177
local tests and extended VM smoke covering experiments, writable .pth refusal
and SIGKILL recovery. Receipts/source hashes are under deploy/verification in
the corresponding revisions. Local simulation tests inject a boundary fixture;
they cannot prove OS isolation. GitHub CI was not demonstrated passing.
Deployment startup must prove its own barrier on the actual host.

No production broker, browser or inspector acceptance evidence exists on main
at publication. No live payment, revenue provider, external relay or Windows
Ollama connectivity was tested. Provider authorization/automation terms, account
permissions, TLS inspection compatibility and hypervisor/network provisioning
require operator review. Merely using a human-like UI does not establish legal
authorization. No architecture here guarantees zero legal or security risk.

Every merged security change must identify executable enforcement, configuration
requirements, test evidence, rollback/recovery and remaining limitations. Features
without applicable evidence remain implemented-but-unverified or designed.


## Reversible improvement controls

The twelve mechanisms documented in [IMPROVEMENTS.md](IMPROVEMENTS.md) introduce
no population authority. Knowledge and retirement evidence derive from trusted
adapter outcomes, never agent claims. Settings proposals are restricted to
bounded economics parameters; identified operator decisions enter a new frozen
generation without editing policy or spending ceilings. Anomaly holds are
rechecked both at payment authorization and outbound dispatch. Small-model and
autopilot actions still pass through the same capability gateway and accounting.
The red-team copy has only local simulated adapters and an ephemeral workspace;
its findings never modify executable code. Its entire fixed corpus has committed
adversarial regression tests. Telemetry uses a separate deterministic event-ID
namespace so experimental instrumentation does not perturb market random draws.

Simulation campaigns require the existing protected simulation launcher.
Unit-test receipts use the documented test-only empty boundary and do not prove
production kernel isolation. No evidence in this release promotes an economic
treatment, authorizes a real-world destination or grants an external payment.
