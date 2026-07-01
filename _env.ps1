# Shared setup for the ask.ps1 / serve.ps1 / compare.ps1 launchers.
# Dot-sourced by them -- not meant to be run directly.

$envFile = Join-Path $PSScriptRoot "key.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
        }
    }
} else {
    Write-Warning "key.env not found next to this script -- OPENROUTER_API_KEY won't be set."
}

# Works around a segfault seen during embedding: torch and numpy each bundle
# their own OpenMP runtime, which crashes intermittently on Windows/conda
# unless this is set. The other two just make things quieter/deterministic.
$env:KMP_DUPLICATE_LIB_OK = "TRUE"
$env:OMP_NUM_THREADS = "1"
$env:TOKENIZERS_PARALLELISM = "false"
$env:PYTHONIOENCODING = "utf-8"

$Python = "C:\Users\robot\anaconda3\python.exe"
