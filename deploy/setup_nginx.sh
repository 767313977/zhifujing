#!/usr/bin/env bash
# 给云端这台机器配一个「带密码的网页入口」（nginx + Basic Auth）。
#
# ## 为什么需要它
#
# 后端的 8000 端口**没有任何鉴权**：谁扫到都能看你的自选股、复盘笔记，
# 还能 POST /api/admin/collect 触发采集 —— 那会白烧 iFinD 配额（每天上限就
# 五千次，烧光了第二天的采集也会一起失败）。
#
# 所以 **不要**把 8000 直接对外（systemd unit 里绑 127.0.0.1 就是这个原因），
# 必须前面挡一层带密码的反向代理。
#
# ## 用法（在服务器上）
#
#     sudo bash deploy/setup_nginx.sh
#
# 跑之前先在腾讯云控制台的「防火墙」里放行 8080 端口。
#
# ## 一个必须知道的局限
#
# Basic Auth 走的是**明文 HTTP** —— 密码在链路上不加密，同网段的嗅探能看到。
# 家里 / 公司网络够用；要更稳妥就绑个域名配 HTTPS（Let's Encrypt），
# 那是另一件事，这个脚本不碰。
set -euo pipefail

SITE_NAME="${SITE_NAME:-fupan}"
USER_NAME="${USER_NAME:-fupan}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
HTPASSWD="/etc/nginx/.htpasswd"
SITE="/etc/nginx/sites-available/$SITE_NAME"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "要用 root 跑：sudo bash deploy/setup_nginx.sh" >&2
  exit 1
fi

log "安装 nginx 与 htpasswd"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx apache2-utils >/dev/null

if [[ -f "$HTPASSWD" ]]; then
  log "密码文件已存在，跳过。要改密码：sudo htpasswd $HTPASSWD $USER_NAME"
else
  log "设置网页访问的账号密码（账号：$USER_NAME）"
  htpasswd -c "$HTPASSWD" "$USER_NAME"
fi

log "写 nginx 站点配置"
# 定界符带着引号（<<'NGINX'）：里面的 $host / $remote_addr 是 **nginx 的变量**，
# 必须原样写进配置文件。不加引号的话 bash 会先把它们展开成空字符串 ——
# 这是这套脚本里最容易踩的一个坑（install.sh 里反过来踩过一次反引号）。
cat > "$SITE" <<'NGINX'
server {
    # 用 8080 而不是 80：腾讯云**境内**机器没备案时 80 端口会被拦掉，
    # 而「纯 IP + 非 80 端口」不受备案限制。想用 80 就得先备案一个域名。
    listen 8080 default_server;
    server_name _;

    # 全站要密码。**别删这几行** —— 后端本身没有任何鉴权，这层是唯一的门。
    auth_basic "fupan";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # 一次采集要跑几分钟（板块那步是逐个拉 400 多个板块，实测约 8 分钟）。
        # 默认的 60s 会让 nginx 提前断开，页面上显示成「采集失败」——
        # 而后台其实还在采，这种「假失败」最难排查。
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
NGINX

log "启用站点（并摘掉 nginx 自带的默认页）"
ln -sf "$SITE" "/etc/nginx/sites-enabled/$SITE_NAME"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

log "完成。确认腾讯云控制台的「防火墙」已放行 8080 后，浏览器打开："
echo "    http://<服务器IP>:8080/"
echo "    账号：$USER_NAME   密码：你刚设的那个"
