[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "DryRun")]
    [switch]$DryRun,

    [Parameter(Mandatory = $true, ParameterSetName = "Configure")]
    [switch]$Configure,

    [Parameter(Mandatory = $true, ParameterSetName = "Canary")]
    [switch]$Canary,

    [Parameter(Mandatory = $true, ParameterSetName = "DryRun")]
    [Parameter(Mandatory = $true, ParameterSetName = "Configure")]
    [string]$SecretArn,

    [Parameter(Mandatory = $true, ParameterSetName = "DryRun")]
    [Parameter(Mandatory = $true, ParameterSetName = "Configure")]
    [string]$CreatorUserId,

    [Parameter(ValueFromRemainingArguments = $true)]
    [object[]]$RemainingArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$awsProfile = "gen-automation-staging"
$awsRegion = "eu-central-1"
$awsAccountId = "861912887470"
$secretArnPattern = "^arn:aws:secretsmanager:eu-central-1:861912887470:secret:gen-automation-staging/x/oauth1-[A-Za-z0-9]{6}$"
$creatorIdPattern = "^[1-9][0-9]{0,18}$"
$instanceIdPattern = "^i-[0-9a-f]{8,17}$"
$commandIdPattern = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
$operatorArnPattern = "^arn:aws:sts::861912887470:assumed-role/AWSReservedSSO_GenAutomationStagingDeployer_[A-Fa-f0-9]{16}/[A-Za-z0-9+=,.@_-]{2,64}$"
$safeConfigureOutput = "X OAuth 1.0a runtime settings were configured and the stopped controller is healthy."
$alreadyConfiguredOutput = "The exact OAuth 1.0a runtime settings are already configured and healthy."
$canaryOutput = "X OAuth 1.0a account binding passed. No media was uploaded and no post was created."

function Invoke-CapturedNative {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $nativeOutput = @()
    $standardOutput = [Collections.Generic.List[string]]::new()
    $standardError = [Collections.Generic.List[string]]::new()
    $nativeErrorRecords = [Collections.Generic.List[Management.Automation.ErrorRecord]]::new()
    $savedErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell converts redirected native stderr into ErrorRecord objects.
        # Capture them without allowing the script-wide Stop preference to terminate
        # before the native exit code can be checked.
        $ErrorActionPreference = "Continue"
        $nativeOutput = @(& $Executable @Arguments 2>&1)
        $nativeExitCode = $LASTEXITCODE
        foreach ($outputItem in $nativeOutput) {
            if ($outputItem -is [Management.Automation.ErrorRecord]) {
                $standardError.Add($outputItem.ToString())
                $nativeErrorRecords.Add($outputItem)
            }
            else {
                $standardOutput.Add($outputItem.ToString())
            }
        }
    }
    finally {
        $ErrorActionPreference = $savedErrorActionPreference
        foreach ($nativeErrorRecord in $nativeErrorRecords) {
            for ($errorIndex = 0; $errorIndex -lt $Error.Count; $errorIndex++) {
                if ([object]::ReferenceEquals($Error[$errorIndex], $nativeErrorRecord)) {
                    $Error.RemoveAt($errorIndex)
                    break
                }
            }
        }
    }

    $nativeOutput = @()
    $nativeErrorRecords.Clear()
    return [pscustomobject]@{
        ExitCode = $nativeExitCode
        StandardOutput = ($standardOutput -join "`n").Trim()
        StandardError = ($standardError -join "`n").Trim()
    }
}

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    $nativeResult = Invoke-CapturedNative -Executable $Executable -Arguments $Arguments
    if ($nativeResult.ExitCode -ne 0) {
        $nativeResult.StandardOutput = "[cleared]"
        $nativeResult.StandardError = "[cleared]"
        throw $FailureMessage
    }
    $resolvedOutput = $nativeResult.StandardOutput
    $nativeResult.StandardOutput = "[cleared]"
    $nativeResult.StandardError = "[cleared]"
    return $resolvedOutput
}

function ConvertTo-PosixLiteral {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if ($Value.Contains("'")) {
        throw "A bounded command value is invalid."
    }
    return "'$Value'"
}

