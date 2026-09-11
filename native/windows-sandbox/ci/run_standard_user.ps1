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
$script = Join-Path $PSScriptRoot "native_smoke.py"
$reportDirectory = Split-Path -Parent $Report
$stdoutPath = Join-Path $reportDirectory "standard-user-stdout.txt"
$stderrPath = Join-Path $reportDirectory "standard-user-stderr.txt"
$processReport = Join-Path $reportDirectory "standard-user-process.json"
$identityReport = Join-Path $reportDirectory "standard-user-identity.json"
$smokeProcess = $null

function Quote-PowerShellLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

try {
    if (Get-LocalUser -Name $userName -ErrorAction SilentlyContinue) {
        Remove-LocalUser -Name $userName
    }
    $ciUser = New-LocalUser -Name $userName -Password $password -PasswordNeverExpires
    $usersGroup = Get-LocalGroup -SID ([Security.Principal.SecurityIdentifier]::new("S-1-5-32-545"))
    if (-not @(Get-LocalGroupMember -Group $usersGroup | Where-Object { $_.SID -eq $ciUser.SID }).Count) {
        Add-LocalGroupMember -Group $usersGroup -Member $ciUser
    }
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
    & icacls.exe $reportDirectory /grant "${env:COMPUTERNAME}\${userName}:(OI)(CI)M" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not grant the CI account access to its report directory" }

    $pythonArguments = "-I `"$script`" --helper `"$Helper`" --node-source `"$NodeSource`" --report `"$Report`""
    # Start-Process supports Credential + LoadUserProfile on Windows. Avoid the
    # scheduler path that returned TASK_HAS_NOT_RUN on both hosted runner images.
    # Verify the actual identity before running the unchanged Python smoke.
    # https://learn.microsoft.com/powershell/module/microsoft.powershell.management/start-process
    $launcher = @"
`$ErrorActionPreference = 'Stop'
try {
    `$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    `$principal = [Security.Principal.WindowsPrincipal]::new(`$identity)
    `$isAdmin = `$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (`$identity.User.Value -ne $(Quote-PowerShellLiteral $ciUser.SID.Value) -or `$isAdmin) {
        throw 'standard-user launcher did not receive the exact non-admin CI identity'
    }
    `$identityData = @{
        userSid = `$identity.User.Value; isAdministrator = `$isAdmin
        sessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
    }
    `$identityData | ConvertTo-Json | Set-Content -LiteralPath $(Quote-PowerShellLiteral $identityReport) -Encoding utf8
    # Loading the user registry does not replace inherited USERPROFILE/TEMP.
    # Obtain these from this authenticated user's known folders, never the host.
    # A fresh logon may not have created every folder; the default overload
    # returns an empty string for a missing directory. Ask Windows to create it.
    # https://learn.microsoft.com/dotnet/api/system.environment.specialfolderoption
    `$knownFolders = @{}
    foreach (`$folderName in @('UserProfile', 'LocalApplicationData', 'ApplicationData')) {
        `$knownFolders[`$folderName] = [Environment]::GetFolderPath(`$folderName, 'Create')
        `$identityData.knownFolders = `$knownFolders
        `$identityData | ConvertTo-Json | Set-Content -LiteralPath $(Quote-PowerShellLiteral $identityReport) -Encoding utf8
        if (-not `$knownFolders[`$folderName]) { throw "Windows could not initialize CI known folder: `$folderName" }
        if (`$folderName -eq 'UserProfile') { `$env:USERPROFILE = `$knownFolders[`$folderName] }
    }
    `$env:LOCALAPPDATA = `$knownFolders.LocalApplicationData
    `$env:APPDATA = `$knownFolders.ApplicationData
    `$env:TEMP = Join-Path `$env:LOCALAPPDATA 'Temp'
    `$env:TMP = `$env:TEMP
    New-Item -ItemType Directory -Force -Path `$env:TEMP | Out-Null
    `$child = Start-Process -FilePath $(Quote-PowerShellLiteral $Python) -ArgumentList $(Quote-PowerShellLiteral $pythonArguments) -Wait -PassThru -RedirectStandardOutput $(Quote-PowerShellLiteral $stdoutPath) -RedirectStandardError $(Quote-PowerShellLiteral $stderrPath)
    exit `$child.ExitCode
}
catch {
    (`$_ | Out-String) | Out-File -LiteralPath $(Quote-PowerShellLiteral $stderrPath) -Append -Encoding utf8
    exit 1
}
"@
    # CreateProcessWithLogonW permits only 1024 command-line characters. Keep
    # the trusted fixture in a local file rather than expanding it to base64.
    $launcherPath = Join-Path $reportDirectory "standard-user-launcher.ps1"
    $launcher | Set-Content -LiteralPath $launcherPath -Encoding utf8BOM
    $powershell = Join-Path $env:SystemRoot "System32/WindowsPowerShell/v1.0/powershell.exe"
    $launcherArguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$launcherPath`""
    if ($powershell.Length + $launcherArguments.Length + 3 -ge 1024) {
        throw "standard-user launcher path exceeds the Windows logon command-line limit"
    }
    $credential = [Management.Automation.PSCredential]::new("${env:COMPUTERNAME}\$userName", $password)
    $smokeProcess = Start-Process -FilePath $powershell `
        -ArgumentList $launcherArguments `
        -WorkingDirectory $reportDirectory -Credential $credential -LoadUserProfile -PassThru
    # Keep the process handle alive even if the smoke fails before our first wait.
    $null = $smokeProcess.Handle
    if (-not $smokeProcess.WaitForExit(15 * 60 * 1000)) {
        throw "standard-user sandbox process timed out after 15 minutes"
    }
    if ($smokeProcess.ExitCode -ne 0) {
        throw "standard-user sandbox process failed with exit $($smokeProcess.ExitCode)"
    }
    if (-not (Test-Path -LiteralPath $Report)) {
        throw "standard-user sandbox task did not produce its timing report"
    }
}
catch {
    $failure = $_
    $status = @{ error = $failure.Exception.Message; started = $null -ne $smokeProcess }
    if ($null -ne $smokeProcess) {
        $status.processId = $smokeProcess.Id
        $status.hasExited = $smokeProcess.HasExited
        if ($smokeProcess.HasExited) { $status.exitCode = $smokeProcess.ExitCode }
    }
    $status | ConvertTo-Json | Set-Content -LiteralPath $processReport -Encoding utf8
    Write-Host ("standard-user process: " + ($status | ConvertTo-Json -Compress))
    foreach ($streamPath in @($stdoutPath, $stderrPath)) {
        if (Test-Path -LiteralPath $streamPath) {
            Write-Host ("standard-user captured " + [IO.Path]::GetFileName($streamPath) + " (last 100 lines):")
            Get-Content -LiteralPath $streamPath -Tail 100 | Write-Host
        }
    }
    throw $failure
}
finally {
    # Stop only the process tree started above, never processes selected by name.
    if ($null -ne $smokeProcess) {
        if (-not $smokeProcess.HasExited) {
            $smokeProcess.Kill($true)
            if (-not $smokeProcess.WaitForExit(10000)) { throw "CI smoke process did not stop" }
        }
        $smokeProcess.Dispose()
    }
    Remove-LocalUser -Name $userName -ErrorAction SilentlyContinue
}
