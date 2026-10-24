#!/usr/bin/env bash
# 在一台全新的 Ubuntu / Debian 服务器上把复盘站点跑起来（只做「采集 + 飞书推送」）。
#
# 前置：用本机 scripts/pack_deploy.py 打好包，传到服务器并解开到 /opt/fupan，
#       然后在 /opt/fupan 下执行：sudo bash deploy/install.sh
#
# 跑完这台机器就自己按交易日的采集时刻（`config.collect_hour/minute`）采集并推送到飞书了，
# 不依赖本机开机。当前**收盘后**是 15:05，另有 17:30 / 19:30 收盘后那一趟、22:00 / 22:30
# 成分股预取（都在 config.py 里，上面这句只是给第一次看这脚本的人一个方向感）。
#
# 可覆盖的环境变量：APP_DIR（默认 /opt/fupan）、APP_USER（默认 fupan）、SERVICE（默认 fupan）
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/fupan}"
APP_USER="${APP_USER:-fupan}"
SERVICE="${SERVICE:-fupan}"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "要用 root 跑：sudo bash deploy/install.sh"
[[ -f "$APP_DIR/backend/requirements.txt" ]] || die "$APP_DIR 下没有 backend/requirements.txt，先把包解开"
[[ -f "$APP_DIR/.env" ]] || die "$APP_DIR/.env 不在，采集会因为拿不到 IFIND_AUTH_TOKEN 而失败"

# ------------------------------------------------------- Python 版本（硬约束）
# numpy 要求 >= 3.12（它跟着 pandas 一起装），pandas 3.0 与 akshare 要求 >= 3.11。
# 不先查的话，会在 `pip install` 那一步才炸 —— 而它前面已经 apt 装了一堆、
# 后面还要下几百 MB，白等一场。Debian 12 是 3.11、Ubuntu 22.04 是 3.10，都不够；
# Ubuntu 24.04 / Debian 13 自带 3.12+，直接就能用。
PYTHON="${PYTHON:-python3}"
PY_VER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 未知)"
PY_OK="$("$PYTHON" -c 'import sys; print(1 if sys.version_info >= (3, 12) else 0)' 2>/dev/null || echo 0)"
if [[ "$PY_OK" != "1" ]]; then
  die "$PYTHON 是 $PY_VER，但依赖要求 >= 3.12。两条路：
    1) 换成 Ubuntu 24.04+ / Debian 13+，自带 3.12 以上；
    2) 在旧系统上装高版本 Python，再指定给这个脚本：
         sudo apt-get install -y software-properties-common
         sudo add-apt-repository -y ppa:deadsnakes/ppa
         sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv
         sudo PYTHON=python3.12 bash deploy/install.sh"
fi

# ---------------------------------------------------------------- 时区（必做）
# 不是为了日志好看。代码判断「今天是不是交易日」「现在过没过采集时刻」用的是
# **系统本地时间**（date.today() / datetime.now()）：
# - 定点采集本身不受影响（APScheduler 显式指定了 Asia/Shanghai）
# - 但**启动补采会失效** —— 它把北京时间 17:40 当成 UTC 09:40，判定「还没到采集
#   时刻」直接跳过。本机不常开、靠补采兜底的那套逻辑就废了。
if command -v timedatectl >/dev/null 2>&1; then
  log "设置时区为 Asia/Shanghai"
  timedatectl set-timezone Asia/Shanghai
fi

# ---------------------------------------------------------------- 密钥（必做）
# 部署包**不再携带 .env**（2026-10-22）：它含 iFinD token / 飞书 webhook / LLM key，
# 跟着每个历史压缩包扩散太危险，而且覆盖式写入会把服务器配置换成本机调试值。
# 服务器这份只手工维护；全新机器先 scp 一份上来再跑本脚本。
if [[ ! -f "$APP_DIR/.env" ]]; then
  die "缺少 $APP_DIR/.env：部署包不再携带密钥。请先把服务器的 .env 放好（参考仓库根 .env），再重跑本脚本。"
