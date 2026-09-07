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

try {
    if (Get-LocalUser -Name $userName -ErrorAction SilentlyContinue) {
        Remove-LocalUser -Name $userName
    }
    New-LocalUser -Name $userName -Password $password -PasswordNeverExpires | Out-Null
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
    & icacls.exe $reportDirectory /grant "${env:COMPUTERNAME}\${userName}:(OI)(CI)M" | Out-Null

    $actionArguments = "-I `"$script`" --helper `"$Helper`" --node-source `"$NodeSource`" --report `"$Report`""
    $action = New-ScheduledTaskAction -Execute $Python -Argument $actionArguments
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -User "${env:COMPUTERNAME}\$userName" `
        -Password $passwordText `
        -RunLevel Limited `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    $started = Get-Date
    $deadline = (Get-Date).AddMinutes(15)
    do {
        Start-Sleep -Seconds 1
        $task = Get-ScheduledTask -TaskName $taskName
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        if ((Get-Date) -gt $deadline) {
            throw "standard-user sandbox task timed out"
        }
    } while (
        $info.LastRunTime -lt $started.AddSeconds(-1) -or
        $task.State -in @("Running", "Queued")
    )
    if ($info.LastTaskResult -ne 0) {
        throw "standard-user sandbox task failed with result $($info.LastTaskResult)"
    }
    if (-not (Test-Path -LiteralPath $Report)) {
        throw "standard-user sandbox task did not produce its timing report"
    }
}
finally {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-LocalUser -Name $userName -ErrorAction SilentlyContinue
}
