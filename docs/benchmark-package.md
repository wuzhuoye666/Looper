# Benchmark Package 开发者合同

Looper 通过配置驱动的 Benchmark Package 接入新套件。新增普通套件不应修改 API、调度器或 Worker；套件维护者提交 manifest、运行入口和 normalizer，平台只执行稳定协议。

## 包的最小组成

一个源码目录至少包含 `benchmark.yaml`。本地受信任开发包可以同时包含启动器和 normalizer；生产包应把它们放进固定 digest 的容器镜像。

| 配置区域 | 维护者必须说明的事实 |
| --- | --- |
| `metadata` | 稳定 ID、版本、许可证、不可变源码 commit 或 digest |
| `spec.parameters` | 候选参数类型、范围、默认值和条件 |
| `spec.workloads` | task/workload 身份、权重及套件原生元数据 |
| `spec.adapter` | Adapter 协议、执行模型、主指标、必过检查、命名输入、标准输出 |
| `spec.runtime` | 隔离方式、固定镜像、生命周期命令、超时和允许退出码 |
| `spec.metrics` | 指标名称、单位、方向、类型和最低样本量 |
| `spec.outputs` | 证据上限以及必须保留的原始 artifact；套件原生结构化输出使用 `raw-result` |
| `spec.scenario` | 采购问题、角色拓扑、主指标、正确性/SLO 门禁和负载策略 |

目录中的事实与页面描述分开管理。版本、源码、镜像、指标和运行行为以 manifest 为唯一事实源；负责人、中文说明和业务标签可以在 Looper 中维护，但每次修改保留事件记录。

## 稳定 Adapter 协议

`spec.adapter.protocol` 当前固定为 `looper-adapter/v1`。`executionModel` 用来描述套件形态，而不是选择后端专用代码：

- `batch-suite`：SPEC 类 task 集合、编译与批处理；
- `service-stack`：DCPerf 类服务、客户端、readiness 和负载发生器；
- `database`、`storage`、`network`、`distributed`、`accelerator`；
- `custom`：仍遵守相同输入输出协议，由容器内部自行编排。

非场景型套件用 `primaryMetric` 指向 `spec.metrics` 中有方向的主指标，并用 `requiredChecks` 列出 `result.json` 中必须通过的检查 ID。场景型套件还可以用 `spec.scenario.slo_gates` 声明正确性、安全和 SLO 硬门禁。这样 SPEC 类批处理套件不需要伪造服务拓扑，DCPerf 类服务套件仍可表达完整场景约束。

`inputs` 声明套件需要的命名资源。支持 dataset、artifact、config、endpoint、secret、device 和 topology。需要文件挂载时使用 `/looper/input/...` 下的绝对路径；需要可追溯内容时启用 `digestRequired`。配置只声明 secret，不保存 secret 明文。

所有 Adapter 最终必须生成：

- `metrics.jsonl`：每行一个 `looper MetricObservation v1alpha1`；
- `result.json`：运行状态以及 correctness、SLO、安全和统计检查；
- manifest 声明的原始 artifact。

原始套件可以继续输出任意 CSV、JSON、日志、报告或直方图。`normalize` 阶段负责转换；Looper 不解析套件私有格式。
其中套件原生 JSON/CSV 结果使用 `raw-result`，标准化后的 Looper 结果使用 `result`。旧包原有的 `result`、`dataset` 角色继续兼容，不自动改写。

## 生命周期

Worker 按以下顺序执行存在的阶段：

1. `prepare`：数据、编译或服务准备；
2. `warmup`：按实验合同重复预热；
3. `run`：运行原始套件；
4. `normalize`：转换为标准结果；
5. `validate`：补充套件原生验证；
6. `collect`：整理额外证据；
7. `cleanup`：尽力清理，平台仍会强制回收进程或容器。

命令必须使用参数数组，不能提交 Shell 字符串。可使用 `{envelope}`、`{input}`、`{output}`、`{workspace}` 和 `{benchmarkRoot}` 占位符；`{python}` 只允许受信任的本地开发包使用。

生产容器默认无网络、只读根文件系统、移除 capabilities、禁止提权，并且镜像必须固定到 `@sha256`。远程导入不能获得本地进程信任。

## DCPerf 与 SPEC 类套件如何配置

DCPerf 集成包选择 `service-stack`。workload metadata 保存角色和服务配置，Adapter 在容器内部完成部署、readiness、负载发生与原生结果收集，再输出 goodput、尾延迟、错误率和资源证据。

SPEC 类集成包选择 `batch-suite`。workload 对应 task 或 task group，参数声明编译/运行配置、copies 和线程，Adapter 调用套件官方工具并保留原始报告，再按 task 输出观测。套件原生聚合分数作为有来源的结果保留，审计层仍使用 task 级结果计算 leverage 和排序稳定性。

两者不共享套件私有字段，只共享 Run Envelope、生命周期、标准观测和证据边界。

## 导入与验证流程

1. 在“注册 Benchmark”页面选择 UTF-8 YAML 或 JSON 配置；
2. Looper 先执行 JSON Schema 校验，再从文件读取身份、运行合同和指标；
3. 维护者补充正确性说明、Base/Reference 和跨环境审计声明；
4. 保存后查看每条服务端约束和依据；
5. Stage 0 配置只能进入目录，不能运行；
6. 可执行配置必须使用固定 digest 容器、`looper-adapter/v1` 和 `normalize` 阶段；
7. 先运行 fixture/冒烟，再进入兼容性矩阵和正式审计。

仓库中的 `benchmarks/config-driven-fixture` 是合同测试样例，不是性能 Benchmark，也不能作为选型证据。它证明 suite-owned producer 与 normalizer 可以仅通过配置被通用 Worker 执行。该 fixture 故意使用本地进程且不声明生产源码 revision，因此从注册页导入时会显示生产门禁失败；程序员应以页面逐项约束为清单，把正式包改为固定 digest 容器并补齐不可变来源。
