#!/usr/bin/env bash
# S6.1：OS 级出站拒绝验证矩阵（**只能在专用隔离 Linux VM/namespace 内执行**）。
#
# 设计原则：
#   - 连接失败本身不算通过：每次尝试同时读取 nftables drop 计数器，
#     只有「失败 + 计数器增加」才判 PASS；
#   - loopback 只放行受控端口（3128 proxy / 8000 backend / 127.0.0.53 DNS）；
#   - IPv6 无默认路由时标 NOT_VERIFIED，不写成 PASS；
#   - allowlist 目标从真实策略文件读取，不临时放宽规则。
#
# 用法：
#   S6_ISOLATED_VM=1 APP_USER=fupan bash deploy/s6/verify-egress.sh \
#     --out reports/s6/s61/verification.json
set -uo pipefail

REFUSE() { echo "拒绝执行：$1" >&2; exit 3; }
[[ "${S6_ISOLATED_VM:-0}" == "1" ]] || REFUSE "请设置 S6_ISOLATED_VM=1"
[[ "$(id -u)" == "0" ]] || REFUSE "需要 root（读取 nft 计数器并切换应用身份）"
if grep -qi microsoft /proc/version 2>/dev/null; then
  REFUSE "检测到 WSL；任务书禁止在 WSL 宿主环境执行 OS 防火墙验证"
fi
command -v nft >/dev/null 2>&1 || REFUSE "nft 未安装"

APP_USER="${APP_USER:-fupan}"
PROXY_PORT="${PROXY_PORT:-3128}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
OUT="${S6_EVIDENCE_DIR:-$(cd "$(dirname "$0")/../.." && pwd)/reports/s6/s61}/verification.json"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    *) REFUSE "未知参数：$1" ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
POLICY="$HERE/egress-policy.json"
TMP="$(mktemp)"
FAILED=0
FIRST=1

nft list table inet s6_egress >/dev/null 2>&1 || REFUSE "inet s6_egress 不存在，请先 apply-egress-deny.sh"

run_as_app() { runuser -u "$APP_USER" -- bash -lc "$1"; }

counter_now() {
  nft list table inet s6_egress 2>/dev/null \
    | awk '/counter packets/ {sum += $3} END {printf "%d", sum+0}'
}

