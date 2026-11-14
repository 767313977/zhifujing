#!/usr/bin/env bash
# S6 OS 级出站拒绝验证脚本（**本轮未执行**，仅供隔离环境 / 审批后生产使用）
#
# 用法（必须在隔离机器上、以 root 执行）：
#   S6_ISOLATED_ENV=1 APP_USER=fupan ./deploy/s6/verify-egress.sh
#
# 它会做真实的负向测试，并把结果写成 JSON：
#   - 应用 uid 直接连公网 IP（TCP 80/443）→ 必须失败
#   - 应用 uid 解析外部域名 → 必须失败或只走指定 resolver
#   - 应用 uid 发 UDP/53 到 8.8.8.8 → 必须失败
#   - 应用 uid 用 curl 直连公网 → 必须失败（curl 存在时）
#   - 应用 uid 通过受控 proxy 访问 allowlist 域名 → 按策略决定（记录）
#   - IPv4 与 IPv6 各测一次
#
# ⚠️ 结论只对**执行本脚本的那台机器、那一刻的规则**有效；不得据此声称其它环境已隔离。

set -uo pipefail

if [[ "${S6_ISOLATED_ENV:-0}" != "1" ]]; then
  echo "拒绝执行：请设置 S6_ISOLATED_ENV=1，并在隔离环境中运行（不得在开发机执行）" >&2
  exit 2
fi

APP_USER="${APP_USER:-fupan}"
PROXY_PORT="${PROXY_PORT:-3128}"
OUT="${OUT:-/tmp/s6-egress-verification.json}"
FAILED=0

run_as_app() {
  # 以应用身份执行命令；返回退出码
  if command -v sudo >/dev/null 2>&1; then
    sudo -u "$APP_USER" bash -lc "$1"
  else
    runuser -u "$APP_USER" -- bash -lc "$1"
  fi
}

record() {
  local name="$1" expected="$2" rc="$3" detail="$4"
  local verdict="PASS"
  if [[ "$expected" == "block" && "$rc" == "0" ]]; then verdict="FAIL"; FAILED=1; fi
  if [[ "$expected" == "allow" && "$rc" != "0" ]]; then verdict="FAIL"; FAILED=1; fi
  if [[ "$FIRST" == "1" ]]; then FIRST=0; else printf ',\n' >> "$TMP"; fi
  printf '{"case":"%s","expected":"%s","exit_code":%s,"verdict":"%s","detail":"%s"}' \
    "$name" "$expected" "$rc" "$verdict" "$detail" >> "$TMP"
}

TMP="$(mktemp)"
FIRST=1
FAILED=0

run_as_app "timeout 5 bash -c '</dev/tcp/1.1.1.1/443'" >/dev/null 2>&1
record "tcp_ipv4_public" "block" "$?" "direct 1.1.1.1:443"

run_as_app "timeout 5 bash -c '</dev/tcp/[2606:4700:4700::1111]/443'" >/dev/null 2>&1
record "tcp_ipv6_public" "block" "$?" "direct [2606:4700:4700::1111]:443"

run_as_app "getent hosts example.com" >/dev/null 2>&1
record "dns_external" "block" "$?" "getent hosts example.com"

run_as_app "timeout 5 python3 -c \"import socket;socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(b'x',('8.8.8.8',53))\"" >/dev/null 2>&1
record "udp_public_dns" "block" "$?" "UDP 8.8.8.8:53"

if command -v curl >/dev/null 2>&1; then
  run_as_app "timeout 8 curl -sS -o /dev/null http://93.184.216.34/" >/dev/null 2>&1
  record "curl_ip_literal" "block" "$?" "curl http://93.184.216.34/"
else
  record "curl_ip_literal" "block" "127" "curl not installed"
fi

run_as_app "timeout 8 curl -sS -o /dev/null -x http://127.0.0.1:${PROXY_PORT} https://api.tushare.pro/" >/dev/null 2>&1
record "proxy_allowlisted_target" "allow" "$?" "via controlled proxy"

{
  echo "["
  cat "$TMP"
  echo "]"
} > "$OUT"
rm -f "$TMP"
echo "verification written to $OUT (failed=$FAILED)" >&2
exit "$FAILED"
