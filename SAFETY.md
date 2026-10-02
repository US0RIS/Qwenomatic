# Qwenomatic real-world safety boundary

Qwenomatic treats population agents as untrusted optimizers. In simulation, the
only external world is the deterministic in-process market. The moment
Qwenomatic is given a real-world adapter, it must run in **strict mode**.

Strict mode is intentionally inconvenient to widen. The safe path is:

```text
model output (JSON data only)
        |
        v
AgentRuntime parser
        |
        v
existing ToolGateway
  capability token
  policy engine
  per-call + agent + farm budgets
  exact human approval when required
  append-only attribution
        |
        v
durable external-action record
        |
        v
existing ToolGateway again
        |
        v
ConfiguredExternalTool
        |
        v
safety-gateway container
  verifies the same token + safety fingerprint
  fixed adapter + fixed destination
  fixed payee/currency for payments
  credential fingerprint check
  hard payment cap
        |
        v
approved upstream only
```

The population container has no WAN/default egress route. It is attached only
to Docker's `internal: true` network with the safety gateway. Provider
credentials are mounted only into the gateway container, not Qwenomatic.

## Acceptance criteria

| # | Requirement | Implementation |
|---|---|---|
| 1 | Barrier outside model reach | `deploy/strict/docker-compose.yml` attaches Qwenomatic only to an internal Docker network. The safety gateway is a separate container. Strict startup performs an actual forbidden TCP probe. |
| 2 | Agents never choose traffic destinations | `config/adapters.yaml` owns fixed URL/method. Agent schemas may not expose URL, host, endpoint, destination, route, payee, account, credential, command, script, code, query, SQL, or execution fields. The gateway rejects them recursively too. |
| 3 | Money only through approved channels | Payment adapters have a fixed payee and currency, a policy-matched hard per-action cap, agent/farm budgets, pending-spend reservation, and a material-spend approval threshold. Provider credentials exist only in the broker. |
| 4 | No code execution for agents | Model output is parsed as JSON data and routed only to typed tools. There is no shell/exec adapter. External adapter schemas cannot accept command/script/code/query fields. Workspace writes remain inert data. |
| 5 | Fail closed | In strict mode, missing human seal, gateway error, credential mismatch, policy mismatch, model-route mismatch, adapter mismatch, or successful forbidden egress prevents startup or dispatch. |
| 6 | Prove the barrier | `deploy/strict/acceptance.py` opens real sockets from inside the population container. It requires public egress and direct model bypass to fail, while the safety gateway and brokered local model remain reachable. |
| 7 | Every adapter uses the existing gateway | Real-world tools are registered in the same `ToolRegistry`, require the same capability token, pass the same `ToolGateway` twice (request + dispatch), and use the same budgets and append-only attribution. |
| 8 | Widening is human-only | Adding/changing an adapter, destination, payee, credential SHA-256, policy capability, model route, or safety setting changes the safety fingerprint. Strict startup requires an operator-authored `safety_config_approved` ledger event for that exact fingerprint. |

## Sealed adapter format

The shipped `config/adapters.yaml` is empty. That is deliberate: there is no
real-world authority by default.

A real adapter is declarative. Example only:

```yaml
version: 1
adapters:
  approved-vendor-payment:
    tool: payments.approved_vendor
    description: Pay the one vendor approved by the operator.
    action_class: spend.material
    kind: payment

    args_schema:
      amount: number
      memo: str
    optional_args: [memo]

    destination:
      url: https://provider.example/v1/payments
      method: POST
      timeout_seconds: 20

    credential:
      file: /run/qwenomatic-secrets/provider-token
      sha256: <sha256-of-the-exact-secret-file>
      header: Authorization
      prefix: "Bearer "

    payment:
      amount_field: amount
      payee: vendor-account-fixed-by-operator
      currency: USD
      hard_cap_per_action: 25
      material_threshold: 5

    response_fields: [status]
    reference_field: id
```

The matching tool must also appear in `config/policy.yaml`:

```yaml
capabilities:
  payments.approved_vendor:
    rate_limit_per_tick: 1
    max_spend_per_call: 25
    material_spend_threshold: 5
```

