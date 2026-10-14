<#
  git push 的重试包装（Windows / PowerShell 用）。

  ### 为什么需要它

  这台机器到 github.com 的链路是**间歇性丢包**，不是固定故障、更不是 git 配置问题：
  同一分钟内 5 次 TCP 连 20.205.243.166:443，实测 2 次超时、3 次 110ms 成功；
  过一会儿再测 12/12 全成功、每次 1.1 秒。而 **git 自己不会重试** —— 一次坏窗口
  就让整笔 push 失败，最坏还见过连上后卡 **257 秒**才报 `Recv fail`。

  实测过换传输配置（`http.version=HTTP/1.1`、`http.sslBackend=openssl`、两者叠加）
  **都没有区别**：链路正常时四档都是 1.1~1.2 秒，链路坏时四档全失败。所以这里
  不改任何 git 配置、不配代理，只做「限时 + 重试」这一件事。

  ### ⚠️ 2026-10-14 更正：本机现在有代理虚拟网卡了

  上面那句「没有 VPN 网卡」已经不成立 —— 本机多了 **`FastLink`** 虚拟网卡，它的
  DNS 是 **198.18.0.2**，而 198.18.0.0/15 是代理软件的 **fake-IP 段**：
  `Resolve-DnsName github.com` 现在返回 **198.18.0.37**（不是 GitHub 的真实 IP）。

  这带来一个**新的失败模式**，与「链路丢包」表现相同但成因不同：
  **隧道一断，DNS 缓存里还是那个假 IP，所有连它的请求都会一直超时**，
  直到缓存过期或手动 `ipconfig /flushdns` 才会重新解析。

  所以本脚本在「连接类失败」后会先 **flush DNS 再重试**（下面 $flushablePattern），
  并把每次解析到的 IP 记进日志 —— 出问题时一眼能看出走的是假 IP 还是真实 IP。

  真实 IP 直连是通的（2026-10-14 实测：`140.82.112~116.3` / `20.205.243.166` 六个
  IP 的 443 全部可达，`curl --resolve github.com:443:140.82.112.3` 拉
  `info/refs` 返回 200）。也就是说**如果隧道长期不稳，把真实 IP 写进 hosts 是可行的
  兜底**（代价：万一哪天直连被墙，得再删掉）—— 这一步没做，要动系统配置时先问。

  ### 用法

      scripts\git-push.ps1                 # 默认：单次 90 秒超时，失败等 20 秒，最多 5 次
      scripts\git-push.ps1 -MaxTry 8       # 坏窗口持续较久时多试几次
      scripts\git-push.ps1 -TimeoutSec 30  # 缩短单次等待，快速轮转

  退出码：0 = 推成功；1 = 试满次数仍未成功（或分支没有上游）。

  重试只是重发同一笔提交，**不会重复提交、也不用重新 commit**。

  ### 失败日志

  每一次运行的**每一次尝试**都往 `%USERPROFILE%\.git-push.log` 追一行
  （时间戳 / 结果 / 耗时 / 报错签名），**成功也记** —— 只有失败记录的话没有分母，
  算不出「某时段的失败率」。攒几天后按小时聚合，就能看出坏窗口集中在什么时段、
  每次持续多久（2026-10-09 加，为的是先有数据再决定要不要换通道）。

  ⚠️ 本文件必须存成 **UTF-8 带 BOM**。PowerShell 5.1 对没有 BOM 的 .ps1 会按系统
  ANSI（中文机器上是 GBK）解码：中文注释先变乱码，接着乱码字节会把字符串的引号
  吃掉、直接解析失败（踩过一次）。改动后请确认编码没有退回去。
#>
param(
  [int]$MaxTry = 5,
  [int]$TimeoutSec = 90,
  [int]$BackoffSec = 20
)

$ErrorActionPreference = 'Continue'

# 用**脚本所在位置**推仓库根，而不是当前目录 —— 这样从任何目录调用都成立
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# 失败日志（见文件头「失败日志」段）：放用户目录，不进仓库、不干扰 git status。
# 记不上也不能让推送失败，所以整段包了 try。
$logPath = Join-Path $env:USERPROFILE '.git-push.log'

function Write-PushLog([string]$line) {
  try {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $logPath -Value "$stamp | $line" -Encoding UTF8
  } catch { }
}

# 报错签名截断：git 的报错可能很长（还会带 URL），日志里只留前 160 字
function Get-Sig([string]$text) {
  $one = (($text -split "`n") | Where-Object { $_ } | Select-Object -Last 1)
  if ($null -eq $one) { return '' }
  if ($one.Length -gt 160) { return $one.Substring(0, 160) + '...' }
  return $one
}

