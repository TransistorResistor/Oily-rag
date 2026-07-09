<#
.SYNOPSIS
    Launch either of the repository's two user-facing demos.
.EXAMPLE
    .\demo.ps1 rag
.EXAMPLE
    .\demo.ps1 enrichment
.EXAMPLE
    .\demo.ps1 rag -Offline -NoReranker
#>
param(
    [Parameter(Position=0)]
    [ValidateSet("rag", "enrichment")]
    [string]$Surface = "rag",
    [switch]$Offline,
    [switch]$NoReranker,
    [switch]$NoOpen,
    [string]$DbPath = "rag_test.db",
    [string]$Proposals = "enrich_demo\proposals.json"
)

. "$PSScriptRoot\_env.ps1"
if ($Offline -and $Surface -eq "rag") {
    . "$PSScriptRoot\offline_env.ps1" -NoReranker:$NoReranker
}

if ($Surface -eq "rag") {
    $argsList = @("compare_server.py", "--db", $DbPath)
    if ($NoOpen) { $argsList += "--no-open" }
    & $Python @argsList
} else {
    $argsList = @("enrich_demo\demo_server.py", "--proposals", $Proposals)
    if ($NoOpen) { $argsList += "--no-open" }
    & $Python @argsList
}
