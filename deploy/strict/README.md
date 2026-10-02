# Strict real-world deployment

This Compose stack is the supported topology for giving Qwenomatic any
real-world adapter.

## Topology

- **qwenomatic** — unprivileged, read-only root filesystem, no Linux
  capabilities, attached only to `farm_internal`, which Docker marks
  `internal: true`. It mounts the ledger/data volume and capability secret,
  but no provider credentials.
- **safety-gateway** — separate trusted process. It is attached to both
  `farm_internal` and `broker_egress`, validates the same capability tokens,
  and is the only component that sees provider credentials.
- **safety-init** — creates the shared capability-signing secret once in a named
  volume. It is infrastructure setup, not agent-controlled authority.

The local model remains on the host at the fixed
`safety.model_upstream` from `config/farm.yaml`. Qwenomatic itself cannot
connect there directly; it uses `http://safety-gateway:8787/model/v1`.

## First run

Start the local Qwen/Ollama server first. The shipped model name can be
overridden for both trusted processes with `QWENOMATIC_MODEL`.

Then:

```bash
cd deploy/strict
docker compose build
docker compose run --rm --no-deps qwenomatic safety-approve --operator YOUR_NAME
docker compose up -d safety-gateway
./acceptance.sh
docker compose up -d qwenomatic
```

The farm intentionally refuses to start if the safety approval was made under
different effective deployment settings.

## Acceptance

`acceptance.sh` runs `acceptance.py` inside the Qwenomatic service image and
network. It verifies:

1. the safety gateway is reachable;
2. its sealed health attestation is valid;
3. direct public egress to `1.1.1.1:443` fails;
4. a direct connection to the host model on port 11434 fails;
5. the local model remains reachable through the gateway.

Do not enable real-world adapters unless the script prints
`STRICT SAFETY ACCEPTANCE: PASS`.

## Provider credentials

Place credential files in `provider-secrets/`. They are gitignored and
mounted only into the safety gateway. Reference them from `adapters.yaml` as
`/run/qwenomatic-secrets/<name>` and seal their exact SHA-256 in config.

The broker refuses to start if a credential does not match its sealed hash.

## Changing authority

Stop the farm before changing an adapter, destination, credential hash, payee,
hard cap, policy capability, or model route. After reviewing the change, run
`safety-approve` again. This writes a human-authored receipt to the
append-only ledger.

Never make `provider-secrets` or `config` writable in the Qwenomatic
container.
