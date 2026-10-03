# Safety boundary and acceptance criteria

All production Supervisor instances, including simulation, require the Linux
launcher. Direct runs and native Windows runs now refuse to initialize. On
Windows use a dedicated Linux VM (or a correctly configured WSL2 Linux host).
There is no unprotected fallback, environment flag, or YAML bypass.

## Operator setup

Install this checkout in a root-owned directory such as /opt/qwenomatic. Keep
code, configuration, Python and its installed packages inaccessible for writing
by the farm user. Use a dedicated Linux VM: the launcher temporarily enables IPv4
forwarding and adds a uniquely named NAT table, then restores them at shutdown.
It serializes launches using a host lock. Install iproute2, nftables, util-linux,
Python 3.11+ and PyYAML. Create an unprivileged dedicated qwenomatic user. Keep
data outside the checkout, e.g. /var/lib/qwenomatic, owned by that user.

The operator must explicitly author a manifest with exactly these fields:

* version: 1
* operator: a nonempty operator identity
* approval_reference: a nonempty reference to the reviewed access decision
* model_url: null for simulation, or the exact local model base URL
* adapters: an explicit list, empty unless each adapter has been reviewed

The example manifest deliberately has invalid null approval fields. It cannot
authorize access by default. Install the reviewed manifest under /etc/qwenomatic
as root, mode 0444; its entire ancestor chain must be root-owned and not group or
world writable. Match farm.yaml's inference backend and base URL to it.

Endpoints require literal IPv4 addresses and fixed ports and paths. DNS, UDP,
IPv6, arbitrary loopback destinations, redirects and proxies are not available.
The local model must listen on an approved host LAN/private address reachable
from the namespace; host 127.0.0.1 is not the namespace's loopback. Bind the model
carefully on the host and protect its inbound access separately.

Run, for example:

    sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/launch.py \
      --manifest /etc/qwenomatic/manifest.json --user qwenomatic \
      --python /opt/qwenomatic-runtime/bin/python \
      --config-dir /opt/qwenomatic/config --data-dir /var/lib/qwenomatic

Start the launcher with the trusted system interpreter and **-I -S**, never
with a venv interpreter that has already loaded site packages as root. The
launcher checks the lexical venv path, every file/directory in its tree,
symlink targets and ancestors before executing it. Writable site-packages or
.pth files are refused. System-site-package inheritance is refused. Use an
explicit root-owned venv for --python. Root can still change trusted code;
the launcher does not protect against a malicious operator.

Use --check-only for the live proof without starting a farm, --ticks N for a
bounded run, --generations N for closed generations, or --operation init / --operation verify. status, approvals and the
read-only dashboard can read the ledger outside the namespace. approve, deny,
stop and resume queue operator requests; they are applied by the isolated
supervisor on its next tick. A stopped farm needs a launcher run to process them.

## Fixed real-world contracts

An adapter entry uses name (a unique real.* name), kind, endpoint and
credential_file. Supported kinds are compiled trusted code, never dynamic
imports, scripts, SQL or model-generated programs:

* fixed_json accepts only text (at most 4096 UTF-8 bytes). Its approved HTTPS
  service must implement a plain-text submission contract with no paid effect.
  Every external.fixed_write requires an operator approval bound to that exact
  request, even when policy.yaml omits the class. This covers review of spam,
  deceptive content and inappropriate publication; isolation cannot review content.
* fixed_payment accepts only positive integer amount_cents. Configuration must
  additionally contain payee, hard_cap_cents and approval_threshold_cents.
  Threshold equality requires approval; zero means every payment needs it.
  The HTTPS service receives amount_cents, the fixed payee, USD and an
  Idempotency-Key. This is a narrow integration contract, not a ready-made
  Stripe/bank API adapter. Review/implement the receiving service before enabling.

Credential files are root-owned JSON containing only a nonempty token, e.g.
{"token":"..."}; grant the farm user read access using its primary group and mode
0440. Never put tokens in farm.yaml, prompts, tool arguments or inline model
configuration. Root ownership, ancestor protection and credential fingerprints
are checked before registration. Credentials remain in trusted supervisor code;
the model receives only schemas and sanitized queue receipts.

Also add the exact adapter name to policy.yaml capabilities and farm.yaml
mutation.tools. Existing genotype preferences determine token scope. These
settings cannot create an adapter or a network destination. Registration is
frozen after startup. Change destinations, adapters or credentials only by an
operator-reviewed root-owned manifest/credential update and a relaunch. The
ledger records the operator, approval reference, manifest digest, destinations,
adapter contracts and credential fingerprints. No adapter is enabled by default.

## Eight requirements and receipts

| Requirement | Enforcement / evidence |
|---|---|
| External network barrier | Root-created Linux netns and nftables default-drop input, output and forwarding; exact IPv4/TCP endpoint allowlist. Farm runs non-root with zero capabilities and no_new_privs. |
| Agents cannot route | Fixed endpoints, paths, payees and credential references; strict tool schemas reject all extra arguments. |
| Payment safety | Integer-cent hard caps, mandatory material approval, existing generation/day budgets, and attributed ledger reservation before dispatch. |
| No agent code execution | Model JSON stays data; no shell/query/code adapter or dynamic adapter loader. Strict finite numeric validation and JSON parsing. |
| Fail closed | Supervisor initialization, each tick, queueing and dispatch require confirmed evidence and a kernel rejection; check errors propagate and abort work. |
| Actual blocked connection | Launcher proves its controlled TCP canary reachable from the same namespace, installs final rules atomically, then startup connects again and requires a local policy rejection or the exact ICMP administrative-prohibition code from the Linux socket error queue. Timeouts, refusals and generic no-route errors are insufficient proof. |
| Existing gateway | Real adapters use the same token, policy, rate limit, budget and TOOL_INVOKED ledger path; dispatcher accepts only committed matching gateway receipts. |
| Human access expansion | Root-owned explicit reviewed manifest, fixed compiled kinds, root-owned credentials, frozen registration, operator-attributed ACCESS_APPROVED receipt. |

