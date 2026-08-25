# VGO 性能波动选型套件

这个 Looper Benchmark 把现有的《Variability-Guided Performance Optimization》论文复现脚本接入“新建选型研究”。它不是合成占位测试：目标机上的每次执行都会调用复现包原有的 `scripts/run_case.sh`，运行 Matmul、7-Zip、LBM 或 SAD 的 profile、随机区块交替基线/优化和 rollback 阶段，并保留原始 CSV、全部阶段 metadata 和日志。

## 执行边界

- 目标机：Ubuntu 22.04、x86_64、至少 8 个逻辑 CPU 和约 16 GiB 内存。
- 权限：需要可无密码执行的 `sudo`。Matmul、7-Zip 和 LBM 接受原复现包的 `PARTIAL GO`，此时 profile 自动退化到软件 perf 事件并明确标记能力边界；SAD 因为需要修改并恢复 THP，仍必须通过包含硬件 perf 事件和 THP 可逆写入的 `FULL GO`。
- 准备：用户只需选择 VGO 和已连接的干净 Ubuntu 22.04 机器。Worker 自动下发套件，`prepare.py` 校验固定源码快照，然后调用原复现包的 `check_environment.sh`、`setup_ubuntu.sh`、`validate_machine.sh` 和 Matmul calibration；`perf`、Parboil、SHARP、p7zip 和 tcmalloc 均在这一步自动安装或构建。
- 测量：`producer.py` 只负责编排，实际 workload 由固定快照中的 `run_case.sh/run_case.py` 执行。
- 结果：`normalizer.py` 从原始 VGO CSV 计算基线/优化 CV、中位数、P95、改善比例、rollback 漂移、正确率和 CPU steal P95；`vgo-diagnostics.json` 还会汇总 CSV 中所有可用 perf、进程、内存、THP 和环境参数。

首次准备会下载约 353 MiB 的 Parboil 标准数据集并构建原生 workload；成功后以 dependency-lock digest 为键保存完整运行目录，后续实验直接复用。来源快照只对原 `setup_ubuntu.sh` 做了一项实机兼容修正：移除对不存在的 `benchmarks/matmul/*.sh` 通配路径执行 `chmod`，实际 workload 入口及测试逻辑未改动。

默认采用论文正式轮次的 10%，且整个诊断只执行 1 个 Looper attempt：Matmul 为 30 baseline + 20 profile + 30 mitigated + 5 rollback，其他 workload 为 50 + 20 + 50 + 5。baseline 与 mitigated 被拆成 5 个确定性随机、顺序平衡的区块交替运行；默认不设置轮间等待，只在 profile 和 rollback 前预热 1 次。主指标 `runtime_cv` 是区块基线 CV，同时展示优化组 CV 与改善比例。

套件声明并完整记录这些参数：`diagnostic_scale_percent`、`ab_blocks`、`warmups`、`per_run_timeout_seconds`、`inter_run_delay_milliseconds` 和 `order_seed`。实际派生轮数、每个区块顺序、执行命令及采用的 workload 专属优化策略都会写入 `vgo-native.json` 和最终结果扩展字段。

`vgo-source.tar.gz` 是从用户指定的论文复现目录提取的最小可执行快照。`source-lock.json` 固定压缩包和每个原始脚本/配置文件的 SHA-256，防止接入过程中悄悄替换测试实现。
