param(
    [string]$Repo = "",
    [ValidateSet("smoke", "baseline")][string]$Mode = "smoke",
    [string]$Experiment = "baseline"
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Create .venv and install dependencies first; see README.md."
}
if (-not $Repo) { $Repo = Join-Path $projectRoot "examples\maintenance" }
Push-Location $projectRoot
try {
    & $pythonPath -m code_ontology doctor --env-file .env
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $pythonPath -m code_ontology run --repo $Repo --config "configs/$Mode.yaml" --experiment $Experiment --env-file .env
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

