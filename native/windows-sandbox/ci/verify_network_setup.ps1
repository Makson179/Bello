param(
    [Parameter(Mandatory = $true)][string]$Helper,
    [Parameter(Mandatory = $true)][ValidateSet("Prepare", "Restore")][string]$Phase,
    [Parameter(Mandatory = $true)][string]$SnapshotPath,
    [Parameter(Mandatory = $true)][string]$Report
)

$ErrorActionPreference = "Stop"
$serviceName = "BelloOfflineNetwork"
$expectedImage = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'BelloOfflineNetwork/bello-windows-sandbox.exe'))

function Invoke-BoundedCapture([string]$File, [string[]]$Arguments) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $File
    foreach ($argument in $Arguments) { $start.ArgumentList.Add($argument) }
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    $started = $false
    try {
        if (-not $process.Start()) { throw "Network setup check did not start" }
        $started = $true
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(60000)) { throw "Network setup check timed out" }
        $output = $stdout.GetAwaiter().GetResult()
        $diagnostic = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0 -or $diagnostic) {
            throw "Network setup check failed with exit $($process.ExitCode): $diagnostic $output"
        }
        return $output
    }
    finally {
        if ($started -and -not $process.HasExited) {
            $process.Kill($true)
            if (-not $process.WaitForExit(10000)) { throw "Network setup check did not stop" }
        }
        $process.Dispose()
    }
}

function Invoke-NetworkOperation([string]$Operation) {
    $output = Invoke-BoundedCapture $Helper @($Operation, '--network')
    $status = $output | ConvertFrom-Json -AsHashtable
    if ($status.protocolVersion -ne 1 -or $status.policyVersion -ne 1 -or
        $status.kind -cne 'networkPreparation' -or $status.operation -cne $Operation.Substring(5) -or
        $status.serviceName -cne $serviceName -or $status.installPath -ine $expectedImage) {
        throw "Unexpected network setup response: $output"
    }
    foreach ($field in @('installed', 'running', 'prepared', 'changed', 'binaryMatches')) {
        if ($status[$field] -isnot [bool]) { throw "Network setup field is not Boolean: $field" }
    }
    foreach ($field in @('servicePid', 'activeLeases', 'retainedLeases')) {
        if ($status[$field] -isnot [long] -and $status[$field] -isnot [int]) {
            throw "Network setup field is not an integer: $field"
        }
        if ($status[$field] -lt 0) { throw "Negative network setup field: $field" }
    }
    if ($status.prepared -and (-not $status.installed -or -not $status.running -or
        -not $status.binaryMatches -or $status.servicePid -eq 0)) {
        throw "Prepared network service is not installed, running, or the exact helper binary"
    }
    if (-not $status.installed -and ($status.running -or $status.prepared -or $status.servicePid -ne 0)) {
        throw "Absent network service reported an active process"
    }
    return $status
}

function Read-ServiceSnapshot {
    # Query only Bello's fixed service. Never enumerate or export other services,
    # firewall rules, environment variables, or packet contents.
    $services = @(Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -OperationTimeoutSec 30)
    if ($services.Count -eq 0) { return $null }
    if ($services.Count -ne 1) { throw "Network service name did not identify exactly one service" }
    $service = $services[0]
    $sc = Join-Path ([Environment]::SystemDirectory) 'sc.exe'
    $securityText = (Invoke-BoundedCapture $sc @('sdshow', $serviceName)).Trim()
    $security = [Security.AccessControl.RawSecurityDescriptor]::new($securityText)
    if ($null -eq $security.DiscretionaryAcl) { throw "Network service unexpectedly has a null DACL" }
    $imageHash = $null
    $imageSecurity = $null
    if (Test-Path -LiteralPath $expectedImage -PathType Leaf) {
        $imageHash = (Get-FileHash -LiteralPath $expectedImage -Algorithm SHA256).Hash
        $imageSecurity = (Get-Acl -LiteralPath $expectedImage).Sddl
    }
    return @{
        account = $service.StartName; startMode = $service.StartMode
        path = $service.PathName; type = $service.ServiceType
        interactive = $service.DesktopInteract; errorControl = $service.ErrorControl
        security = $security.GetSddlForm([Security.AccessControl.AccessControlSections]::All)
        imageHash = $imageHash; imageSecurity = $imageSecurity
    }
}

