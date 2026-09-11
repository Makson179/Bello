param(
    [Parameter(Mandatory = $true)][string]$Helper,
    [Parameter(Mandatory = $true)][ValidateSet("Prepare", "Restore")][string]$Phase,
    [Parameter(Mandatory = $true)][string]$SnapshotPath,
    [Parameter(Mandatory = $true)][string]$Report
)

$ErrorActionPreference = "Stop"
$expectedMask = 0x12019f
$expectedCapability = "Bello.Sandbox.NullDevice.v1"

# This reader never opens a caller-selected path or writes a security descriptor.
# NtQuerySecurityObject preserves the raw kernel descriptor, including its label;
# Get-Acl/SE_FILE_OBJECT would incorrectly treat the DOS NUL alias as a file.
if (-not ("BelloCiNullSecurity" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BelloCiNullSecurity {
    [StructLayout(LayoutKind.Sequential)] struct UnicodeString {
        public ushort Length, MaximumLength; public IntPtr Buffer;
    }
    [StructLayout(LayoutKind.Sequential)] struct ObjectAttributes {
        public int Length; public IntPtr RootDirectory, ObjectName;
        public uint Attributes; public IntPtr SecurityDescriptor, SecurityQualityOfService;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoStatus {
        public IntPtr Status; public UIntPtr Information;
    }
    [DllImport("ntdll.dll")] static extern int NtOpenFile(out IntPtr handle, uint access,
        ref ObjectAttributes attributes, out IoStatus status, uint share, uint options);
    [DllImport("ntdll.dll")] static extern int NtQuerySecurityObject(IntPtr handle,
        uint information, IntPtr descriptor, uint length, out uint needed);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    static void Check(int status, string operation) {
        if (status < 0) throw new InvalidOperationException(operation + " NTSTATUS=0x" +
            unchecked((uint)status).ToString("x8"));
    }
    public static byte[] Read() {
        IntPtr text = IntPtr.Zero, name = IntPtr.Zero, handle = IntPtr.Zero, data = IntPtr.Zero;
        try {
            const string path = @"\Device\Null";
            text = Marshal.StringToHGlobalUni(path);
            var unicode = new UnicodeString { Length = (ushort)(path.Length * 2),
                MaximumLength = (ushort)((path.Length + 1) * 2), Buffer = text };
            name = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
            Marshal.StructureToPtr(unicode, name, false);
            var attributes = new ObjectAttributes { Length = Marshal.SizeOf(typeof(ObjectAttributes)),
                ObjectName = name, Attributes = 0x1040 }; // CASE_INSENSITIVE | DONT_REPARSE
            IoStatus io;
            Check(NtOpenFile(out handle, 0x20000, ref attributes, out io, 7, 0), "NtOpenFile(Null)");
            uint needed;
            // OWNER | GROUP | DACL | LABEL. No SACL/label write or privilege change.
            int status = NtQuerySecurityObject(handle, 0x17, IntPtr.Zero, 0, out needed);
            if (status != unchecked((int)0xc0000023)) Check(status, "NtQuerySecurityObject(size)");
            if (needed == 0 || needed > 1024 * 1024) throw new InvalidOperationException("Invalid Null descriptor size");
            data = Marshal.AllocHGlobal((int)needed);
            uint returned;
            Check(NtQuerySecurityObject(handle, 0x17, data, needed, out returned), "NtQuerySecurityObject(Null)");
            if (returned == 0 || returned > needed) throw new InvalidOperationException("Invalid returned descriptor size");
            var bytes = new byte[returned];
            Marshal.Copy(data, bytes, 0, bytes.Length);
            return bytes;
        }
        finally {
            if (data != IntPtr.Zero) Marshal.FreeHGlobal(data);
            if (handle != IntPtr.Zero) CloseHandle(handle);
            if (name != IntPtr.Zero) Marshal.FreeHGlobal(name);
            if (text != IntPtr.Zero) Marshal.FreeHGlobal(text);
        }
    }
}
'@
}

function Invoke-NullOperation([string]$Operation) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Helper
    $start.ArgumentList.Add($Operation)
    $start.ArgumentList.Add("--null-device")
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    $started = $false
    try {
        if (-not $process.Start()) { throw "Null setup helper did not start" }
        $started = $true
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(60000)) { throw "Null setup helper timed out" }
        $output = $stdout.GetAwaiter().GetResult()
        $diagnostic = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0 -or $diagnostic) {
            throw "$Operation --null-device failed with exit $($process.ExitCode): $diagnostic"
        }
        $status = $output | ConvertFrom-Json -AsHashtable
        if ($status.protocolVersion -ne 1 -or $status.kind -ne "nullDevicePreparation" -or
            $status.operation -ne $Operation.Substring(5) -or $status.path -cne '\Device\Null' -or
            $status.capabilityName -cne $expectedCapability -or $status.capabilitySid -notmatch '^S-1-15-3-' -or
            $status.accessMask -ne $expectedMask -or $status.lifetime -cne "untilReboot" -or
            $status.prepared -isnot [bool] -or $status.changed -isnot [bool]) {
            throw "unexpected Null setup response: $output"
        }
        return $status
    }
    finally {
        if ($started -and -not $process.HasExited) {
            $process.Kill($true)
            if (-not $process.WaitForExit(10000)) { throw "Null setup helper did not stop" }
        }
        $process.Dispose()
    }
}

