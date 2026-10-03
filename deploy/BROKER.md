# Phase 1 broker deployment

This branch depends on PR #6. It intentionally rejects its direct-access v1
manifests when they contain a real adapter or inference endpoint. Empty v1
simulation manifests still work. Do not migrate a running service silently.

## Trust domains

Use three dedicated Linux VMs: farm, broker and inference. Enforce the same
allowlist at each hypervisor firewall as in each VM's namespace firewall. A
namespace isolates network routing but shares the kernel; it is not a substitute
for those VM boundaries. The privileged launchers manage namespaces inside their
own dedicated VMs. Nothing installs or verifies your hypervisor firewall for you.

* Farm: root-owned checkout, whole root-owned runtime, dedicated farm UID. Its
  only TCP destination is the broker's fixed address and port. There are no
  provider keys or broker/inspector/revenue signing keys here. It has only a
  short-lived mTLS client session key and trusted public verification keys.
* Broker: distinct UID and primary group, root-owned policy and private keys,
  private broker-owned SQLite state. Its network permits explicit pinned public
  provider IPs on TCP 443 and, optionally, the fixed inference endpoint. It
  accepts only explicit source IPs. No general DNS, UDP or IPv6 route is enabled.
* Inference: third UID, private VM, no egress allow rule. Only the broker may
  connect to its fixed listening port. Preinstall the model as an operator. The
  farm cannot reach Ollama directly, including `/api/pull`.

The controlled smoke test runs these trust domains as separate UIDs/namespaces
inside one disposable VM so the kernel checks are reproducible offline. It does
not demonstrate hypervisor isolation or a production three-VM installation.

## Operator installation

Install `cryptography>=46`, PyYAML and libseccomp in a root-owned runtime; use
system Python with `-I -S` for the privileged launchers. The Python executable,
whole virtualenv, checkout and ancestors must be root-owned and not writable by
service/farm users. A writable package or `.pth` file refuses startup.

Create distinct non-root users/groups. Broker secret files must be root-owned,
mode `0440`, with the broker primary group; use a root-owned `0750` parent with
that group. This applies to provider credentials, audit signing key, TLS server
key and broker policy/service configuration. Public CA/server certificates can
be `0444` in protected directories. The farm client private key is a **session
credential only**, readable by the farm but writable only by root. Do not reuse
it for provider authentication. Its certificate SHA-256 DER fingerprint maps to
one configured farm; never accept an agent-supplied identity as authority.

Create the broker state directory owned by the broker, mode `0700`, under
protected ancestors. Install root-owned `/etc/qwenomatic/provenance.json` in the
farm with the public Ed25519 key(s), e.g. `{"broker":"<64 lowercase hex>"}`.
Only provision inspector/revenue public keys when those services are installed;
no corresponding private key belongs in the farm. Broker audit signing keys are
raw 32-byte Ed25519 private keys; certificates/other keys use PEM.

Explicitly provision IPv4 forwarding in each dedicated VM. The service launcher
refuses when it is off and never changes this host-wide setting. Its private
`/30` subnet, root-approved peers, listen endpoint and program paths have no
production defaults. Ensure that addresses are routed between your VMs and that
NAT-adjusted source IPs match the inbound peer allowlist. Do not expose a service
on an Internet-facing interface.

A broker deployment file has exactly `service`, `subnet`, `peers`. `service` is
an absolute path to protected JSON. That service file has exactly:

* `policy`: `version`, `operator`, `approval_reference`, `farms`, `services`.
* `signing_key`, `tls_cert`, `tls_key`, `client_ca`: absolute protected paths.
* `listen`: the explicit namespace child address and TCP port.
* `state_dir`: private absolute path; `slots`: bounded frontend concurrency.
* `inference`: null, or the strict inference schema below.

Every farm has `certificates` (explicit fingerprints) and `limits`. Every service
has `kind` (`fixed_json` or `fixed_payment` only), `endpoint` (HTTPS 443), `account`,
`credential_file`, `payee`, `hard_cap_cents`, `limits`, `farms`. `fixed_json`
requires null payee/zero payment cap. `fixed_payment` requires a fixed payee and
positive integer cap. Credential JSON has exactly `token`, a nonempty printable
ASCII bearer token. Never put a token in a farm manifest, URL or environment.

Every limits object explicitly includes integer `requests`, `messages`,
`purchases`, `bytes`, `spend_cents`, `concurrency`, `period_seconds`. Costs are
reserved atomically before transport. Fixed periods use broker time; backwards
clock movement refuses operations. Matching accounts and matching destination
hosts share the same configured limits and cumulative counter across farms.
The farm-wide counter covers every configured external service. Inference uses a
separate shared durable request ceiling, input/output bounds and concurrency
ceiling; it grants no Internet permission.

The inference schema is exactly `endpoint`, `model`, `max_tokens`,
`max_messages`, `max_content_bytes`, `requests_per_period`, `period_seconds`,
`concurrency`. The endpoint is literal IPv4 HTTP with an explicit port and the
exact path `/v1/chat/completions`. Requests use one fixed model, text-only
messages and bounded numeric fields. Tools, streaming, URLs/images and other
management routes are rejected. An inference service deployment has the same
outer fields and an inner service file with exactly `listen`, `executable`;
the operator-installed executable is started with the fixed argument `serve`.

