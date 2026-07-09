# Dot-source this file before running RAG commands in a network-isolated host.
param(
    [string]$CachePath = (Join-Path $PSScriptRoot ".hf-cache"),
    [switch]$NoReranker
)

$resolved = [System.IO.Path]::GetFullPath($CachePath)
$manifest = Join-Path $resolved "ragkit-models.json"
if (-not (Test-Path $manifest)) {
    throw "Portable model cache not found at '$resolved'. Run prepare_offline.py on a connected machine first."
}

$env:HF_HOME = $resolved
$env:HF_HUB_CACHE = Join-Path $resolved "hub"
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
$env:RAGKIT_OFFLINE = "1"
$env:KMP_DUPLICATE_LIB_OK = "TRUE"
$env:OMP_NUM_THREADS = "1"
$env:TOKENIZERS_PARALLELISM = "false"
$env:PYTHONIOENCODING = "utf-8"

if ($NoReranker) {
    $env:RAGKIT_DISABLE_RERANK = "1"
} else {
    Remove-Item Env:RAGKIT_DISABLE_RERANK -ErrorAction SilentlyContinue
}

Write-Host "Offline mode enabled; Hugging Face cache: $resolved"
