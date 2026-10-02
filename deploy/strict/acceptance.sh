#!/bin/sh
set -eu

docker compose up -d safety-gateway
docker compose run --rm --no-deps --entrypoint python qwenomatic /app/deploy/strict/acceptance.py
