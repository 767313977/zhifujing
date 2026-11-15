#!/usr/bin/env bash
# S6.8 guest-side OS egress acceptance (run as root INSIDE the dedicated VM).
#
#   --phase apply        snapshot -> apply -> idempotency -> controlled DNS ->
#                        extra native/subprocess cases -> verify-egress.sh
#                        (rules are left in place for the reboot test)
#   --phase post-reboot  check rules persisted -> verify again -> rollback
#
# Every blocked case requires BOTH a non-zero exit code AND an nftables counter
# increase. A connection failure without an OS counter is recorded as
# FAIL_NO_OS_COUNTER, never as PASS.
set -uo pipefail

PHASE=""
DEPLOY_DIR="/opt/s6/deploy/s6"
EVIDENCE_DIR="/opt/s6/evidence"
HOST_DNS="172.31.240.1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2 ;;
    --deploy-dir) DEPLOY_DIR="$2"; shift 2 ;;
    --evidence-dir) EVIDENCE_DIR="$2"; shift 2 ;;
    --host-dns) HOST_DNS="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REFUSE() { echo "REFUSE: $1" >&2; exit 3; }
[[ "$(id -u)" == "0" ]] || REFUSE "root required"
grep -qi microsoft /proc/version 2>/dev/null && REFUSE "WSL is forbidden"
command -v nft >/dev/null 2>&1 || REFUSE "nft missing"
[[ -n "$PHASE" ]] || REFUSE "--phase apply|post-reboot required"
mkdir -p "$EVIDENCE_DIR"

counter_now() {
  nft list table inet s6_egress 2>/dev/null \
    | awk '/counter packets/ {sum += $3} END {printf "%d", sum+0}'
}

table_hash() {
  nft list table inet s6_egress 2>/dev/null | sha256sum | awk '{print $1}'
}

CASE_FILE="$EVIDENCE_DIR/cases-$PHASE.jsonl"
: > "$CASE_FILE"

record() {
  python3 - "$CASE_FILE" "$1" "$2" "$3" "$4" "$5" "$6" "$7" <<'PY'
import json, sys
path, name, target, proto, expected, rc, delta, detail = sys.argv[1:9]
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({
        "case": name, "target": target, "protocol": proto, "expected": expected,
        "exit_code": int(rc), "counter_delta": int(delta), "detail": detail[:300],
    }, ensure_ascii=False) + "\n")
PY
}

run_as_app() { runuser -u fupan -- bash -lc "$1"; }

block_case() {
  local name="$1" target="$2" proto="$3" cmd="$4"
  local before after rc out
  before="$(counter_now)"
  out="$(run_as_app "$cmd" 2>&1)"; rc=$?
  sleep 0.2
  after="$(counter_now)"
  local delta=$((after-before))
  local verdict="PASS"
  if [[ "$rc" == "0" ]]; then verdict="FAIL"; elif [[ "$delta" -le 0 ]]; then verdict="FAIL_NO_OS_COUNTER"; fi
  record "$name" "$target" "$proto" "block" "$rc" "$delta" "$verdict rc=$rc out=${out:0:120}"
}

