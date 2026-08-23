# Benchmark Package 开发者合同

Looper 通过配置驱动的 Benchmark Package 接入新套件。新增普通套件不应修改 API、调度器或 Worker；套件维护者提交 manifest、运行入口和 normalizer，平台只执行稳定协议。

## 包的最小组成

一个可执行接入包至少包含 `benchmark.yaml`、Adapter 和所需的自动部署脚本，并以 ZIP 导入。生产包优先把运行入口放进固定 digest 容器；需要直接测量新购宿主机时，可以使用经过最终登记确认的受信任 local-process 包与 managed provisioning。

| 配置区域 | 维护者必须说明的事实 |
| --- | --- |
| `metadata` | 稳定 ID、版本、许可证、不可变源码 commit 或 digest |
| `spec.parameters` | 候选参数类型、范围、默认值和条件 |
| `spec.workloads` | task/workload 身份、权重及套件原生元数据 |
| `spec.adapter` | Adapter 协议、执行模型、主指标、必过检查、命名输入、标准输出 |
| `spec.runtime` | 隔离方式、固定镜像、依赖锁、执行策略、生命周期命令、超时和允许退出码 |
| `spec.metrics` | 指标名称、单位、方向、类型和最低样本量 |
| `spec.outputs` | 证据上限以及必须保留的原始 artifact；套件原生结构化输出使用 `raw-result` |
| `spec.scenario` | 采购问题、角色拓扑、主指标、正确性/SLO 门禁和负载策略 |
| `spec.infrastructure` | 每类机器的角色、数量范围、最低硬件、放置关系和网络链路 |
| `spec.audit` | 默认重复次数、Reference 策略、跨环境轴和正式准入所需证据 |

目录中的事实与页面描述分开管理。版本、源码、镜像、指标和运行行为以 manifest 为唯一事实源；负责人、中文说明和业务标签可以在 Looper 中维护，但每次修改保留事件记录。

## 稳定 Adapter 协议

`spec.adapter.protocol` 当前固定为 `looper-adapter/v1`。`executionModel` 用来描述套件形态，而不是选择后端专用代码：

- `batch-suite`：SPEC 类 task 集合、编译与批处理；
- `service-stack`：DCPerf 类服务、客户端、readiness 和负载发生器；
- `database`、`storage`、`network`、`distributed`、`accelerator`；
- `custom`：仍遵守相同输入输出协议，由容器内部自行编排。

非场景型套件用 `primaryMetric` 指向 `spec.metrics` 中有方向的主指标，并用 `requiredChecks` 列出 `result.json` 中必须通过的检查 ID。场景型套件还可以用 `spec.scenario.slo_gates` 声明正确性、安全和 SLO 硬门禁。这样 SPEC 类批处理套件不需要伪造服务拓扑，DCPerf 类服务套件仍可表达完整场景约束。

`inputs` 声明套件需要的命名资源。支持 dataset、artifact、config、endpoint、secret、device 和 topology。需要文件挂载时使用 `/looper/input/...` 下的绝对路径；需要可追溯内容时启用 `digestRequired`。配置只声明 secret，不保存 secret 明文。

创建实验时使用 `input_bindings`/`inputBindings` 为这些声明绑定资源引用。调度器拒绝缺失、未知、类型不一致或缺少必需 SHA-256 的绑定；secret 必须使用 `secret://` 引用。绑定会进入 Run Envelope 的 `inputs`，但明文密钥不会进入信封。

所有 Adapter 最终必须生成：

- `metrics.jsonl`：每行一个 `looper MetricObservation v1alpha1`；
- `result.json`：运行状态以及 correctness、SLO、安全和统计检查；
- manifest 声明的原始 artifact。

## 多机与最低机型

`scenario.roles` 表达业务角色，`infrastructure.nodeGroups` 表达实际机器。多机套件不能只写一句 `topology: multi-node`：每个机器组必须给出 `minimum/default/maximum` 数量、计分归属、OS/架构、CPU/内存/加速器/存储/网络下限以及放置关系。

当前多机执行使用 `infrastructure.orchestration: adapter`：Looper 在 `primaryNodeGroup` 启动统一入口，Adapter 通过 required `topology` 输入获得其他节点引用并负责编排。`orchestration: looper` 是预留合同，在多角色调度器落地前只能登记 Stage 0，不能标为已经可执行。完整说明和模板见 `docs/benchmark-integration.md` 与 `docs/examples/benchmark-multi-node.yaml`。

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