Payments queue a durable intent and reserve the full amount as external spend.
Dispatch occurs at the start of the next tick, after queued operator commands.
No HTTP request is sent from within an uncommitted ledger transaction. Policy
changes, capability revocation, halted/retired agents or configuration changes
cancel pending dispatch. A durable OUTBOUND_ATTEMPTED marker precedes sending.
There are no automatic retries after errors, crashes or ambiguous results.

Reservations remain charged conservatively even when a send is rejected,
cancelled or ambiguous. OUTBOUND_RESULT records accepted, rejected/unknown or
cancelled status. An accepted HTTP status is the approved service's acceptance,
not independent bank-settlement evidence. The operator must reconcile unknown
results against that service using the recorded invocation/Idempotency-Key.
Do not manually retry until the external outcome is established. Budget
reservations are deliberately not released automatically.

## Verification

Run python -m pytest for simulation, gateway and adversarial tests. Those tests
explicitly inject a simulated boundary through a pytest fixture; they are not
kernel-isolation evidence and cannot enable a production bypass.

The separate Linux kernel-safety CI job runs deploy/kernel_smoke.py as root in a
root-owned installation. It uses the actual launcher and farm, no mocks, and
asserts live positive control, kernel rejection, ledger integrity and operator
approval attribution. It also proves unisolated startup and a writable manifest
are refused. A successful kernel-safety job is required before claiming OS
isolation has been verified on that host. Startup repeats the proof on every run.

The actual acceptance test has also passed in a separate Linux VM. See
[deploy/verification/README.md](deploy/verification/README.md) for the console
transcript, tested source hashes and the distinction between VM proof and CI status.

The barrier protects against model output and the supplied agent tools. A
compromised root/kernel, operator-approved service that behaves outside its
contract, or modified trusted supervisor code is outside that boundary. Existing
workspace tools confine agents to their own data directories; do not execute
files written by agents.

## Experiments and Windows workflow

The protected launcher supports --operation evolution-ab, --operation campaign
(with --pairs N), and --operation generation-zero, all with --generations N.
Each fixed entry point runs after dropping privileges inside the same verified
namespace. All experiment arms load --config-dir; select the model, endpoint,
concurrency and seeds there. The A/B control still overrides evolution settings.
No arbitrary script path, module or command is accepted. For example:

    sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/launch.py \
      --manifest /etc/qwenomatic/manifest.json --user qwenomatic \
      --python /opt/qwenomatic-runtime/bin/python \
      --config-dir /opt/qwenomatic/config --data-dir /var/lib/qwenomatic/ab \
      --operation evolution-ab --generations 2

For E13-style generation runs use --operation run --generations N with the
operator's E13 configuration. There is no separate E13 entry point in this
checkout. generation-zero prints the generation audit and evidence.

Native Windows simulation is also refused. Run the farm and experiments in a
dedicated Linux VM or WSL2 installation with working netns/nftables. A Windows
Ollama server needs a private host address reachable from Linux and the farm
namespace. Set its bind address deliberately, allow only that private interface
and VM/WSL source in the Windows inbound firewall, and approve that exact
literal IP/port in the manifest and farm.yaml. Do not expose Ollama on a public
interface. Windows tuning/benchmark scripts remain operator utilities; they
do not launch an isolated farm. Existing native farm examples are superseded
by the protected Linux command above. Address changes require manifest review.

## Payment relay is a separate trust boundary

TLS certificates must cover the literal IP endpoint. Many provider APIs require
DNS names, so this contract generally needs an operator-managed relay with an
IP certificate. This repository does not implement or verify a provider relay.
The farm boundary cannot enforce what that external service does afterward.
Before enabling fixed_payment, the relay must independently fix its provider
endpoint, account and payee; reject unexpected fields, currencies and amounts;
enforce hard per-payment and cumulative caps; enforce material approval;
store provider credentials outside the farm; authenticate requests; and durably
deduplicate Idempotency-Key values before calling the provider. Bind approval
to the exact payment and use a separate authenticated operator approval path,
not a model-supplied boolean. Reconcile ambiguous provider outcomes without
blind retries. Test these controls at the relay before approving its destination.
Farm-side caps alone are not end-to-end payment safety evidence.

## Interrupted launcher recovery

Before any host network mutation, the launcher writes a root-only recovery
record under /run with its exact namespace, interface, NAT table, original
forwarding value and (once allocated) evidence path. A new launch refuses while
this record exists. SIGTERM triggers normal cleanup. After SIGKILL run:

    sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/launch.py --cleanup

Cleanup takes the same exclusive host lock, kills surviving namespace children,
removes only the recorded resources and evidence, restores forwarding, and
removes the record last. It is repeatable when some resources are already gone;
failed cleanup retains the record for retry. Do not change forwarding manually
while a run owns it. After a reboot, namespaces, nft rules and /run state are
normally gone; forwarding comes from the host's boot configuration. Cleanup
cannot run while a process still holds the lock. Use a dedicated VM to keep
these host-wide forwarding changes away from other workloads.
