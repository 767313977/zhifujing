#!/usr/bin/env bash
# S6.1：应用 OS 级出站默认拒绝（**只能在专用隔离 Linux VM / 网络命名空间中执行**）。
#
# 安全守卫（任一不满足即拒绝执行）：
#   1. S6_ISOLATED_VM=1 显式声明；
#   2. root；
#   3. 不是 WSL（/proc/version 含 microsoft 或 systemd-detect-virt=wsl）；
#   4. 未挂载宿主盘 /mnt/c、/mnt/d（避免接触生产库/密钥/配置）；
#   5. nft 可用。
#
# 行为：
#   - 先调用 snapshot-egress-state.sh 保存 before 基线；
#   - 只创建/替换独立表 `inet s6_egress`，不 flush 其它表；
#   - 可选：用 systemd-run 安排 15 分钟自动回滚（除非 S6_NO_AUTO_ROLLBACK=1）；
#   - 重复执行幂等（先删本表再重建，仍不影响其它表）。
set -euo pipefail

REFUSE() { echo "拒绝执行：$1" >&2; exit 3; }

[[ "${S6_ISOLATED_VM:-0}" == "1" ]] || REFUSE "请设置 S6_ISOLATED_VM=1，并确认处于专用隔离 Linux VM/网络命名空间"
[[ "$(id -u)" == "0" ]] || REFUSE "需要 root"
if grep -qi microsoft /proc/version 2>/dev/null; then
  REFUSE "检测到 WSL；任务书禁止在 WSL 宿主环境执行防火墙规则"
fi
if command -v systemd-detect-virt >/dev/null 2>&1; then
  VIRT="$(systemd-detect-virt 2>/dev/null || true)"
  [[ "$VIRT" != "wsl" ]] || REFUSE "systemd-detect-virt=wsl；请使用专用 VM"
  [[ "$VIRT" != "none" ]] || REFUSE "systemd-detect-virt=none；看起来是物理机/宿主，请使用专用 VM"
fi
for host_mount in /mnt/c /mnt/d; do
  if mountpoint -q "$host_mount" 2>/dev/null; then
    REFUSE "$host_mount 已挂载宿主盘；请先 umount，避免接触生产数据"
  fi
done
command -v nft >/dev/null 2>&1 || REFUSE "nft 未安装"

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_USER="${APP_USER:-fupan}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups}"
OUT_DIR="${S6_EVIDENCE_DIR:-$HERE/../../reports/s6/s61}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

id "$APP_USER" >/dev/null 2>&1 || REFUSE "用户 $APP_USER 不存在"

# 1) 保存 before 基线（路由/DNS/防火墙/代理/namespace）
bash "$HERE/snapshot-egress-state.sh" --out "$OUT_DIR" --suffix "before-$STAMP"

# 2) 额外备份当前完整 ruleset（便于人工对照）
mkdir -p "$BACKUP_DIR"
nft list ruleset > "$BACKUP_DIR/nft-ruleset.$STAMP.before" 2>/dev/null || true

# 3) 幂等：只删除本方案的表（不 flush 全局）
if nft list table inet s6_egress >/dev/null 2>&1; then
  nft delete table inet s6_egress
fi

# 4) 生成规则（替换占位 uid）
TMP_RULES="$(mktemp)"
sed "s/\"fupan\"/\"$APP_USER\"/g" "$HERE/egress-deny.nft" > "$TMP_RULES"
nft -f "$TMP_RULES"
rm -f "$TMP_RULES"

# 5) 保存 after 基线
bash "$HERE/snapshot-egress-state.sh" --out "$OUT_DIR" --suffix "after-$STAMP"

# 6) 防锁死：15 分钟后自动回滚（可用 S6_NO_AUTO_ROLLBACK=1 关闭）
if [[ "${S6_NO_AUTO_ROLLBACK:-0}" != "1" ]] && command -v systemd-run >/dev/null 2>&1; then
  systemd-run --on-active=15min --unit="s6-egress-auto-rollback" \
    "$HERE/rollback-egress.sh" >/dev/null 2>&1 || \
    echo "警告：无法安排自动回滚，请保持 VM 控制台可用" >&2
fi

echo "已应用 inet s6_egress（APP_USER=$APP_USER, stamp=$STAMP）"
echo "基线目录：$OUT_DIR"
echo "下一步：S6_ISOLATED_VM=1 APP_USER=$APP_USER bash $HERE/verify-egress.sh"
