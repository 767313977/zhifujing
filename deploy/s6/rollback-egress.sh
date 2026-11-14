#!/usr/bin/env bash
# S6 回滚 OS 级出站拒绝（**本轮未执行**）
#
# 只删除本方案创建的独立表 `inet s6_egress`，不改动主机既有规则；
# 因此回滚不会「短暂开放全部公网」。
set -euo pipefail

if [[ "${S6_ISOLATED_ENV:-0}" != "1" ]]; then
  echo "拒绝执行：请设置 S6_ISOLATED_ENV=1" >&2
  exit 2
fi

if nft list table inet s6_egress >/dev/null 2>&1; then
  nft delete table inet s6_egress
  echo "已删除 inet s6_egress"
else
  echo "inet s6_egress 不存在（可能已回滚）"
fi

# 回滚后必须重新验证：应用 uid 应恢复为「按主机既有策略」，但本方案的默认拒绝已撤销。
echo "请记录回滚时间，并运行 verify-egress.sh 说明当前实际状态（预期不再是全阻断）。"