function Assert-ServiceReady($Status) {
    if (-not $Status.prepared -or $Status.activeLeases -ne 0 -or $Status.retainedLeases -ne 0) {
        throw "CI setup requires a ready network service with no active or retained leases"
    }
    $snapshot = Read-ServiceSnapshot
    if ($null -eq $snapshot -or $snapshot.account -cne 'LocalSystem' -or
        $snapshot.startMode -cne 'Auto' -or $snapshot.type -cne 'Own Process' -or
        $snapshot.interactive -ne $false -or $snapshot.path -cne ('"' + $expectedImage + '" --network-service')) {
        throw "Network service does not have the fixed LocalSystem/automatic/noninteractive image configuration"
    }
    if ($snapshot.imageHash -cne (Get-FileHash -LiteralPath $Helper -Algorithm SHA256).Hash) {
        throw "Installed network service binary differs from the helper under test"
    }
    $service = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -OperationTimeoutSec 30
    if ($service.State -cne 'Running' -or $service.ProcessId -ne $Status.servicePid) {
        throw "SCM and the network broker disagree about the running service process"
    }
    return $snapshot
}

function Assert-ServiceEqual($Expected, $Actual) {
    if ($null -eq $Expected -or $null -eq $Actual) {
        if ($null -ne $Expected -or $null -ne $Actual) { throw "Network setup did not restore the original service presence" }
        return
    }
    foreach ($field in @('account', 'startMode', 'path', 'type', 'interactive', 'errorControl',
        'security', 'imageHash', 'imageSecurity')) {
        if ($Expected[$field] -cne $Actual[$field]) { throw "Network setup changed an existing service field: $field" }
    }
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SnapshotPath) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Report) | Out-Null
if ($Phase -eq 'Prepare') {
    $beforeStatus = Invoke-NetworkOperation 'host-status'
    $before = Read-ServiceSnapshot
    # Do not claim ownership of an incomplete or conflicting previous install.
    # Clean CI is either entirely unprepared or already valid and preserved.
    if ($beforeStatus.installed -and -not $beforeStatus.prepared) {
        throw 'CI will not change an existing incomplete network service installation'
    }
    if ($beforeStatus.installed -ne ($null -ne $before)) { throw 'SCM presence disagrees with network status' }
    if ($beforeStatus.prepared) { $null = Assert-ServiceReady $beforeStatus }
    @{ status = $beforeStatus; service = $before } | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $SnapshotPath -Encoding utf8
    $prepared = Invoke-NetworkOperation 'host-prepare'
    $again = Invoke-NetworkOperation 'host-prepare'
    $status = Invoke-NetworkOperation 'host-status'
    foreach ($result in @($prepared, $again, $status)) { $null = Assert-ServiceReady $result }
    if ($again.changed -or $prepared.changed -eq $beforeStatus.prepared -or $status.changed) {
        throw 'Network preparation was not idempotent'
    }
    if ($beforeStatus.prepared) { Assert-ServiceEqual $before (Read-ServiceSnapshot) }
    @{ phase = $Phase; prepared = $true; idempotent = $true; serviceName = $serviceName
        account = 'LocalSystem'; binaryMatches = $true; activeLeases = 0; retainedLeases = 0
        preservedExistingSetup = $beforeStatus.prepared } | ConvertTo-Json |
        Set-Content -LiteralPath $Report -Encoding utf8
}
else {
    $snapshot = Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json -AsHashtable
    if ($snapshot.status.serviceName -cne $serviceName -or $snapshot.status.installPath -ine $expectedImage) {
        throw 'Network cleanup does not match its saved fixed service'
    }
    $status = Invoke-NetworkOperation 'host-status'
    if ($status.activeLeases -ne 0 -or $status.retainedLeases -ne 0) {
        throw 'Network cleanup refuses to remove active or retained per-run filters'
    }
    if (-not $snapshot.status.prepared) {
        $removed = Invoke-NetworkOperation 'host-remove'
        $again = Invoke-NetworkOperation 'host-remove'
        $status = Invoke-NetworkOperation 'host-status'
        if ($removed.installed -or $again.installed -or $status.installed -or $again.changed -or $status.changed) {
            throw 'Network removal was not idempotent'
        }
    }
    else { $null = Assert-ServiceReady $status }
    Assert-ServiceEqual $snapshot.service (Read-ServiceSnapshot)
    @{ phase = $Phase; originalServiceRestored = $true; noActiveOrRetainedLeases = $true
        preservedExistingSetup = $snapshot.status.prepared } | ConvertTo-Json |
        Set-Content -LiteralPath $Report -Encoding utf8
}
