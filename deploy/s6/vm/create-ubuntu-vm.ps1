#Requires -RunAsAdministrator
<#
.SYNOPSIS
    S6.8 create an isolated Ubuntu Hyper-V VM for OS egress acceptance.

.DESCRIPTION
    Read-only by default (dry run). Pass -Apply to create the switch/NAT/VM.
    It never changes Windows Firewall, proxy, DNS or routing.

    Sequence:
      1. validate ISO SHA256, free space, RAM, subnet conflicts, VM name;
      2. create Internal vSwitch + host gateway + NAT (only with -Apply);
      3. create Generation 2 VM (4 vCPU / 6 GB / 64 GB dynamic VHDX);
      4. attach Ubuntu ISO, disable Guest Service Interface, no shared folders;
      5. optionally start the VM and wait for heartbeat;
      6. optionally create a baseline checkpoint (after OS install only).

.PARAMETER IsoPath
    Ubuntu Server 24.04 LTS x86_64 ISO.

.PARAMETER IsoSha256
    Expected SHA256 of the ISO (lower/upper case both accepted).

.PARAMETER Apply
    Actually create resources. Without it the script only prints the plan.

.PARAMETER StartVm
    Start the VM after creation (installer must be completed in vmconnect).

.PARAMETER CreateBaselineCheckpoint
    Create a checkpoint after the guest reports heartbeat OK (post-install).

.EXAMPLE
    .\create-ubuntu-vm.ps1 -IsoPath D:\iso\ubuntu-24.04-live-server-amd64.iso -IsoSha256 <sha>
    .\create-ubuntu-vm.ps1 -IsoPath ... -IsoSha256 ... -Apply -StartVm
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$IsoPath,
    [Parameter(Mandatory = $true)][string]$IsoSha256,
    [switch]$Apply,
    [switch]$StartVm,
    [switch]$CreateBaselineCheckpoint,
    [string]$VmName = "s6-egress-vm",
    [string]$VmRoot = "D:\s6-vm",
    [string]$SwitchName = "s6-egress",
    [string]$NatName = "s6-egress-nat",
    [string]$VmSubnet = "172.31.240.0/24",
    [string]$VmGateway = "172.31.240.1",
    [int]$Vcpu = 4,
    [int]$StartupMemoryGB = 6,
    [int]$DiskGB = 64,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Administrator privileges required."
    }
}

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}

function Assert-Iso {
    if (-not (Test-Path -LiteralPath $IsoPath)) {
        throw "ISO not found: $IsoPath"
    }
    $actual = (Get-FileHash -LiteralPath $IsoPath -Algorithm SHA256).Hash.ToLower()
    $expected = $IsoSha256.Trim().ToLower()
    if ($actual -ne $expected) {
        throw "ISO SHA256 mismatch. expected=$expected actual=$actual"
    }
    return [pscustomobject]@{ path = (Resolve-Path -LiteralPath $IsoPath).Path; sha256 = $actual }
}

function Assert-Resources {
    $repo = Get-RepoRoot
    $vmRootFull = [System.IO.Path]::GetFullPath($VmRoot)
    if ($vmRootFull.StartsWith($repo, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "VmRoot must not be inside the repository: $vmRootFull"
    }
    $cs = Get-CimInstance Win32_ComputerSystem
    $ramGb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 2)
    if ($ramGb -lt ($StartupMemoryGB + 4)) {
        throw "Not enough RAM: $ramGb GB (need at least $($StartupMemoryGB + 4) GB)."
    }
    $drive = [System.IO.Path]::GetPathRoot($vmRootFull).TrimEnd("\")
    $driveLetter = $drive.TrimEnd(":")
    $volume = Get-Volume -DriveLetter $driveLetter
    if ($null -eq $volume) {
        throw "Cannot resolve volume for $vmRootFull"
    }
    $freeGb = [math]::Round($volume.SizeRemaining / 1GB, 2)
    if ($freeGb -lt ($DiskGB + 10)) {
        throw "Not enough free space on $drive : $freeGb GB (need $($DiskGB + 10) GB)."
    }
    return [pscustomobject]@{
        ram_gb = $ramGb
        logical_processors = $cs.NumberOfLogicalProcessors
        vm_root = $vmRootFull
        drive = $drive
        free_gb = $freeGb
    }
}

function Assert-NetworkPlan {
    $existingIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -like "172.31.240.*" }
    if ($existingIp) {
        throw "Subnet conflict: 172.31.240.0/24 already used by $($existingIp.InterfaceAlias)."
    }
    $existingNat = Get-NetNat -ErrorAction SilentlyContinue | Where-Object { $_.InternalIPInterfaceAddressPrefix -eq $VmSubnet }
    if ($existingNat) {
        throw "NAT already exists for $VmSubnet : $($existingNat.Name)"
    }
    $switch = Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue
    if ($switch -and $switch.SwitchType -ne "Internal") {
        throw "Existing switch $SwitchName is $($switch.SwitchType); expected Internal."
    }
    if (Get-VM -Name $VmName -ErrorAction SilentlyContinue) {
        throw "VM $VmName already exists."
    }
    return [pscustomobject]@{
        switch_exists = [bool]$switch
        nat_exists = [bool]$existingNat
        subnet = $VmSubnet
        gateway = $VmGateway
    }
}

