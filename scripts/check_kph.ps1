<#
.SYNOPSIS
    开盘红数据服务（StockDataServer.exe）只读探针 —— 单文件 PowerShell 版。

.DESCRIPTION
    与 scripts/check_kph.py 等价：把《开盘红接口说明书 v2.7.2》里与本站相关的接口
    真机跑一遍，输出一份「实测字段对照」的 markdown（默认落在当前目录）。

    给「跑 exe 的那台机器没有 Python」的场景用：本文件不依赖任何东西，
    Windows PowerShell 5.1 自带能力即可（Invoke-WebRequest + ConvertFrom-Json）。

    三条硬约束（与 py 版一致）：
    1. 只读：不调 /api/kph/login、不调 /api/send_hex、不改任何东西；
    2. 不制造订阅：get_market_sentiment / get_realtime_lhb / get_kph_trends 默认不碰，
       改用 _once / _range；加 -WithSubscribe 才跑订阅型，且跑完自动退订；
    3. 服务没起时给人话提示，退出码 2。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\check_kph.ps1
    powershell -ExecutionPolicy Bypass -File .\check_kph.ps1 -Base http://192.168.1.100:5000
    powershell -ExecutionPolicy Bypass -File .\check_kph.ps1 -Only limit_up,zd_stat
    powershell -ExecutionPolicy Bypass -File .\check_kph.ps1 -SelfTest   # 不联网，只验渲染
#>
param(
    [string]$Base = "http://127.0.0.1:5000",
    [int]$TimeoutSec = 20,
    [string]$Only = "",
    [switch]$WithSubscribe,
    [string]$Out = "",
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
# 控制台输出中文用 UTF-8（Windows 10+ 的 PowerShell 5.1 支持；失败也不影响落盘）
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
} catch { }

