#Requires -RunAsAdministrator
<#
.SYNOPSIS
    S6.8 run the guest OS egress acceptance over SSH and pull evidence back.

.DESCRIPTION
    Host side only orchestrates:
      - checks the VM is running and SSH is reachable;
      - starts a controlled DNS probe on the host gateway (apply phase only);
      - copies deploy/s6 + guest runner into the VM (no shared folders);
      - runs snapshot/apply/verify/N01-N24 inside the VM;
      - pulls evidence back and records how many DNS queries reached the host.

    It never applies firewall rules on the Windows host.

.PARAMETER Phase
    apply | post-reboot. post-reboot also checks nft persistence and rollback.

.EXAMPLE
    .\invoke-guest-egress-acceptance.ps1 -GuestAddress 172.31.240.10 `
      -SshKeyPath $env:USERPROFILE\.ssh\s6_vm -Phase apply
#>
[CmdletBinding()]
param(
    [string]$VmName = "s6-egress-vm",
    [string]$GuestAddress = "172.31.240.10",
    [string]$GuestUser = "fupan",
    [Parameter(Mandatory = $true)][string]$SshKeyPath,
    [ValidateSet("apply", "post-reboot")][string]$Phase = "apply",
    [string]$EvidenceOut = "",
    [int]$HostDnsSeconds = 120,
    [string]$HostDnsBind = "172.31.240.1"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
if ([string]::IsNullOrWhiteSpace($EvidenceOut)) {
    $EvidenceOut = Join-Path $repo "reports\s6_8_9\egress\guest-$Phase"
}
New-Item -ItemType Directory -Force -Path $EvidenceOut | Out-Null

if (-not (Test-Path -LiteralPath $SshKeyPath)) {
    throw "SSH key not found: $SshKeyPath"
}

$vm = Get-VM -Name $VmName -ErrorAction SilentlyContinue
if (-not $vm) { throw "VM not found: $VmName" }
if ($vm.State -ne "Running") { throw "VM $VmName is $($vm.State); start it first." }

$sshBase = @(
    "-i", $SshKeyPath,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=NUL",
    "-o", "ConnectTimeout=10"
)
$target = "$GuestUser@$GuestAddress"
& ssh.exe @sshBase $target "true"
if ($LASTEXITCODE -ne 0) { throw "SSH to $target failed." }

$dnsLog = Join-Path $EvidenceOut "host-dns-$Phase.jsonl"
$probe = $null
if ($Phase -eq "apply") {
    $probeScript = Join-Path $PSScriptRoot "host-dns-probe.py"
    $python = Join-Path $repo ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) { $python = "python.exe" }
    $probe = Start-Process -FilePath $python -ArgumentList @(
        $probeScript, "--bind", $HostDnsBind, "--log", $dnsLog, "--duration", "$HostDnsSeconds"
    ) -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 2
    if ($probe.HasExited) {
        throw "Host DNS probe exited early. Check $dnsLog"
    }
}

try {
    & ssh.exe @sshBase $target "sudo mkdir -p /opt/s6/deploy /opt/s6/evidence && sudo chown -R ${GuestUser}:${GuestUser} /opt/s6/evidence"
    if ($LASTEXITCODE -ne 0) { throw "Failed to prepare /opt/s6 in guest." }

    & scp.exe -i $SshKeyPath -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -r `
        (Join-Path $repo "deploy\s6") "${target}:/opt/s6/deploy/"
    if ($LASTEXITCODE -ne 0) { throw "scp deploy/s6 failed." }

    & scp.exe -i $SshKeyPath -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL `
        (Join-Path $PSScriptRoot "guest\run-egress-acceptance.sh") "${target}:/opt/s6/run-egress-acceptance.sh"
    if ($LASTEXITCODE -ne 0) { throw "scp guest runner failed." }

    & ssh.exe @sshBase $target "sudo bash /opt/s6/run-egress-acceptance.sh --phase $Phase --evidence-dir /opt/s6/evidence --host-dns $HostDnsBind"
    $guestRc = $LASTEXITCODE

    & scp.exe -i $SshKeyPath -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -r `
        "${target}:/opt/s6/evidence/." $EvidenceOut
    $scpRc = $LASTEXITCODE
} finally {
    if ($probe -and -not $probe.HasExited) {
        Stop-Process -Id $probe.Id -Force -ErrorAction SilentlyContinue
    }
}

$dnsQueries = 0
if (Test-Path -LiteralPath $dnsLog) {
    $dnsQueries = @(Get-Content -LiteralPath $dnsLog | Where-Object { $_.Trim() -ne "" }).Count
}
$dnsSummary = [ordered]@{
    phase = $Phase
    bind = $HostDnsBind
    log = $dnsLog
    queries_received = $dnsQueries
    policy_ok = ($dnsQueries -eq 0)
}
$dnsSummary | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $EvidenceOut "host-dns-summary-$Phase.json")

$expected = if ($Phase -eq "apply") { "n01_n24_results.json" } else { "n01_n24_post_reboot.json" }
$missing = -not (Test-Path -LiteralPath (Join-Path $EvidenceOut $expected))
$result = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    vm = $VmName
    guest = $target
    phase = $Phase
    guest_exit_code = $guestRc
    scp_exit_code = $scpRc
    evidence_dir = $EvidenceOut
    expected_file = $expected
    expected_file_present = (-not $missing)
    host_dns = $dnsSummary
    verdict = if ($guestRc -eq 0 -and -not $missing -and ($Phase -ne "apply" -or $dnsQueries -eq 0)) { "PASS" } else { "FAIL" }
}
$result | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $EvidenceOut "invoke-result-$Phase.json")
$result | ConvertTo-Json -Depth 6
if ($result.verdict -ne "PASS") { exit 1 }
exit 0