Resolve approved DNS names as an operator and install protected `/etc/hosts`
pins in the broker VM. At launch and each connection, every resolved IPv4 answer is
checked; AAAA records are not requested or routed; IPv6 and all non-global IPv4 are refused, including LAN, loopback,
metadata and mapped addresses. A changed answer outside the installed firewall
refuses the operation. Names without a usable protected DNS/hosts resolution
fail closed. DNS rotation requires an operator relaunch, never an automatic
firewall expansion. TLS connects to the validated IP, verifying the DNS name in
the certificate; SNI and HTTP Host use that same configured name. All redirects
are denied, including same-host redirects.

Launch inference and broker with explicit arguments, for example:

```sh
sudo /usr/bin/python3 -I -S deploy/service_launch.py --role inference \
  --config /etc/qwenomatic/inference-deploy.json --user qinfer \
  --farm-user qfarm --python /opt/qwenomatic-runtime/bin/python
sudo /usr/bin/python3 -I -S deploy/service_launch.py --role broker \
  --config /etc/qwenomatic/broker-deploy.json --user qbroker \
  --farm-user qfarm --python /opt/qwenomatic-runtime/bin/python
```

Run each as a separately managed service. `--check-only` installs the boundary,
proves a live canary succeeds before protection, then proves administrative
rejection after protection as the unprivileged UID, and cleans up.

The farm uses a version **2** manifest with exactly `version`, `operator`,
`approval_reference`, `model_url`, `adapters`, `broker`. `broker` has exactly
`host`, `port`, `ca_file`, `client_cert`, `client_key`, `audit_public_key`.
`model_url` is null for simulation or exactly `https://<host>:<port>/v1`.
Text adapters have `name`, `kind`, `service`; payments also have `payee`,
`hard_cap_cents`, `approval_threshold_cents` as bookkeeping/gateway controls.
The broker independently owns the actual payee and hard cap. The farm manifest
has no provider destination or provider credential path. Launch the farm using
`deploy/launch.py` and the protected experiment options from PR #6.

## Independent authorization

Every external action currently needs both the existing gateway authorization
and a separate root-operator broker grant. No inspector delegation exists in
Phase 1. Root sends a length-prefixed canonical JSON request to
`<state_dir>/operator.sock`. It is `0600` inside `0700` state; SO_PEERCRED accepts
only UID 0. This IPC is not an agent tool and is not reachable over the network.
The operator request schema is one of:

* `action=grant`, `farm`, `request`, `expires`, `operator`.
* `action=revoke`, `farm`, `invocation_id`, `operator`.
* `action=halt`, `operator`.

`request` contains exactly `service`, `args`, `invocation_id`. Use the same
invocation ID from the farm's durable outbound intent. Grants expire within five
minutes, use broker time and bind the policy hash and canonical wire content,
including method/path/account/fixed payee. Text accepts only `text` (4096 UTF-8
bytes); payment accepts only integer `amount_cents`. No URL, host, payee, command
or arbitrary HTTP headers can be supplied. A mismatched or consumed grant is
refused before credentials or network are used. Revocations and budget failures
never make an attempted request retryable. Provider rejection/disconnection is
ambiguous and requires operator reconciliation; automatic retries do not exist.

The kill switch durably halts all new work, revokes every grant and shuts down
tracked inbound/upstream sockets. WebSockets and streams cannot be opened in
this protocol. A broker restart with an unresolved attempt halts rather than
resetting authority. A persisted halt remains halted. No autonomous unhalt or
policy migration endpoint exists. Changing policy on an existing database
refuses startup; plan an operator-reviewed migration retaining prior audit and
idempotency evidence. Do not delete a database to clear budgets or uncertainty.

Broker receipts are Ed25519-signed, append-only, hash-chained and stored in
broker-private SQLite WAL with synchronous FULL. The farm's event store accepts
broker/inspector/revenue authorship only with a matching independently verified
signature. Farm-local agent attribution is bookkeeping, not a security ceiling.
A farm that can replace its own SQLite file cannot thereby forge broker receipts
or change broker ceilings. Broker root/kernel compromise remains a trust limit;
keep protected audit backups outside the VM for rollback/truncation detection.

## Parser containment and recovery

TLS/HTTP frontends start as fresh disposable interpreters, with no provider keys
or audit signing key in memory. They load only TLS server material, then apply a
mandatory libseccomp allowlist and resource limits. They cannot open files,
create sockets, connect, fork or exec. Their one accepted socket and fixed pipes
carry bounded data. JSON workers use an even smaller syscall allowlist. Missing
seccomp or parser failures refuse a request. TLS 1.3/mTLS is mandatory. Input is
limited to one HTTP request per connection, 8192-byte headers, 65536-byte bodies,
strict duplicate-free JSON, fixed Host, no Transfer-Encoding and no unknown
headers. Frontends have a 12-second deadline; upstream connections have a
10-second total deadline and 5-second socket timeout. Provider response handling
reads only a bounded status line, never external headers or content into the
farm. This reduces exposure; it does not prove TLS libraries have no bugs.

