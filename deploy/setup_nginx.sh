#!/usr/bin/env bash
# 给云端这台机器配对外入口：nginx 反向代理 + HTTPS（可选 Basic Auth）。
#
# ## 为什么需要它
#
# 后端绑死 `127.0.0.1:8000`（systemd unit 就是这么写的、只对内），要能从公网访问
# 就得有个反向代理；顺带由 nginx 处理 TLS（80 上的 ACME 校验、80→443 跳转）。
#
# ⚠️ **鉴权不在这一层**：2026-09-28 起后端自己有一套登录体系（设计见文档 §8.69），
# `/api/*` 全部要求登录，只放行 `/api/auth/login`、`/api/auth/register`、`/api/health`；
# 注册要一次性邀请码。所以别把「这里必须有密码」当成这套东西的前提 ——
# `AUTH=off`（现在的默认值）是正常的，反代本身照旧。
#
# ## 用法（在服务器上）
#
#     sudo bash deploy/setup_nginx.sh                     # 只有 8080（纯 IP 访问时用）
#     sudo DOMAIN=a.com CERT_EMAIL=me@x.com bash deploy/setup_nginx.sh   # 再加 80 / 443 + HTTPS
#
# 想把 www 一起签进**同一张**证书（两个域名都能用 https、都不报警告）：
#
#     sudo DOMAIN=a.com ALT_DOMAINS="www.a.com" CERT_EMAIL=me@x.com bash deploy/setup_nginx.sh
#
# 几个可选开关（默认值就是当前线上在用的那套）：
#
#     AUTH=on|off          nginx 这层的 Basic Auth 密码，**默认 off**
#     FALLBACK_8080=on|off 额外留一个 8080 明文兜底入口，**默认 off**
#
# ⚠️ 两个 off 都是 2026-09-28 定的，因为**站点自己有了登录体系**（设计见文档 §8.69）：
# `/api/*` 全部要求登录、只放行 login / register / health，邀请码是注册的唯一门。
# 所以 nginx 那层密码不再是必需的；而 8080 在明文 http 上**登录不了**
# （cookie 带 `Secure`），留着只让人困惑。
# 想看当前线上到底是什么样：`sudo nginx -T | grep -n -e listen -e auth_basic`。

