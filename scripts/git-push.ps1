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

  本机侧也排查过：hosts 里没有 github 条目、WinHTTP 是直连无代理、没有 VPN 网卡。

  ### 用法

      scripts\git-push.ps1                 # 默认：单次 90 秒超时，失败等 20 秒，最多 5 次
      scripts\git-push.ps1 -MaxTry 8       # 坏窗口持续较久时多试几次
      scripts\git-push.ps1 -TimeoutSec 30  # 缩短单次等待，快速轮转

  退出码：0 = 推成功；1 = 试满次数仍未成功（或分支没有上游）。

  重试只是重发同一笔提交，**不会重复提交、也不用重新 commit**。

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

# 分支没有上游时，重试 5 次也没用（那是配置问题不是链路问题），提前退出并给命令
$branch = (& git -C $repo rev-parse --abbrev-ref HEAD 2>$null)
& git -C $repo rev-parse --abbrev-ref '@{u}' *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "当前分支（$branch）没有上游分支，重试解决不了，先跑一次：" -ForegroundColor Red
  Write-Host "    git push -u origin $branch"
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

for ($i = 1; $i -le $MaxTry; $i++) {
  Write-Host ("[{0}/{1}] push {2}（单次限时 {3}s）…" -f $i, $MaxTry, $branch, $TimeoutSec)
  $r = Invoke-PushOnce

  if ($null -eq $r) {
    Write-Host ("      超时 {0}s，已掐断" -f $TimeoutSec) -ForegroundColor Yellow
  } elseif ($r.code -eq 0) {
    ($r.out -split "`n") | Where-Object { $_ } | ForEach-Object { "      $_" }
    Write-Host "      推送成功" -ForegroundColor Green
    # 再确认一次真的同步了，免得只看 git 的输出
    (& git -C $repo status -sb) | ForEach-Object { "      $_" }
    exit 0
  } else {
    $last = ($r.out -split "`n" | Where-Object { $_ } | Select-Object -Last 1)
    Write-Host ("      失败 exit={0}：{1}" -f $r.code, $last) -ForegroundColor Yellow
  }

  if ($i -lt $MaxTry) { Start-Sleep -Seconds $BackoffSec }
}

Write-Host ("试满 {0} 次仍未推上去。稍后再跑一次这条命令即可。" -f $MaxTry) -ForegroundColor Red
exit 1
