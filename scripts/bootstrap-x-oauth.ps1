[CmdletBinding()]
param(
    [switch]$PortalRefreshToken,
    [switch]$OAuth1,

    [Parameter(ValueFromRemainingArguments = $true)]
    [object[]]$RemainingArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($RemainingArguments.Count -ne 0 -or ($PortalRefreshToken -and $OAuth1)) {
    throw "This helper accepts either -PortalRefreshToken or -OAuth1."
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$awsConfigFile = Join-Path $env:TEMP "gen-automation-staging-aws-config"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "The repository Python environment is unavailable."
}
if (-not (Test-Path -LiteralPath $awsConfigFile -PathType Leaf)) {
    throw "The staging AWS profile configuration is unavailable."
}

$env:AWS_CONFIG_FILE = $awsConfigFile
Push-Location -LiteralPath $repositoryRoot
try {
    if ($OAuth1) {
        & $python -I -m gen_automation.x_oauth_bootstrap_cli --oauth1
    }
    elseif ($PortalRefreshToken) {
        & $python -I -m gen_automation.x_oauth_bootstrap_cli --portal-refresh-token
    }
    else {
        & $python -I -m gen_automation.x_oauth_bootstrap_cli
    }
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $exitCode
