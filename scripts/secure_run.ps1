param(
    [string]$DataDir = "var\secure",
    [string]$Model = "qwen3:14b",
    [int]$MaxConcurrency = 2,
    [Nullable[int]]$Generations = $null,
    [Nullable[int]]$Ticks = $null,
    [ValidateSet("server","on","off","adaptive")]
    [string]$ThinkingMode = "server"
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Repo

$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Missing .venv. Create it and install Qwenomatic first."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop is required for the fail-closed network boundary."
}

$DataPath = [System.IO.Path]::GetFullPath((Join-Path $Repo $DataDir))
New-Item -ItemType Directory -Force -Path $DataPath | Out-Null
$Ledger = Join-Path $DataPath "ledger.sqlite3"

if (-not (Test-Path $Ledger)) {
    & $Python -m supervisor.cli --data-dir $DataPath init
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# Config alone cannot activate a route. The operator must approve the exact
# current definition once, and any later edit invalidates that approval.
$SecurityStatus = & $Python -m supervisor.cli --data-dir $DataPath security status 2>&1
if ($LASTEXITCODE -ne 0) {
    $SecurityStatus | Write-Host
    exit $LASTEXITCODE
}
$LocalModelApproved = $SecurityStatus | Select-String -Pattern "destination\s+local_model\s+APPROVED"
if (-not $LocalModelApproved) {
    Write-Host ""
    Write-Host "The local_model destination is not operator-approved for its current definition."
    Write-Host "Review config\security.yaml, then explicitly run:"
    Write-Host ('  {0} -m supervisor.cli --data-dir "{1}" security approve destination local_model' -f $Python, $DataPath)
    exit 3
}

$SecurityDir = Join-Path $DataPath "security"
New-Item -ItemType Directory -Force -Path $SecurityDir | Out-Null
$Manifest = Join-Path $SecurityDir "egress-manifest.json"
& $Python -m supervisor.cli --data-dir $DataPath security manifest --output $Manifest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Compose receives absolute host paths only through operator-owned environment.
$env:QWENOMATIC_DATA_DIR = $DataPath.Replace("\", "/")
$env:QWENOMATIC_EGRESS_MANIFEST = $Manifest.Replace("\", "/")

docker compose -f docker-compose.secure.yml up -d --build broker
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$dockerArgs = @(
    "compose", "-f", "docker-compose.secure.yml", "run", "--rm", "--build", "farm",
    "--data-dir", "/data", "run",
    "--backend", "openai_compatible",
    "--model", $Model,
    "--max-concurrency", "$MaxConcurrency",
    "--thinking-mode", $ThinkingMode
)
if ($Generations -ne $null) {
    $dockerArgs += @("--generations", "$Generations")
}
if ($Ticks -ne $null) {
    $dockerArgs += @("--ticks", "$Ticks")
}

try {
    & docker @dockerArgs
    exit $LASTEXITCODE
}
finally {
    docker compose -f docker-compose.secure.yml stop broker | Out-Null
}