fi

# ---------------------------------------------------------------- 数据库（可选）
# `--with-db` 打的包里放的是 `fupan.db.seed`，**不是** `fupan.db`（2026-10-22）：
# 以前包里直接带 `fupan.db`，解压那一步就静默覆盖了线上库（用户 / 邀请码 / 笔记 /
# 自选股全在里面，不可逆）。改成 seed 之后，解压永远碰不到线上库，是否采用由这里决定：
#   新机器（没有 fupan.db）→ 直接把 seed 当初始库
#   已有库 → 先原样备份，再要求显式 `--force-db` 才覆盖
DATA_DIR="$APP_DIR/backend/data"
if [[ -f "$DATA_DIR/fupan.db.seed" ]]; then
  if [[ -f "$DATA_DIR/fupan.db" ]]; then
    DB_BACKUP="$DATA_DIR/fupan.db.bak-$(date +%Y%m%d-%H%M%S)"
    cp -p "$DATA_DIR/fupan.db" "$DB_BACKUP"
    log "线上库已备份：$DB_BACKUP（$(du -h "$DB_BACKUP" | cut -f1)）"
    if [[ ! " $* " =~ " --force-db " ]]; then
      die "包里带了初始数据库，但 $DATA_DIR/fupan.db 已存在（已备份为 $(basename "$DB_BACKUP")）。
     **没有**覆盖线上库。确认要用包里的库覆盖，重跑：
       sudo bash deploy/install.sh --force-db"
    fi
    log "按 --force-db 用包里的库覆盖线上库"
    mv -f "$DATA_DIR/fupan.db.seed" "$DATA_DIR/fupan.db"
    # 旧库的 WAL / SHM 必须一起清掉，否则会被 SQLite 当成新库的日志回放
    rm -f "$DATA_DIR/fupan.db-wal" "$DATA_DIR/fupan.db-shm"
  else
    log "没找到已有数据库，用包里的初始库"
    mv "$DATA_DIR/fupan.db.seed" "$DATA_DIR/fupan.db"
  fi
fi

log "安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq "${PYTHON}-venv" python3-pip ca-certificates >/dev/null

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  log "创建运行用户 $APP_USER"
  # --no-create-home：目录是解包时就有了，让 useradd 别去动它
  useradd --system --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

log "建虚拟环境并装依赖（akshare + pandas 有几百 MB，要等一会儿）"
"$PYTHON" -m venv "$APP_DIR/.venv"
# 走国内镜像：依赖包共 233 MB，从 PyPI 直连在腾讯云上要十几分钟还容易断；
# 换清华源大约两三分钟。要改回官方源就 PIP_INDEX=https://pypi.org/simple
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
# ⚠️ 用 `python -m pip` 而不是 `.venv/bin/pip`（2026-10-24 修）：
# 那批入口脚本的可执行位**可能已经被抹掉**（实测服务器上 pip/uvicorn 都是 644，
# 是历史某次整目录 chmod 留下的），直接调会 `Permission denied`（退出码 126），
# 部署中断在半路。`python -m pip` 只依赖解释器本身，与脚本权限无关。
VENV_PY="$APP_DIR/.venv/bin/python"
"$VENV_PY" -m pip install -q -i "$PIP_INDEX" --upgrade pip
"$VENV_PY" -m pip install -q -i "$PIP_INDEX" -r "$APP_DIR/backend/requirements.txt"
# 顺手把入口脚本的可执行位补回来：以后谁再直接调 `.venv/bin/uvicorn` 也不会踩同一个坑
chmod 755 "$APP_DIR/.venv/bin/"* 2>/dev/null || true

