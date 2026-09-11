param(
    [Parameter(Mandatory = $true)][string]$Helper,
    [Parameter(Mandatory = $true)][ValidateSet("Prepare", "Restore")][string]$Phase,
    [Parameter(Mandatory = $true)][string]$SnapshotPath,
    [Parameter(Mandatory = $true)][string]$Report,
    [ValidatePattern('^[A-Za-z]:$')][string]$Drive
)

$ErrorActionPreference = "Stop"
$expectedMask = 0x120088
$expectedCapability = "Bello.Sandbox.SystemRootMetadata.v1"
$selectedDrive = if ($Drive) { $Drive.ToUpperInvariant() } else { "" }
$expectedKinds = if ($selectedDrive) { @("additionalDriveRoot") } else { @("systemDriveRoot", "userProfiles") }

function Invoke-HostOperation([string]$Operation) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Helper
    $start.ArgumentList.Add($Operation)
    if ($selectedDrive) {
        $start.ArgumentList.Add("--drive")
        $start.ArgumentList.Add($selectedDrive)
    }
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    $started = $false
    try {
        if (-not $process.Start()) { throw "host setup helper did not start" }
        $started = $true
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(60000)) { throw "host setup helper timed out" }
        $output = $stdout.GetAwaiter().GetResult()
        $diagnostic = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "$Operation failed with exit $($process.ExitCode): $diagnostic"
        }
        $status = $output | ConvertFrom-Json -AsHashtable
        if ($status.protocolVersion -ne 1 -or $status.kind -ne "hostPreparation" -or
            $status.operation -ne $Operation.Substring(5) -or $status.capabilityName -ne $expectedCapability -or
            $status.metadataMask -ne $expectedMask -or $status.systemRoot -notmatch '^[A-Za-z]:\\$' -or
            $status.capabilitySid -notmatch '^S-1-15-3-') {
            throw "unexpected host setup response: $output"
        }
        $targets = @($status.targets)
        if ($targets.Count -ne $expectedKinds.Count) { throw "unexpected number of selected host targets" }
        foreach ($kind in $expectedKinds) {
            $matching = @($targets | Where-Object { $_.kind -eq $kind })
            if ($matching.Count -ne 1 -or $matching[0].path -notmatch '^[A-Za-z]:\\' -or
                $matching[0].prepared -isnot [bool] -or $matching[0].changed -isnot [bool]) {
                throw "invalid or duplicated fixed host target: $kind"
            }
        }
        $expectedRoot = if ($selectedDrive) { "$selectedDrive\" } else { $status.systemRoot }
        $rootKind = if ($selectedDrive) { "additionalDriveRoot" } else { "systemDriveRoot" }
        $rootTarget = @($targets | Where-Object { $_.kind -eq $rootKind })[0]
        if ($rootTarget.path -cne $expectedRoot -or
            $status.prepared -ne (@($targets | Where-Object { $_.prepared }).Count -eq $expectedKinds.Count) -or
            $status.changed -ne (@($targets | Where-Object { $_.changed }).Count -gt 0)) {
            throw "inconsistent host target aggregate status"
        }
        return $status
    }
    finally {
        if ($started -and -not $process.HasExited) {
            $process.Kill($true)
            if (-not $process.WaitForExit(10000)) { throw "host setup helper did not stop" }
        }
        $process.Dispose()
    }
}

function Read-AclSnapshot([string]$Target) {
    $security = Get-Acl -LiteralPath $Target
    $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new(
        $security.GetSecurityDescriptorBinaryForm(), 0)
    if ($null -eq $descriptor.DiscretionaryAcl) { throw "CI target unexpectedly has a null DACL" }
    $aces = @(
        foreach ($ace in $descriptor.DiscretionaryAcl) {
            $bytes = [byte[]]::new($ace.BinaryLength)
            $ace.GetBinaryForm($bytes, 0)
            @{
                binary = [Convert]::ToBase64String($bytes)
                sid = if ($ace -is [Security.AccessControl.KnownAce]) { $ace.SecurityIdentifier.Value } else { $null }
                mask = if ($ace -is [Security.AccessControl.KnownAce]) { $ace.AccessMask } else { $null }
                type = [int]$ace.AceType
                flags = [int]$ace.AceFlags
            }
        }
    )
    return @{
        owner = $descriptor.Owner.Value
        group = $descriptor.Group.Value
        control = [int]$descriptor.ControlFlags
        revision = [int]$descriptor.DiscretionaryAcl.Revision
        aces = $aces
    }
}