function Read-NullSnapshot {
    $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new([BelloCiNullSecurity]::Read(), 0)
    if ($null -eq $descriptor.DiscretionaryAcl) { throw "Null unexpectedly has a null DACL" }
    $sacl = $null
    if ($null -ne $descriptor.SystemAcl) {
        $bytes = [byte[]]::new($descriptor.SystemAcl.BinaryLength)
        $descriptor.SystemAcl.GetBinaryForm($bytes, 0)
        $sacl = [Convert]::ToBase64String($bytes)
    }
    $aces = @(
        foreach ($ace in $descriptor.DiscretionaryAcl) {
            $bytes = [byte[]]::new($ace.BinaryLength)
            $ace.GetBinaryForm($bytes, 0)
            @{
                binary = [Convert]::ToBase64String($bytes)
                sid = if ($ace -is [Security.AccessControl.KnownAce]) { $ace.SecurityIdentifier.Value } else { $null }
                mask = if ($ace -is [Security.AccessControl.KnownAce]) { $ace.AccessMask } else { $null }
                type = [int]$ace.AceType; flags = [int]$ace.AceFlags
            }
        }
    )
    return @{ owner = $descriptor.Owner.Value; group = $descriptor.Group.Value
        control = [int]$descriptor.ControlFlags; revision = [int]$descriptor.DiscretionaryAcl.Revision
        sacl = $sacl; aces = $aces }
}

function Assert-NullEqual($Expected, $Actual) {
    foreach ($field in @("owner", "group", "control", "revision", "sacl")) {
        if ($Expected[$field] -cne $Actual[$field]) { throw "Null setup changed unrelated security field: $field" }
    }
    $before = @($Expected.aces | ForEach-Object { $_.binary }) | ConvertTo-Json -Compress
    $after = @($Actual.aces | ForEach-Object { $_.binary }) | ConvertTo-Json -Compress
    if ($before -cne $after) { throw "Null setup changed unrelated ACE bytes or order" }
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SnapshotPath) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Report) | Out-Null
if ($Phase -eq "Prepare") {
    $beforeStatus = Invoke-NullOperation "host-status"
    $before = Read-NullSnapshot
    @{ status = $beforeStatus; acl = $before } | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $SnapshotPath -Encoding utf8
    $prepared = Invoke-NullOperation "host-prepare"
    $again = Invoke-NullOperation "host-prepare"
    $status = Invoke-NullOperation "host-status"
    foreach ($result in @($prepared, $again, $status)) {
        if (-not $result.prepared -or $result.capabilitySid -cne $beforeStatus.capabilitySid) {
            throw "Null preparation did not retain the fixed capability and ready state"
        }
    }
    if ($again.changed -or $prepared.changed -eq $beforeStatus.prepared) { throw "Null preparation was not idempotent" }
    $after = Read-NullSnapshot
    $matching = @($after.aces | Where-Object { $_.sid -eq $status.capabilitySid })
    if ($matching.Count -ne 1 -or $matching[0].type -ne 0 -or
        $matching[0].flags -ne 0 -or $matching[0].mask -ne $expectedMask) {
        throw "Null setup did not produce exactly the fixed non-inheriting capability ACE"
    }
    if (-not $beforeStatus.prepared) {
        if (@($before.aces | Where-Object { $_.sid -eq $status.capabilitySid }).Count) { throw "Unexpected pre-existing Null capability ACE" }
        $after.aces = @($after.aces | Where-Object { $_.sid -ne $status.capabilitySid })
    }
    Assert-NullEqual $before $after
    @{ phase = $Phase; path = $status.path; accessMask = $expectedMask; capabilitySid = $status.capabilitySid
        prepared = $true; idempotent = $true; unrelatedAclUnchanged = $true; labelUnchanged = $true } |
        ConvertTo-Json | Set-Content -LiteralPath $Report -Encoding utf8
}
else {
    $snapshot = Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json -AsHashtable
    $status = Invoke-NullOperation "host-status"
    if ($status.capabilitySid -cne $snapshot.status.capabilitySid -or $snapshot.status.path -cne '\Device\Null') {
        throw "Null cleanup does not match its saved fixed target"
    }
    if (-not $snapshot.status.prepared) {
        $removed = Invoke-NullOperation "host-remove"
        $again = Invoke-NullOperation "host-remove"
        $status = Invoke-NullOperation "host-status"
        if ($removed.prepared -or $again.prepared -or $status.prepared -or $again.changed) { throw "Null removal was not idempotent" }
    }
    elseif (-not $status.prepared) { throw "Pre-existing Null preparation disappeared" }
    Assert-NullEqual $snapshot.acl (Read-NullSnapshot)
    @{ phase = $Phase; originalAclRestored = $true; labelUnchanged = $true
        preservedExistingSetup = $snapshot.status.prepared } |
        ConvertTo-Json | Set-Content -LiteralPath $Report -Encoding utf8
}