# ------------------------------------------------------- 清理旧的前端产物
# vite 每次构建都给产物换一套哈希文件名（index-abc.js → index-def.js），而部署是
# **解压覆盖** —— 旧的那套不会被删，每部署一次就多留约 1 MB。实测攒到 26 个文件
# / 17 MB，而 index.html 只用其中最新的两个。clean_dist.py 从 index.html 出发
# 按引用关系回收（详见其模块说明）。
#
# **失败不能让部署中断**：清理只是省磁盘，清不掉不该把整个安装拖垮，所以吞掉退出码
# 只告警。（这里的 PYTHON 是系统 python3，clean_dist.py 只用标准库。）
log "清理旧的前端产物"
if ! "$PYTHON" "$APP_DIR/scripts/clean_dist.py" --dist "$APP_DIR/frontend/dist"; then
  printf '\033[1;33m警告:\033[0m 旧产物清理失败，已跳过（不影响运行）\n'
fi

log "写 systemd 服务"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=A股复盘站点（采集 + 飞书推送）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR/backend
Environment=PYTHONUNBUFFERED=1
# 时区也钉在**服务进程**上（2026-10-10 加）：代码判「今天是不是交易日」「过没过采集
# 时刻」用的是进程本地时间（date.today / datetime.now）。上面那段 timedatectl 只在
# 系统带 systemd-timedated 时才跑（脚本里套了 command -v）—— 万一哪台机器没有它，
# 启动补采会把北京时间当 UTC 判、差 8 小时直接跳过。这里钉死之后，无论系统时钟怎么配，
# date.today() 都是北京日期。
Environment=TZ=Asia/Shanghai
# 云端**永远**是采集机。2026-10-22 起部署包不再带 .env（服务器那份手工维护），
# 但这条仍保留：systemd 环境变量优先级高于 .env，万一手工拷配置时写成 false，
# 也不会把云端的定时采集静悄悄关掉。
Environment=SCHEDULER_ENABLED=true
# iFinD 的 TLS 证书校验（2026-10-12 加）。**放这里比只写 .env 稳**：.env 是手工
# 维护的，换机器 / 重装容易漏；systemd 里钉死之后不会再漂。路径用系统 CA 包：
# iFinD 那张证书是 DigiCert 签发的公共证书，用系统信任库即可校验。
Environment=IFIND_CA_FILE=/etc/ssl/certs/ca-certificates.crt
# 生产不开放开发源的 CORS（2026-10-23 体检后加）：main.py 只在
# settings.enable_dev_cors 为真时才把 http://localhost:5173 加进 allow_origins。
# 放 systemd 而不是 .env：.env 是手工维护的、换机器容易漏，而这是**生产语义**。
Environment=ENABLE_DEV_CORS=false
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=10
# 采集是长任务，停服务时给 APScheduler 收尾的时间
TimeoutStopSec=30

# 只绑 127.0.0.1：这台机器不需要对外暴露任何端口，采集与推送全是出站请求。
# 以后要在外网看网页，另配反向代理（deploy/setup_nginx.sh 那套），**不要**把这行改成 0.0.0.0。
# 注意 .env 里的 HOST=0.0.0.0 是给本机用的（让同一 WiFi 下的手机能打开）——
# 这条命令显式指定了 --host，所以不受它影响；但**也别把它改成 python -m app.main**，
# 那样会去读 .env，等于把这个服务直接挂到公网上 —— 后端 2026-09-28 起有登录体系，
# 但公网 IP + 明文 http 那条路会让密码明文过网（api/auth.py 的 _require_secure_transport
# 会直接拒登录，页面开着也进不去）。
#
# （写注释时注意：这个 heredoc 故意没加引号，变量要展开；所以注释里**不能出现反引号**，
#   反引号在 bash 里是命令替换，会被真的执行一次 —— 踩过。）
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
# ---- 以下加固 2026-10-23 体检后加（当时 systemd-analyze exposure = 8.5）----
# LimitNOFILE：默认软上限只有 1024，采集并发 + 全市场日线查询容易碰到 fd 上限。
LimitNOFILE=65535
UMask=0027
ProtectHome=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
UNIT

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---------------------------------------------------- 权限规整（2026-10-23 加）
# 背景（体检发现）：部署包在 **Windows** 上打出来，tar 里是 `-rw-rw-rw-`/`drwxrwxrwx`。
# 解压覆盖后生产上 218 个文件变成 0666（后端 100 个 .py + 前端 75 个 dist JS），
# 任意本地 UID 都能改线上代码。打包脚本（scripts/pack_deploy.py）已从源头规整，
# 这里再做一道兜底 —— 老包 / 手工 scp 的文件也能被纠正。
# ⚠️ 只规整**随包发布的目录**，别碰 `.venv`（bin/ 下的脚本要有可执行位）与
# `backend/data`（700，库里是数据不是代码）。
for _dir in backend/app frontend/dist scripts deploy; do
  [[ -d "$APP_DIR/$_dir" ]] || continue
  find "$APP_DIR/$_dir" -type d -exec chmod 755 {} \;
  find "$APP_DIR/$_dir" -type f -exec chmod 644 {} \;