function ConvertTo-AwsCliWindowsFileReference {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $absolutePath = [IO.Path]::GetFullPath($Path)
    if ($absolutePath -notmatch "^[A-Za-z]:\\") {
        throw "The private parameter path must be an absolute Windows drive path."
    }
    $normalizedPath = $absolutePath.Replace("\", "/")
    $fileReference = "file://$normalizedPath"
    if ([Text.Encoding]::UTF8.GetByteCount($fileReference) -gt 4096) {
        throw "The private parameter file reference exceeds its bounded size."
    }
    return $fileReference
}

function Get-Sha256Hex {
    param(
        [Parameter(Mandatory = $true)]
        [byte[]]$Bytes
    )

    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function ConvertTo-GzipBase64 {
    param(
        [Parameter(Mandatory = $true)]
        [byte[]]$Bytes
    )

    $memory = [IO.MemoryStream]::new()
    try {
        $gzip = [IO.Compression.GZipStream]::new(
            $memory,
            [IO.Compression.CompressionMode]::Compress,
            $true
        )
        try {
            $gzip.Write($Bytes, 0, $Bytes.Length)
        }
        finally {
            $gzip.Dispose()
        }
        return [Convert]::ToBase64String($memory.ToArray())
    }
    finally {
        $memory.Dispose()
    }
}

if ($RemainingArguments.Count -ne 0) {
    throw "This helper accepts only its documented parameters."
}
if (($DryRun -or $Configure) -and $SecretArn -notmatch $secretArnPattern) {
    throw "The secret ARN is outside the exact OAuth 1.0a staging boundary."
}
if (($DryRun -or $Configure) -and $CreatorUserId -notmatch $creatorIdPattern) {
    throw "The creator ID must contain 1 to 19 digits and cannot start with zero."
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$infraRoot = Join-Path $repositoryRoot "infra\aws-staging"
$hostHelperPath = Join-Path $infraRoot "deploy\configure-x-oauth1.sh"
$validatorPath = Join-Path $infraRoot "deploy\validate-deployment.sh"
$awsConfigFile = Join-Path $env:TEMP "gen-automation-staging-aws-config"
if (-not (Test-Path -LiteralPath $awsConfigFile -PathType Leaf)) {
    throw "The fixed staging AWS profile configuration is unavailable."
}
if (-not (Test-Path -LiteralPath $hostHelperPath -PathType Leaf)) {
    throw "The reviewed host helper is unavailable."
}
if (-not (Test-Path -LiteralPath $validatorPath -PathType Leaf)) {
    throw "The reviewed deployment validator is unavailable."
}

$awsExecutable = (Get-Command aws -CommandType Application -ErrorAction Stop).Source
$tofuExecutable = (Get-Command tofu -CommandType Application -ErrorAction Stop).Source
$environmentNames = @(
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_CONFIG_FILE",
    "AWS_PAGER",
    "AWS_CA_BUNDLE",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_STS",
    "AWS_ENDPOINT_URL_S3",
    "AWS_ENDPOINT_URL_SSM",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy"
)
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

$parametersPath = $null
try {
    foreach ($environmentName in $environmentNames) {
        Remove-Item "Env:$environmentName" -ErrorAction SilentlyContinue
    }
    $env:AWS_PROFILE = $awsProfile
    $env:AWS_REGION = $awsRegion
    $env:AWS_DEFAULT_REGION = $awsRegion
    $env:AWS_CONFIG_FILE = $awsConfigFile
    $env:AWS_PAGER = ""

    $identityInvocation = @{
        Executable = $awsExecutable
        Arguments = @(
            "sts", "get-caller-identity",
            "--profile", $awsProfile,
            "--region", $awsRegion,
            "--output", "json",
            "--no-cli-pager"
        )
        FailureMessage = "The fixed staging AWS identity could not be verified."
    }
    $identityJson = Invoke-CheckedNative @identityInvocation
    try {
        $identity = $identityJson | ConvertFrom-Json
    }
    catch {
        throw "The fixed staging AWS identity returned an invalid response."
    }
    if ($identity.Account -ne $awsAccountId -or $identity.Arn -notmatch $operatorArnPattern) {
        throw "The active AWS identity is not the approved staging deployer."
    }
    $identityJson = "[cleared]"

    $instanceInvocation = @{
        Executable = $tofuExecutable
        Arguments = @("-chdir=$infraRoot", "output", "-raw", "control_plane_instance_id")
        FailureMessage = "The exact staging instance could not be resolved from reviewed state."
    }
    $instanceId = Invoke-CheckedNative @instanceInvocation
    if ($instanceId -notmatch $instanceIdPattern) {
        throw "The reviewed staging state returned an invalid instance ID."
    }

    $hostHelperBytes = [IO.File]::ReadAllBytes($hostHelperPath)
    $strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
    $hostHelperText = $strictUtf8.GetString($hostHelperBytes)
    if ($hostHelperText.Contains("`r")) {
        throw "The reviewed host helper must use LF line endings."
    }
    $hostHelperText = "[validated]"
    $hostHelperSha256 = Get-Sha256Hex -Bytes $hostHelperBytes
    $hostHelperArchiveBase64 = ConvertTo-GzipBase64 -Bytes $hostHelperBytes
    $hostHelperBytes = @()

    $validatorBytes = [IO.File]::ReadAllBytes($validatorPath)
    $validatorText = $strictUtf8.GetString($validatorBytes)
    if ($validatorText.Contains("`r")) {
        throw "The reviewed deployment validator must use LF line endings."
    }
    $validatorText = "[validated]"
    $validatorSha256 = Get-Sha256Hex -Bytes $validatorBytes
    $validatorArchiveBase64 = ConvertTo-GzipBase64 -Bytes $validatorBytes
    $validatorBytes = @()

    if ($Canary) {
        $remoteArguments = "--canary"
        $executionTimeout = "300"
        $comment = "Zero-post X OAuth1 account-binding check"
        $expectedOutputs = @($canaryOutput)
    }
    else {
        $validatorRemotePath = '"$payload_dir/validate-deployment.sh"'
        $remoteArguments = (
            "--configure $(ConvertTo-PosixLiteral $SecretArn) " +
            "$(ConvertTo-PosixLiteral $CreatorUserId) $validatorRemotePath " +
            "$(ConvertTo-PosixLiteral $validatorSha256)"
        )
        $executionTimeout = "1200"
        $comment = "Configure exact staging X OAuth1 runtime binding"
        $expectedOutputs = @($safeConfigureOutput, $alreadyConfiguredOutput)
    }

    $remoteTemplate = @'
payload_dir=$(/usr/bin/mktemp --directory /tmp/gen-automation-x-oauth1.XXXXXX) && /usr/bin/chmod 0700 "$payload_dir" && trap '/usr/bin/rm -rf -- "$payload_dir"' EXIT && /usr/bin/printf '%s' {0} | /usr/bin/base64 --decode | /usr/bin/gzip --decompress >"$payload_dir/configure-x-oauth1.sh" && /usr/bin/printf '%s' {1} | /usr/bin/base64 --decode | /usr/bin/gzip --decompress >"$payload_dir/validate-deployment.sh" && /usr/bin/chmod 0600 "$payload_dir/configure-x-oauth1.sh" "$payload_dir/validate-deployment.sh" && /usr/bin/printf '%s  %s\n' {2} "$payload_dir/configure-x-oauth1.sh" | /usr/bin/sha256sum --check --status && /usr/bin/printf '%s  %s\n' {3} "$payload_dir/validate-deployment.sh" | /usr/bin/sha256sum --check --status && /usr/bin/bash -n "$payload_dir/configure-x-oauth1.sh" && /usr/bin/bash -n "$payload_dir/validate-deployment.sh" && /usr/bin/sudo /usr/bin/bash "$payload_dir/configure-x-oauth1.sh" {4}
'@
    $remoteCommand = [string]::Format(
        $remoteTemplate.Trim(),
        (ConvertTo-PosixLiteral $hostHelperArchiveBase64),
        (ConvertTo-PosixLiteral $validatorArchiveBase64),
        (ConvertTo-PosixLiteral $hostHelperSha256),
        (ConvertTo-PosixLiteral $validatorSha256),
        $remoteArguments
    )
    $hostHelperArchiveBase64 = "[cleared]"
    $validatorArchiveBase64 = "[cleared]"
    if ([Text.Encoding]::UTF8.GetByteCount($remoteCommand) -gt 24576) {
        throw "The reviewed SSM command exceeds its bounded size."
    }

    if ($DryRun) {
        Write-Output "Dry run passed. No AWS command was submitted."
        Write-Output "Target: exact staging control plane $instanceId"
        Write-Output "Host helper SHA-256: $hostHelperSha256"
        Write-Output "Deployment validator SHA-256: $validatorSha256"
        return
    }

    $parametersPath = Join-Path $env:TEMP ("gen-automation-x-oauth1-ssm-{0}.json" -f [Guid]::NewGuid().ToString("N"))
    $parameters = @{
        commands = @($remoteCommand)
        executionTimeout = @($executionTimeout)
    }
    $parametersJson = $parameters | ConvertTo-Json -Compress -Depth 4
    [IO.File]::WriteAllText($parametersPath, $parametersJson, [Text.UTF8Encoding]::new($false))
    $parametersJson = "[cleared]"
    $remoteCommand = "[cleared]"
    $parametersUri = ConvertTo-AwsCliWindowsFileReference -Path $parametersPath

    $sendInvocation = @{
        Executable = $awsExecutable
        Arguments = @(
            "ssm", "send-command",
            "--profile", $awsProfile,
            "--region", $awsRegion,
            "--instance-ids", $instanceId,
            "--document-name", "AWS-RunShellScript",
            "--comment", $comment,
            "--timeout-seconds", "120",
            "--max-concurrency", "1",
            "--max-errors", "0",
            "--parameters", $parametersUri,
            "--query", "Command.CommandId",
            "--output", "text",
            "--no-cli-pager"
        )
        FailureMessage = "The bounded staging command could not be submitted."
    }
    $commandId = Invoke-CheckedNative @sendInvocation
    if ($commandId -notmatch $commandIdPattern) {
        throw "AWS returned an invalid command identifier."
    }

    $deadline = [DateTime]::UtcNow.AddMinutes(25)
    while ([DateTime]::UtcNow -lt $deadline) {
        $invocationResult = Invoke-CapturedNative -Executable $awsExecutable -Arguments @(
                "ssm", "get-command-invocation",
                "--profile", $awsProfile,
                "--region", $awsRegion,
                "--command-id", $commandId,
                "--instance-id", $instanceId,
                "--query", "{CommandId:CommandId,InstanceId:InstanceId,Status:Status,ResponseCode:ResponseCode,Output:StandardOutputContent}",
                "--output", "json",
                "--no-cli-pager"
        )
        if ($invocationResult.ExitCode -ne 0) {
            $invocationError = $invocationResult.StandardError
            $invocationResult.StandardOutput = "[cleared]"
            $invocationResult.StandardError = "[cleared]"
            if ($invocationError -notmatch "InvocationDoesNotExist") {
                $invocationError = "[cleared]"
                throw "The bounded staging command status could not be read."
            }
            $invocationError = "[cleared]"
            Start-Sleep -Seconds 3
            continue
        }
        try {
            $invocation = $invocationResult.StandardOutput | ConvertFrom-Json
        }
        catch {
            throw "AWS returned an invalid command status."
        }
        $invocationResult.StandardOutput = "[cleared]"
        $invocationResult.StandardError = "[cleared]"
        if ($invocation.CommandId -ne $commandId -or $invocation.InstanceId -ne $instanceId) {
            throw "AWS returned status for an unexpected command target."
        }
        switch ($invocation.Status) {
            "Pending" { Start-Sleep -Seconds 3; continue }
            "InProgress" { Start-Sleep -Seconds 3; continue }
            "Delayed" { Start-Sleep -Seconds 3; continue }
            "Success" {
                if ($invocation.ResponseCode -ne 0 -or $expectedOutputs -notcontains $invocation.Output.Trim()) {
                    throw "The staging command returned an invalid success result."
                }
                Write-Output $invocation.Output.Trim()
                return
            }
            default { throw "The staging command failed safely with status $($invocation.Status)." }
        }
    }
    throw "The bounded staging command did not finish before its local deadline."
}
finally {
    if ($null -ne $parametersPath -and (Test-Path -LiteralPath $parametersPath -PathType Leaf)) {
        Remove-Item -LiteralPath $parametersPath -Force
    }
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
