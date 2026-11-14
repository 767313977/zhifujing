#!/usr/bin/env bash
# S6 应用 OS 级出站拒绝（**本轮未执行**）
#
# 前置条件与风险见 egress-deny.nft 顶部注释。要点：
#   - 只允许在隔离 Staging 或经审批的生产变更窗口执行；
#   - 先在隔离环境跑 verify-egress.sh，确认全部 block 用例通过；
#   - 应用前自动备份当前 ruleset；回滚用 rollback-egress.sh。
set -euo pipefail

if [[ "${S6_ISOLATED_ENV:-0}" != "1" ]]; then
  echo "拒绝执行：请设置 S6_ISOLATED_ENV=1（禁止在日常开发机修改全局防火墙）" >&2
  exit 2
fi

APP_USER="${APP_USER:-fupan}"
POLICY="$(cd "$(dirname "$0")" && pwd)/egress-deny.nft"
BACKUP_DIR="${BACKUP_DIR:-/var/backups}"

command -v nft >/dev/null 2>&1 || { echo "nft 未安装" >&2; exit 3; }
id "$APP_USER" >/dev/null 2>&1 || { echo "用户 $APP_USER 不存在" >&2; exit 3; }

# 1) 备份当前规则
mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
nft list ruleset > "$BACKUP_DIR/nft-ruleset.$STAMP.before"
echo "已备份：$BACKUP_DIR/nft-ruleset.$STAMP.before"

# 2) 把规则里的占位 uid 换成真实用户（若与 fupan 不同）
TMP_RULES="$(mktemp)"
sed "s/\"fupan\"/\"$APP_USER\"/g" "$POLICY" > "$TMP_RULES"

# 3) 应用（独立表，删除即回滚）
nft -f "$TMP_RULES"
rm -f "$TMP_RULES"
echo "已应用 s6_egress 表；请立即运行 verify-egress.sh"

# 4) 规则不持久化：显式提示（生产需写入 /etc/nftables.conf 并由 systemd 加载）
echo "⚠️ 本脚本不写 /etc/nftables.conf：重启后会丢失。生产持久化需单独审批。"
