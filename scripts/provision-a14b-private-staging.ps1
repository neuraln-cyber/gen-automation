[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Image,

    [Parameter(Mandatory = $true)]
    [string]$DeploymentId,

    [Parameter(ValueFromRemainingArguments = $true)]
    [object[]]$RemainingArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$awsProfile = "gen-automation-staging"
$awsRegion = "eu-central-1"
$imagePattern = "^ghcr[.]io/neuraln-cyber/gen-automation-a14b-registry/video-worker-a14b-private@sha256:[0-9a-f]{64}$"
$deploymentIdPattern = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
$instanceIdPattern = "^i-[0-9a-f]{8,17}$"

if ($RemainingArguments.Count -ne 0) {
    throw "This helper accepts only its documented public identifiers."
}
if ($Image -notmatch $imagePattern) {
    throw "The A14B image must be the exact private repository at an immutable digest."
}
if ($DeploymentId -notmatch $deploymentIdPattern) {
    throw "The A14B deployment identifier is invalid."
}
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$infraRoot = Join-Path $repositoryRoot "infra\aws-staging"
$pythonExecutable = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$tofuApplications = @(
    Get-Command tofu -CommandType Application -All -ErrorAction Stop
)
if ($tofuApplications.Count -lt 1) {
    throw "The reviewed OpenTofu executable is unavailable."
}
$tofuExecutable = [string]$tofuApplications[0].Source
if (
    [string]::IsNullOrWhiteSpace($tofuExecutable) -or
    -not (Test-Path -LiteralPath $tofuExecutable -PathType Leaf)
) {
    throw "The reviewed OpenTofu executable is unavailable."
}
$awsConfigFile = Join-Path $env:TEMP "gen-automation-staging-aws-config"
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "The repository's reviewed Python environment is unavailable."
}
if (-not (Test-Path -LiteralPath $awsConfigFile -PathType Leaf)) {
    throw "The fixed staging AWS profile configuration is unavailable."
}

$fixedEnvironmentNames = @(
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_PAGER",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT",
    "AWS_EC2_METADATA_DISABLED",
    "AWS_CA_BUNDLE",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_S3",
    "AWS_ENDPOINT_URL_STS",
    "AWS_ENDPOINT_URL_SSM",
    "AWS_S3_ENDPOINT",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "TF_WORKSPACE",
    "TF_DATA_DIR",
    "TF_CLI_CONFIG_FILE",
    "TF_INPUT",
    "TF_CLI_ARGS",
    "TF_CLI_ARGS_output",
    "TF_CLI_ARGS_workspace",
    "TF_LOG",
    "TF_LOG_PATH",
    "TF_LOG_CORE",
    "TF_LOG_PROVIDER",
    "TOFU_WORKSPACE",
    "TOFU_DATA_DIR",
    "TOFU_CLI_CONFIG_FILE",
    "TOFU_INPUT",
    "TOFU_CLI_ARGS",
    "TOFU_CLI_ARGS_output",
    "TOFU_CLI_ARGS_workspace",
    "TOFU_LOG",
    "TOFU_LOG_PATH",
    "TOFU_LOG_CORE",
    "TOFU_LOG_PROVIDER",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY"
)
$environmentNames = $fixedEnvironmentNames
$savedEnvironment = @{}
foreach ($environmentName in $environmentNames) {
    $environmentPath = "Env:$environmentName"
    if (Test-Path $environmentPath) {
        $savedEnvironment[$environmentName] = @($true, (Get-Item $environmentPath).Value)
    }
    else {
        $savedEnvironment[$environmentName] = @($false, $null)
    }
}

try {
    foreach ($environmentName in $environmentNames) {
        Remove-Item "Env:$environmentName" -ErrorAction SilentlyContinue
    }
    $env:AWS_PROFILE = $awsProfile
    $env:AWS_DEFAULT_PROFILE = $awsProfile
    $env:AWS_REGION = $awsRegion
    $env:AWS_DEFAULT_REGION = $awsRegion
    $env:AWS_CONFIG_FILE = $awsConfigFile
    $env:AWS_PAGER = ""
    $env:TF_INPUT = "0"
    $env:TOFU_INPUT = "0"

    $savedErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $workspaceOutput = @(
            & $tofuExecutable "-chdir=$infraRoot" workspace show 2>$null
        )
        $workspaceExitCode = $LASTEXITCODE
        if ($workspaceExitCode -eq 0) {
            $workspaceName = ($workspaceOutput -join "").Trim()
        }
        else {
            $workspaceName = ""
        }
        $workspaceOutput = @()
        if ($workspaceExitCode -ne 0 -or $workspaceName -ne "default") {
            throw "The reviewed staging state is not on the default workspace."
        }
        $tofuOutput = @(
            & $tofuExecutable "-chdir=$infraRoot" output -raw control_plane_instance_id 2>$null
        )
        $tofuExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    if ($tofuExitCode -ne 0) {
        $tofuOutput = @()
        throw "The exact staging instance could not be resolved from reviewed state."
    }
    $instanceId = ($tofuOutput -join "").Trim()
    $tofuOutput = @()
    if ($instanceId -notmatch $instanceIdPattern) {
        throw "The reviewed staging state returned an invalid instance identifier."
    }

    Push-Location $repositoryRoot
    try {
        & $pythonExecutable -m gen_automation.a14b_registry_operator_cli `
            --image $Image `
            --deployment-id $DeploymentId `
            --instance-id $instanceId
        if ($LASTEXITCODE -ne 0) {
            throw "The one-use A14B registry authorization did not complete."
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    foreach ($environmentName in $environmentNames) {
        $saved = $savedEnvironment[$environmentName]
        if ($saved[0]) {
            Set-Item "Env:$environmentName" $saved[1]
        }
        else {
            Remove-Item "Env:$environmentName" -ErrorAction SilentlyContinue
        }
    }
}