The policy cap must exactly match the broker hard cap; the policy materiality
threshold may be stricter but not looser.

### What agents may choose

Agents may choose only the fields in `args_schema`. For the payment example,
that is an amount and an inert memo. They cannot choose:

- URL, hostname, route, endpoint, or redirect;
- payee, recipient, account number, wallet, or currency;
- credential, API key, token, or secret;
- command, executable, script, code, SQL, GAQL, query, or shell input.

The broker adds the fixed payee/currency and credential after the request has
already passed the supervisor gateway.

## Human sealing procedure

Any authority change requires the farm to be stopped.

1. Edit `config/adapters.yaml` and, if adding a tool, `config/policy.yaml`.
2. Put any provider credential in `deploy/strict/provider-secrets/` (never in git).
3. Compute its SHA-256 and put that hash in the adapter's `credential.sha256`.
4. Inspect the destination, payee, hard cap, material threshold, and argument schema.
5. Record the decision:

```bash
docker compose -f deploy/strict/docker-compose.yml run --rm --no-deps \
  qwenomatic safety-approve --operator YOUR_NAME \
  --note "approved fixed vendor payment adapter"
```

6. Check:

```bash
docker compose -f deploy/strict/docker-compose.yml run --rm --no-deps \
  qwenomatic safety-status
```

A subsequent change to any sealed authority produces a different fingerprint.
The old approval no longer starts the farm, old capability tokens fail
verification, and an old pending approval cannot be used under the new policy.

## Payment failure semantics

Real money is treated more conservatively than an ordinary HTTP request.

1. An agent request first passes `ToolGateway`.
2. If material, it waits for a human approval bound to the exact request digest
   and exact safety fingerprint.
3. The approved request becomes a durable `external_action_requested` ledger
   event. Its amount is now reserved against budgets.
4. On a later supervisor phase the exact action is re-authorized through
   `ToolGateway`.
5. Before network I/O, Qwenomatic commits `external_action_dispatching`.
6. The broker sends the fixed request with the durable action ID as an
   idempotency key.
7. A confirmed provider result and the corresponding settled-spend ledger event
   are committed atomically.
8. A timeout, connection loss, provider 5xx, or supervisor crash after the
   dispatch marker leaves the action `uncertain` or `dispatching`. Its money
   remains reserved. Qwenomatic never automatically retries it.

An operator verifies the provider and resolves it with:

```bash
qwenomatic external-actions
qwenomatic external-confirm ACTION_ID --reference PROVIDER_REFERENCE
# or
qwenomatic external-reject ACTION_ID --note "provider confirms no charge"
```

Those decisions are operator-authored ledger events.

## Strict startup attestation

Before the supervisor creates new capability authority or runs population work,
strict mode requires all of the following:

- the current safety fingerprint has an operator approval receipt;
- `safety-gateway /healthz` says it is in sealed mode;
- the broker reports the same adapter-config hash;
- the broker reports the same safety fingerprint;
- the configured local-model upstream hash matches;
- every configured credential file exists and matches its approved SHA-256;
- a real direct TCP connection from Qwenomatic to the forbidden probe fails;
- the OpenAI-compatible model base URL is exactly the safety gateway's
  `/model/v1` path.

An exception or inconclusive check is a startup failure, not a warning.

## OS/container proof

Run the deployment acceptance test after starting Ollama (or the configured
local model) on the host:

```bash
cd deploy/strict
docker compose build
docker compose run --rm --no-deps qwenomatic safety-approve --operator YOUR_NAME
docker compose up -d safety-gateway
./acceptance.sh
```

The final line must be:

```text
STRICT SAFETY ACCEPTANCE: PASS
```

The test is valid only because it runs inside the same container/network
namespace as the population. It does not inspect the Compose file and infer
that egress is blocked; it attempts the connections.

## Status vocabulary

Application-level safety controls can be called verified when the automated
test suite passes. The OS/network boundary must additionally pass the strict
Docker acceptance test on the target host. Until that receipt exists, the
correct status is **implemented, host verification pending**.

Simulation does not imply real-world safety verification.
