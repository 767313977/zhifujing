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
#     sudo bash deploy/setup_nginx.sh                     # 只有 8080（纯 IP 访问时用）
#     sudo DOMAIN=a.com CERT_EMAIL=me@x.com bash deploy/setup_nginx.sh   # 再加 80 / 443 + HTTPS
#
# 跑之前先在腾讯云控制台的「防火墙」里放行端口：8080；开 HTTPS 还要 80 和 443。
#
# ## 两种模式
#
# **不带 DOMAIN**：只开 8080 明文。纯 IP 访问时只能这样。
#
# **带 DOMAIN**：80 与 443 都开，80 只用来做 ACME 校验、其余 301 跳 https。
# ⚠️ 前提是域名**已经通过 ICP 备案、并已解析到这台机器**。腾讯云的未备案拦截是
# 按 Host 拦 80/443 的，没过备案时连 Let's Encrypt 的校验请求都会被拦掉，
# 签发必然失败 —— 所以这一步只能在备案下来之后做，顺序不能颠倒。
#
# 8080 两种模式都保留：出问题时（DNS 挂了 / 证书过期了）它是还能进去修的兜底入口。
# 代价是它仍是**明文**，Basic Auth 密码在链路上不加密；HTTPS 稳定后可以把 8080
# 那段整块删掉。
set -euo pipefail

SITE_NAME="${SITE_NAME:-fupan}"
USER_NAME="${USER_NAME:-fupan}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
# 空 = 不启用 HTTPS。给了域名就必须同时给邮箱（Let's Encrypt 注册要一个）
DOMAIN="${DOMAIN:-}"
CERT_EMAIL="${CERT_EMAIL:-}"
HTPASSWD="/etc/nginx/.htpasswd"
SITE="/etc/nginx/sites-available/$SITE_NAME"
# 鉴权 + 反代 + 超时这三件事在 443 与 8080 两个 server 块里都要用，抽成 snippet。
# 抽出来不是为了少打字，是**auth_basic 只允许有一处定义** —— 复制成两份之后
# 迟早有人只改一处（比如想临时关密码），另一处还开着。
PROXY_SNIPPET="/etc/nginx/snippets/$SITE_NAME-proxy.conf"
# certbot --webroot 的校验文件根目录（ACME 的 http-01 会来取这里的东西）
WEBROOT="/var/www/certbot"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "要用 root 跑：sudo bash deploy/setup_nginx.sh" >&2
  exit 1
fi

if [[ -n "$DOMAIN" && -z "$CERT_EMAIL" ]]; then
  echo "开了 DOMAIN 就必须给 CERT_EMAIL（Let's Encrypt 注册要一个邮箱）：" >&2
  echo "    sudo DOMAIN=a.com CERT_EMAIL=me@x.com bash deploy/setup_nginx.sh" >&2
  exit 1
fi
# 只放行域名本身的字符。带了 http:// 、端口或路径进来会写出一份坏配置，
# 而 nginx -t 的报错完全看不出是这里的问题（它只会说 server_name 不合法）
if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "DOMAIN 只能是域名本身（不要带 http://、端口或路径）：$DOMAIN" >&2
  exit 1
