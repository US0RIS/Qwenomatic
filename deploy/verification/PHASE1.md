# Phase 1 verification and delivery report

This change is based on the exact contents of PR #6, head
`a51db52a12147e026cb39c4a171bea9820b12647`, which remains an unmerged prerequisite.
It is a draft implementation change, not installation on the operator's computer.

## Enforced by code

- Farm routing permits only the fixed broker; legacy direct model/provider
  manifests refuse. Fixed tools retain the existing gateway and durable outbox.
- Separate broker/inference UIDs and namespaces, cleared capabilities,
  no_new_privs, protected runtime/config/secrets and live startup rejection proof.
- Provider credentials/audit signing key are outside the supervisor; farm mTLS
  session credentials grant no provider/signing authority.
- Exact canonical operation grants through root-only IPC, short broker-clock
  expiry, policy binding, atomic one-time consumption, revocation and replay refusal.
- Fixed public HTTPS destinations with all-answer IP validation, pinned sockets,
  matching DNS certificate/SNI/Host and no redirects; no LAN/metadata/IPv6 route.
- Independent durable farm/account/destination ceilings, fixed payees/payment caps,
  no automatic retry, and conservative startup halt after unresolved attempts.
- Disposable seccomp-contained TLS/HTTP/JSON parsers, bounded framing, resources
  and deadlines; no provider/signing keys in parsers.
- Fixed chat-only proxy and inference egress isolation; no management/stream/tool
  operation. Signed independent event authorship and operator-only access events.
- Broker-private signed append-only hash chain, durable kill switch/socket shutdown,
  protected launcher recovery and cleanup without deleting authority state.

## Operator and provider requirements

Production requires distinct dedicated farm/broker/inference VMs with separate
kernels and hypervisor firewall/routing. The launchers do not install or attest
hypervisor rules. Root must provision protected code/runtime, explicit policies,
DNS pins, certificates/session identities, provider scopes, forwarding, protected
public verification keys and off-VM audit backups. Nothing enables live providers
by default. Read ../BROKER.md for exact schemas, launch, grant and recovery.

A fixed_payment provider endpoint must honor the reviewed fixed contract,
idempotency and provider-side restrictions. HTTP success is not bank settlement.
Provider-specific integration, revenue ingestion/holdback/refund/payout controls
and envelopes belong to Phase 2. Browser and inspector autonomy are later phases.

## Actual verification

The console receipts are from QEMU with Ubuntu Linux 6.8.0-138-generic, 3 GiB RAM,
two virtual CPUs and **no external network interfaces**. The fixture's TLS and
metadata/inference servers are local controlled services, never Internet targets.

- `phase1-baseline-and-kernel-console.txt`: existing actual farm startup and ledger
  proof, all fixed experiment operations, writable .pth refusal without execution,
  SIGKILL/idempotent cleanup, and broker boundary acceptance; both exit codes zero.
- `phase1-broker-console.txt`: final broker acceptance, including production
  version-2 farm launcher init/ledger proof and production inference-client health,
  mTLS positive control, independent grant, replay refusal, direct farm provider/
  inference/metadata rejection, broker metadata/canary rejection, inference egress
  rejection and farm inability to read provider/signing/config secrets; exit zero.
- `phase1-source-sha256.json`: 77 production/configuration/acceptance files compared
  byte-for-byte against the final VM rootfs before publication. The baseline
  acceptance test and its exercised production sources are unchanged between
  those successful runs. Tests/source fixtures are not substituted for kernel rules.
- `phase1-tests.txt`: full local regression and broker adversarial suite: **231 passed**.

Intentional SIGTERM at fixture shutdown produces KeyboardInterrupt tracebacks in
service-launcher logs; the acceptance process still exits zero after cleanup.
Timeout/refusal alone is not accepted as isolation evidence: live controls are
paired with Linux ICMP administrative-prohibition checks.

CI is configured to repeat both smoke tests on Linux and the cross-platform unit
suite. This report does not claim GitHub CI passed or that Windows/production
three-VM/provider deployment was verified. Linux seccomp-specific unit tests are
explicitly skipped on non-Linux hosts; that skip grants no production bypass.

## Residual risk and acceptance scope

Models cannot issue operations outside the independent configured/granted scope
through this protocol, assuming trusted broker/kernel/configuration integrity.
This is not a proof against every exploit of the broker, TLS stack, kernel or
approved service. Human-approved text can still be harmful. Broker root can alter
or roll back its storage; external backups are necessary to detect rollback.
Configuration mistakes and authorization/automation terms need operator review.
The controlled VM validates namespaces/UIDs, not separate hypervisor kernels.
No zero-risk or blanket legal guarantee is made.
