# CVM 首轮只读实测记录

> 状态：read-only preflight completed；Looper CLI blocked by runtime dependencies  
> 日期：2026-08-23  
> 边界：没有修改配置、安装软件、启动压力负载或验证收益。

## 1. 实测结论

- 目标是 Ubuntu 24.04.4 LTS、Linux 6.8.0-137-generic、x86_64、KVM，8 vCPU、单 NUMA 节点。
- 根文件系统是 `/dev/nvme0n1p3` 上的 ext4；WSL manifest 中三个固定 `sda` 路径在本机不存在。
- 原 20 项口径保持 `6×3 + IRQ + MTU`，实测 15 succeeded、5 unavailable、0 permission-denied、0 failed。
- 两个 `numa_balancing_scan_period_*` 路径在本机内核不存在；不以论文或旧内核文档数值填补。
- `perf stat -e cycles -- true` 成功，证明本机此次 root 会话可以使用该硬件事件；这不代表全部 PMU 事件都可用。
- `stress-ng`、`fio`、`iperf3` 不存在，因此 CPU、存储和网络压力闭环还不能按计划运行。
- Python 3.12.3 和 PyYAML 可用，但 `pydantic`、`typer` 不存在，因此当前 Looper CLI 不能在本机启动。

完整机器可读证据见 `.artifacts/system-opt/cvm-readonly-probe.json`。主机标识只保存 `/etc/machine-id` 的 SHA-256，不保存登录口令。

## 2. 事实与未验证项

已验证：环境指纹、根盘类型、20 个原选择器的存在性与读权限、工具路径、`perf cycles` 最小操作检查。

未验证：配置写入、持久化、冲突所有权、服务重启影响、snapshot/verify/rollback、压力稳定性、指标区分度、搜索收益和业务 SLO。

`/proc/sys/vm/swappiness` 对 root 显示可写只是一项权限事实，不等于允许修改，也不证明修改安全。

## 3. 下一步需要确认的映射

准备候选映射时应把三个存储项的选择器从 WSL 的 `sda` 改为本机根盘 `nvme0n1`。依据是 `findmnt /` 和 `lsblk` 都指向 `/dev/nvme0n1p3`；影响范围是三个 I/O 观测项，不影响其他 17 项。

仍未验证：多盘场景中是否应只观察根盘、云盘设备名在重启/换机后是否稳定、NVMe 虚拟设备暴露的各项语义是否与文档候选完全一致。因此在用户确认映射策略前，不把该映射写入 CVM manifest。

两个缺失的 NUMA 扫描参数也不自动替换。应先针对本机 6.8 内核枚举全量 NUMA 接口、核对官方文档，再由用户确认是否保持 unavailable 或选择新候选。
