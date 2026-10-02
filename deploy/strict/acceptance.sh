#!/bin/sh
set -eu

docker compose up -d safety-gateway

# Instantiate the real strict supervisor once. Its constructor performs the
# fail-closed safety attestation and writes a safety_attested ledger receipt.
docker compose run --rm --no-deps qwenomatic --data-dir /data run --ticks 0

# Then prove the network behavior independently from inside the same service
# namespace. This prints the final acceptance receipt.
docker compose run --rm --no-deps --entrypoint python qwenomatic /app/deploy/strict/acceptance.py