function Assert-AclEqual($Expected, $Actual) {
    foreach ($field in @("owner", "group", "control", "revision")) {
        if ($Expected[$field] -ne $Actual[$field]) { throw "host setup changed unrelated ACL field: $field" }
    }
    $before = @($Expected.aces | ForEach-Object { $_.binary }) | ConvertTo-Json -Compress
    $after = @($Actual.aces | ForEach-Object { $_.binary }) | ConvertTo-Json -Compress
    if ($before -cne $after) { throw "host setup changed unrelated ACE bytes or order" }
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SnapshotPath) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Report) | Out-Null
if ($Phase -eq "Prepare") {
    $beforeStatus = Invoke-HostOperation "host-status"
    # The product supports mixed states. This disposable CI fixture refuses one
    # before any mutation so all-target removal cannot erase pre-existing setup.
    $preparedCount = @($beforeStatus.targets | Where-Object { $_.prepared }).Count
    if ($preparedCount -gt 0 -and $preparedCount -lt $expectedKinds.Count) {
        throw "CI setup fixture requires selected targets initially all prepared or all absent"
    }
    $before = @{}
    foreach ($target in $beforeStatus.targets) { $before[$target.kind] = Read-AclSnapshot $target.path }
    $snapshot = @{ selectedDrive = $selectedDrive; status = $beforeStatus; acls = $before }
    # Save before the first mutation, so an assertion failure still permits rollback.
    $snapshot | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SnapshotPath -Encoding utf8
    $prepared = Invoke-HostOperation "host-prepare"
    $again = Invoke-HostOperation "host-prepare"
    $status = Invoke-HostOperation "host-status"
    if (-not $prepared.prepared -or -not $status.prepared -or -not $again.prepared -or $again.changed) {
        throw "host prepare did not establish an idempotent ready state"
    }
    foreach ($result in @($prepared, $again, $status)) {
        if ($result.systemRoot -cne $beforeStatus.systemRoot -or
            $result.capabilitySid -cne $beforeStatus.capabilitySid) {
            throw "host setup changed its fixed target or capability identity"
        }
        foreach ($target in $result.targets) {
            $original = @($beforeStatus.targets | Where-Object { $_.kind -eq $target.kind })[0]
            if ($target.path -cne $original.path) { throw "host setup changed an OS target path" }
        }
    }
    foreach ($target in $status.targets) {
        $after = Read-AclSnapshot $target.path
        $capabilityAces = @($after.aces | Where-Object { $_.sid -eq $status.capabilitySid })
        if ($capabilityAces.Count -ne 1 -or $capabilityAces[0].type -ne 0 -or
            $capabilityAces[0].flags -ne 0 -or $capabilityAces[0].mask -ne $expectedMask) {
            throw "setup did not produce exactly the non-inheriting metadata-only capability ACE"
        }
        if ($beforeStatus.prepared) {
            if ($prepared.changed) { throw "prepare changed an already prepared host" }
        }
        else {
            if (-not $prepared.changed -or @($before[$target.kind].aces | Where-Object { $_.sid -eq $status.capabilitySid }).Count) {
                throw "unexpected capability ACL before first setup"
            }
            $after.aces = @($after.aces | Where-Object { $_.sid -ne $status.capabilitySid })
        }
        Assert-AclEqual $before[$target.kind] $after
    }
    @{
        phase = $Phase; systemRoot = $status.systemRoot; capabilitySid = $status.capabilitySid
        metadataMask = $expectedMask; prepared = $status.prepared; changed = $prepared.changed
        targets = $status.targets
        idempotent = $true; unrelatedAclUnchanged = $true; inherits = $false
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Report -Encoding utf8
}
else {
    $snapshot = Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json -AsHashtable
    if ([string]$snapshot.selectedDrive -cne $selectedDrive) {
        throw "host cleanup selection does not match the saved setup selection"
    }
    $status = Invoke-HostOperation "host-status"
    if ($status.systemRoot -cne $snapshot.status.systemRoot -or
        $status.capabilitySid -cne $snapshot.status.capabilitySid) {
        throw "host cleanup target does not match the saved setup target"
    }
    foreach ($target in $status.targets) {
        $original = @($snapshot.status.targets | Where-Object { $_.kind -eq $target.kind })[0]
        if ($target.path -cne $original.path) { throw "host cleanup OS target does not match the saved target" }
    }
    if (-not $snapshot.status.prepared) {
        # All foreground tests and their child Jobs have finished before this CI step.
        # Remove only this fixture's new exact ACE, never overwrite the host DACL.
        $removed = Invoke-HostOperation "host-remove"
        $again = Invoke-HostOperation "host-remove"
        $status = Invoke-HostOperation "host-status"
        if ($removed.prepared -or $again.prepared -or $status.prepared -or $again.changed) {
            throw "host remove did not establish an idempotent unprepared state"
        }
    }
    elseif (-not $status.prepared) { throw "pre-existing host setup disappeared" }
    foreach ($target in $status.targets) {
        Assert-AclEqual $snapshot.acls[$target.kind] (Read-AclSnapshot $target.path)
    }
    @{ phase = $Phase; originalAclRestored = $true; preservedExistingSetup = $snapshot.status.prepared } |
        ConvertTo-Json | Set-Content -LiteralPath $Report -Encoding utf8
}
