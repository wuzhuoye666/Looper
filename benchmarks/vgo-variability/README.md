# VGO 性能波动选型套件

这个 Looper Benchmark 把现有的《Variability-Guided Performance Optimization》论文复现脚本接入“新建选型研究”。它不是合成占位测试：目标机上的每次执行都会调用复现包原有的 `scripts/run_case.sh`，运行 Matmul、7-Zip、LBM 或 SAD 的 baseline 阶段，并保留原始 CSV、阶段 metadata 和日志。

## 执行边界

- 目标机：Ubuntu 22.04、x86_64、至少 8 个逻辑 CPU 和约 16 GiB 内存。
- 权限：需要可无密码执行的 `sudo`。Matmul、7-Zip 和 LBM baseline 接受原复现包的 `PARTIAL GO`；SAD 因为需要修改并恢复 THP，仍必须通过包含硬件 perf 事件和 THP 可逆写入的 `FULL GO`。
- 准备：用户只需选择 VGO 和已连接的干净 Ubuntu 22.04 机器。Worker 自动下发套件，`prepare.py` 校验固定源码快照，然后调用原复现包的 `check_environment.sh`、`setup_ubuntu.sh`、`validate_machine.sh` 和 Matmul calibration；`perf`、Parboil、SHARP、p7zip 和 tcmalloc 均在这一步自动安装或构建。
- 测量：`producer.py` 只负责编排，实际 workload 由固定快照中的 `run_case.sh/run_case.py` 执行。
- 结果：`normalizer.py` 从原始 VGO CSV 计算运行时间 CV、中位数、P95、正确率和 CPU steal P95，并生成 Looper 标准证据。

首次准备会下载约 353 MiB 的 Parboil 标准数据集并构建原生 workload；成功后以 dependency-lock digest 为键保存完整运行目录，后续实验直接复用。来源快照只对原 `setup_ubuntu.sh` 做了一项实机兼容修正：移除对不存在的 `benchmarks/matmul/*.sh` 通配路径执行 `chmod`，实际 workload 入口及测试逻辑未改动。

默认每个 Looper attempt 内执行 10 个 VGO 样本和 1 次预热；Looper 再按选型研究的重复数进行时间分块重复。主指标是 `runtime_cv`，越低表示同一 workload 的运行时间越稳定。

`vgo-source.tar.gz` 是从用户指定的论文复现目录提取的最小可执行快照。`source-lock.json` 固定压缩包和每个原始脚本/配置文件的 SHA-256，防止接入过程中悄悄替换测试实现。
