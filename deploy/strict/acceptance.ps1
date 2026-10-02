$ErrorActionPreference = "Stop"

docker compose up -d safety-gateway
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker compose run --rm --no-deps qwenomatic --data-dir /data run --ticks 0
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker compose run --rm --no-deps --entrypoint python qwenomatic /app/deploy/strict/acceptance.py
exit $LASTEXITCODE
