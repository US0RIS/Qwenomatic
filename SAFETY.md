# Safety boundary and acceptance criteria

This Phase 1 branch depends on PR #6. Production Supervisor instances, including
simulation, require the protected Linux launcher. There is no native Windows
fallback. Use dedicated Linux VMs; WSL2 alone does not establish separate kernels
for farm, broker and inference. No bypass flag or agent-controlled configuration
can waive startup verification.

## Trust boundaries and operator setup

The farm can reach only one fixed broker IPv4/TCP endpoint. Provider credentials,
external destinations, policy, authorization state and signing keys live in a
separate broker UID/namespace. Production deployment requires a separate broker
VM/kernel and hypervisor firewall, and a third restricted inference VM. Namespace
isolation shares a kernel; the acceptance fixture is not a three-VM deployment.

Follow [deploy/BROKER.md](deploy/BROKER.md) for installation, exact configuration
schemas, certificate provisioning, secret permissions, launch and recovery.
Use trusted system Python with `-I -S` for privileged launchers. They check the
whole root-owned venv tree, symlink targets and installed packages before use;
service Python loads packages only after privilege drop. Dedicated service UIDs
have zero capabilities, empty supplementary groups and no_new_privs.

Farm manifests are version 2 and contain an explicitly approved broker endpoint
and public verification material, with no provider credentials or destinations.
The farm has a limited mTLS session key; it is not a provider or signing key.
Version 1 is accepted only for empty simulation (no model URL or real adapter).
Legacy direct-access manifests refuse; migration is an operator decision.

The broker resolves approved DNS names using protected operator pins, validates
all answers and connects to a checked public IPv4 address while verifying the
configured DNS certificate name. Host and SNI agree. Private, loopback,
link-local, multicast, metadata, IPv6 and addresses outside installed rules
refuse. No DNS/UDP egress is granted. All redirects refuse, including same-host
redirects. The only private outbound exception is an explicitly configured
inference endpoint exposing the fixed chat operation. The inference namespace
has no outbound allow rule. Hypervisor firewall/routing remains operator setup.

## Fixed tools and independent authority

Real adapters remain compiled fixed_json/fixed_payment tools through the existing
gateway: capability, epoch/context binding, schemas, policy, rate/budget checks,
ledger attribution and durable outbound reservation. No URL, account, payee,
headers, command, script, query or executable adapter is model-selectable.
The supervisor submits canonical data to the broker instead of doing provider
transport. Text accepts only bounded text; payment accepts only integer cents.

Every external call also needs a separate root-operator broker grant, issued
through protected local IPC with SO_PEERCRED UID 0. A supervisor approval alone
has no broker authority. Grants bind authenticated farm identity, fixed service,
account, POST method, path, canonical body hash, idempotency key, broker policy
hash and expiry (at most five minutes). Consumption, attempts and cumulative
reservations commit atomically before network. Changed, expired, revoked or
replayed requests refuse. The broker never trusts claimed agent identity for
security ceilings. Session certificates map to explicit farms.

The broker enforces independent request, concurrency, message, purchase, byte
and spending ceilings across each farm, shared account and shared destination.
Counters survive restart and backward clocks refuse. Payment payees/caps are
broker-owned. All external actions need broker operator approval in Phase 1;
there is no autonomous inspector delegation. The farm's material-payment and
mandatory exact-text approval checks remain additional controls.

Model output stays validated data. Neither tools nor broker execute model code.
Registries freeze. Agent-produced workspace files must never be executed.

## Audit, failure and shutdown

Broker audit is append-only through SQLite triggers, hash-chained and Ed25519
signed in broker-private storage with WAL and synchronous FULL. The farm can
import only independently verified broker/inspector/revenue evidence. Inspector
and revenue authorship support does not implement those later-phase services.
ACCESS_APPROVED remains operator-only. Credentials are excluded from receipts.