function New-IsolatedSwitch {
    if (-not (Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue)) {
        New-VMSwitch -Name $SwitchName -SwitchType Internal | Out-Null
        Start-Sleep -Seconds 3
    }
    $adapter = Get-NetAdapter | Where-Object { $_.Name -eq "vEthernet ($SwitchName)" } | Select-Object -First 1
    if ($null -eq $adapter) {
        throw "Host adapter vEthernet ($SwitchName) not found."
    }
    if (-not (Get-NetIPAddress -IPAddress $VmGateway -ErrorAction SilentlyContinue)) {
        New-NetIPAddress -IPAddress $VmGateway -PrefixLength 24 -InterfaceIndex $adapter.ifIndex | Out-Null
    }
    if (-not (Get-NetNat -Name $NatName -ErrorAction SilentlyContinue)) {
        New-NetNat -Name $NatName -InternalIPInterfaceAddressPrefix $VmSubnet | Out-Null
    }
}

function New-IsolatedVm {
    New-Item -ItemType Directory -Force -Path $VmRoot | Out-Null
    $vhdPath = Join-Path $VmRoot "$VmName-os.vhdx"
    if (Test-Path -LiteralPath $vhdPath) {
        throw "VHDX already exists: $vhdPath"
    }
    New-VHD -Path $vhdPath -SizeBytes ($DiskGB * 1GB) -Dynamic | Out-Null
    New-VM -Name $VmName -Generation 2 -MemoryStartupBytes ($StartupMemoryGB * 1GB) `
        -VHDPath $vhdPath -SwitchName $SwitchName -Path $VmRoot | Out-Null
    Set-VMProcessor -VMName $VmName -Count $Vcpu
    Set-VMMemory -VMName $VmName -DynamicMemoryEnabled $false
    Set-VM -Name $VmName -AutomaticStartAction Nothing -AutomaticStopAction ShutDown
    try {
        Set-VM -Name $VmName -EnhancedSessionTransportType VMBus
    } catch {
        Write-Warning "Set-VM EnhancedSessionTransportType not supported: $($_.Exception.Message)"
    }
    Add-VMDvdDrive -VMName $VmName -Path $IsoPath | Out-Null
    $dvd = Get-VMDvdDrive -VMName $VmName
    Set-VMFirmware -VMName $VmName -EnableSecureBoot On `
        -SecureBootTemplate MicrosoftUEFICertificateAuthority `
        -FirstBootDevice $dvd | Out-Null
    Disable-VMIntegrationService -VMName $VmName -Name "Guest Service Interface" -ErrorAction SilentlyContinue
    return $vhdPath
}

function Wait-Heartbeat {
    $deadline = (Get-Date).AddMinutes(10)
    while ((Get-Date) -lt $deadline) {
        $vm = Get-VM -Name $VmName
        $hb = Get-VMIntegrationService -VMName $VmName -Name "Heartbeat" -ErrorAction SilentlyContinue
        if ($vm.State -eq "Running" -and $hb -and $hb.PrimaryStatusDescription -eq "OK") {
            return $true
        }
        Start-Sleep -Seconds 5
    }
    return $false
}

Assert-Admin
$repo = Get-RepoRoot
if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = Join-Path $repo "reports\s6_8_9\vm"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$iso = Assert-Iso
$resources = Assert-Resources
$network = Assert-NetworkPlan

$plan = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    apply = [bool]$Apply
    host = $env:COMPUTERNAME
    vm_name = $VmName
    vm_root = $resources.vm_root
    iso = $iso
    resources = $resources
    network = $network
    config = [ordered]@{
        generation = 2
        vcpu = $Vcpu
        startup_memory_gb = $StartupMemoryGB
        dynamic_memory = $false
        disk_gb = $DiskGB
        secure_boot_template = "MicrosoftUEFICertificateAuthority"
        guest_service_interface = "disabled"
        shared_folders = "none"
        enhanced_session_transport = "VMBus"
    }
    actions = @()
}

if (-not $Apply) {
    $plan.actions += "DRY_RUN: no switch/NAT/VM created"
    $plan | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 (Join-Path $OutDir "vm_create_plan.json")
    $plan | ConvertTo-Json -Depth 8
    Write-Host "Dry run complete. Re-run with -Apply to create resources." -ForegroundColor Yellow
    exit 0
}

New-IsolatedSwitch
$plan.actions += "switch/NAT/gateway ensured"
$vhd = New-IsolatedVm
$plan.actions += "VM created with VHDX $vhd"
if ($StartVm) {
    Start-VM -Name $VmName
    $plan.actions += "VM started"
    $ok = Wait-Heartbeat
    $plan.heartbeat_ok = $ok
    if (-not $ok) {
        Write-Warning "Heartbeat not OK within 10 minutes; complete Ubuntu install in vmconnect."
    }
}
if ($CreateBaselineCheckpoint) {
    $hb = Get-VMIntegrationService -VMName $VmName -Name "Heartbeat" -ErrorAction SilentlyContinue
    if (-not ($hb -and $hb.PrimaryStatusDescription -eq "OK")) {
        throw "Refusing checkpoint: guest heartbeat is not OK (finish OS install first)."
    }
    Checkpoint-VM -Name $VmName -SnapshotName "s6-baseline"
    $plan.actions += "baseline checkpoint created"
}

$vm = Get-VM -Name $VmName
$plan.after = [ordered]@{
    state = $vm.State
    generation = $vm.Generation
    processor_count = $vm.ProcessorCount
    memory_startup = $vm.MemoryStartup
    dynamic_memory = $vm.DynamicMemoryEnabled
    switch = (Get-VMSwitch -Name $SwitchName).SwitchType
    nat = (Get-NetNat -Name $NatName -ErrorAction SilentlyContinue).InternalIPInterfaceAddressPrefix
    checkpoints = @(Get-VMSnapshot -VMName $VmName -ErrorAction SilentlyContinue).Count
}
$plan | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 (Join-Path $OutDir "vm_create_result.json")
$plan | ConvertTo-Json -Depth 8
Write-Host "VM $VmName created. Complete Ubuntu install via vmconnect, then run guest bootstrap." -ForegroundColor Green