# 分支没有上游时，重试 5 次也没用（那是配置问题不是链路问题），提前退出并给命令
$branch = (& git -C $repo rev-parse --abbrev-ref HEAD 2>$null)
& git -C $repo rev-parse --abbrev-ref '@{u}' *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "当前分支（$branch）没有上游分支，重试解决不了，先跑一次：" -ForegroundColor Red
  Write-Host "    git push -u origin $branch"
  Write-PushLog ("RESULT NOUPSTREAM branch={0}" -f $branch)
  exit 1
}

function Invoke-PushOnce {
  # 单次尝试放进 job 里，超时才能掐断 —— git 本身没有超时开关
  $job = Start-Job -ScriptBlock {
    param($dir)
    Set-Location $dir
    $out = & git push 2>&1
    [pscustomobject]@{ code = $LASTEXITCODE; out = ($out -join "`n") }
  } -ArgumentList $repo

  if (Wait-Job $job -Timeout $TimeoutSec) {
    $r = Receive-Job $job
    Remove-Job $job -Force
    return $r
  }
  Stop-Job $job
  Remove-Job $job -Force
  return $null   # $null 表示这一次超时了
}

# 「连不上」这类失败的特征。命中就先 flush DNS 再重试 —— 见文件头那段：
# 本机 DNS 走 FastLink 网卡的 fake-IP（198.18.0.2），隧道断开后缓存里仍是
# 198.18.0.x 那个假 IP，光重试是连不上的，必须让它重新解析。
$flushablePattern = 'Could not connect|Failed to connect|Connection was reset|Recv failure|Connection timed out'

# 解析到的 github.com 地址。只为日志留痕：198.18. 开头 = 走代理假 IP，
# 其它 = 真实 IP。解析失败也不能让脚本挂掉，所以包 try。
function Get-GitHubIp {
  try {
    $ip = (Resolve-DnsName github.com -Type A -ErrorAction Stop |
      Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress
    if ($ip) { return $ip }
  } catch { }
  return '解析失败'
}

$runStarted = Get-Date
Write-PushLog ("START branch={0} resolved={1}" -f $branch, (Get-GitHubIp))

for ($i = 1; $i -le $MaxTry; $i++) {
  Write-Host ("[{0}/{1}] push {2}（单次限时 {3}s）…" -f $i, $MaxTry, $branch, $TimeoutSec)
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $r = Invoke-PushOnce
  $sw.Stop()
  $secs = [math]::Round($sw.Elapsed.TotalSeconds, 1)

  if ($null -eq $r) {
    Write-Host ("      超时 {0}s，已掐断" -f $TimeoutSec) -ForegroundColor Yellow
    Write-PushLog ("attempt {0}/{1} TIMEOUT {2}s" -f $i, $MaxTry, $secs)
  } elseif ($r.code -eq 0) {
    ($r.out -split "`n") | Where-Object { $_ } | ForEach-Object { "      $_" }
    Write-Host "      推送成功" -ForegroundColor Green
    Write-PushLog ("attempt {0}/{1} OK {2}s" -f $i, $MaxTry, $secs)
    # 再确认一次真的同步了，免得只看 git 的输出
    (& git -C $repo status -sb) | ForEach-Object { "      $_" }
    $total = [math]::Round(((Get-Date) - $runStarted).TotalSeconds, 1)
    Write-PushLog ("RESULT OK attempts={0} total={1}s" -f $i, $total)
    exit 0
  } else {
    $last = Get-Sig $r.out
    Write-Host ("      失败 exit={0}：{1}" -f $r.code, $last) -ForegroundColor Yellow
    Write-PushLog ("attempt {0}/{1} FAIL {2}s :: {3}" -f $i, $MaxTry, $secs, $last)
  }

  # 连不上 → 先清 DNS 缓存（本机是 fake-IP 场景，不清的话下一次必然还是连同一个假 IP）
  if ($last -match $flushablePattern -or $null -eq $r) {
    $before = Get-GitHubIp
    ipconfig /flushdns *> $null
    $after = Get-GitHubIp
    if ($before -ne $after) {
      Write-Host ("      已清 DNS 缓存：{0} → {1}" -f $before, $after) -ForegroundColor Yellow
    } else {
      Write-Host ("      已清 DNS 缓存（解析结果不变：{0}）" -f $after) -ForegroundColor DarkGray
    }
    Write-PushLog ("  dns-flush before={0} after={1}" -f $before, $after)
  }

  if ($i -lt $MaxTry) { Start-Sleep -Seconds $BackoffSec }
}

Write-Host ("试满 {0} 次仍未推上去。稍后再跑一次这条命令即可。" -f $MaxTry) -ForegroundColor Red
$total = [math]::Round(((Get-Date) - $runStarted).TotalSeconds, 1)
Write-PushLog ("RESULT FAIL attempts={0} total={1}s" -f $MaxTry, $total)
exit 1