# ------------------------------------------------------------------ 接口清单
# 顺序即报告顺序：先大盘 / 情绪，再池子 / 榜，再板块，最后个股与代理。
# Target = 站内可对接的表 / 页面；Note = 单位、坑、一次性 vs 订阅。
$Calls = @(
    [pscustomobject]@{ Key = 'zd_stat'; Path = '/api/get_zd_stat'; Params = @{}; Target = 'MarketSentiment（涨跌家数、涨跌停家数）/ 首页情绪面板'; Note = 'zdList 是 -10…10 档位的家数分布，可对应 BreadthDaily 的档位统计'; Subscribe = $false }
    [pscustomobject]@{ Key = 'sentiment_once'; Path = '/api/get_market_sentiment_once'; Params = @{ type = 0 }; Target = 'MarketSentiment（情绪分、涨停数、连板高度）'; Note = '一次性版（订阅→取一帧→退订）；订阅版 get_market_sentiment 默认不调'; Subscribe = $false }
    [pscustomobject]@{ Key = 'daban_head'; Path = '/api/get_daban_head'; Params = @{}; Target = '情绪面板（今日/昨日涨停数、封板率）'; Note = 'todayFB = 封板率（%）'; Subscribe = $false }
    [pscustomobject]@{ Key = 'daban_count'; Path = '/api/get_daban_list_count'; Params = @{}; Target = '打板页各 Tab 条数（可与 LimitPool 行数对账）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'zt_situation'; Path = '/api/get_zt_situation'; Params = @{}; Target = '涨停梯队（连板高度分布）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'market_energy'; Path = '/api/get_market_energy'; Params = @{}; Target = '市场能量（成交额）/ 首页'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'weight_performance'; Path = '/api/get_weight_performance'; Params = @{}; Target = '权重股表现 / 首页'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'northbound'; Path = '/api/get_northbound_funds'; Params = @{}; Target = 'HsgtDaily（北向资金）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'limit_up'; Path = '/api/get_limit_up_stock_list'; Params = @{ order = 1; count = 3 }; Target = 'LimitPool（pool_type=zt）'; Note = 'list[].1=代码、2=名称、5=连板标记、100=原始列数组'; Subscribe = $false }
    [pscustomobject]@{ Key = 'limit_down'; Path = '/api/get_limit_down_stock_list'; Params = @{ order = 1; count = 3 }; Target = 'LimitPool（pool_type=dt）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'broken'; Path = '/api/get_broken_stock_list'; Params = @{ order = 1; count = 3 }; Target = 'LimitPool（炸板）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'natural_limit'; Path = '/api/get_natural_limit_stock_list'; Params = @{ order = 1; count = 3 }; Target = 'LimitPool（自然涨停）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'once_limit_down'; Path = '/api/get_once_limit_down_stock_list'; Params = @{ order = 1; count = 3 }; Target = 'LimitPool（曾跌停）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'new_stock'; Path = '/api/get_new_stock_stock_list'; Params = @{ order = 1; count = 3 }; Target = '新股池（站内暂无表）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'auction'; Path = '/api/get_auction_stock_list'; Params = @{ order = 1; count = 3 }; Target = 'AuctionSnapshot（竞价）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'market_radar'; Path = '/api/get_market_radar'; Params = @{}; Target = '异动雷达 / 首页（站内暂无表）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'lhb_range'; Path = '/api/get_realtime_lhb_range'; Params = @{ start_time = 93000; end_time = 150000; count = 3 }; Target = 'Lhb / LhbInstitution'; Note = '区间一次性版；订阅版 get_realtime_lhb 默认不调'; Subscribe = $false }
    [pscustomobject]@{ Key = 'forecast_lhb'; Path = '/api/get_forecast_lhb'; Params = @{ count = 3 }; Target = '龙虎榜 · 要涨吗（站内暂无表）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'plate_quotas'; Path = '/api/get_plate_type_quotas'; Params = @{ plate_type = 4; count = 5 }; Target = 'SectorDaily / SectorBasic'; Note = 'plate_type 只能 4(行业)/6(地区)/7(精选)，别的值直接 500'; Subscribe = $false }
    [pscustomobject]@{ Key = 'hot_block'; Path = '/api/get_hot_block_list'; Params = @{ st = 5 }; Target = '热门板块（站内暂无表）'; Note = '每页条数是 st 不是 count（count=2 仍返回 50 条）'; Subscribe = $false }
    [pscustomobject]@{ Key = 'block_ex'; Path = '/api/get_block_stock_list_ex'; Params = @{ st = 5 }; Target = 'SectorMember（板块成分股）'; Note = 'bk_code 缺省时脚本用 plate_quotas 返回的第一个 plateId 补上'; Subscribe = $false }
    [pscustomobject]@{ Key = 'index_quotas'; Path = '/api/get_index_simple_quotas'; Params = @{ stock_ids = 'SH000001,SZ399001,SZ399006' }; Target = 'IndexDaily（指数简版行情）'; Note = 'price / incPrice ÷10000 才是点位'; Subscribe = $false }
    [pscustomobject]@{ Key = 'main_index_trends'; Path = '/api/get_main_index_trends'; Params = @{}; Target = '指数分时（首页 / 指数页）'; Note = 'points[].price ÷10000'; Subscribe = $false }
    [pscustomobject]@{ Key = 'stock_pk'; Path = '/api/get_stock_pk'; Params = @{ stock_id = '600519' }; Target = '个股快照 + 盘口（个股页）'; Note = 'price / avg / high / low ÷10000'; Subscribe = $false }
    [pscustomobject]@{ Key = 'stock_mx'; Path = '/api/get_stock_mx'; Params = @{ stock_id = '600519' }; Target = '逐笔成交明细（个股页）'; Note = 'price ÷10000'; Subscribe = $false }
    [pscustomobject]@{ Key = 'related_theme'; Path = '/api/get_stock_related_theme'; Params = @{ stock_id = '600519' }; Target = 'StockConcept（个股关联主题）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'related_plate'; Path = '/api/get_stock_related_plate'; Params = @{ stock_id = '600519' }; Target = '个股关联板块（个股页）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'today_zt_reason'; Path = '/api/get_today_zt_reason'; Params = @{ code = '600519' }; Target = 'LimitReason（今日涨停原因）'; Note = '参数是 code（纯 6 位），不是 stock_id'; Subscribe = $false }
    [pscustomobject]@{ Key = 'message_express'; Path = '/api/get_message_express'; Params = @{ code = '600519' }; Target = '个股消息快报（个股页 / 简报）'; Note = ''; Subscribe = $false }
    [pscustomobject]@{ Key = 'pop_rank'; Path = '/api/get_stock_pop_rank'; Params = @{ type_ = 1; count = 3 }; Target = '开盘红人气榜（站内暂无表）'; Note = '排序字段是 type_（带下划线）；非法值不报错、直接回默认 50 条'; Subscribe = $false }
    [pscustomobject]@{ Key = 'abnormal_price'; Path = '/api/get_abnormal_price'; Params = @{ stock_id = 'SH600519' }; Target = '异动预警线（个股页）'; Note = '这里要带市场前缀（SH/SZ），与其它个股接口不同'; Subscribe = $false }
    [pscustomobject]@{ Key = 'quotes'; Path = '/api/get_stock_quotes'; Params = @{ secids = '1.600519,0.000001' }; Target = 'StockDaily 当日快照校验（实时行情）'; Note = '腾讯/东财代理，无外层包装；secids 是东财格式（1.=沪 0.=深）'; Subscribe = $false }
    [pscustomobject]@{ Key = 'kline'; Path = '/api/get_stock_kline'; Params = @{ code = '600519'; daynum = 5; source = 'tencent' }; Target = 'StockDaily（日 K 交叉校验）'; Note = 'klines 是逗号分隔字符串数组'; Subscribe = $false }
    [pscustomobject]@{ Key = 'tradeday'; Path = '/api/get_tradeday_list'; Params = @{ daynum = 5 }; Target = 'TradeCalendar（交易日历）'; Note = ''; Subscribe = $false }
    # 订阅型：默认不跑，-WithSubscribe 才碰（跑完脚本会退订）
    [pscustomobject]@{ Key = 'kph_trends'; Path = '/api/get_kph_trends'; Params = @{ code = '600519'; timeout = 5 }; Target = '个股分时（个股页，可替代/校验东财分时）'; Note = '订阅型；跑完脚本会调 /api/unsubscribe_kph_all'; Subscribe = $true }
)

# ------------------------------------------------------------------ 工具函数

function Get-Prop {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

function Test-List {
    param($Value)
    if ($null -eq $Value) { return $false }
    if ($Value -is [string]) { return $false }
    return ($Value -is [System.Collections.IEnumerable])
}

function Get-Shape {
    param($Value)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [string]) { return 'string' }
    if ($Value -is [bool]) { return 'bool' }
    if ($Value -is [ValueType]) { return $Value.GetType().Name.ToLower() }
    if (Test-List $Value) {
        $arr = @($Value)
        if ($arr.Count -eq 0) { return 'list[0]' }
        return "list[$($arr.Count)] of $(Get-Shape $arr[0])"
    }
    $names = @($Value.PSObject.Properties | ForEach-Object { $_.Name })
    $shown = ($names | Select-Object -First 12) -join ','
    $more = ''
    if ($names.Count -gt 12) { $more = '…' }
    return "dict{$shown$more}"
}

function Get-Sample {
    param($Value)
    # 空数组在 ConvertTo-Json 下会变成空/null，直接给 [] 更贴近实际
    if ((Test-List $Value) -and (@($Value).Count -eq 0)) { return '[]' }
    $text = $null
    # 必须用 -InputObject：管道会把数组拆成一个个元素，序列化出来的就不是数组了
    try { $text = ConvertTo-Json -InputObject $Value -Compress -Depth 6 } catch { $text = [string]$Value }
    if ($null -eq $text) { $text = 'null' }
    $text = $text.Replace("`r", ' ').Replace("`n", ' ').Replace('|', '\|')
    if ($text.Length -gt 60) { $text = $text.Substring(0, 60) + '…' }
    return $text
}

function Get-FieldRows {
    param($Payload)
    if ($null -eq $Payload) { return @() }
    $data = $Payload
    $response = Get-Prop $Payload 'response'
    if ($null -ne $response) { $data = Get-Prop $response 'data' }
    if ($null -eq $data -or $data -is [string] -or $data -is [ValueType]) { return @() }

    $rows = New-Object System.Collections.ArrayList
    foreach ($prop in @($data.PSObject.Properties)) {
        $value = $prop.Value
        $first = $null
        if (Test-List $value) {
            $arr = @($value)
            if ($arr.Count -gt 0 -and -not ($arr[0] -is [string]) -and $null -ne $arr[0]) { $first = $arr[0] }
        }
        if ($null -ne $first -and -not (Test-List $first) -and -not ($first -is [ValueType])) {
            foreach ($inner in @($first.PSObject.Properties | Select-Object -First 10)) {
                $null = $rows.Add([pscustomobject]@{
                        Path   = "$($prop.Name)[0].$($inner.Name)"
                        Kind   = (Get-Shape $inner.Value)
                        Sample = (Get-Sample $inner.Value)
                    })
            }
        } elseif (-not (Test-List $value) -and -not ($value -is [ValueType]) -and -not ($value -is [string]) -and $null -ne $value -and @($value.PSObject.Properties).Count -gt 0) {
            foreach ($inner in @($value.PSObject.Properties | Select-Object -First 10)) {
                $null = $rows.Add([pscustomobject]@{
                        Path   = "$($prop.Name).$($inner.Name)"
                        Kind   = (Get-Shape $inner.Value)
                        Sample = (Get-Sample $inner.Value)
                    })
            }
        } else {
            $null = $rows.Add([pscustomobject]@{
                    Path   = $prop.Name
                    Kind   = (Get-Shape $value)
                    Sample = (Get-Sample $value)
                })
        }
    }
    return $rows
}

function Join-Query {
    param($Params)
    if ($null -eq $Params -or $Params.Count -eq 0) { return '' }
    $pairs = @()
    foreach ($key in $Params.Keys) {
        $pairs += "$key=$([uri]::EscapeDataString([string]$Params[$key]))"
    }
    return ($pairs -join '&')
}

function Invoke-Probe {
    param([string]$BaseUrl, $Call, [int]$Timeout)
    $query = Join-Query $Call.Params
    $url = "$BaseUrl$($Call.Path)"
    if ($query) { $url = "$url`?$query" }

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $status = $null
    $text = $null
    $errorText = $null
    try {
        $resp = Invoke-WebRequest -Uri $url -TimeoutSec $Timeout -UseBasicParsing -ErrorAction Stop
        $status = [int]$resp.StatusCode
        $text = $resp.Content
    } catch {
        $ex = $_.Exception
        # 4xx/5xx 的响应体最有用（"缺少参数 stock_id" 这类），PS 5.1 放在 ErrorDetails 里
        try {
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $text = $_.ErrorDetails.Message }
        } catch { }
        if ($null -ne $ex.Response) {
            try { $status = [int]$ex.Response.StatusCode } catch { }
            if (-not $text) {
                try {
                    $stream = $ex.Response.GetResponseStream()
                    if ($null -ne $stream) {
                        $reader = New-Object System.IO.StreamReader($stream)
                        $text = $reader.ReadToEnd()
                        $reader.Close()
                    }
                } catch { }
            }
        }
        if (-not $text) {
            $errorText = "$($ex.GetType().Name): $($ex.Message)"
        }
    }
    $sw.Stop()
    $ms = [int]$sw.ElapsedMilliseconds

    if ($errorText) {
        return [pscustomobject]@{ Ok = $false; Status = $status; Ms = $ms; Error = $errorText; Json = $null; Url = $url }
    }
    $json = $null
    try { $json = $text | ConvertFrom-Json } catch {
        $snippet = ''
        if ($text) { $snippet = $text.Substring(0, [Math]::Min(300, $text.Length)) }
        return [pscustomobject]@{ Ok = $false; Status = $status; Ms = $ms; Error = "不是合法 JSON：$snippet"; Json = $null; Url = $url }
    }
    $ok = ($null -ne $status -and $status -ge 200 -and $status -lt 300)
    $success = Get-Prop $json 'success'
    if ($success -eq $false) { $ok = $false }
    return [pscustomobject]@{ Ok = $ok; Status = $status; Ms = $ms; Error = ''; Json = $json; Url = $url }
}

function Format-Status {
    param($Result)
    if ($null -eq $Result.Status) { return '不可达' }
    return [string]$Result.Status
}

# ------------------------------------------------------------------ 渲染报告

function New-Report {
    param([string]$BaseUrl, $Health, $Results)

    $state = $Health.Json
    $lines = New-Object System.Collections.Generic.List[string]
    $today = (Get-Date).ToString('yyyy-MM-dd')
    $now = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $connected = Get-Prop $state 'connected'
    $initialized = Get-Prop $state 'initialized'
    $stamp = Get-Prop $state 'timestamp'

    $lines.Add("# 开盘红数据服务 · 接口实测与字段对照（$today）")
    $lines.Add('')
    $lines.Add("- 服务地址：``$BaseUrl``")
    $lines.Add("- ``/api/status``：``connected=$connected``、``initialized=$initialized``、``$stamp``")
    $lines.Add('- 探针：`check_kph.ps1`（只读；订阅型接口默认跳过，跑完会退订）')
    $lines.Add("- 实测时间：$now")
    $lines.Add('')
    $lines.Add('> 数值口径见说明书 §2.4：开盘红协议里价格 / 金额多为 ×10000 的整数，下面「字段」列会标出需要 ÷10000 的那些。')
    $lines.Add('')
    $lines.Add('## 总览')
    $lines.Add('')
    $lines.Add('| 接口 | 结果 | HTTP | 耗时 | 顶层结构 | 站内可对接 |')
    $lines.Add('|---|---|---|---|---|---|')

    foreach ($result in $Results) {
        $call = $result.Call
        $top = '—'
        if ($null -ne $result.Json) {
            $response = Get-Prop $result.Json 'response'
            if ($null -ne $response) { $top = Get-Shape $response } else { $top = Get-Shape $result.Json }
        }
        $mark = '✗'
        if ($result.Ok) { $mark = '✓' }
        $lines.Add("| ``$($call.Path)`` | $mark | $(Format-Status $result) | $($result.Ms) ms | ``$top`` | $($call.Target) |")
    }

    foreach ($result in $Results) {
        $call = $result.Call
        $lines.Add('')
        $lines.Add("## ``$($call.Path)``")
        $lines.Add('')
        if ($call.Params -and $call.Params.Count -gt 0) {
            $lines.Add("- 请求：``GET $($call.Path)?$(Join-Query $call.Params)``")
        }
        if ($call.Note) { $lines.Add("- 说明：$($call.Note)") }
        if ($call.Target) { $lines.Add("- 站内可对接：$($call.Target)") }
        $response = $null
        if ($null -ne $result.Json) { $response = Get-Prop $result.Json 'response' }
        if ($null -ne $response) {
            $cmd = Get-Prop $response 'cmd_id'
            if ($cmd) { $lines.Add("- cmd：``$cmd``") }
        }
        $mark = '✗'
        if ($result.Ok) { $mark = '✓' }
        $lines.Add('')
        $lines.Add("- 结果：$mark（$(Format-Status $result)，$($result.Ms) ms）")

        if (-not $result.Ok -and $result.Error) {
            $lines.Add('')
            $lines.Add('```')
            $lines.Add($result.Error)
            $lines.Add('```')
            continue
        }
        $rows = @(Get-FieldRows $result.Json)
        if ($rows.Count -gt 0) {
            $lines.Add('')
            $lines.Add('| 字段 | 类型 | 样例 |')
            $lines.Add('|---|---|---|')
            foreach ($row in $rows) {
                $lines.Add("| ``$($row.Path)`` | $($row.Kind) | ``$($row.Sample)`` |")
            }
        } else {
            $lines.Add('')
            $lines.Add('（没有可展开的字段：响应不是 dict 结构）')
        }
        $pretty = ''
        try { $pretty = $result.Json | ConvertTo-Json -Depth 12 } catch { $pretty = [string]$result.Json }
        if ($pretty.Length -gt 1600) { $pretty = $pretty.Substring(0, 1600) + "`n…（截断）" }
        $lines.Add('')
        $lines.Add('```json')
        $lines.Add($pretty)
        $lines.Add('```')
    }

    $lines.Add('')
    $lines.Add('## 下一步怎么读这份报告')
    $lines.Add('')
    $lines.Add('1. 「总览」里 ✗ 的先看是不是服务端的 mTLS 断了（说明书 §7.2：某接口超时后整条链路失效，要重启 exe）；')
    $lines.Add('2. 字段列里带 `100` 这种数字键的（打板池），是上游的**原始列数组**，顺序要按说明书或上游 proto 对齐 —— 直接对接时优先用 `1`(代码) / `2`(名称) / `5`(连板) 这些有名字的键；')
    $lines.Add('3. 要接哪几块，按「站内可对接」那列挑，再按现有 `sources/` + `jobs/` 的写法接。')
    $lines.Add('')
    return ($lines -join "`r`n")
}

# ------------------------------------------------------------------ 主流程

if ($SelfTest) {
    # 不联网：用说明书里的示例响应验一遍渲染，确保脚本本身没写坏
    $fakeHealth = [pscustomobject]@{ Ok = $true; Status = 200; Ms = 3; Error = ''; Url = ''; Json = [pscustomobject]@{ connected = $true; initialized = $true; timestamp = '2026-10-04T21:55:51' } }
    $fakeJson = [pscustomobject]@{
        success  = $true
        response = [pscustomobject]@{
            cmd_id = '08 37'
            data   = [pscustomobject]@{
                date     = '2026-09-30'
                indexes  = @()
                list     = @([pscustomobject]@{ '1' = '600131'; '2' = '国网信通'; '5' = 1; '100' = @('', '', '') })
                max_size = 52
            }
        }
    }
    $fakeResult = [pscustomobject]@{ Ok = $true; Status = 200; Ms = 12; Error = ''; Url = ''; Json = $fakeJson; Call = ($Calls | Where-Object { $_.Key -eq 'limit_up' }) }
    $md = New-Report $Base $fakeHealth @($fakeResult)
    Write-Output $md
    Write-Output ''
    Write-Output '（自检完成：未联网）'
    exit 0
}

$Base = $Base.TrimEnd('/')
$wanted = @()
if ($Only) { $wanted = @($Only.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
$selected = @($Calls | Where-Object {
        (-not $wanted -or $wanted -contains $_.Key) -and ($WithSubscribe -or -not $_.Subscribe)
    })

Write-Output "==> 探针目标：$Base（$($selected.Count) 个接口）"

$health = Invoke-Probe $Base ([pscustomobject]@{ Key = 'status'; Path = '/api/status'; Params = @{} }) ([Math]::Min($TimeoutSec, 5))
if ($null -eq $health.Status) {
    Write-Output "✗ 连不上 $Base：$($health.Error)"
    Write-Output '  先把 StockDataServer.exe 跑起来，等控制台出现「远程服务器连接成功！」再试。'
    exit 2
}
$connected = Get-Prop $health.Json 'connected'
$initialized = Get-Prop $health.Json 'initialized'
Write-Output "✓ 服务在线：connected=$connected initialized=$initialized"

$results = New-Object System.Collections.ArrayList
$plateId = ''
foreach ($call in $selected) {
    # block_ex 的 bk_code 用板块列表里的第一个（脚本内自动补，免得手填）
    if ($call.Key -eq 'block_ex' -and $plateId -and -not $call.Params.ContainsKey('bk_code')) {
        $call.Params['bk_code'] = $plateId
    }
    $result = Invoke-Probe $Base $call $TimeoutSec
    $result | Add-Member -NotePropertyName Call -NotePropertyValue $call
    $null = $results.Add($result)

    $mark = '✗'
    if ($result.Ok) { $mark = '✓' }
    $extra = ''
    if (-not $result.Ok -and $result.Error) { $extra = "  $($result.Error)" }
    Write-Output ("{0} {1,-18} {2,4}  {3,5} ms{4}" -f $mark, $call.Key, (Format-Status $result), $result.Ms, $extra)

    if ($call.Key -eq 'plate_quotas' -and $null -ne $result.Json) {
        $response = Get-Prop $result.Json 'response'
        $data = Get-Prop $response 'data'
        $items = @(Get-Prop $data 'items')
        if ($items.Count -gt 0) {
            $first = $items[0]
            $pidValue = Get-Prop $first 'plateId'
            if (-not $pidValue) { $pidValue = Get-Prop $first 'plate_id' }
            if ($pidValue) { $plateId = [string]$pidValue }
        }
    }
}

# 订阅型接口用完就退订（别把服务端订阅名额占着）
$usedSubscribe = @($selected | Where-Object { $_.Subscribe })
if ($usedSubscribe.Count -gt 0) {
    $clearing = Invoke-Probe $Base ([pscustomobject]@{ Key = 'unsub'; Path = '/api/unsubscribe_kph_all'; Params = @{} }) $TimeoutSec
    if ($clearing.Ok) { Write-Output '✓ 已退订全部分时订阅' } else { Write-Output '✗ 退订失败（可手动 GET /api/unsubscribe_kph_all）' }
}

if (-not $Out) { $Out = "开盘红接口实测_$(Get-Date -Format 'yyyyMMdd').md" }
$outFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Out)
$dir = Split-Path -Parent $outFull
if ($dir -and -not (Test-Path $dir)) { $null = New-Item -ItemType Directory -Path $dir -Force }

$markdown = New-Report $Base $health $results
# UTF-8 with BOM：Windows 记事本 / Excel 打开中文都不乱码
[System.IO.File]::WriteAllText($outFull, $markdown, (New-Object System.Text.UTF8Encoding($true)))
Write-Output ''
Write-Output "报告：$outFull"

$failed = @($results | Where-Object { -not $_.Ok })
if ($failed.Count -gt 0) {
    $names = ($failed | ForEach-Object { $_.Call.Key }) -join ', '
    Write-Output "有 $($failed.Count) 个接口没跑通：$names"
    exit 1
}
exit 0
