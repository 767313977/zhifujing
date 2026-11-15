# S6.8 VM 网络拓扑

- 状态：**NOT_APPLICABLE / S61_VM_UNAVAILABLE**
- 未创建 VM，因此没有 VM 网卡、vSwitch、NAT 或主机-only 网络可记录。

## 宿主网络（只读，未修改）

- 本轮未改动 Windows 防火墙、代理、DNS、路由或网卡；
- 未在 WSL2 内执行 nftables/iptables；
- 所有 E2E/Staging 服务仅监听 `127.0.0.1`（8021 / 8012 / 8030）。

## VM 就绪后的目标拓扑（未实施）

```text
Windows 宿主
  └── Hyper-V 独立 vSwitch（Internal / 不桥接生产网段）
        └── Ubuntu LTS VM
              ├── eth0: 仅 VM 内部网段
              ├── 专用低权限用户 fupan 运行服务
              ├── nftables inet s6_egress：默认 drop
              ├── 只放行受控 loopback 端口（127.0.0.1:3128/8000、::1 同）
              └── 禁用共享剪贴板/拖放/宿主目录挂载
```

## 禁止项（本轮遵守）

- 未在宿主/WSL 应用任何防火墙规则；
- 未把应用层 wrapper 的 `blocked=true` 当成 OS 证据；
- 未将 `NOT_VERIFIED` 改为 PASS。
