#Requires -RunAsAdministrator
<#
.SYNOPSIS
    S6.1 专用 Linux VM 准备脚本（启用 Hyper-V → 隔离网络 → 建 VM → 建检查点）。

.DESCRIPTION
    ⚠️ 本脚本**不做**任何宿主机防火墙改动，也不把 WSL2 当验收环境。
    它只负责把「独立 Ubuntu VM」准备好，OS Egress Deny 的 apply/verify/rollback
    仍然在 VM 内执行 deploy/s6/ 下的脚本。

    用法：
      # 第一步：启用 Hyper-V（会要求重启宿主机）
      .\prepare-hyperv-vm.ps1 -EnableHyperV

      # 重启后：创建隔离网络 + VM（需要预先转换好的 Ubuntu VHDX）
      .\prepare-hyperv-vm.ps1 -CreateVm -UbuntuVhdx D:\vm\ubuntu-24.04.vhdx -VmName s6-egress-vm

.PARAMETER EnableHyperV
    启用 Microsoft-Hyper-V-All（需要重启）。

.PARAMETER CreateVm
    创建内部 vSwitch + NAT、2 vCPU / 4GB / 30GB 的 VM，并建立初始检查点。

.PARAMETER UbuntuVhdx
    Ubuntu LTS 的 VHDX（**必须由执行者自行准备**；脚本不联网下载镜像）。

.PARAMETER VmName
    虚拟机名称，默认 s6-egress-vm。

.PARAMETER VmRoot
    VM 文件根目录，默认 C:\s6-vm。
#>
[CmdletBinding(DefaultParameterSetName = "Help")]
param(
    [Parameter(ParameterSetName = "Enable")]
    [switch]$EnableHyperV,

    [Parameter(ParameterSetName = "Create")]
    [switch]$CreateVm,

    [Parameter(ParameterSetName = "Create")]
    [string]$UbuntuVhdx,

    [Parameter(ParameterSetName = "Create")]
    [string]$VmName = "s6-egress-vm",

    [Parameter(ParameterSetName = "Create")]
    [string]$VmRoot = "C:\s6-vm",

    [Parameter(ParameterSetName = "Create")]
    [string]$SwitchName = "s6-internal",

    [Parameter(ParameterSetName = "Create")]
    [string]$NatName = "s6-nat",

    [Parameter(ParameterSetName = "Create")]
    [string]$VmSubnet = "172.31.60.0/24",

    [Parameter(ParameterSetName = "Create")]
    [string]$VmGateway = "172.31.60.1"
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "需要管理员权限运行。"
    }
}

function Enable-HyperVFeature {
    $feature = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All
    if ($feature.State -eq "Enabled") {
        Write-Host "Hyper-V 已启用，无需重启。" -ForegroundColor Green
        return
    }
    Write-Host "启用 Microsoft-Hyper-V-All（不会自动重启）…" -ForegroundColor Yellow
    Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -All -NoRestart | Out-Null
    Write-Host "已启用。请**手动重启宿主机**，然后执行 -CreateVm。" -ForegroundColor Yellow
    Write-Host "注意：本脚本不会修改宿主机防火墙，也不会把 WSL2 当验收环境。" -ForegroundColor Yellow
}

function New-IsolatedNetwork {
    if (-not (Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue)) {
        Write-Host "创建内部 vSwitch $SwitchName …"
        New-VMSwitch -Name $SwitchName -SwitchType Internal | Out-Null
    }
    $adapter = Get-NetAdapter | Where-Object { $_.Name -like "*$SwitchName*" } | Select-Object -First 1
    if ($null -eq $adapter) {
        throw "找不到 vSwitch 对应的宿主网卡，无法配置 NAT 网关。"
    }
    if (-not (Get-NetIPAddress -IPAddress $VmGateway -ErrorAction SilentlyContinue)) {
        New-NetIPAddress -IPAddress $VmGateway -PrefixLength 24 -InterfaceIndex $adapter.ifIndex | Out-Null
    }
    if (-not (Get-NetNat -Name $NatName -ErrorAction SilentlyContinue)) {
        New-NetNat -Name $NatName -InternalIPInterfaceAddressPrefix $VmSubnet | Out-Null
    }
    Write-Host "内部网络就绪：$SwitchName / $VmSubnet（NAT: $NatName）" -ForegroundColor Green
}

function New-S6Vm {
    if (-not $UbuntuVhdx -or -not (Test-Path $UbuntuVhdx)) {
        throw "请提供已转换好的 Ubuntu VHDX 路径（-UbuntuVhdx）。脚本不自动下载镜像。"
    }
    New-Item -ItemType Directory -Force -Path $VmRoot | Out-Null
    if (Get-VM -Name $VmName -ErrorAction SilentlyContinue) {
        throw "虚拟机 $VmName 已存在，请先重命名或删除。"
    }
    Write-Host "创建 VM $VmName（2 vCPU / 4GB）…"
    New-VM -Name $VmName -MemoryStartupBytes 4GB -VHDPath $UbuntuVhdx `
        -SwitchName $SwitchName -Path $VmRoot | Out-Null
    Set-VMProcessor -VMName $VmName -Count 2
    Set-VMMemory -VMName $VmName -DynamicMemoryEnabled $false
    # 关闭「主机目录共享」相关的集成服务（不挂载 Windows 用户目录）
    Disable-VMIntegrationService -VMName $VmName -Name "Guest Service Interface" -ErrorAction SilentlyContinue
    Checkpoint-VM -Name $VmName -SnapshotName "s6-baseline"
    Write-Host "VM 与初始检查点已创建。" -ForegroundColor Green
    Write-Host "下一步：在 VM 内配置静态 IP $VmGateway 网关、安装 nftables/curl，" -ForegroundColor Yellow
    Write-Host "然后依次执行 deploy/s6/rollback-egress.sh → apply-egress-deny.sh → verify-egress.sh。" -ForegroundColor Yellow
}

Assert-Admin
switch ($PSCmdlet.ParameterSetName) {
    "Enable" { Enable-HyperVFeature }
    "Create" {
        if ((Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All).State -ne "Enabled") {
            throw "Hyper-V 未启用。先执行 -EnableHyperV 并重启。"
        }
        New-IsolatedNetwork
        New-S6Vm
    }
    default {
        Write-Host "见脚本头部注释中的用法。"
    }
}