SIGTERM and ordinary errors clean up namespaces, child processes, NAT and
namespace evidence. SIGKILL requires the root-only recovery command:

```sh
sudo /usr/bin/python3 -I -S deploy/service_launch.py --role broker --cleanup
sudo /usr/bin/python3 -I -S deploy/service_launch.py --role inference --cleanup
```

A root-owned recovery record is written before network mutation. Startup refuses
an interrupted record; cleanup kills namespace children before removing its
resources. Persistent broker state/audit is preserved. Locks prevent two
launchers from managing the same service role simultaneously. Reboot clears
`/run`; the persistent database still conservatively preserves grants, usage,
attempts and halt. A stale operator socket is replaced only inside protected
broker state after the exclusive launcher lock is held.

## Evidence

Run the full Python suite and both root-only VM smoke tests:

```sh
python -m pytest
sudo /opt/qwenomatic-runtime/bin/python -I deploy/kernel_smoke.py \
  --python /opt/qwenomatic-runtime/bin/python
sudo /opt/qwenomatic-runtime/bin/python -I deploy/broker_kernel_smoke.py \
  --python /opt/qwenomatic-runtime/bin/python
```

The broker smoke is destructive **only to its disposable VM fixture**: it
installs temporary test IPs, CA trust and hosts entries, then restores them. It
requires `qbroker`, `qinfer`, `nobody`, iproute2, nftables, setpriv, `/usr/bin/test`
and libseccomp. It never contacts an external server. Live positive controls are
paired with ICMP administrative-rejection evidence; timeout/refused connections
alone are not accepted as network safety proof. Unit tests cover replay races,
canonical mutation, shared ceilings, expiry/revocation, restart, backwards clock,
forged authorship, schema attacks, smuggling, DNS rebinding and syscall denial.

## Operator command utility and review disposition

Use the root-only helper rather than constructing IPC manually. Prepare an
immutable root-owned JSON request file containing exactly `service`, `args`,
`invocation_id`, using the invocation ID in the farm's durable outbound intent.
Review the configured service, account and actual content before granting.

```sh
sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/operator.py \
  --config /etc/qwenomatic/broker.json --operator owner \
  grant --farm farm --request-file /etc/qwenomatic/action.json --ttl 60
sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/operator.py \
  --config /etc/qwenomatic/broker.json --operator owner \
  revoke --farm farm --invocation-id exact-invocation
sudo /usr/bin/python3 -I -S /opt/qwenomatic/deploy/operator.py \
  --config /etc/qwenomatic/broker.json --operator owner halt
```

The helper validates protected config/request files, broker-private state and
socket permissions, then checks the Unix peer UID. The broker independently
checks root caller credentials and all grant scope/expiry. A returned refusal
exits unsuccessfully. TTL is 1..300 seconds; the broker's clock remains expiry
authority. This helper is operator software, not a model-exposed tool. No new
credential, executable adapter or autonomous access expansion is added.

The following deliberate limits were reviewed before merge:

* **Fixed periods, not rolling windows.** A cap C permits up to 2C in a short
  interval straddling a period boundary. For example, 100 cents/hour can permit
  100 cents at 12:59:59 and another 100 at 13:00:00. Configure caps/float for this
  burst exposure. Concurrency and all other scope checks still apply. A rolling
  exposure envelope is a separate money-flow change, not silently claimed here.
* **Conservative farm accounting.** Broker refusals before dispatch are currently
  treated as unknown by the farm gateway, and the local expense reservation stays
  charged. This prevents unsafe refunds/retries but may exhaust a budget despite
  no send. Signed pre-attempt refusal reconciliation belongs to a subsequent
  accounting change; do not erase broker/farm state to recover capacity.
* **Audit scale.** Each inference request signs/fsyncs audit; startup verifies the
  whole chain in O(number of records). The acceptance test does not establish
  sustained production throughput or startup performance at large volume. Keep
  audit backups; measured, operator-reviewed retention/anchoring is needed before
  changing this persistence guarantee.
* **TLS frontend trust.** The main broker receives authenticated certificate
  fingerprints from its disposable frontend. Seccomp removes credentials and
  broad syscall authority, but does not prove the frontend/TLS library cannot be
  compromised or falsify that report. Treat it as part of the broker's trusted
  authentication implementation, not an independently proven identity oracle.
* **Clock and rotation.** A backward wall-clock step can refuse broker startup
  and operations. Restore correct time conservatively; never reset persistent
  authority/usage to work around it. Provider IP rotation outside installed pins
  requires operator relaunch. Off-VM backups, not the SQLite chain alone, detect
  privileged rollback/truncation.

The inference template option accepts only `chat_template_kwargs` with exactly
one boolean `enable_thinking`; arbitrary template parameters remain prohibited.
