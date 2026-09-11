# CI-only, bounded diagnostics. Raw machine-wide ETL/XML never enters OutputDirectory.
# https://learn.microsoft.com/windows-server/administration/windows-commands/logman-create-trace
# https://learn.microsoft.com/windows-server/administration/windows-commands/tracerpt
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$Repository = (Resolve-Path (Join-Path $PSScriptRoot "../../..")).Path,
    [string]$NativeTestExecutable,
    [ValidateRange(1, 60)][int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$processProvider = "Microsoft-Windows-Kernel-Process"
$fileProvider = "Microsoft-Windows-Kernel-File"
$providerNamesByGuid = @{}

function Invoke-BoundedProcess {
    param([string]$Executable, [string[]]$Arguments, [int]$Limit = 60)
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Executable
    $info.WorkingDirectory = $Repository
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $child = [System.Diagnostics.Process]::new()
    $child.StartInfo = $info
    $childStarted = $false
    try {
        $beforeStart = [DateTime]::UtcNow
        if (-not $child.Start()) { throw "Could not start $Executable" }
        $childStarted = $true
        $childId = $child.Id
        $stdout = $child.StandardOutput.ReadToEndAsync()
        $stderr = $child.StandardError.ReadToEndAsync()
        $timedOut = -not $child.WaitForExit($Limit * 1000)
        if ($timedOut) {
            # Only this newly started process and its descendants; no name-wide kill.
            $child.Kill($true)
            if (-not $child.WaitForExit(5000)) { throw "Owned process $childId did not stop" }
        }
        if (-not [System.Threading.Tasks.Task]::WaitAll(
            [System.Threading.Tasks.Task[]]@($stdout, $stderr), 5000
        )) { throw "Owned process $childId left its output pipes open" }
        return [pscustomobject]@{
            ProcessId = $childId; Started = $beforeStart; Ended = [DateTime]::UtcNow
            ExitCode = $child.ExitCode; TimedOut = $timedOut
            Stdout = $stdout.Result; Stderr = $stderr.Result
        }
    }
    finally {
        if ($childStarted -and -not $child.HasExited) { $child.Kill($true) }
        $child.Dispose()
    }
}

function Invoke-TraceUtility {
    param([string]$Executable, [string[]]$Arguments)
    $result = Invoke-BoundedProcess $Executable $Arguments
    if ($result.TimedOut -or $result.ExitCode -ne 0) {
        throw "$Executable failed (exit $($result.ExitCode), timeout $($result.TimedOut)): $($result.Stderr.Trim()) $($result.Stdout.Trim())"
    }
    return $result
}

function Convert-TraceNumber {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    if ($Value.StartsWith("0x", [StringComparison]::OrdinalIgnoreCase)) {
        return [Convert]::ToUInt64($Value.Substring(2), 16)
    }
    return [UInt64]::Parse($Value, [Globalization.CultureInfo]::InvariantCulture)
}

function Get-PayloadValue {
    param($Payload, [string[]]$Names)
    foreach ($name in $Names) {
        if ($Payload.Contains($name)) { return [string]$Payload[$name] }
    }
    return $null
}

function Read-TraceEvents {
    param([string]$XmlPath)
    # XmlReader avoids loading an expanded machine-wide document into a DOM.
    $settings = [Xml.XmlReaderSettings]::new()
    $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $settings.MaxCharactersInDocument = 512MB
    $reader = [Xml.XmlReader]::Create($XmlPath, $settings)
    $events = [Collections.Generic.List[object]]::new()
    try {
        while (-not $reader.EOF) {
            if ($reader.NodeType -ne [Xml.XmlNodeType]::Element -or $reader.LocalName -ne "Event") {
                [void]$reader.Read()
                continue
            }
            $document = [Xml.XmlDocument]::new()
            $document.XmlResolver = $null
            $document.LoadXml($reader.ReadOuterXml())
            $system = $document.SelectSingleNode("/*[local-name()='Event']/*[local-name()='System']")
            if ($null -eq $system) { continue }
            $providerNode = $system.SelectSingleNode("*[local-name()='Provider']")
            if ($null -eq $providerNode) { continue }
            $provider = $providerNode.GetAttribute("Name")
            if (-not $provider) { $provider = $providerNamesByGuid[$providerNode.GetAttribute("Guid").Trim('{}').ToLowerInvariant()] }
            if ($provider -notin @($processProvider, $fileProvider)) { continue }
            $payload = [ordered]@{}
            foreach ($node in $document.SelectNodes(
                "/*[local-name()='Event']/*[local-name()='EventData']/* | /*[local-name()='Event']/*[local-name()='UserData']//*[not(*)]"
            )) {
                $name = $node.GetAttribute("Name")
                if (-not $name) { $name = $node.LocalName }
                # Process command lines/environment are unnecessary for file-access diagnosis.
                if ($name -match '(?i)environment|commandline|cmdline') { continue }
                $payload[$name] = $node.InnerText
            }
            $execution = $system.SelectSingleNode("*[local-name()='Execution']")
            $time = $system.SelectSingleNode("*[local-name()='TimeCreated']")
            $eventId = $system.SelectSingleNode("*[local-name()='EventID']")
            if ($null -eq $execution -or $null -eq $time -or $null -eq $eventId) {
                throw "Trace event lacks execution, timestamp, or event ID metadata"
            }
            $events.Add([pscustomobject]@{
                Sequence = $events.Count
                Time = [DateTime]::Parse($time.GetAttribute("SystemTime"),
                    [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
                Provider = $provider; EventId = [int]$eventId.InnerText
                ProcessId = Convert-TraceNumber $execution.GetAttribute("ProcessID")
                ThreadId = Convert-TraceNumber $execution.GetAttribute("ThreadID")
                Payload = $payload
            })
            if ($events.Count -gt 250000) { throw "Diagnostic event limit exceeded; no partial trace will be presented as complete" }
        }
    }
    finally { $reader.Dispose() }
    if ($events.Count -eq 0) { throw "No decoded Kernel-File/Kernel-Process events; provider schema unavailable" }
    return $events.ToArray() | Sort-Object Time, Sequence
}

function Export-OwnedEvents {
    param($Events, $ProbeResult, [string]$Destination)
    # Compute lifetimes before the ancestry closure: PID reuse must not attach an
    # unrelated process to our tree. Kernel-Process event 1=start, 2=stop.
    $processEvents = @($Events | Where-Object Provider -eq $processProvider)
    $lifetimes = [Collections.Generic.List[object]]::new()
    foreach ($event in $processEvents) {
        if ($event.EventId -ne 1) { continue }
        $processIdValue = Convert-TraceNumber (Get-PayloadValue $event.Payload @("ProcessID", "ProcessId"))
        $parentIdValue = Convert-TraceNumber (Get-PayloadValue $event.Payload @("ParentProcessID", "ParentProcessId"))
        if ($null -eq $processIdValue -or $null -eq $parentIdValue) { throw "Process-start schema lacks process/parent IDs" }
        $lifetimes.Add([pscustomobject]@{
            ProcessId = $processIdValue; ParentId = $parentIdValue
            Start = $event.Time; End = [DateTime]::MaxValue; Own = $false
            Image = Get-PayloadValue $event.Payload @("ImageName", "ImageFileName")
        })
    }
    foreach ($life in $lifetimes) {
        foreach ($event in $processEvents) {
            if ($event.Time -lt $life.Start) { continue }
            $eventProcessId = Convert-TraceNumber (Get-PayloadValue $event.Payload @("ProcessID", "ProcessId"))
            if ($eventProcessId -ne $life.ProcessId) { continue }
            if ($event.EventId -eq 2 -or ($event.EventId -eq 1 -and $event.Time -gt $life.Start)) {
                $life.End = $event.Time
                break
            }
        }
    }
    $roots = @($lifetimes | Where-Object {
        $_.ProcessId -eq $ProbeResult.ProcessId -and $_.Start -ge $ProbeResult.Started -and $_.Start -le $ProbeResult.Ended
    })
    if ($roots.Count -ne 1) { throw "Could not identify exactly one traced probe process; no machine-wide events will be exported" }
    $roots[0].Own = $true
    do {
        $changed = $false
        foreach ($life in $lifetimes) {
            if ($life.Own) { continue }
            foreach ($parent in $lifetimes) {
                if ($parent.Own -and $parent.ProcessId -eq $life.ParentId -and
                    $life.Start -ge $parent.Start -and $life.Start -lt $parent.End) {
                    $life.Own = $true; $changed = $true; break
                }
            }
        }
    } while ($changed)
    $ownedLifetimes = @($lifetimes | Where-Object Own)
    $pendingIrps = @{}
    $selected = [Collections.Generic.List[object]]::new()
    $linkedCompletions = 0
    foreach ($event in $Events) {
        $candidateId = $event.ProcessId
        if ($event.Provider -eq $processProvider) {
            $candidateId = Convert-TraceNumber (Get-PayloadValue $event.Payload @("ProcessID", "ProcessId"))
        }
        $isOwn = $false
        foreach ($life in $ownedLifetimes) {
            if ($candidateId -eq $life.ProcessId -and $event.Time -ge $life.Start -and $event.Time -le $life.End) {
                $isOwn = $true; break
            }
        }
        $relatedStart = $null
        if ($event.Provider -eq $fileProvider) {
            $irpText = Get-PayloadValue $event.Payload @("Irp", "IrpPtr")
            $irp = Convert-TraceNumber $irpText
            if ($null -ne $irp -and $irp -ne 0) {
                $status = Get-PayloadValue $event.Payload @("NtStatus", "Status")
                if ($null -ne $status) {
                    # OperationEnd can run under PID 4. Match only the current
                    # operation, not every historical occurrence of a reused IRP.
                    if ($pendingIrps.ContainsKey($irp) -and $pendingIrps[$irp].Own) {
                        $isOwn = $true
                        $relatedStart = $pendingIrps[$irp].Sequence
                        $linkedCompletions++
                    }
                    $pendingIrps.Remove($irp)
                }
                else { $pendingIrps[$irp] = @{ Own = $isOwn; Sequence = $event.Sequence } }
            }
        }
        if ($isOwn) {
            $selected.Add([pscustomobject]@{
                Sequence = $event.Sequence; Time = $event.Time.ToString("o")
                Provider = $event.Provider; EventId = $event.EventId
                ProcessId = $event.ProcessId; ThreadId = $event.ThreadId
                RelatedStartSequence = $relatedStart; Payload = $event.Payload
            })
        }
    }
    $fileEvents = @($selected | Where-Object Provider -eq $fileProvider)
    if ($fileEvents.Count -eq 0) { throw "No file events attributable to the probe tree" }
    $selected | ForEach-Object { ConvertTo-Json -InputObject $_ -Depth 8 -Compress } |
        Set-Content -LiteralPath $Destination -Encoding utf8
    return [pscustomobject]@{
        TotalDecodedEvents = $Events.Count; ExportedEvents = $selected.Count
        FileEvents = $fileEvents.Count; LinkedCompletions = $linkedCompletions
        OwnedProcesses = @($ownedLifetimes | Select-Object ProcessId, ParentId, Start, End, Image)
    }
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$report = [ordered]@{ Status = "running"; Cases = @(); Error = $null }
$summaryPath = Join-Path $OutputDirectory "summary.json"
try {
    if (-not $IsWindows) { throw "ETW diagnostics require Windows PowerShell 7 on the CI runner" }
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Kernel ETW diagnostics require the elevated CI account; sandbox commands themselves remain restricted"
    }
    $logman = (Get-Command logman.exe -ErrorAction Stop).Source
    $tracerpt = (Get-Command tracerpt.exe -ErrorAction Stop).Source
    foreach ($provider in @($fileProvider, $processProvider)) {
        $null = Invoke-TraceUtility $logman @("query", "providers", $provider)
        $metadata = Get-WinEvent -ListProvider $provider -ErrorAction Stop
        $providerNamesByGuid[$metadata.Id.ToString().ToLowerInvariant()] = $provider
    }
    if (-not $NativeTestExecutable) {
        $testBinaries = @(Get-ChildItem -LiteralPath (Join-Path $Repository "native/windows-sandbox/target/debug/deps") -Filter "bello_windows_sandbox-*.exe")
        if ($testBinaries.Count -ne 1) { throw "Pass -NativeTestExecutable: expected exactly one already-built Rust test executable" }
        $NativeTestExecutable = $testBinaries[0].FullName
    }
    $cases = @(
        @{ Name = "cmd"; Exe = $NativeTestExecutable; Args = @("--exact", "acl::tests::object_only_grants_support_new_children_without_exposing_private_files", "--nocapture", "--test-threads=1") },
        @{ Name = "node"; Exe = $Python; Args = @("-m", "pytest", "-v", "-s", "tests/test_runtime_sandbox.py::test_native_windows_parallel_cleanup_preserves_other_command_access") }
    )
    foreach ($case in $cases) {
        $caseReport = [ordered]@{ Name = $case.Name; Status = "running"; TestExitCode = $null; TestTimedOut = $null; Trace = $null; Error = $null }
        $report.Cases += $caseReport
        $privateDirectory = Join-Path ([IO.Path]::GetTempPath()) ("bello-etw-private-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $privateDirectory | Out-Null
        $etl = Join-Path $privateDirectory "capture.etl"
        $xml = Join-Path $privateDirectory "capture.xml"
        $traceSummary = Join-Path $privateDirectory "trace-summary.txt"
        $session = "BelloFileProbe-" + [Guid]::NewGuid().ToString("N")
        $sessionMayExist = $false
        try {
            # Direct -ets session: no persistent data collector to leave behind.
            $sessionMayExist = $true
            $null = Invoke-TraceUtility $logman @("create", "trace", $session, "-ets",
                "-o", $etl, "-f", "bincirc", "-max", "32", "-bs", "64", "-nb", "16", "64",
                "-rf", "00:01:30", "-p", $fileProvider, "0xffffffffffffffff", "5")
            $null = Invoke-TraceUtility $logman @("update", "trace", $session, "-ets", "-p", $processProvider, "0x10", "5")
            $sessionConfiguration = Invoke-TraceUtility $logman @("query", $session, "-ets")
            foreach ($provider in @($fileProvider, $processProvider)) {
                if (-not $sessionConfiguration.Stdout.Contains($provider)) {
                    throw "Own ETW session does not contain both required providers after update: missing $provider"
                }
            }
            $probe = Invoke-BoundedProcess $case.Exe $case.Args $TimeoutSeconds
            $caseReport.TestExitCode = $probe.ExitCode
            $caseReport.TestTimedOut = $probe.TimedOut
            ($probe.Stdout + $probe.Stderr) | Set-Content -LiteralPath (Join-Path $OutputDirectory ($case.Name + "-test.txt")) -Encoding utf8
            Write-Host "$($case.Name) diagnostic replay: exit=$($probe.ExitCode), timeout=$($probe.TimedOut)"
            $null = Invoke-TraceUtility $logman @("stop", $session, "-ets")
            $sessionMayExist = $false
            $null = Invoke-TraceUtility $tracerpt @($etl, "-o", $xml, "-of", "XML", "-summary", $traceSummary, "-y")
            $events = @(Read-TraceEvents $xml)
            $caseReport.Trace = Export-OwnedEvents $events $probe (Join-Path $OutputDirectory ($case.Name + "-events.jsonl"))
            $caseReport.Trace | Add-Member -NotePropertyName CaptureBytes -NotePropertyValue (Get-Item -LiteralPath $etl).Length
            $caseReport.Trace | Add-Member -NotePropertyName Completeness -NotePropertyValue "Bounded circular trace; absence of an event does not prove absence of an operation."
            $caseReport.Status = "captured"
        }
        catch { $caseReport.Status = "diagnostic_failed"; $caseReport.Error = $_.Exception.Message }
        finally {
            if ($sessionMayExist) {
                try { $null = Invoke-TraceUtility $logman @("stop", $session, "-ets") }
                catch { $caseReport.Status = "diagnostic_failed"; $caseReport.Error += "; trace stop failed: " + $_.Exception.Message }
            }
            # Never publish raw ETL/XML or unrelated runner events, even on failure.
            foreach ($privateFile in @($etl, $xml, $traceSummary)) {
                if (Test-Path -LiteralPath $privateFile) { Remove-Item -LiteralPath $privateFile -Force }
            }
            Remove-Item -LiteralPath $privateDirectory
            $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $summaryPath -Encoding utf8
        }
    }
    $report.Status = if (@($report.Cases | Where-Object { $_.Status -ne "captured" }).Count) { "diagnostic_failed" } else { "captured" }
}
catch { $report.Status = "diagnostic_failed"; $report.Error = $_.Exception.Message }
finally { $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $summaryPath -Encoding utf8 }

if ($report.Status -ne "captured") { Write-Warning "ETW diagnosis failed; see summary.json"; exit 2 }
if (@($report.Cases | Where-Object { $_.TestTimedOut -or $_.TestExitCode -ne 0 }).Count) { exit 1 }
exit 0