done
find "$APP_DIR/scripts" -name '*.sh' -exec chmod 755 {} \; 2>/dev/null || true
chmod 600 "$APP_DIR/.env"
chmod 700 "$APP_DIR/backend/data" 2>/dev/null || true

log "启动服务"
systemctl daemon-reload
systemctl enable "$SERVICE"
# 用 restart 而不是 enable --now：后者里的 start 对**已在运行**的服务是 no-op，
# 重跑本脚本更新代码时新代码不会真正加载（日志里还是旧进程）。restart 对
# inactive 的服务等同于 start（首次部署也能正常起），对 running 的服务则强制
# stop + start 重新加载。这样「重跑 install.sh」天然就是「更新 + 重启」。
systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager --lines=15 status "$SERVICE" || true

# ------------------------------------------------- 前端产物自检（防白屏，2026-10-12 加）
# 事故：上面的 clean_dist.py 因为引用正则不全，把**按需加载出来的页面文件全删了** ——
# index.html 还在、其它页面全部 404、登录页都打不开，整站白屏。而当时的部署后核验
# 只看了 index.html，所以没发现。
#
# 这一步从首页出发**真的去请求**每一层引用（页面 chunk / CSS / 字体），缺一个就报错，
# 让「白屏」在部署时变成一次**显式失败**，而不是等用户打开页面才发现。
# 允许重试 3 次：首次部署时服务刚起来，等它把端口绑上。
log "前端产物自检"
selfcheck_ok=0
for _ in 1 2 3; do
  if "$PYTHON" "$APP_DIR/scripts/check_dist.py" --base "http://127.0.0.1:8000"; then
    selfcheck_ok=1
    break
  fi
  sleep 3
done
if [[ "$selfcheck_ok" != "1" ]]; then
  printf '\033[1;31m错误:\033[0m 前端产物自检未通过 —— 页面文件缺失，站点会白屏。\n' >&2
  exit 1
fi

cat <<'DONE'

跑起来了。接下来自己确认两件事：

  1. 采集能不能真的连通（iFinD 的授权可能绑 IP，换了机器未必认）：
       sudo -u fupan /opt/fupan/.venv/bin/python /opt/fupan/scripts/push_brief.py --print
     打出简报内容就说明本地库没问题。

     ⚠️ **没有手动采集接口了**（2026-09-28 用户要求禁止手动采集，`POST
     /api/admin/collect` 与两个页面上的按钮都已删除）—— 它点一下就白跑一轮、
     约 50 次 iFinD 调用。要强制重采就重启服务，`start()` 里的启动补采会在
     「当天数据缺失或有失败步骤」时补跑：
       sudo systemctl restart fupan

  2. 看日志确认定时任务挂上了：
       journalctl -u fupan -n 50 --no-pager
     应该能看到「定时采集已启动：交易日 HH:MM」（时刻取自 config.collect_hour/minute）。

  3. 这台机器开始推简报之后，把**本机那套停掉**。
     简报的去重是查 collect_log 决定的，而两边各查自己的库 ——
     不停的话到点会收到两条一模一样的简报。

DONE
