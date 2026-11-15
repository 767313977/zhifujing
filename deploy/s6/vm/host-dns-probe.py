"""S6.8 controlled DNS target for N01/N02.

绑定在专用 vSwitch 的宿主网关地址（默认 172.31.240.1:53），同时监听 UDP/TCP。
它只做两件事：
1. 记录「是否收到来自 Guest 的 DNS 查询」——收到说明 Guest 的默认拒绝失效；
2. 返回一个最小的 NOERROR/0 answer 响应，便于区分「策略放行」与「端口无人监听」。

用法（宿主，管理员）：
    python deploy/s6/vm/host-dns-probe.py --bind 172.31.240.1 --log <jsonl> --duration 90
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import threading
import time
from datetime import datetime
from pathlib import Path


def _log(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _response(query: bytes) -> bytes:
    if len(query) < 12:
        return query
    # header: id, flags=0x8180, qdcount copied, an/ns/ar=0
    header = query[:2] + struct.pack(">HHHH", 0x8180, struct.unpack(">H", query[4:6])[0], 0, 0, 0)
    return header + query[12:]


def udp_server(bind: str, port: int, log_path: Path, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind, port))
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        _log(
            log_path,
            {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "protocol": "udp",
                "client": f"{addr[0]}:{addr[1]}",
                "query_bytes": len(data),
            },
        )
        try:
            sock.sendto(_response(data), addr)
        except OSError:
            pass
    sock.close()


def tcp_server(bind: str, port: int, log_path: Path, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind, port))
    sock.listen(8)
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with conn:
            conn.settimeout(3)
            try:
                length_raw = conn.recv(2)
                if len(length_raw) != 2:
                    continue
                length = struct.unpack(">H", length_raw)[0]
                data = conn.recv(length)
                _log(
                    log_path,
                    {
                        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "protocol": "tcp",
                        "client": f"{addr[0]}:{addr[1]}",
                        "query_bytes": len(data),
                    },
                )
                response = _response(data)
                conn.sendall(struct.pack(">H", len(response)) + response)
            except OSError:
                continue
    sock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="172.31.240.1")
    parser.add_argument("--port", type=int, default=53)
    parser.add_argument("--log", required=True)
    parser.add_argument("--duration", type=int, default=90)
    args = parser.parse_args()

    log_path = Path(args.log).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    stop = threading.Event()
    threads = [
        threading.Thread(target=udp_server, args=(args.bind, args.port, log_path, stop), daemon=True),
        threading.Thread(target=tcp_server, args=(args.bind, args.port, log_path, stop), daemon=True),
    ]
    for thread in threads:
        thread.start()
    print(json.dumps({"status": "listening", "bind": args.bind, "port": args.port, "log": str(log_path)}))
    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        pass
    stop.set()
    for thread in threads:
        thread.join(timeout=2)
    lines = len(log_path.read_text(encoding="utf-8").splitlines()) if log_path.exists() else 0
    print(json.dumps({"status": "stopped", "queries_received": lines}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