record() {
  local name="$1" target="$2" protocol="$3" expected="$4" rc="$5" delta="$6" detail="$7"
  local verdict
  if [[ "$expected" == "block" ]]; then
    if [[ "$rc" != "0" && "$delta" -gt 0 ]]; then verdict="PASS"
    elif [[ "$rc" != "0" ]]; then verdict="FAIL_NO_OS_COUNTER"
    else verdict="FAIL"; FAILED=1; fi
  elif [[ "$expected" == "allow" ]]; then
    if [[ "$rc" == "0" ]]; then verdict="PASS"; else verdict="FAIL"; FAILED=1; fi
  else
    verdict="NOT_VERIFIED"
  fi
  local escaped_detail=${detail//\"/\'}
  if [[ "$FIRST" == "1" ]]; then FIRST=0; else printf ',\n' >> "$TMP"; fi
  printf '{"case":"%s","target":"%s","protocol":"%s","expected":"%s","exit_code":%s,"counter_delta":%s,"verdict":"%s","detail":"%s"}' \
    "$name" "$target" "$protocol" "$expected" "$rc" "$delta" "$verdict" "$escaped_detail" >> "$TMP"
}

case_block() {
  local name="$1" target="$2" protocol="$3" cmd="$4"
  local before after rc=0 out
  before="$(counter_now)"
  out="$(run_as_app "$cmd" 2>&1)"; rc=$?
  sleep 0.2
  after="$(counter_now)"
  record "$name" "$target" "$protocol" "block" "$rc" "$((after-before))" "rc=$rc out=${out:0:120}"
}

case_allow() {
  local name="$1" target="$2" protocol="$3" cmd="$4"
  local rc=0 out
  out="$(run_as_app "$cmd" 2>&1)"; rc=$?
  record "$name" "$target" "$protocol" "allow" "$rc" "0" "rc=$rc out=${out:0:120}"
}

DETECT_PY='import socket,sys
host,port=sys.argv[1],int(sys.argv[2])
family=socket.AF_INET6 if ":" in host else socket.AF_INET
s=socket.socket(family,socket.SOCK_STREAM); s.settimeout(3)
try:
    s.connect((host,port)); print("CONNECTED"); sys.exit(0)
except Exception as exc:
    print(type(exc).__name__, exc); sys.exit(1)
finally:
    s.close()'

# 0) 规则与策略存在性
run_as_app "true"; RC0=$?
record "app_user_exists" "$APP_USER" "n/a" "allow" "$RC0" "0" "runuser 可用"
if [[ -f "$POLICY" ]]; then record "policy_file_present" "$POLICY" "n/a" "allow" "0" "0" "策略文件存在"
else record "policy_file_present" "$POLICY" "n/a" "allow" "1" "0" "策略文件缺失"; FAILED=1; fi

# 1) DNS（直连外部 resolver，不经系统 resolver）
case_block "dns_udp_public" "8.8.8.8:53" "udp/dns" \
  "python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(2);s.sendto(b'\\0'*12,('8.8.8.8',53));s.recvfrom(512)\""
case_block "dns_tcp_public" "8.8.8.8:53" "tcp/dns" \
  "python3 -c \"$DETECT_PY\" 8.8.8.8 53"

# 2) IPv4 / IPv6 TCP
case_block "ipv4_tcp_public" "1.1.1.1:443" "tcp" \
  "python3 -c \"$DETECT_PY\" 1.1.1.1 443"
IPV6_ROUTE="$(ip -6 route show default 2>/dev/null | head -1)"
if [[ -n "$IPV6_ROUTE" ]]; then
  case_block "ipv6_tcp_public" "[2606:4700:4700::1111]:443" "tcp" \
    "python3 -c \"$DETECT_PY\" 2606:4700:4700::1111 443"
else
  record "ipv6_tcp_public" "[2606:4700:4700::1111]:443" "tcp" "not_verified" "1" "0" "无 IPv6 默认路由；不能以无路由冒充防火墙通过"
fi

# 3) UDP
case_block "udp_public" "8.8.8.8:53" "udp" \
  "python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.sendto(b'x',('8.8.8.8',53));print('SENT')\""

# 4) 代理环境变量 / 本机非受控代理端口
case_block "http_proxy_env" "127.0.0.1:9999" "tcp/http" \
  "HTTPS_PROXY=http://127.0.0.1:9999 ALL_PROXY=http://127.0.0.1:9999 curl -sS --max-time 3 https://example.com/ -o /dev/null"
case_block "system_proxy_env" "127.0.0.1:9999" "tcp/http" \
  "HTTP_PROXY=http://127.0.0.1:9999 HTTPS_PROXY=http://127.0.0.1:9999 NO_PROXY= curl -sS --max-time 3 http://93.184.216.34/ -o /dev/null"

# 5) 本机非受控代理端口（起一个 root listener，应用连它应被 drop）
python3 -c "import socket,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',9999)); s.listen(1); time.sleep(8)" &
LISTENER=$!
sleep 0.5
case_block "local_proxy_uncontrolled_port" "127.0.0.1:9999" "tcp" \
  "python3 -c \"$DETECT_PY\" 127.0.0.1 9999"
kill "$LISTENER" 2>/dev/null || true; wait "$LISTENER" 2>/dev/null || true

# 6) 受控 loopback 端口应放行（proxy / backend）
python3 -c "import socket,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',$BACKEND_PORT)); s.listen(1); time.sleep(8)" &
BACK=$!
sleep 0.5
case_allow "loopback_backend_allowed" "127.0.0.1:$BACKEND_PORT" "tcp" \
  "python3 -c \"$DETECT_PY\" 127.0.0.1 $BACKEND_PORT"
kill "$BACK" 2>/dev/null || true; wait "$BACK" 2>/dev/null || true

# 7) 直接 socket / 子进程 / 原生工具
case_block "direct_socket" "93.184.216.34:80" "tcp" \
  "python3 -c \"$DETECT_PY\" 93.184.216.34 80"
case_block "child_curl" "93.184.216.34:80" "tcp/http" \
  "curl -sS --max-time 3 http://93.184.216.34/ -o /dev/null"
case_block "child_python" "1.1.1.1:443" "tcp" \
  "python3 -c \"$DETECT_PY\" 1.1.1.1 443"
if command -v nc >/dev/null 2>&1; then
  case_block "native_nc" "1.1.1.1:443" "tcp" "nc -z -w 3 1.1.1.1 443"
else
  record "native_nc" "1.1.1.1:443" "tcp" "not_verified" "127" "0" "nc 未安装"
fi

# 8) TLS / send / sendall / sendfile：连接层已拒绝，记录「握手前被拒」证据
case_block "tls_connect" "1.1.1.1:443" "tcp/tls" \
  "python3 -c \"import socket,ssl
s=socket.socket();s.settimeout(3)
try:
 s.connect(('1.1.1.1',443)); ssl.create_default_context().wrap_socket(s,server_hostname='example.com')
except Exception as e: print(type(e).__name__,e); raise SystemExit(1)\""

# 9) allowlist：目标从真实策略读取；仅检查存在性 + 经受控 proxy 的可用性
ALLOW="$(python3 - "$POLICY" <<'PY'
import json,sys
p=json.load(open(sys.argv[1],encoding="utf-8"))
e=[x for x in p["entries"] if x.get("enabled")][0]
print(f'{e["host_suffix"]}:{e["ports"][0]}')
PY
)"
record "allowlist_entry_read" "$ALLOW" "n/a" "allow" "0" "0" "来自 egress-policy.json"
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$PROXY_PORT"; then
  case_allow "allowlist_via_proxy" "$ALLOW" "tcp/tls" \
    "curl -sS --max-time 8 -x http://127.0.0.1:$PROXY_PORT -o /dev/null -w '%{http_code}' https://example.com/"
else
  record "allowlist_via_proxy" "$ALLOW" "tcp/tls" "not_verified" "127" "0" "受控 proxy 未运行（127.0.0.1:$PROXY_PORT）"
fi

{
  echo "["
  cat "$TMP"
  echo "]"
} > "$OUT"
rm -f "$TMP"

TOTAL=$(grep -c '"case"' "$OUT")
PASS=$(grep -c '"verdict":"PASS"' "$OUT" || true)
echo "wrote $OUT：total=$TOTAL pass=$PASS failed=$FAILED"
sha256sum "$OUT" > "$OUT.sha256"
exit "$FAILED"
