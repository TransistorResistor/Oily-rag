<#
.SYNOPSIS
    Launch the RAG model bench: side-by-side comparison of up to 3 models,
    plus a context-only preview. This is the one web UI.
.DESCRIPTION
    Preloads the embedder at startup (so the first query is instant) and opens
    the browser automatically. Reads OPENROUTER_API_KEY from key.env via _env.ps1.
.EXAMPLE
    .\compare.ps1
    # opens http://localhost:8099
.EXAMPLE
    .\compare.ps1 -NoOpen        # don't auto-open the browser
#>
param(
    [string]$DbPath = "rag_test.db",
    [int]$Port = 8099,
    [switch]$NoOpen
)

. "$PSScriptRoot\_env.ps1"

$pyArgs = @("compare_server.py", "--db", $DbPath, "--port", $Port)
if ($NoOpen) { $pyArgs += "--no-open" }

& $Python @pyArgs
