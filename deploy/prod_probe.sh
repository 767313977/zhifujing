#!/usr/bin/env bash
# 生产服务器只读信息采集（P1.1-03）。
#
# 只读：不写库、不重启服务、不改配置；输出中请把 token / 密码 / 内网地址打码后再回传。
# 用法（在你的服务器上，用有权限的账号）：
#     sudo bash deploy/prod_probe.sh > prod_probe.txt 2>&1
#
# 需要额外确认的 4 项：
#   1) 实际 Worker 数（看 ExecStart 里有没有 --workers）；
#   2) nginx proxy_read_timeout；
#   3) SQLite 所在文件系统的可用空间；
#   4) 一次真实盘后采集期间的 RSS 峰值（脚本只采集 Web 主进程，采集若在同进程内即为同一 PID）。
set -u
SERVICE="${SERVICE:-fupan}"

echo "== 时间 =="
date -Is
echo

echo "== 内存 =="
free -m
echo

echo "== CPU =="
nproc
echo

echo "== cgroup / systemd =="
systemctl show "$SERVICE" -p MainPID -p MemoryCurrent -p MemoryMax -p TasksCurrent
systemctl show "$SERVICE" -p ExecStart -p Restart -p FragmentPath
echo

echo "== 服务配置（会包含环境变量，回传前请打码）=="
systemctl cat "$SERVICE"
echo

echo "== 进程 =="
ps -eo pid,ppid,rss,etime,cmd --sort=-rss | head -25
echo

echo "== 磁盘 =="
df -h
echo

echo "== 数据库文件与所在盘 =="
DB_PATH="$(systemctl show "$SERVICE" -p ExecStart --value | grep -oE '/[^ "]+\.db' | head -1)"
if [ -n "${DB_PATH:-}" ]; then
  ls -lh "$DB_PATH" "$DB_PATH"-wal "$DB_PATH"-shm 2>/dev/null
  df -h "$(dirname "$DB_PATH")"
else
  echo "未从 ExecStart 解析到 .db 路径，请手工填写"
fi
echo

echo "== nginx 超时 =="
grep -rn "proxy_read_timeout\|proxy_send_timeout\|proxy_connect_timeout" /etc/nginx 2>/dev/null | head -20
echo

echo "== 最近 7 天 OOM / 重启 =="
journalctl -u "$SERVICE" --since "7 days ago" 2>/dev/null | grep -iE "killed|oom|memory|Started|Stopped" | tail -30
echo

echo "== 采集期 RSS 峰值（前台采样 120 秒；请在盘后采集期间运行）=="
PID="$(systemctl show "$SERVICE" -p MainPID --value)"
if [ -n "${PID:-}" ] && [ "$PID" != "0" ]; then
  for _ in $(seq 1 120); do ps -o rss= -p "$PID" 2>/dev/null; sleep 1; done | sort -n | tail -1 | awk '{printf "峰值 RSS: %.1f MB\n", $1/1024}'
else
  echo "服务未运行"
fi
echo

echo "== 说明 =="
echo "1) 本脚本不访问数据库；数据库覆盖审计请在【一致性备份副本】上运行："
echo "   python scripts/prod_readonly_audit.py --db /path/to/prod_copy.db --json audit.json"
echo "2) Schema 差异（同样是副本）："
echo "   python scripts/schema_inventory.py --db /path/to/prod_copy.db --compare /path/to/release.db --json schema.json"
echo "3) 回传前请删除/打码：token、密码、Environment 行、内网域名与主机名。"