# 跑之前先在腾讯云控制台的「防火墙」里放行端口：8080；开 HTTPS 还要 80 和 443。
# 用默认值（AUTH=off + FALLBACK_8080=off）时，防火墙里只需要 80 和 443。
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
# 可选的额外域名（空格分隔），和主域名一起进 server_name、一起签进同一张证书。
#
# 为什么**要显式给**、而不是自动带上 www：ACME 的 http-01 校验要求**每个域名都能
# 解析到本机**，自动塞一个没配解析的 `www.<主域名>` 会让整次签发失败 —— 连主域名
# 的证书都拿不到。所以默认只签主域名，需要时自己列出来。
ALT_DOMAINS="${ALT_DOMAINS:-}"
# nginx 这一层要不要 Basic Auth 密码：on / off。
#
# ⚠️ **2026-09-28 起后端自己有了登录体系**（设计见文档 §8.69）：`/api/*` 全部要求
# 登录，只放行 `/api/auth/login`、`/api/auth/register`、`/api/health`。也就是说
# 这一层**不再是「唯一的门」**，只是可选的一道额外面。
# 用户 2026-09-28 明确要求取消密码 → 默认改成 **off**（否则重跑一次脚本就会
# 静悄悄地又把密码加回来，那种「多弹一个框」的困惑很难追）。
# 想开：`AUTH=on` 重跑一次（.htpasswd 文件还在，密码不变）。
AUTH="${AUTH:-off}"
# 有域名时，要不要**额外**留一个 8080 明文兜底入口：on / off。
#
# **默认 off**（2026-09-28 起，见设计文档 §8.69.7）：站点开始要登录密码了，而会话
# cookie 带着 `Secure` —— 浏览器在明文 http 上根本不发它，于是 8080 只会变成
# 「页面打得开、但登录不了」的困惑入口，还平白留一条明文链路。
# 应急（证书过期 / DNS 挂了）才开；不带 DOMAIN 的那套纯 IP 模式不受这个开关影响
# （那边 8080 就是唯一的入口）。
FALLBACK_8080="${FALLBACK_8080:-off}"
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
# 要签进同一张证书的所有域名。**主域名必须排第一** —— certbot 拿第一个 -d 的名字
# 作为 /etc/letsencrypt/live/<名字>/ 的目录名，排在后面的话证书路径会跟着变，
# 下面写死的 `live/$DOMAIN/` 就对不上了。
SERVER_NAMES="$DOMAIN${ALT_DOMAINS:+ $ALT_DOMAINS}"
# 用 read -ra 拆而不是直接 `for x in $ALT_DOMAINS`：后者会做**通配符展开**，
# 万一值里带 * 会先去匹配当前目录的文件名，再轮到下面那条正则校验。
read -ra ALL_DOMAINS <<< "$SERVER_NAMES" || true
# 只放行域名本身的字符。带了 http:// 、端口或路径进来会写出一份坏配置，
# 而 nginx -t 的报错完全看不出是这里的问题（它只会说 server_name 不合法）
for _name in "${ALL_DOMAINS[@]}"; do
  if [[ ! "$_name" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "域名只能是域名本身（不要带 http://、端口或路径）：$_name" >&2
    exit 1
  fi
done
# 端口会同时进 sed 的替换串和 nginx 配置：非数字、或带 | 的值会写出一份坏配置，
# 而 nginx -t 同样指不到这里。10# 是为了让 08000 这类前导 0 不按八进制解析。
if [[ ! "$BACKEND_PORT" =~ ^[0-9]+$ ]] || (( 10#$BACKEND_PORT < 1 || 10#$BACKEND_PORT > 65535 )); then
  echo "BACKEND_PORT 要是 1-65535 的整数（当前：$BACKEND_PORT）" >&2
  exit 1
fi
# AUTH 只认 on / off。校验不是洁癖：写成 `AUTH=Off`/`AUTH=no` 这类不报错的话，
# 会被静默当成 off —— 也就是**你以为开着密码，其实全站裸奔**。
if [[ "$AUTH" != "on" && "$AUTH" != "off" ]]; then
  echo "AUTH 只能是 on（要密码）或 off（不要密码）（当前：$AUTH）" >&2
  exit 1
fi
if [[ "$FALLBACK_8080" != "on" && "$FALLBACK_8080" != "off" ]]; then
  echo "FALLBACK_8080 只能是 on 或 off（当前：$FALLBACK_8080）" >&2
  exit 1
fi

log "安装 nginx 与 htpasswd"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx apache2-utils >/dev/null
if [[ -n "$DOMAIN" ]]; then
  apt-get install -y -qq certbot >/dev/null
fi

if [[ "$AUTH" == "on" ]]; then
  if [[ -f "$HTPASSWD" ]]; then
    log "密码文件已存在，跳过。要改密码：sudo htpasswd $HTPASSWD $USER_NAME"
  else
    log "设置网页访问的账号密码（账号：$USER_NAME）"
    htpasswd -c "$HTPASSWD" "$USER_NAME"
  fi
else
  # 不跳过这一步的话，下面 htpasswd -c 会**交互式**要密码，脚本就卡在那里了。
  log "AUTH=off：不设账号密码，入口不再要密码"
fi

log "写鉴权 + 反代片段（$PROXY_SNIPPET）"
# 定界符带着引号（<<'NGINX'）：里面的 $host / $remote_addr 是 **nginx 的变量**，
# 必须原样写进配置文件。不加引号的话 bash 会先把它们展开成空字符串 ——
# 这是这套脚本里最容易踩的一个坑（install.sh 里反过来踩过一次反引号）。
# 规则：大括号里**没有** nginx 变量的用不带引号的定界符（好展开 $DOMAIN 之类）；
# 含 nginx 变量的用带引号的，再把要填的值做成 __占位符__ 走 sed。
install -d /etc/nginx/snippets
if [[ "$AUTH" == "on" ]]; then
  cat > "$PROXY_SNIPPET" <<'NGINX'
# 全站要密码。**别删这几行** —— 后端本身没有任何鉴权，这层是唯一的门。
auth_basic "fupan";
auth_basic_user_file /etc/nginx/.htpasswd;
NGINX
else
  # 把「这里没有鉴权」写进配置本体（而不是只改脚本）：以后在服务器上翻到这份
  # 配置时，能立刻看出是**故意的**，不会当成哪次改漏了。
  cat > "$PROXY_SNIPPET" <<'NGINX'
# ⚠️ 全站**没有鉴权**（AUTH=off，2026-09-28 起按用户要求取消密码）。后端本身
# 也没有任何鉴权，所以任何知道域名的人都能看自选股 / 复盘笔记，还能
# POST /api/admin/collect **触发采集** —— 那会白烧 iFinD 配额。
# 想加回来：重跑一次带 `AUTH=on` 的 setup_nginx.sh，或把下面两行补在这里：
#     auth_basic "fupan";
#     auth_basic_user_file /etc/nginx/.htpasswd;
NGINX
fi
cat >> "$PROXY_SNIPPET" <<'NGINX'
proxy_pass http://127.0.0.1:__BACKEND_PORT__;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
# ⚠️ 必须有这一行：后端靠**实际协议**决定会话 cookie 要不要带 `Secure`
# （见 app/api/auth.py 的 _set_cookie）。少了它，uvicorn 只看到 127.0.0.1 上的
# 明文 http，云端也会被判成 http —— cookie 少一个 Secure，静默降级。
# 后端认得它是因为 uvicorn 默认开了 proxy-headers，且信任来自 127.0.0.1 的代理。
proxy_set_header X-Forwarded-Proto $scheme;

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
    server_name $SERVER_NAMES;

    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 503; }
}
NGINX
    nginx -t
    systemctl reload nginx

    # 每个域名一个 -d，**顺序与 ALL_DOMAINS 一致**（第一个决定证书目录名）。
    CERT_ARGS=()
    for _name in "${ALL_DOMAINS[@]}"; do
      CERT_ARGS+=(-d "$_name")
    done
    log "申请证书（Let's Encrypt）：$SERVER_NAMES"
    certbot certonly --webroot -w "$WEBROOT" "${CERT_ARGS[@]}" \
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
    # ⚠️ 这里**不检查**现有证书覆盖了哪些域名。已经有 a.com 的证书、再带
    # ALT_DOMAINS="www.a.com" 重跑时，它会直接跳过申请 —— 而 443 那份配置已经
    # 把 www 写进 server_name 了，结果是 www 报「证书与域名不匹配」。
    # 要**换域名列表**就先删掉旧证书再来：sudo certbot delete --cert-name a.com
    log "证书已存在，跳过申请：$CERT"
  fi

  log "写正式站点配置（80 跳转 + 443）"
  cat > "$SITE" <<'NGINX'
# 80：只做两件事 —— 给 ACME 校验放行、其余全部 301 到 https
server {
    listen 80 default_server;
    server_name __SERVER_NAMES__;

    location /.well-known/acme-challenge/ { root __WEBROOT__; }
    location / { return 301 https://$host$request_uri; }
}

# 443：正式入口
server {
    # **开 HTTP/2**（2026-10-10 加）：一次页面加载要发十几个 `/api/*` 请求，而
    # HTTP/1.1 下浏览器对同一个域名最多只开 6 条连接、且不能多路复用 —— 弱网时请求
    # 排队 / 连接被重置，前端就报 `Failed to fetch`（用户 2026-10-10 报的正是这个）。
    # ⚠️ 写法随 nginx 版本：`listen ... http2` 是 1.24 及更早的写法，线上就是 1.24；
    # nginx ≥ 1.25.1 把它拆成了 `listen 443 ssl;` + `http2 on;`，升级后要改回来。
    listen 443 ssl http2;
    server_name __SERVER_NAMES__;

    ssl_certificate     /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/__DOMAIN__/privkey.pem;
    # 老 nginx 的默认值还允许 TLSv1 / 1.1，显式收窄到 1.2+
    ssl_protocols TLSv1.2 TLSv1.3;

    location / { include __SNIPPET__; }
}
NGINX

  # 8080 明文兜底入口，**默认不开**（2026-09-28 起，见设计文档 §8.69.7）：
  # 站点开始要登录密码了，而会话 cookie 带着 `Secure` —— 浏览器在明文 http 上
  # **根本不会发它**，于是 8080 变成「页面打得开、但登录不了」，只让人困惑，
  # 还平白留一条明文入口。真要应急（证书挂了/DNS 挂了）就临时开：
  #     sudo DOMAIN=... FALLBACK_8080=on bash deploy/setup_nginx.sh
  if [[ "$FALLBACK_8080" == "on" ]]; then
    log "保留 8080 明文兜底入口（FALLBACK_8080=on）"
    cat >> "$SITE" <<'NGINX'

# 8080：明文兜底入口（DNS 挂了 / 证书过期了还能进来修）。
# ⚠️ 在它上面**登录不了**（cookie 带 Secure，明文 http 不发）——
# 它的用途只是「能打开页面确认服务活着 / 看 nginx 报错」。
server {
    listen 8080 default_server;
    server_name _;

    location / { include __SNIPPET__; }
}
NGINX
  fi

  # sed 必须在**追加 8080 那段之后**跑：那段里也有 __SNIPPET__ 占位符
  sed -i \
    -e "s|__DOMAIN__|$DOMAIN|g" \
    -e "s|__SERVER_NAMES__|$SERVER_NAMES|g" \
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
  for _name in "${ALL_DOMAINS[@]}"; do
    echo "    https://$_name/"
  done
  echo "    证书续期自检：sudo certbot renew --dry-run"
  if [[ "$FALLBACK_8080" == "on" ]]; then
    echo "    （8080 明文兜底也开着：能打开页面，但在上面**登录不了**）"
  else
    echo "    （8080 明文入口已关闭；要应急开就加 FALLBACK_8080=on 重跑）"
  fi
else
  echo "    确认腾讯云控制台的「防火墙」已放行 8080 后，浏览器打开："
  echo "    http://<服务器IP>:8080/"
fi
if [[ "$AUTH" == "on" ]]; then
  echo "    账号：$USER_NAME   密码：你刚设的那个"
else
  echo "    nginx 这层没有密码（AUTH=off）—— 站点自己的登录在页面上，"
  echo "    没有邀请码注册不了账号（见设计文档 §8.69）。"
fi
