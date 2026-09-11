param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Helper,
    [Parameter(Mandatory = $true)][string]$NodeSource,
    [Parameter(Mandatory = $true)][string]$Report
)

$ErrorActionPreference = "Stop"
$userName = "BelloSandboxCI"
$passwordText = "Bello-CI-Only-9f4d!"
$password = ConvertTo-SecureString $passwordText -AsPlainText -Force
$taskName = "BelloNativeSandboxStandardUser"
$script = Join-Path $PSScriptRoot "native_smoke.py"
$reportDirectory = Split-Path -Parent $Report
$stdoutPath = Join-Path $reportDirectory "standard-user-stdout.txt"
$stderrPath = Join-Path $reportDirectory "standard-user-stderr.txt"
$taskReport = Join-Path $reportDirectory "standard-user-task.json"
$task = $null
$info = $null

function Quote-PowerShellLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

try {
    if (Get-LocalUser -Name $userName -ErrorAction SilentlyContinue) {
        Remove-LocalUser -Name $userName
    }
    New-LocalUser -Name $userName -Password $password -PasswordNeverExpires | Out-Null
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
    & icacls.exe $reportDirectory /grant "${env:COMPUTERNAME}\${userName}:(OI)(CI)M" | Out-Null

    $pythonArguments = "-I `"$script`" --helper `"$Helper`" --node-source `"$NodeSource`" --report `"$Report`""
    # Run the same smoke under the same limited account. This launcher only
    # captures errors that Task Scheduler otherwise discards, including failure
    # to start Python. Neither its command nor its output contains the password.
    $launcher = @"
`$ErrorActionPreference = 'Stop'
try {
    `$child = Start-Process -FilePath $(Quote-PowerShellLiteral $Python) -ArgumentList $(Quote-PowerShellLiteral $pythonArguments) -Wait -PassThru -RedirectStandardOutput $(Quote-PowerShellLiteral $stdoutPath) -RedirectStandardError $(Quote-PowerShellLiteral $stderrPath)
    exit `$child.ExitCode
}
catch {
    (`$_ | Out-String) | Out-File -LiteralPath $(Quote-PowerShellLiteral $stderrPath) -Append -Encoding utf8
    exit 1
}
"@
    $encodedLauncher = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($launcher))
    $powershell = Join-Path $env:SystemRoot "System32/WindowsPowerShell/v1.0/powershell.exe"
    $action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoLogo -NoProfile -NonInteractive -EncodedCommand $encodedLauncher"
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -User "${env:COMPUTERNAME}\$userName" `
        -Password $passwordText `
        -RunLevel Limited `
        -Force | Out-Null
    $before = Get-ScheduledTaskInfo -TaskName $taskName
    $started = Get-Date
    $launchDeadline = $started.AddSeconds(60)
    $deadline = $started.AddMinutes(15)
    $observedRunning = $false
    Start-ScheduledTask -TaskName $taskName
    do {
        Start-Sleep -Seconds 1
        $task = Get-ScheduledTask -TaskName $taskName
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        $active = $task.State -in @("Running", "Queued")
        $ran = $info.LastRunTime -gt $before.LastRunTime
        if ($task.State -eq "Running") { $observedRunning = $true }
        if (-not $active -and ($ran -or $observedRunning)) { break }
        if (-not $active -and $info.LastTaskResult -ne $before.LastTaskResult) {
            throw "standard-user scheduled task failed before Python started"
        }
        if (-not $observedRunning -and -not $ran -and (Get-Date) -gt $launchDeadline) {
            throw "standard-user scheduled task did not start within 60 seconds"
        }
        if ((Get-Date) -gt $deadline) { throw "standard-user sandbox task timed out after 15 minutes" }
    } while ($true)
    if ($info.LastTaskResult -ne 0) {
        throw "standard-user sandbox task failed with result $($info.LastTaskResult)"
    }
    if (-not (Test-Path -LiteralPath $Report)) {
        throw "standard-user sandbox task did not produce its timing report"
    }
}
catch {
    $failure = $_
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task -and $null -ne $info) {
        Write-Host ("standard-user task: State={0}; LastTaskResult={1} (0x{1:X8}); LastRunTime={2:o}" -f $task.State, [uint32]$info.LastTaskResult, $info.LastRunTime)
        @{
            state = [string]$task.State
            lastTaskResult = [uint32]$info.LastTaskResult
            lastTaskResultHex = "0x{0:X8}" -f [uint32]$info.LastTaskResult
            lastRunTime = $info.LastRunTime.ToString("o")
            error = $failure.Exception.Message
        } | ConvertTo-Json | Set-Content -LiteralPath $taskReport -Encoding utf8
    }
    foreach ($streamPath in @($stdoutPath, $stderrPath)) {
        if (Test-Path -LiteralPath $streamPath) {
            Write-Host ("standard-user captured " + [IO.Path]::GetFileName($streamPath) + " (last 100 lines):")
            Get-Content -LiteralPath $streamPath -Tail 100 | Write-Host
        }
    }
    throw $failure
}
finally {
    # This fixture owns precisely this scheduled task; never leave its process
    # running when the timeout path unregisters the task/removes the CI account.
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task -and $task.State -in @("Running", "Queued")) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-LocalUser -Name $userName -ErrorAction SilentlyContinue
}
