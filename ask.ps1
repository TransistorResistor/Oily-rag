<#
.SYNOPSIS
    Ask the RAG demo a one-off question via OpenRouter.
.EXAMPLE
    .\ask.ps1 "What is the range of the AIM-120?"
.EXAMPLE
    .\ask.ps1 "Which air-to-air missiles have a mass under 100 kg?" -AutoFilter
.EXAMPLE
    .\ask.ps1 "Compare the F-22 and F-35 radars" -Model qwen3-14b
#>
param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$Question,
    [string]$DbPath = "rag_test.db",
    [string]$Model = "qwen3-14b",
    [switch]$AutoFilter
)

. "$PSScriptRoot\_env.ps1"

$pyArgs = @("ragkit.py", "ask", $Question, "--db", $DbPath, "--backend", "openrouter", "--model", $Model)
if ($AutoFilter) { $pyArgs += "--auto-filter" }

& $Python @pyArgs