allow_case() {
  local name="$1" target="$2" proto="$3" cmd="$4"
  local rc out
  out="$(run_as_app "$cmd" 2>&1)"; rc=$?
  local verdict="PASS"; [[ "$rc" == "0" ]] || verdict="FAIL"
  record "$name" "$target" "$proto" "allow" "$rc" "0" "$verdict rc=$rc out=${out:0:120}"
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

if [[ "$PHASE" == "apply" ]]; then
  export S6_EVIDENCE_DIR="$EVIDENCE_DIR"
  S6_ISOLATED_VM=1 S6_NO_AUTO_ROLLBACK=1 bash "$DEPLOY_DIR/apply-egress-deny.sh"
  H1="$(table_hash)"
  S6_ISOLATED_VM=1 S6_NO_AUTO_ROLLBACK=1 bash "$DEPLOY_DIR/apply-egress-deny.sh"
  H2="$(table_hash)"
  if [[ "$H1" == "$H2" && -n "$H1" ]]; then
    record "N21_apply_idempotent" "inet s6_egress" "n/a" "allow" "0" "0" "PASS same hash $H1"
  else
    record "N21_apply_idempotent" "inet s6_egress" "n/a" "allow" "1" "0" "FAIL hash $H1 -> $H2"
  fi

  # N01/N02: controlled DNS target on the host gateway (OS drop must be proven
  # by counter delta; the host probe log must stay empty).
  block_case "N01_dns_udp_controlled" "$HOST_DNS:53" "udp/dns" \
    "python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(2);s.sendto(b'\\0'*12,('$HOST_DNS',53));s.recvfrom(512)\""
  block_case "N02_dns_tcp_controlled" "$HOST_DNS:53" "tcp/dns" \
    "python3 -c \"$DETECT_PY\" $HOST_DNS 53"

  # N06 IPv6 UDP (mark NOT_VERIFIED when there is no IPv6 path)
  IPV6_ROUTE="$(ip -6 route show default 2>/dev/null | head -1)"
  if [[ -n "$IPV6_ROUTE" ]]; then
    block_case "N06_ipv6_udp_public" "[2606:4700:4700::1111]:53" "udp" \
      "python3 -c \"import socket;s=socket.socket(socket.AF_INET6,socket.SOCK_DGRAM);s.settimeout(2);s.sendto(b'x',('2606:4700:4700::1111',53));print('SENT')\""
  else
    record "N06_ipv6_udp_public" "[2606:4700:4700::1111]:53" "udp" "not_verified" "1" "0" "no IPv6 default route"
  fi

  # N09 ALL_PROXY (SOCKS on an uncontrolled local port)
  block_case "N09_all_proxy" "127.0.0.1:9999" "tcp/socks" \
    "ALL_PROXY=socks5://127.0.0.1:9999 curl -sS --max-time 3 https://example.com/ -o /dev/null"

  # N12 C native socket
  cat > /tmp/s6_c_socket.c <<'EOF'
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>
int main(void) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(443);
    inet_pton(AF_INET, "1.1.1.1", &addr.sin_addr);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
        puts("CONNECTED"); close(fd); return 0;
    }
    perror("connect"); close(fd); return 1;
}
EOF
  if gcc -O2 -o /tmp/s6_c_socket /tmp/s6_c_socket.c 2>/dev/null; then
    block_case "N12_c_native_socket" "1.1.1.1:443" "tcp" "/tmp/s6_c_socket"
  else
    record "N12_c_native_socket" "1.1.1.1:443" "tcp" "not_verified" "127" "0" "gcc unavailable"
  fi

  # N14 socket sendfile / N15 connected send (connect is already blocked)
  block_case "N14_sendfile" "93.184.216.34:80" "tcp/sendfile" \
    "python3 -c \"import socket,os;s=socket.socket();s.settimeout(3);s.connect(('93.184.216.34',80));f=open('/etc/hostname','rb');os.sendfile(s.fileno(),f.fileno(),0,64)\""
  block_case "N15_connected_send" "93.184.216.34:80" "tcp/send" \
    "python3 -c \"import socket;s=socket.socket();s.settimeout(3);s.connect(('93.184.216.34',80));s.sendall(b'GET / HTTP/1.0\\r\\n\\r\\n')\""

  S6_ISOLATED_VM=1 APP_USER=fupan S6_EVIDENCE_DIR="$EVIDENCE_DIR" \
    bash "$DEPLOY_DIR/verify-egress.sh" --out "$EVIDENCE_DIR/verification-apply.json"
  VERIFY_RC=$?

  python3 - "$EVIDENCE_DIR/verification-apply.json" "$CASE_FILE" "$EVIDENCE_DIR/n01_n24_results.json" "$VERIFY_RC" <<'PY'
import json, sys
verify_path, case_path, out_path = sys.argv[1:4]
cases = json.load(open(verify_path, encoding="utf-8"))
by_name = {c["case"]: c for c in cases}
extra = {}
for line in open(case_path, encoding="utf-8"):
    item = json.loads(line)
    extra[item["case"]] = item