The operator kill switch durably revokes grants, halts new activity and closes
tracked inbound/upstream sockets. This protocol has no WebSocket or streaming
operation. A persisted halt survives restart. Unresolved attempted calls halt
broker startup; unknown provider outcomes remain charged and cannot be retried
automatically. Changing policy against existing broker state refuses rather
than resetting budgets. Operator reconciliation/migration must preserve evidence.
An HTTP success is service acceptance, not bank settlement.

TLS/HTTP parsing runs in fresh disposable unprivileged workers with no provider
or audit keys. After TLS material is loaded, mandatory seccomp forbids file opens,
new sockets/connections, fork and exec. JSON workers have a smaller syscall
allowlist. Duplicate fields, smuggling, transfer encoding, unknown headers/routes
and invalid values refuse. Header/body, CPU/memory, connection and upstream
deadlines are bounded. Parser or inspection errors never authorize a call.

Launchers record recovery state before network changes and serialize through
locks. SIGTERM cleans up. Root-only `--cleanup` recovers interrupted namespaces,
children, NAT and evidence after SIGKILL. Broker/inference launchers never change
host-wide forwarding; operators provision it. The farm launcher retains its
PR #6 forwarding restoration. Persistent authority/audit is preserved.

## Eight requirements and Phase 1 evidence

| Requirement | Enforcement |
|---|---|
| External barrier | Farm, broker and inference namespaces with default-drop nftables, separate unprivileged UIDs/capability removal; separate production VMs and hypervisor firewalls required. |
| Agents cannot route | Farm reaches one broker; broker fixes service/account/method/path/payee and validates/pins provider addresses. |
| Payment safety | Farm gateway caps/budgets plus independent broker grants, fixed payee, hard cap, cumulative ceilings and durable attempt records; provider contract review remains required. |
| No agent code execution | Strict data schemas, compiled fixed adapters, frozen registry; no shell/query/dynamic loader. |
| Fail closed | Missing/mismatched evidence, kernel probe errors, invalid permissions/configuration/parser failures refuse; no fallback. |
| Actual blocked connection | Launchers prove a live canary first, then require kernel administrative rejection as the service UID. Controlled VM tests additionally exercise real TLS, metadata and inference listeners. |
| Existing gateway | Fixed adapters retain capability/policy/budget/ledger path; broker grants add independent authority. |
| Human access expansion | Protected operator manifests/policy/secrets, fixed compiled types, operator-only access events and local grant IPC; no inspector delegation yet. |

## Experiments, verification and remaining limits

Protected run/init/verify, Generation Zero, evolution A/B and campaign operations
remain available with --generations and reviewed operator configuration; no
arbitrary entry point is accepted. A/B and campaign fixtures verify compatibility,
not scientific results. Existing Linux/Windows examples granting the farm direct
Ollama access are superseded: configure the protected broker chat endpoint.

Run the full suite and both controlled root-only kernel acceptance tests as
listed in deploy/BROKER.md. Unit tests use explicit simulation fixtures and do
not prove routing. VM acceptance pairs live positive controls with exact ICMP
administrative-prohibition evidence; timeouts/refusals do not count. Receipts
and source hashes belong under deploy/verification. Startup rechecks each host.

Residual risk includes compromised root/kernel/hypervisor, trusted broker code or
TLS/runtime vulnerabilities, audit rollback by the broker's privileged owner,
incorrect hypervisor/provider/account setup and harmful content approved by a
human. Keep audit backups outside the broker VM for truncation/rollback detection.
Approved APIs can be vulnerable; schema and destination controls do not prove
all accepted requests harmless or legally authorized. Review service automation
permissions and account scope before deployment.

Phase 1 implements the fixed payment contract but not provider-specific settlement,
refunds, revenue ingestion, holdback, payout envelopes or account provisioning.
Those are Phase 2. Browser UI automation and TLS inspection are Phase 3; autonomous
inspector grants are Phase 4; the full adversarial campaign is Phase 5. None is
implied by this branch, and no zero-risk guarantee is claimed.
