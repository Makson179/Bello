param([Parameter(Mandatory = $true)][string]$Report)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Report) | Out-Null
try {
    # Only the fixed broker's bounded error records; no unrelated event logs,
    # packet capture, command lines, environment, or service enumeration.
    $events = @(Get-WinEvent -FilterHashtable @{
        LogName = 'Application'; ProviderName = 'BelloOfflineNetwork'; Id = 1
        StartTime = [DateTime]::Now.AddHours(-1)
    } -MaxEvents 20 | ForEach-Object {
        $message = [string]$_.Message
        if ($message.Length -gt 4096) { $message = $message.Substring(0, 4096) }
        @{ time = $_.TimeCreated.ToUniversalTime().ToString('o'); eventId = $_.Id; message = $message }
    })
    @{ source = 'BelloOfflineNetwork'; events = $events } | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $Report -Encoding utf8
}
catch {
    @{ source = 'BelloOfflineNetwork'; diagnosticUnavailable = $true
        error = $_.Exception.Message } | ConvertTo-Json |
        Set-Content -LiteralPath $Report -Encoding utf8
}
