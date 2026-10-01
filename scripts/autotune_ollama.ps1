param(
    [string]$Model = "qwen3:14b",
    [int[]]$ParallelLevels = @(1,2,3,4),
    [int]$Requests = 8,
    [int]$MaxTokens = 1024
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$bench = Join-Path $repo "scripts\benchmark_ollama.py"

if (-not (Test-Path $python)) { throw "Python venv not found at $python" }

function Stop-OllamaListener {
    $listener = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        $pids = $listener | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $pids) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    }
}

function Wait-Ollama {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 2
            return
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "Ollama server did not become ready"
}

Write-Host "IMPORTANT: quit the Ollama desktop app before running this script so it does not respawn the server."
Write-Host "Testing server parallelism levels: $($ParallelLevels -join ', ')"
Write-Host "Flash Attention: ON"
Write-Host "KV cache: q8_0"
Write-Host ""

$rows = @()

foreach ($parallel in $ParallelLevels) {
    Write-Host "=== Testing OLLAMA_NUM_PARALLEL=$parallel ==="
    Stop-OllamaListener

    $env:OLLAMA_NUM_PARALLEL = "$parallel"
    $env:OLLAMA_FLASH_ATTENTION = "1"
    $env:OLLAMA_KV_CACHE_TYPE = "q8_0"
    $env:OLLAMA_CONTEXT_LENGTH = "4096"

    $log = Join-Path $env:TEMP "qwenomatic-ollama-$parallel.log"
    $err = Join-Path $env:TEMP "qwenomatic-ollama-$parallel.err.log"
    Remove-Item $log,$err -ErrorAction SilentlyContinue

    $server = Start-Process -FilePath "ollama" -ArgumentList "serve" -PassThru -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err

    try {
        Wait-Ollama
        $raw = & $python $bench --model $Model --concurrency "$parallel" --requests $Requests --max-tokens $MaxTokens --json
        if ($LASTEXITCODE -ne 0) { throw "benchmark exited with code $LASTEXITCODE" }
        $result = $raw | ConvertFrom-Json
        $r = $result.results[0]
        $psOutput = & ollama ps
        $rows += [pscustomobject]@{
            Parallel = $parallel
            AggregateTokPerSec = [double]$r.aggregate_completion_tokens_per_second
            MedianLatencySec = [double]$r.median_latency_seconds
            BatchSec = [double]$r.batch_seconds
            ValidJson = [int]$r.valid_json
            OllamaPs = ($psOutput -join " ")
        }
        Write-Host ("{0}: {1:N2} aggregate completion tok/s; median latency {2:N2}s" -f $parallel, $r.aggregate_completion_tokens_per_second, $r.median_latency_seconds)
    } catch {
        Write-Warning "Parallel=$parallel failed: $_"
        if (Test-Path $err) { Get-Content $err -Tail 20 }
    } finally {
        Stop-OllamaListener
        if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
    }
}

if (-not $rows) { throw "No benchmark configuration completed successfully." }

Write-Host ""
Write-Host "=== RESULTS ==="
$rows | Sort-Object Parallel | Format-Table Parallel,AggregateTokPerSec,MedianLatencySec,BatchSec,ValidJson -AutoSize

$best = $rows | Sort-Object AggregateTokPerSec -Descending | Select-Object -First 1
Write-Host ""
Write-Host ("BEST: parallel={0} at {1:N2} aggregate completion tok/s" -f $best.Parallel, $best.AggregateTokPerSec)
Write-Host ""
Write-Host "Start Ollama for Qwenomatic with:"
Write-Host ("  powershell -ExecutionPolicy Bypass -File scripts\start_ollama_optimized.ps1 -Parallel {0}" -f $best.Parallel)
Write-Host "And pass this to Qwenomatic:"
Write-Host ("  --max-concurrency {0}" -f $best.Parallel)
