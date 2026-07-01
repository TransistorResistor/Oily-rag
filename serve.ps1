<#
.SYNOPSIS
    Alias for compare.ps1 — the single-answer UI has been unified into the
    model-comparison bench. Kept so `.\serve.ps1` still works.
.DESCRIPTION
    Launches the same web bench as compare.ps1 (select one model for a
    single-answer flow, or up to three to compare). Preloads the embedder and
    opens the browser. For single-answer, non-OpenRouter backends
    (local / anthropic / openai), use `.\ask.ps1` instead.
.EXAMPLE
    .\serve.ps1
    # opens http://localhost:8099
#>
param(
    [string]$DbPath = "rag_test.db",
    [int]$Port = 8099,
    [switch]$NoOpen
)

& "$PSScriptRoot\compare.ps1" -DbPath $DbPath -Port $Port -NoOpen:$NoOpen
