<#
  撤销 scripts\git-push.ps1 用的那段 GitHub hosts 直连（2026-10-14 加）。

  ### 什么时候用

  hosts 里钉的四个真实 IP 是**当时的实测结果**。GitHub 会换 IP，ISP 也可能哪天
  把直连掐掉 —— 那时推送会失败，而 hosts 的优先级高于 DNS，代理再正常也救不回来。
  遇到这种情况就跑这个脚本把那段删掉、回到走 FastLink 的 fake-IP。

  ### 用法

      scripts\undo-github-hosts.ps1        # 删掉标记段并清 DNS 缓存

  需要管理员权限（改 hosts）。删之前会自动备份到 hosts.bak-fupan-<时间戳>。
  只删两个标记之间的内容，你在 hosts 里其他的自定义条目不受影响。
#>

$ErrorActionPreference = 'Stop'

$hosts = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$begin = '# >>> fupan: GitHub 直连'
$end = '# <<< fupan: GitHub 直连'

if (-not (Test-Path $hosts)) { Write-Host "找不到 hosts 文件：$hosts" -ForegroundColor Red; exit 1 }

$lines = Get-Content $hosts
$hasBlock = ($lines | Where-Object { $_ -like "$begin*" }).Count -gt 0
if (-not $hasBlock) {
  Write-Host "hosts 里没有 fupan 的 GitHub 直连段，不用动。" -ForegroundColor Yellow
  exit 0
}

# 备份：hosts 改坏了会影响全机解析，先留一份带时间戳的
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$bak = "$hosts.bak-fupan-$stamp"
Copy-Item $hosts $bak -Force
Write-Host "已备份到 $bak"

$kept = New-Object System.Collections.Generic.List[string]
$skipping = $false
foreach ($line in $lines) {
  if ($line -like "$begin*") { $skipping = $true; continue }
  if ($line -like "$end*") { $skipping = $false; continue }
  if ($skipping) { continue }
  $kept.Add($line)
}

# 收掉尾部多余空行：删掉那一段后会留下两三行空白，看着像文件坏了
while ($kept.Count -gt 0 -and $kept[$kept.Count - 1].Trim() -eq '') { $kept.RemoveAt($kept.Count - 1) }

Set-Content -Path $hosts -Value $kept -Encoding UTF8
ipconfig /flushdns | Out-Null

Write-Host "已删除 GitHub 直连段并清 DNS 缓存。" -ForegroundColor Green
$resolved = (Resolve-DnsName github.com -Type A -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress } | Select-Object -ExpandProperty IPAddress) -join ', '
Write-Host "现在 github.com 解析为：$resolved"
Write-Host "（198.18. 开头 = 又走回代理的 fake-IP，属预期。）"
