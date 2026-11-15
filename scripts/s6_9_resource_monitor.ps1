#Requires -RunAsAdministrator
<#
.SYNOPSIS
    S6.9 short-smoke resource sampler (read-only).
.DESCRIPTION
    Samples the uvicorn staging process (CPU/RSS/handles/threads), disk free
    space and the newest s7-soak server.log size every N seconds.
    Usage:
      powershell -File scripts/s6_9_resource_monitor.ps1 -Seconds 240 -OutFile reports\s6_8_9\soak\resource_samples.jsonl
#>
[CmdletBinding()]
param(
    [int]$Seconds = 240,
    [int]$IntervalSeconds = 2,
    [string]$OutFile = "reports\s6_8_9\soak\resource_samples.jsonl"
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path (Split-Path $OutFile -Parent) | Out-Null
Set-Content -Encoding UTF8 -Path $OutFile -Value ""

$deadline = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $deadline) {
    $stamp = (Get-Date).ToString("o")
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "uvicorn" -and $_.CommandLine -match "app\.main:app" }
    $samples = @()
    foreach ($p in $procs) {
        $gp = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $gp) { continue }
        $samples += [ordered]@{
            pid = $p.ProcessId
            cpu_seconds = [math]::Round($gp.CPU, 3)
            rss_mb = [math]::Round($gp.WorkingSet64 / 1MB, 2)
            private_mb = [math]::Round($gp.PrivateMemorySize64 / 1MB, 2)
            handles = $gp.HandleCount
            threads = $gp.Threads.Count
        }
    }
    $diskC = Get-PSDrive C -ErrorAction SilentlyContinue
    $diskD = Get-PSDrive D -ErrorAction SilentlyContinue
    $log = Get-ChildItem $env:TEMP -Directory -Filter "s7-soak-*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        ForEach-Object { Join-Path $_.FullName "server.log" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    $logBytes = if ($log) { (Get-Item -LiteralPath $log).Length } else { 0 }
    $line = [ordered]@{
        at = $stamp
        processes = $samples
        disk_c_free_gb = if ($diskC) { [math]::Round($diskC.Free / 1GB, 2) } else { $null }
        disk_d_free_gb = if ($diskD) { [math]::Round($diskD.Free / 1GB, 2) } else { $null }
        server_log_bytes = $logBytes
    }
    ($line | ConvertTo-Json -Compress -Depth 5) | Add-Content -Encoding UTF8 -Path $OutFile
    Start-Sleep -Seconds $IntervalSeconds
}
Write-Output "resource monitor finished: $OutFile"
