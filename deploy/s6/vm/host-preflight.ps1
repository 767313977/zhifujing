# S6.8 Hyper-V 主机只读预检（2026-11-15）
# 只读取系统信息：不创建 VM、不创建虚拟交换机、不修改防火墙或网络配置。
$ErrorActionPreference = "Stop"

Write-Host "`n========== A. OS / BUILD ==========" -ForegroundColor Cyan
Get-CimInstance Win32_OperatingSystem |
    Select-Object Caption, Version, BuildNumber, OSArchitecture |
    Format-List

Write-Host "`n========== B. PHYSICAL MEMORY ==========" -ForegroundColor Cyan
$cs = Get-CimInstance Win32_ComputerSystem
[pscustomobject]@{
    TotalRAM_GB = [math]::Round($cs.TotalPhysicalMemory / 1GB, 2)
    LogicalProcessors = $cs.NumberOfLogicalProcessors
} | Format-List

Write-Host "`n========== C. VOLUMES ==========" -ForegroundColor Cyan
Get-Volume |
    Where-Object DriveLetter |
    Select-Object DriveLetter, FileSystemLabel, FileSystem,
        @{N="SizeGB";E={[math]::Round($_.Size / 1GB, 2)}},
        @{N="FreeGB";E={[math]::Round($_.SizeRemaining / 1GB, 2)}} |
    Format-Table -AutoSize

Write-Host "`n========== D. NETWORK ADAPTERS ==========" -ForegroundColor Cyan
Get-NetAdapter |
    Select-Object Name, InterfaceDescription, Status, LinkSpeed,
        MacAddress, Virtual |
    Format-Table -AutoSize

Write-Host "`n========== E. IP CONFIGURATION ==========" -ForegroundColor Cyan
Get-NetIPConfiguration |
    Select-Object InterfaceAlias, InterfaceIndex,
        IPv4Address, IPv4DefaultGateway, DNSServer |
    Format-List

Write-Host "`n========== F. EXISTING VIRTUAL SWITCHES ==========" -ForegroundColor Cyan
Get-VMSwitch -ErrorAction SilentlyContinue |
    Select-Object Name, SwitchType, NetAdapterInterfaceDescription,
        AllowManagementOS |
    Format-List

Write-Host "`n========== G. HYPER-V HOST SETTINGS ==========" -ForegroundColor Cyan
Get-VMHost |
    Select-Object ComputerName, VirtualMachinePath,
        VirtualHardDiskPath, LogicalProcessorCount,
        MemoryCapacity, MemoryAvailable |
    Format-List

Write-Host "`n========== H. SUPPORTED VM CONFIGURATION ==========" -ForegroundColor Cyan
Get-VMHostSupportedVersion |
    Select-Object Name, Version |
    Format-Table -AutoSize

Write-Host "`n========== I. HYPERVISOR BOOT CONFIG ==========" -ForegroundColor Cyan
bcdedit /enum "{current}" |
    Select-String "hypervisorlaunchtype"

Write-Host "`n========== PREFLIGHT COMPLETE ==========" -ForegroundColor Green
