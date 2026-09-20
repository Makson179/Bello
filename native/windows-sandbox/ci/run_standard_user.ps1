param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Helper,
    [Parameter(Mandatory = $true)][string]$NodeSource,
    [Parameter(Mandatory = $true)][string]$NativeTestBinary,
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
$mutexHolder = $null

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

    # Keep the production mutex alive from an actually elevated creator until
    # the complete ordinary-user smoke finishes. Do not acquire/wait on it here.
    $holderStart = [Diagnostics.ProcessStartInfo]::new()
    $holderStart.FileName = $NativeTestBinary
    $holderStart.UseShellExecute = $false
    $holderStart.RedirectStandardInput = $true
    $holderStart.RedirectStandardOutput = $true
    $holderStart.RedirectStandardError = $true
    foreach ($argument in @('--exact', 'global_acl_lock::tests::ci_hold_global_acl_object', '--nocapture')) {
        $holderStart.ArgumentList.Add($argument)
    }
    $holderStart.Environment['BELLO_TEST_ACL_HOLDER'] = '1'
    $mutexHolder = [Diagnostics.Process]::Start($holderStart)
    $holderErrors = $mutexHolder.StandardError.ReadToEndAsync()
    $holderReady = $null
    $holderDeadline = [Diagnostics.Stopwatch]::StartNew()
    while ($holderDeadline.Elapsed.TotalSeconds -lt 10) {
        $lineTask = $mutexHolder.StandardOutput.ReadLineAsync()
        $remaining = [Math]::Max(1, 10000 - [int]$holderDeadline.ElapsedMilliseconds)
        if (-not $lineTask.Wait($remaining)) { break }
        $line = $lineTask.Result
        if ($null -eq $line) { break }
        if ($line.StartsWith('BELLO_ACL_HOLDER_READY=')) {
            $holderReady = $line.Substring('BELLO_ACL_HOLDER_READY='.Length) | ConvertFrom-Json
            break
        }
    }
    if ($null -eq $holderReady -or $holderReady.elevated -ne $true -or $holderReady.integrityRid -lt 0x3000 -or $mutexHolder.HasExited) {
        throw 'elevated ACL mutex holder did not become ready within 10 seconds'
    }
    $holderReady | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reportDirectory 'global-acl-mutex-holder.json') -Encoding utf8

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
        environmentBefore = @{
            USERPROFILE = `$env:USERPROFILE; LOCALAPPDATA = `$env:LOCALAPPDATA
            APPDATA = `$env:APPDATA; TEMP = `$env:TEMP
        }
    }
    `$identityData | ConvertTo-Json | Set-Content -LiteralPath $(Quote-PowerShellLiteral $identityReport) -Encoding utf8
    # Loading the user registry does not replace inherited USERPROFILE/TEMP.
    # Compile only this CI helper in the account's already writable report dir.
    `$env:TEMP = $(Quote-PowerShellLiteral $reportDirectory)
    `$env:TMP = `$env:TEMP
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class BelloCiUserEnvironment {
    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr data, int length, out int needed);
    [DllImport("advapi32.dll")]
    private static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
    [DllImport("advapi32.dll")]
    private static extern IntPtr GetSidSubAuthority(IntPtr sid, int index);
    public static int IntegrityRid(IntPtr token) {
        int needed;
        GetTokenInformation(token, 25, IntPtr.Zero, 0, out needed);
        if (Marshal.GetLastWin32Error() != 122 || needed < IntPtr.Size || needed > 16384)
            throw new InvalidOperationException("Invalid CI token integrity query");
        IntPtr data = Marshal.AllocHGlobal(needed);
        try {
            if (!GetTokenInformation(token, 25, data, needed, out needed))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "GetTokenInformation(TokenIntegrityLevel) failed");
            IntPtr sid = Marshal.ReadIntPtr(data);
            int count = Marshal.ReadByte(GetSidSubAuthorityCount(sid));
            if (count == 0) throw new InvalidOperationException("Invalid CI integrity SID");
            return Marshal.ReadInt32(GetSidSubAuthority(sid, count - 1));
        }
        finally { Marshal.FreeHGlobal(data); }
    }
    [DllImport("userenv.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateEnvironmentBlock(out IntPtr block, IntPtr token, bool inherit);
    [DllImport("userenv.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DestroyEnvironmentBlock(IntPtr block);
    public static Dictionary<string, string> Read(IntPtr token) {
        IntPtr block;
        if (!CreateEnvironmentBlock(out block, token, false))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateEnvironmentBlock failed");
        Exception readError = null;
        try {
            var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            const int maxChars = 1024 * 1024;
            int offset = 0;
            while (offset < maxChars) {
                if (Marshal.ReadInt16(block, offset * 2) == 0) return values;
                int start = offset;
                while (offset < maxChars && Marshal.ReadInt16(block, offset * 2) != 0) offset++;
                if (offset == maxChars) break;
                string entry = Marshal.PtrToStringUni(IntPtr.Add(block, start * 2), offset - start);
                int separator = entry.IndexOf('=', 1);
                if (separator < 1) throw new InvalidOperationException("Invalid OS environment entry");
                // Windows may include hidden =C: drive-working-directory entries.
                // These are not ordinary variables; the launcher sets its cwd explicitly.
                if (entry[0] != '=') values[entry.Substring(0, separator)] = entry.Substring(separator + 1);
                offset++;
            }
            throw new InvalidOperationException("OS environment block exceeded the CI parsing limit");
        }
        catch (Exception error) { readError = error; throw; }
        finally {
            if (!DestroyEnvironmentBlock(block)) {
                var cleanupError = new Win32Exception(Marshal.GetLastWin32Error(), "DestroyEnvironmentBlock failed");
                if (readError != null) throw new AggregateException(readError, cleanupError);
                throw cleanupError;
            }
        }
    }
}
'@
    `$integrity = [BelloCiUserEnvironment]::IntegrityRid(`$identity.Token)
    if (`$integrity -lt 0x2000 -or `$integrity -ge 0x3000) {
        throw 'standard-user launcher did not receive a medium-integrity token'
    }
    `$identityData.integrityRid = `$integrity
    # Ask Windows for this actual user's variables, with no parent inheritance.
    # https://learn.microsoft.com/windows/win32/api/userenv/nf-userenv-createenvironmentblock
    `$userEnvironment = [BelloCiUserEnvironment]::Read(`$identity.Token)
    foreach (`$variableName in @([Environment]::GetEnvironmentVariables().Keys)) {
        if (-not `$variableName.StartsWith('=') -and -not `$userEnvironment.ContainsKey(`$variableName)) {
            [Environment]::SetEnvironmentVariable(`$variableName, `$null, 'Process')
        }
    }
    foreach (`$variable in `$userEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable(`$variable.Key, `$variable.Value, 'Process')
    }
    `$identityData.environmentAfter = @{
        USERPROFILE = `$env:USERPROFILE; LOCALAPPDATA = `$env:LOCALAPPDATA
        APPDATA = `$env:APPDATA; TEMP = `$env:TEMP
    }
    `$identityData | ConvertTo-Json | Set-Content -LiteralPath $(Quote-PowerShellLiteral $identityReport) -Encoding utf8
    # Use the OS-returned paths directly. Shell folder APIs in this already
    # running PowerShell may have cached values from its former environment.
    `$knownFolders = @{}
    `$folderVariables = @{ UserProfile = 'USERPROFILE'; LocalApplicationData = 'LOCALAPPDATA'; ApplicationData = 'APPDATA'; Temp = 'TEMP' }
    foreach (`$folderName in @('UserProfile', 'LocalApplicationData', 'ApplicationData', 'Temp')) {
        `$folderPath = `$userEnvironment[`$folderVariables[`$folderName]]
        `$knownFolders[`$folderName] = `$folderPath
        `$identityData.knownFolders = `$knownFolders
        `$identityData | ConvertTo-Json | Set-Content -LiteralPath $(Quote-PowerShellLiteral $identityReport) -Encoding utf8
        if (-not `$folderPath -or `$folderPath -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+(?:\\|$))') {
            throw "Windows did not return an absolute CI user directory: `$folderName"
        }
        New-Item -ItemType Directory -Force -Path `$folderPath | Out-Null
    }
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
    if ($mutexHolder.HasExited) { throw 'elevated mutex holder exited before the standard-user smoke completed' }
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
    try {
        if ($null -ne $smokeProcess) {
            if (-not $smokeProcess.HasExited) {
                $smokeProcess.Kill($true)
                if (-not $smokeProcess.WaitForExit(10000)) { throw "CI smoke process did not stop" }
            }
            $smokeProcess.Dispose()
        }
    }
    finally {
        try {
            if ($null -ne $mutexHolder) {
                try {
                    $mutexHolder.StandardInput.Dispose()
                    if (-not $mutexHolder.WaitForExit(10000)) {
                        $mutexHolder.Kill($true)
                        if (-not $mutexHolder.WaitForExit(10000)) { throw 'CI mutex holder did not stop' }
                    }
                    $holderErrorText = $holderErrors.GetAwaiter().GetResult()
                    $holderErrorText | Set-Content -LiteralPath (Join-Path $reportDirectory 'global-acl-mutex-holder-stderr.txt') -Encoding utf8
                    if ($mutexHolder.ExitCode -ne 0) { throw "CI mutex holder failed: $holderErrorText" }
                }
                finally { $mutexHolder.Dispose() }
            }
        }
        finally { Remove-LocalUser -Name $userName -ErrorAction SilentlyContinue }
    }
}
