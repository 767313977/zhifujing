#!/usr/bin/env bash
# S6.1：回滚 OS 级出站默认拒绝。
# 只删除本方案创建的独立表 `inet s6_egress`，不触碰宿主/其它表。
# 即使在紧急情况下也应可执行，因此不要求 S6_ISOLATED_VM；但仍拒绝 WSL 与 root 之外的身份。
set -euo pipefail

[[ "$(id -u)" == "0" ]] || { echo "拒绝执行：需要 root" >&2; exit 3; }
if grep -qi microsoft /proc/version 2>/dev/null; then
  echo "拒绝执行：检测到 WSL（任务书禁止在 WSL 宿主环境修改防火墙）" >&2
  exit 3
fi
command -v nft >/dev/null 2>&1 || { echo "拒绝执行：nft 未安装" >&2; exit 3; }

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${S6_EVIDENCE_DIR:-$HERE/../../reports/s6/s61}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if nft list table inet s6_egress >/dev/null 2>&1; then
  nft delete table inet s6_egress
  echo "已删除 inet s6_egress"
else
  echo "inet s6_egress 不存在（可能已回滚）"
fi

bash "$HERE/snapshot-egress-state.sh" --out "$OUT_DIR" --suffix "rollback-$STAMP"

echo "回滚后请确认："
echo "  1) 应用 uid 恢复为主机既有策略；"
echo "  2) before/after 路由、DNS、iptables 快照一致；"
echo "  3) 宿主机防火墙未被本脚本修改。"