def verdict(name, fallback="NOT_RUN", source=None):
    if name in extra:
        text = extra[name]["detail"]
        return "PASS" if " PASS " in f" {text} " else ("NOT_VERIFIED" if "not_verified" in text else "FAIL")
    if source and source in by_name:
        return by_name[source]["verdict"]
    return fallback

mapping = [
    ("N01", "N01_dns_udp_controlled"), ("N02", "N02_dns_tcp_controlled"),
    ("N03", None, "ipv4_tcp_public"), ("N04", None, "udp_public"),
    ("N05", None, "ipv6_tcp_public"), ("N06", "N06_ipv6_udp_public"),
    ("N07", None, "http_proxy_env"), ("N08", None, "system_proxy_env"),
    ("N09", "N09_all_proxy"), ("N10", None, "local_proxy_uncontrolled_port"),
    ("N11", None, "direct_socket"), ("N12", "N12_c_native_socket"),
    ("N13", None, "tls_connect"), ("N14", "N14_sendfile"),
    ("N15", "N15_connected_send"), ("N16", None, "child_curl"),
    ("N17", None, "child_python"), ("N18", None, "native_nc"),
    ("N19", None, "loopback_backend_allowed"), ("N20", None, None),
    ("N21", "N21_apply_idempotent"), ("N22", None, None),
    ("N23", None, None), ("N24", None, None),
]
results = []
for item in mapping:
    nid, extra_name = item[0], item[1]
    source = item[2] if len(item) > 2 else None
    if nid == "N24":
        blocked = [c for c in cases if c.get("expected") == "block"]
        ok = bool(blocked) and all(c.get("counter_delta", 0) > 0 and c.get("verdict") == "PASS" for c in blocked)
        results.append({"id": "N24", "verdict": "PASS" if ok else "FAIL", "source": "all block cases"})
        continue
    if nid in ("N22", "N23"):
        results.append({"id": nid, "verdict": "NOT_RUN", "source": "post-reboot phase"})
        continue
    if nid == "N20":
        results.append({"id": "N20", "verdict": "NOT_RUN", "source": "policy-corruption test deferred to snapshot"})
        continue
    results.append({"id": nid, "verdict": verdict(extra_name, source=source), "source": extra_name or source})

payload = {"phase": "apply", "verification": verify_path, "verify_exit_code": None, "results": results}
try:
    payload["verify_exit_code"] = int(sys.argv[4]) if len(sys.argv) > 4 else None
except Exception:
    pass
json.dump(payload, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("wrote", out_path)
PY
  echo "phase apply complete (verify rc=$VERIFY_RC)"
  exit 0
fi

if [[ "$PHASE" == "post-reboot" ]]; then
  PERSIST="FAIL"
  TABLE_HASH=""
  if nft list table inet s6_egress >/dev/null 2>&1; then
    PERSIST="PASS"
    TABLE_HASH="$(table_hash)"
  fi
  S6_ISOLATED_VM=1 APP_USER=fupan S6_EVIDENCE_DIR="$EVIDENCE_DIR" \
    bash "$DEPLOY_DIR/verify-egress.sh" --out "$EVIDENCE_DIR/verification-post-reboot.json"
  S6_ISOLATED_VM=1 bash "$DEPLOY_DIR/rollback-egress.sh" >/dev/null 2>&1
  if nft list table inet s6_egress >/dev/null 2>&1; then ROLLBACK="FAIL"; else ROLLBACK="PASS"; fi

  python3 - "$EVIDENCE_DIR" "$PERSIST" "$ROLLBACK" "$TABLE_HASH" <<'PY'
import json, sys
evidence, persist, rollback, table_hash = sys.argv[1:5]
payload = {
    "phase": "post-reboot",
    "N23_persist_after_reboot": persist,
    "N22_rollback_exact": rollback,
    "table_hash_after_reboot": table_hash or None,
    "verification": f"{evidence}/verification-post-reboot.json",
}
json.dump(payload, open(f"{evidence}/n01_n24_post_reboot.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(json.dumps(payload, ensure_ascii=False))
PY
  exit 0
fi

REFUSE "unknown phase: $PHASE"
