# WSL2 能力探测记录（2026-08-23）

> 状态：empirical environment record  
> 目标：本机 Ubuntu WSL2；只读探测  
> 边界：仅说明当前 WSL2 实例，不外推腾讯云 CVM、裸机或其他 Linux。WSL2 使用
> Microsoft 定制内核和不同的权限模型；文件名不能替代 JSON 内的环境指纹。

## 计数与命令口径

- 发行版计数：`wsl --list --verbose` 返回 3 个注册项；本次只探测当前默认 Ubuntu。
- 能力项计数：共检查 13 个事实/接口，不做去重。
- 所有命令均为读取、`test -r` 或 `test -w`；没有写配置。

## 实测结果

| 能力 | 结果 | 定性 |
|---|---|---|
| 内核 | 6.18.33.2-microsoft-standard-WSL2, x86_64 | WSL2 Linux guest |
| PSI CPU | `/proc/pressure/cpu` 可读 | 可用于 L1 软件观测 |
| PSI memory | `/proc/pressure/memory` 可读 | 可用于 L1 软件观测 |
| perf_event_paranoid | 2 | 仅是权限事实，不代表事件可用 |
| `perf stat -e cycles` | `No supported events found` | PMU 硬件事件不可用 |
| CPUFreq sysfs | 不存在 | governor 动态域为空，必须排除 |
| `sysctl` 命令 | 当前 `/usr/sbin/sysctl`，procps-ng 4.0.4 | 早期“未安装”记录已被本轮复核推翻；环境状态可能已变化 |
| vm.swappiness | 60，可读，当前用户不可写 | observation-only |
| kernel.numa_balancing | 路径不存在 | unavailable |
| net.core.somaxconn | 4096，可读 | 已观测，未验证可写 |
| THP enabled | `always [madvise] never`，当前用户不可写 | observation-only |
| sda scheduler | `[none] mq-deadline kyber` | 已观测，未验证可写 |
| WSL Python | 3.14.4；独立最小 venv 已安装 Pydantic/PyYAML | 已运行真实只读 inventory；未运行完整测试套件 |

## M1 20 项与工具缺口复核

- 20 项 observation manifest：严格按 `6 × 3 + IRQ + MTU` 计数，不去重；
  WSL2 实测 `14 succeeded + 6 unavailable`。6 个 unavailable 全部来自 CPUFreq
  与 NUMA 路径不存在，没有填补默认值。
- 工具需求按 8 条显式 requirement 计数：PATH 可解析 5 条（`python3`、`sysctl`、
  `perf`、`numactl`、`ethtool`），不可解析 3 条（`stress-ng`、`fio`、`iperf3`）。
- 工具 inventory 只证明 PATH 解析。`perf` 虽已安装，`perf stat -e cycles -- true`
  仍实测失败为 `No supported events found`，所以 PMU operability 仍不可用。
- 工具缺失不触发自动安装；选中某组件 workload 后，其工具会从 optional 提升为
  run-specific critical，缺失时该轮 preflight fail-closed。

机器证据：

- `.artifacts/system-opt/wsl2-m1-20-inventory.json`
- `.artifacts/system-opt/wsl2-tool-inventory.json`
- `.artifacts/system-opt/wsl2-raw-inventory.json`

## 对实现的影响

1. Linux 配置读取支持 `read-file`，不依赖 `sysctl` 二进制存在。
2. PMU/CPUFreq 是可选能力；缺失时保留 unavailable 证据，不阻断软件指标闭环。
3. WSL Profile 目前只能 observation-only。没有 root 写权限和动态域实测前，不生成可搜索配置。
4. WSL 适合验证 `/proc`、`/sys`、PSI、配置采集和 Linux 路径语义；不能作为生产性能区分度的最终裁判。
5. 两份 inventory JSON 使用 v1alpha2 schema，必须记录发行版、内核、架构、
   虚拟化类型和哈希化主机标识，并内嵌不可外推限制。

## B 级覆盖率缺口

- PMU 与 CPUFreq 未覆盖。
- WSL 只安装了运行只读采集所需的最小依赖；完整 CLI/E2E 仍在 Windows 开发宿主的
  simulated backend 验证。
- 这些缺口不造成已产出结果错误，但限制真实 Linux 功能覆盖；交付报告必须持续标注。
