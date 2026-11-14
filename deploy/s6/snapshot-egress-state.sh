#!/usr/bin/env bash
# S6.1：采集隔离环境网络基线（只读，不修改任何规则）。
#
# 用法（在专用隔离 Linux VM 内、以 root 执行）：
#   bash deploy/s6/snapshot-egress-state.sh --out reports/s6/s61
#   bash deploy/s6/snapshot-egress-state.sh --out reports/s6/s61 --suffix after-rollback
#
# 产物（每个文件都会写一份 .sha256）：
#   ip-address.txt / ipv4-routes.txt / ipv6-routes.txt / resolv.txt
#   proxy-env.redacted.txt / nft-ruleset.txt / iptables.txt / ip6tables.txt
#   netns.txt / virt.txt
set -uo pipefail

OUT_DIR=""
SUFFIX="before"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --suffix) SUFFIX="$2"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ -n "$OUT_DIR" ]] || { echo "必须指定 --out" >&2; exit 2; }
mkdir -p "$OUT_DIR"
PREFIX="$OUT_DIR/$SUFFIX"

# 只读采集；失败也要留下文件（避免「没有输出」被误读为「没有变化」）
ip -brief address > "$PREFIX-ip-address.txt" 2>&1 || true
ip route show table all > "$PREFIX-ipv4-routes.txt" 2>&1 || true
ip -6 route show table all > "$PREFIX-ipv6-routes.txt" 2>&1 || true
cat /etc/resolv.conf > "$PREFIX-resolv.txt" 2>&1 || true
env | grep -iE '(^|_)(http|https|all|no)_proxy=' \
  | sed -E 's#(https?://)[^/@]+:[^/@]+@#\1[REDACTED]@#g' \
  > "$PREFIX-proxy-env.redacted.txt" 2>&1 || true
readlink /proc/1/ns/net > "$PREFIX-netns-init.txt" 2>&1 || true
readlink /proc/self/ns/net > "$PREFIX-netns-self.txt" 2>&1 || true
if command -v systemd-detect-virt >/dev/null 2>&1; then
  systemd-detect-virt > "$PREFIX-virt.txt" 2>&1 || true
else
  echo "systemd-detect-virt=MISSING" > "$PREFIX-virt.txt"
fi
if command -v nft >/dev/null 2>&1; then
  nft list ruleset > "$PREFIX-nft-ruleset.txt" 2>&1 || true
else
  echo "nft=MISSING" > "$PREFIX-nft-ruleset.txt"
fi
if command -v iptables-save >/dev/null 2>&1; then
  iptables-save > "$PREFIX-iptables.txt" 2>&1 || true
else
  echo "iptables-save=MISSING" > "$PREFIX-iptables.txt"
fi
if command -v ip6tables-save >/dev/null 2>&1; then
  ip6tables-save > "$PREFIX-ip6tables.txt" 2>&1 || true
else
  echo "ip6tables-save=MISSING" > "$PREFIX-ip6tables.txt"
fi

# 每个快照文件计算 SHA256，便于 before/after 对比与报告引用
( cd "$OUT_DIR" && sha256sum "$(basename "$PREFIX")"*.txt > "$(basename "$PREFIX")-sha256.txt" )

echo "快照已写入 $PREFIX-*.txt"
