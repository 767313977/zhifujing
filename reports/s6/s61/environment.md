# S6.1 环境与隔离

## 结论

`BLOCKED`：缺少专用隔离 Linux VM。任务书 §0.3 明确禁止在日常 Windows 开发机、
WSL 宿主环境或共享宿主机执行防火墙规则；本机只有 Windows 11 + WSL2，
因此**未执行任何 nft/iptables 变更**，G3/G4 保持 `NOT_VERIFIED`。

## 探测结果

| 项 | 值 |
| --- | --- |
| 宿主 | Windows 11 10.0.26340（HAN-LAPTOP） |
| WSL | Ubuntu-24.04，Version 2，State=Stopped |
| WSL 是否可作为隔离环境 | **否**：属 WSL 宿主环境；自动挂载 `/mnt/c`、`/mnt/d` |
| Docker | 未安装 |
| 专用 Linux VM | 不存在 |
| 宿主防火墙 | Domain/Private/Public 均 ON（仅读取） |
| 宿主路由 | 见 `host-firewall.before.txt`（仅读取） |

## 为什么不能在 WSL 执行

1. 任务书明确点名 WSL 宿主环境不可用；
2. `/mnt/c`、`/mnt/d` 暴露生产数据库与配置，违反「不挂载生产数据」；
3. WSL NAT 默认可达宿主 LAN，无法证明「到生产网段无路由」。

## 要求的隔离环境

见 `reports/s6/gate0/environment-isolation.md` §4。最小条件：

- 专用 Linux VM 或独立 network namespace；
- 不挂载生产库/密钥/配置；
- 无生产网段路由；
- 规则仅作用于该 VM/namespace；
- 有 VM 控制台/快照回滚通道；
- 已保存网络、防火墙、代理基线。
