param(
    [ValidateRange(1,8)]
    [int]$Parallel = 2,
    [ValidateSet("f16","q8_0")]
    [string]$KvCache = "q8_0",
    [int]$ContextLength = 4096
)

$env:OLLAMA_NUM_PARALLEL = "$Parallel"
$env:OLLAMA_FLASH_ATTENTION = "1"
$env:OLLAMA_KV_CACHE_TYPE = $KvCache
$env:OLLAMA_CONTEXT_LENGTH = "$ContextLength"

Write-Host "Starting Ollama optimized:"
Write-Host "  parallel        $Parallel"
Write-Host "  flash attention ON"
Write-Host "  KV cache        $KvCache"
Write-Host "  context         $ContextLength"
Write-Host ""
Write-Host "If port 11434 is already occupied, quit the Ollama desktop app/server first."
ollama serve
