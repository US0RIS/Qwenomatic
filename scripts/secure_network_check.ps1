param(
    [string]$DataDir = "var\secure"
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Repo
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Missing .venv" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker Desktop is required" }

$DataPath = [System.IO.Path]::GetFullPath((Join-Path $Repo $DataDir))
New-Item -ItemType Directory -Force -Path $DataPath | Out-Null
$Ledger = Join-Path $DataPath "ledger.sqlite3"
if (-not (Test-Path $Ledger)) {
    & $Python -m supervisor.cli --data-dir $DataPath init
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$Status = & $Python -m supervisor.cli --data-dir $DataPath security status 2>&1
$Approved = $Status | Select-String -Pattern "destination\s+local_model\s+APPROVED"
if (-not $Approved) {
    Write-Host "local_model is not approved. Review config\security.yaml and run:"
    Write-Host ('  {0} -m supervisor.cli --data-dir "{1}" security approve destination local_model' -f $Python, $DataPath)
    exit 3
}

$SecurityDir = Join-Path $DataPath "security"
New-Item -ItemType Directory -Force -Path $SecurityDir | Out-Null
$Manifest = Join-Path $SecurityDir "egress-manifest.json"
& $Python -m supervisor.cli --data-dir $DataPath security manifest --output $Manifest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:QWENOMATIC_DATA_DIR = $DataPath.Replace("\", "/")
$env:QWENOMATIC_EGRESS_MANIFEST = $Manifest.Replace("\", "/")

docker compose -f docker-compose.secure.yml up -d --build broker
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
try {
    docker compose -f docker-compose.secure.yml run --rm --build farm --data-dir /data security check --backend openai_compatible
    exit $LASTEXITCODE
}
finally {
    docker compose -f docker-compose.secure.yml stop broker | Out-Null
}