fi
# 端口会同时进 sed 的替换串和 nginx 配置：非数字、或带 | 的值会写出一份坏配置，
# 而 nginx -t 同样指不到这里。10# 是为了让 08000 这类前导 0 不按八进制解析。
if [[ ! "$BACKEND_PORT" =~ ^[0-9]+$ ]] || (( 10#$BACKEND_PORT < 1 || 10#$BACKEND_PORT > 65535 )); then
  echo "BACKEND_PORT 要是 1-65535 的整数（当前：$BACKEND_PORT）" >&2
  exit 1
fi

log "安装 nginx 与 htpasswd"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx apache2-utils >/dev/null
if [[ -n "$DOMAIN" ]]; then
  apt-get install -y -qq certbot >/dev/null
fi

if [[ -f "$HTPASSWD" ]]; then
  log "密码文件已存在，跳过。要改密码：sudo htpasswd $HTPASSWD $USER_NAME"
else
  log "设置网页访问的账号密码（账号：$USER_NAME）"
  htpasswd -c "$HTPASSWD" "$USER_NAME"
fi

log "写鉴权 + 反代片段（$PROXY_SNIPPET）"
# 定界符带着引号（<<'NGINX'）：里面的 $host / $remote_addr 是 **nginx 的变量**，
# 必须原样写进配置文件。不加引号的话 bash 会先把它们展开成空字符串 ——
# 这是这套脚本里最容易踩的一个坑（install.sh 里反过来踩过一次反引号）。
# 规则：大括号里**没有** nginx 变量的用不带引号的定界符（好展开 $DOMAIN 之类）；
# 含 nginx 变量的用带引号的，再把要填的值做成 __占位符__ 走 sed。
install -d /etc/nginx/snippets
cat > "$PROXY_SNIPPET" <<'NGINX'
# 全站要密码。**别删这几行** —— 后端本身没有任何鉴权，这层是唯一的门。
auth_basic "fupan";
auth_basic_user_file /etc/nginx/.htpasswd;

proxy_pass http://127.0.0.1:__BACKEND_PORT__;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

# 一次采集要跑几分钟（板块那步是逐个拉 400 多个板块，实测约 8 分钟）。
# 默认的 60s 会让 nginx 提前断开，页面上显示成「采集失败」——
# 而后台其实还在采，这种「假失败」最难排查。
proxy_read_timeout 600s;
proxy_send_timeout 600s;
NGINX

# 后端端口走 __占位符__ 而不是 $BACKEND_PORT：这段定界符带引号（里面有 nginx
# 变量必须原样保留），bash 不会展开任何 $，直接写变量名会原样留在配置里。
sed -i "s|__BACKEND_PORT__|$BACKEND_PORT|g" "$PROXY_SNIPPET"

if [[ -n "$DOMAIN" ]]; then
  # ---- 有域名：先拿证书，再写正式配置 ----
  CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
  install -d "$WEBROOT"

  if [[ ! -f "$CERT" ]]; then
    # 还没证书时 80 上**不能**跳 https（一跳就成了「连接被拒」，ACME 校验也过不去），
    # 所以先放一版只服务校验的临时配置，几秒后就被下面的正式配置覆盖。
    log "写临时站点（只为过 ACME 校验）"
    cat > "$SITE" <<NGINX
server {
    listen 80 default_server;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 503; }
}
NGINX
    nginx -t
    systemctl reload nginx

    log "申请证书（Let's Encrypt）"
    certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" \
      --non-interactive --agree-tos --email "$CERT_EMAIL" --keep-until-expiring

    # 用 webroot 模式时 certbot **不会**碰 nginx 配置，续期只是换掉文件。
    # 少了这个钩子，到期那天 nginx 内存里还是旧证书、浏览器照样报错，
    # 而日志里「续期成功」看着一切正常 —— 属于最难发现的那类故障。
    log "装续期钩子（续期后 reload nginx）"
    install -d /etc/letsencrypt/renewal-hooks/deploy
    cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'HOOK'
#!/usr/bin/env bash
# certbot 的 systemd timer 续完证书后执行，让 nginx 加载新证书
systemctl reload nginx
HOOK
    chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  else
    log "证书已存在，跳过申请：$CERT"
  fi

  log "写正式站点配置（80 跳转 + 443）"
  cat > "$SITE" <<'NGINX'
# 80：只做两件事 —— 给 ACME 校验放行、其余全部 301 到 https
server {
    listen 80 default_server;
    server_name __DOMAIN__;

    location /.well-known/acme-challenge/ { root __WEBROOT__; }
    location / { return 301 https://$host$request_uri; }
}

# 443：正式入口
server {
    listen 443 ssl;
    server_name __DOMAIN__;

    ssl_certificate     /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/__DOMAIN__/privkey.pem;
    # 老 nginx 的默认值还允许 TLSv1 / 1.1，显式收窄到 1.2+
    ssl_protocols TLSv1.2 TLSv1.3;

    location / { include __SNIPPET__; }
}

# 8080：明文兜底入口（DNS 挂了 / 证书过期了还能进来修）。稳定后删掉这一段。
server {
    listen 8080 default_server;
    server_name _;

    location / { include __SNIPPET__; }
}
NGINX
  sed -i \
    -e "s|__DOMAIN__|$DOMAIN|g" \
    -e "s|__WEBROOT__|$WEBROOT|g" \
    -e "s|__SNIPPET__|$PROXY_SNIPPET|g" \
    "$SITE"
else
  log "写站点配置（只有 8080 明文）"
  cat > "$SITE" <<NGINX
server {
    # 用 8080 而不是 80：腾讯云**境内**机器没备案时 80 端口会被拦掉，
    # 而「纯 IP + 非 80 端口」不受备案限制。想用 80 就得先备案一个域名。
    listen 8080 default_server;
    server_name _;

    location / { include $PROXY_SNIPPET; }
}
NGINX
fi

log "启用站点（并摘掉 nginx 自带的默认页）"
ln -sf "$SITE" "/etc/nginx/sites-enabled/$SITE_NAME"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

log "完成"
if [[ -n "$DOMAIN" ]]; then
  echo "    https://$DOMAIN/"
  echo "    证书续期自检：sudo certbot renew --dry-run"
  echo "    （8080 仍开着做兜底，不需要了就把配置里那一段删掉）"
else
  echo "    确认腾讯云控制台的「防火墙」已放行 8080 后，浏览器打开："
  echo "    http://<服务器IP>:8080/"
fi
echo "    账号：$USER_NAME   密码：你刚设的那个"