命令必须使用参数数组，不能提交 Shell 字符串。可使用 `{envelope}`、`{input}`、`{output}`、`{workspace}`、`{benchmarkRoot}` 和版本级持久目录 `{cache}` 占位符；`{python}` 只允许受信任的本地进程包使用。

生产容器默认无网络、只读根文件系统、移除 capabilities、禁止提权，并且镜像必须固定到 `@sha256`。受信任 local-process ZIP 只有在操作者查看门禁并最终登记后才获得本地执行许可；仅上传 YAML/JSON 不会获得该许可。

干净目标机使用 `runtime.provisioning.mode: managed`：`hostCapabilities` 是部署前必须存在的 Worker/操作系统能力，`provides` 是 `prepare` 自动安装或解包的套件依赖，`cacheKey` 必须等于 `dependencyLockDigest`。Looper 选择机器时只检查前者，实验启动后下发完整 ZIP、运行 `prepare`，再运行 Benchmark。`prepare` 必须幂等、校验下载摘要，并对缺少网络、sudo 或包管理器给出可操作错误。

Executable 新注册还必须声明：

- `dependencyLockDigest` 以及运行时额外依赖的来源、SHA-256 和许可证；
- `executionPolicy.placement`：隔离容器或目标机 Agent，以及 CPU/NUMA 约束；
- `executionPolicy.network`：无网络或受限出口、允许主机和最大传输字节数；
- `executionPolicy.storage`：仅 workspace 或绑定的 required device 输入，并明确是否允许破坏性 I/O；
- `executionPolicy.environmentEvidence`：系统指纹版本和运行前必须可取得的字段。

策略不是描述性标签。Worker 只有在能力集合覆盖策略时才能 claim；当前本地 Worker 实际执行并声明的生产能力只有 `isolated-container + network.none + storage.workspace`。受限出口和目标设备必须等待具备策略执行能力的 Worker，不能退化为普通 Docker bridge 或随意宿主机路径。每次运行都会重新采集系统指纹并写入 Run Envelope；必需字段缺失时在启动套件前失败。

## DCPerf 与 SPEC 类套件如何配置

DCPerf 集成包选择 `service-stack`。workload metadata 保存角色和服务配置，Adapter 在容器内部完成部署、readiness、负载发生与原生结果收集，再输出 goodput、尾延迟、错误率和资源证据。

SPEC 类集成包选择 `batch-suite`。workload 对应 task 或 task group，参数声明编译/运行配置、copies 和线程，Adapter 调用套件官方工具并保留原始报告，再按 task 输出观测。套件原生聚合分数作为有来源的结果保留，审计层仍使用 task 级结果计算 leverage 和排序稳定性。

两者不共享套件私有字段，只共享 Run Envelope、生命周期、标准观测和证据边界。

## 导入与验证流程

1. 开发者或接入 Skill 在 Package 中完成 manifest、Adapter 和 fixture；
2. 在“注册 Benchmark”页面选择完整 ZIP；Stage 0 合同可只选 UTF-8 YAML 或 JSON；
3. Looper 执行 JSON Schema 和跨字段校验，并从文件自动读取全部注册摘要；
4. 有阻断项时回 Package 修改并重新导入，页面不维护第二份来源、指标或审计说明；
5. 阻断项清零后点击一次“登记到目录”；
6. Stage 0 配置只能进入目录，不能运行；
7. 可执行配置必须包含可下发脚本包或固定 digest 容器、依赖锁、生产执行策略、`looper-adapter/v1` 和 `normalize` 阶段；
8. 先运行 fixture/冒烟，再进入兼容性矩阵和正式审计。`spec.audit` 的缺失属于准入提醒，不阻止注册。

仓库中的 `benchmarks/config-driven-fixture` 是合同测试样例，不是性能 Benchmark，也不能作为选型证据。它证明 suite-owned producer 与 normalizer 可以仅通过配置被通用 Worker 执行。该 fixture 故意使用本地进程且不声明生产源码 revision，因此从注册页导入时会显示生产门禁失败；程序员应以页面逐项约束为清单，把正式包改为固定 digest 容器并补齐不可变来源。
