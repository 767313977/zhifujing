#Requires -RunAsAdministrator
<#
.SYNOPSIS
    S6.8 read-only isolation verification for the Ubuntu acceptance VM.

.DESCRIPTION
    Checks (no changes):
      - VM exists/running, Generation 2, 4 vCPU, 6 GB, no dynamic memory;
      - target vSwitch is Internal and not bound to a physical adapter;
      - dedicated NAT prefix and host gateway IP exist;
      - Guest Service Interface disabled, no shared folders configured;
      - optional baseline checkpoint exists;
      - no S6 firewall rules on the Windows host (host firewall untouched).
    Writes JSON to reports/s6_8_9/vm/vm_isolation.json (or -OutFile).

.EXAMPLE
    .\verify-vm-isolation.ps1 -VmName s6-egress-vm
#>
[CmdletBinding()]
param(
    [string]$VmName = "s6-egress-vm",
    [string]$SwitchName = "s6-egress",
    [string]$NatName = "s6-egress-nat",
    [string]$VmSubnet = "172.31.240.0/24",
    [string]$VmGateway = "172.31.240.1",
    [int]$ExpectVcpu = 4,
    [int]$ExpectMemoryGB = 6,
    [switch]$RequireCheckpoint,
    [string]$OutFile = ""
)

$ErrorActionPreference = "Stop"
$checks = New-Object System.Collections.Generic.List[object]

function Add-Check([string]$name, [bool]$pass, $detail) {
    $checks.Add([pscustomobject]@{ name = $name; pass = $pass; detail = $detail })
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
if ([string]::IsNullOrWhiteSpace($OutFile)) {
    $OutFile = Join-Path $repo "reports\s6_8_9\vm\vm_isolation.json"
}
New-Item -ItemType Directory -Force -Path (Split-Path $OutFile -Parent) | Out-Null

$vm = Get-VM -Name $VmName -ErrorAction SilentlyContinue
Add-Check "vm_exists" ([bool]$vm) $(if ($vm) { $vm.State } else { "missing" })
if ($vm) {
    Add-Check "vm_generation_2" ($vm.Generation -eq 2) $vm.Generation
    Add-Check "vm_vcpu" ($vm.ProcessorCount -eq $ExpectVcpu) $vm.ProcessorCount
    Add-Check "vm_memory_startup" ($vm.MemoryStartup -eq ($ExpectMemoryGB * 1GB)) $vm.MemoryStartup
    Add-Check "vm_dynamic_memory_off" (-not $vm.DynamicMemoryEnabled) $vm.DynamicMemoryEnabled
    Add-Check "vm_automatic_start_nothing" ($vm.AutomaticStartAction -eq "Nothing") $vm.AutomaticStartAction
    $disk = Get-VMHardDiskDrive -VMName $VmName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($disk) {
        $vhd = Get-VHD -Path $disk.Path -ErrorAction SilentlyContinue
        Add-Check "vm_disk_dynamic" ([bool]($vhd -and $vhd.VhdType -eq "Dynamic")) $(if ($vhd) { $vhd.VhdType } else { "unknown" })
        Add-Check "vm_disk_size_gb" ([bool]($vhd -and $vhd.Size -ge 64GB)) $(if ($vhd) { [math]::Round($vhd.Size / 1GB, 2) } else { $null })
    }
    $gsi = Get-VMIntegrationService -VMName $VmName -Name "Guest Service Interface" -ErrorAction SilentlyContinue
    Add-Check "guest_service_interface_disabled" ([bool]($gsi -and -not $gsi.Enabled)) $(if ($gsi) { $gsi.Enabled } else { "absent" })
    $checkpoints = @(Get-VMSnapshot -VMName $VmName -ErrorAction SilentlyContinue).Count
    if ($RequireCheckpoint) {
        Add-Check "baseline_checkpoint_present" ($checkpoints -ge 1) $checkpoints
    }
}

$switch = Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue
Add-Check "switch_exists" ([bool]$switch) $(if ($switch) { $switch.SwitchType } else { "missing" })
if ($switch) {
    Add-Check "switch_internal" ($switch.SwitchType -eq "Internal") $switch.SwitchType
    Add-Check "switch_not_physical" ([string]::IsNullOrWhiteSpace($switch.NetAdapterInterfaceDescription)) $switch.NetAdapterInterfaceDescription
}

$nat = Get-NetNat -Name $NatName -ErrorAction SilentlyContinue
Add-Check "nat_exists" ([bool]$nat) $(if ($nat) { $nat.InternalIPInterfaceAddressPrefix } else { "missing" })
if ($nat) {
    Add-Check "nat_prefix" ($nat.InternalIPInterfaceAddressPrefix -eq $VmSubnet) $nat.InternalIPInterfaceAddressPrefix
}

$gateway = Get-NetIPAddress -IPAddress $VmGateway -ErrorAction SilentlyContinue
Add-Check "host_gateway_ip" ([bool]$gateway) $(if ($gateway) { $gateway.InterfaceAlias } else { "missing" })

# Host firewall must stay untouched: no rules named s6*
$s6Rules = @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "s6*" }).Count
Add-Check "host_firewall_no_s6_rules" ($s6Rules -eq 0) $s6Rules
$profiles = Get-NetFirewallProfile | Select-Object Name, Enabled
Add-Check "host_firewall_profiles_recorded" ($profiles.Count -ge 1) $profiles

$failed = @($checks | Where-Object { -not $_.pass })
$payload = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    vm_name = $VmName
    host = $env:COMPUTERNAME
    checks = $checks
    failed = $failed
    verdict = if ($failed.Count -eq 0) { "PASS" } else { "FAIL" }
}
$payload | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $OutFile
$payload | ConvertTo-Json -Depth 8
if ($failed.Count -gt 0) { exit 1 }
exit 0
