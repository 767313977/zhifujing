#!/usr/bin/env bash
# 在一台全新的 Ubuntu / Debian 服务器上把复盘站点跑起来（只做「采集 + 飞书推送」）。
#
# 前置：用本机 scripts/pack_deploy.py 打好包，传到服务器并解开到 /opt/fupan，
#       然后在 /opt/fupan 下执行：sudo bash deploy/install.sh
#
# 跑完这台机器就自己按交易日 18:00 采集并推送到飞书了，不依赖本机开机。
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
# 不是为了日志好看。代码判断「今天是不是交易日」「现在过没过 18:00」用的是
# **系统本地时间**（date.today() / datetime.now()）：
# - 18:00 的定点采集本身不受影响（APScheduler 显式指定了 Asia/Shanghai）
# - 但**启动补采会失效** —— 它把北京时间 18:10 当成 UTC 10:10，判定「还没到采集
#   时刻」直接跳过。本机不常开、靠补采兜底的那套逻辑就废了。
if command -v timedatectl >/dev/null 2>&1; then
  log "设置时区为 Asia/Shanghai"
  timedatectl set-timezone Asia/Shanghai
fi

log "安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq "${PYTHON}-venv" python3-pip >/dev/null

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
"$APP_DIR/.venv/bin/pip" install -q -i "$PIP_INDEX" --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -i "$PIP_INDEX" -r "$APP_DIR/backend/requirements.txt"

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
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=10
# 采集是长任务，停服务时给 APScheduler 收尾的时间
TimeoutStopSec=30

# 只绑 127.0.0.1：这台机器不需要对外暴露任何端口，采集与推送全是出站请求。
# 以后要在外网看网页，另配反向代理 + 鉴权，**不要**把这行改成 0.0.0.0。
# 注意 .env 里的 HOST=0.0.0.0 是给本机用的（让同一 WiFi 下的手机能打开）——
# 这条命令显式指定了 --host，所以不受它影响；但**也别把它改成 python -m app.main**，
# 那样会去读 .env，等于把这个没有鉴权的服务挂到公网上。
#
# （写注释时注意：这个 heredoc 故意没加引号，变量要展开；所以注释里**不能出现反引号**，
#   反引号在 bash 里是命令替换，会被真的执行一次 —— 踩过。）
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

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

cat <<'DONE'

跑起来了。接下来自己确认两件事：

  1. 采集能不能真的连通（iFinD 的授权可能绑 IP，换了机器未必认）：
       sudo -u fupan /opt/fupan/.venv/bin/python /opt/fupan/scripts/push_brief.py --print
     打出简报内容就说明本地库没问题；再手动触发一次采集验证外部数据源：
       curl -s -X POST http://127.0.0.1:8000/api/admin/collect
     这一步会消耗 iFinD 权益次数，但只跑一次，值得先验证。

  2. 看日志确认定时任务挂上了：
       journalctl -u fupan -n 50 --no-pager
     应该能看到「定时采集已启动：交易日 18:00」。

  3. 这台机器开始推简报之后，把**本机那套停掉**。
     简报的去重是查 collect_log 决定的，而两边各查自己的库 ——
     不停的话 18:00 会收到两条一模一样的简报。

DONE
