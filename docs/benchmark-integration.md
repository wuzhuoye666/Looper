# Looper Benchmark 接入规范

版本：`looper.dev/v1alpha1`  
面向：已有 Benchmark 套件的维护者、接入开发者和 Codex Skill

## 1. 接入目标

Looper 不要求开发者在网页中重新描述一遍 Benchmark。开发者只交付一个标准 Benchmark Package，注册页只做三件事：

1. 导入包含 `benchmark.yaml`、Adapter 和部署脚本的 ZIP 接入包；
2. 自动校验身份、接口、安全边界、机器拓扑和证据；
3. 将通过阻断门禁的不可变版本登记到目录。

Benchmark 必须迁就 Looper 的稳定接口；Looper 不为普通套件新增专用 API、调度器分支或 Worker 分支。

注册和正式准入是两条线：

- **注册**：配置完整、安全、可解释，可以进入目录；
- **可选**：只有可执行、受信任并包含完整远程下发/环境准备能力的当前版本，才进入新建选型的下拉框；
- **正式准入**：已经完成 Reference、跨机器/跨日期、排序稳定性等审计，可以作为选型证据。

`spec.audit` 缺失会产生后续提醒，但不再要求用户在注册页手工勾选。

## 2. 最小交付物

```text
benchmarks/<stable-id>/
├─ benchmark.yaml          # 唯一注册事实源
├─ prepare / installer     # 在用户选定机器后幂等准备依赖
├─ producer / launcher     # 调用原生套件，可放在固定 digest 容器内
├─ normalizer              # 原生结果 -> Looper 标准结果
└─ fixtures/               # 不执行昂贵负载的解析/正确性样例
```

Looper 接收完整 ZIP，但导入和注册阶段只做安全解包、摘要和合同校验，不执行上传方代码。Stage 0 合同仍可只上传 UTF-8 YAML/JSON。生产可执行包优先使用固定 `@sha256` 容器；需要直接测试新购宿主机的套件可以使用受信任 local-process 与 managed provisioning，最终登记 ZIP 的操作就是明确的本地安装授权。

## 3. 三个稳定边界

### 3.1 Manifest：运行前合同

`benchmark.yaml` 必须声明：

| 区域 | 用途 |
| --- | --- |
| `metadata` | 稳定 ID、版本、许可证、不可变源码 commit/digest |
| `spec.parameters` | 可调参数类型、范围、默认值和条件 |
| `spec.workloads` | 原生 task/workload 及生产权重 |
| `spec.scenario` | 决策问题、逻辑角色、主指标、正确性/SLO 门禁 |
| `spec.infrastructure` | 机器组、数量范围、最低机型、放置和网络关系 |
| `spec.adapter` | `looper-adapter/v1`、命名输入、必过检查、标准输出 |
| `spec.runtime` | 固定镜像、依赖锁、安全策略和生命周期命令 |
| `spec.metrics` | 指标单位、方向、类型和最低样本量 |
| `spec.outputs` | 必须保留的原始证据和大小上限 |
| `spec.audit` | 默认重复次数、Reference 策略、环境轴、必需证据 |

### 3.2 Run Envelope：运行时输入

Looper 在运行前生成 `run-envelope.json`。Adapter 从中读取候选参数、workload、主目标机指纹、命名输入和隔离路径，不读取控制面数据库。

命名输入支持：`dataset`、`artifact`、`config`、`endpoint`、`secret`、`device`、`topology`。Secret 只能是 `secret://` 引用；需要追溯的内容必须要求 SHA-256。

### 3.3 标准输出：运行后证据

Adapter 必须生成：

- `metrics.jsonl`：每行一条 Looper MetricObservation；
- `result.json`：运行状态和 `requiredChecks` 结果；
- manifest 声明的原始日志、报告、trace、直方图或 profile。

Normalizer 负责转换套件私有 CSV/JSON/文本。Looper 不直接解析某个套件的私有格式。

## 4. 机器与拓扑合同

`spec.scenario.roles` 描述业务上的逻辑角色；`spec.infrastructure.nodeGroups` 描述实际要准备的机器。两者不要混为一谈。

```yaml
infrastructure:
  orchestration: adapter
  primaryNodeGroup: server
  nodeGroups:
    - id: server
      role: target
      count: {minimum: 2, default: 4, maximum: 16}
      includedInScore: true
      requirements:
        osFamilies: [linux]
        architectures: [x86_64, aarch64]
        capabilities: [container]
        cpu: {minimumLogicalCpus: 16, minimumNumaNodes: 1}
        memory: {minimumGiB: 64}
        network: {minimumGbps: 25, fabrics: [ethernet, roce]}
      placement:
        separateFrom: [client]
        sameZone: true
        dedicated: true
    - id: client
      role: load-generator
      count: {minimum: 1, default: 2, maximum: 8}
      includedInScore: false
      requirements:
        osFamilies: [linux]
        cpu: {minimumLogicalCpus: 8}
        memory: {minimumGiB: 16}
      placement:
        separateFrom: [server]
        sameZone: true
        dedicated: true
  links:
    - source: client
      target: server
      purpose: benchmark traffic
      protocol: tcp
      minimumGbps: 10
      maximumRttMs: 1
```

每个机器组至少回答六个问题：

1. 它是什么角色，是否进入最终成本/得分；
2. 最少、默认、最多需要几台；
3. 支持哪些 OS 与 CPU 架构；
4. CPU、内存、加速器、存储和网络的硬下限是什么；
5. 是否要求 root、perf、systemd、hugepages、裸设备或故障注入；
6. 可以与谁同机、必须与谁隔离、是否要求同可用区和独占机器。

### 4.1 两种编排所有权

| `orchestration` | 含义 | 当前状态 |
| --- | --- | --- |
| `adapter` | Looper 启动主入口，套件根据绑定的 topology 文件/端点编排其他机器 | 当前多机接入方式 |
| `looper` | Looper 按机器组分别分配和启动角色 | 已预留合同；当前 Worker 不支持多角色调度，只能 Stage 0 |

当 `adapter` 管理多台机器时，`spec.adapter.inputs` 必须包含 `required: true` 的 `topology` 输入。拓扑文件只保存节点引用、角色和端点；凭据仍使用独立 `secret` 输入。

### 4.2 硬件下限的执行边界

Schema 和注册服务会验证机器组结构、数量范围和引用关系。当前调度器会检查目标机是否在线以及 capability 集合；CPU/内存/GPU/网络/存储的完整自动匹配仍需扩展统一 inventory。接入者不得把“已经声明最低配置”写成“Looper 已经完成所有机器的自动验机”。Adapter 在 `prepare` 阶段仍应 fail closed，并把验机结果写入 `result.json` 和系统指纹证据。

## 5. 不同套件如何选模型

| 套件形态 | `executionModel` | 典型机器组 | 关键输入/证据 |
| --- | --- | --- | --- |
| Sysbench、SPEC、MESS | `batch-suite` | 1 个 target | dataset/config；原始报告、系统指纹 |
| DCPerf、TailBench++ | `service-stack` | server + client/load-generator | topology/endpoints；尾延迟、请求账本 |
| BenchBase、CloudyBench | `database` | database + client + 可选 controller | endpoint/secret/topology；事务账本、故障证据 |
| IO500 | `storage` | clients + storage | device/topology；逐阶段/逐进程日志、持久性检查 |
| CCL-Bench | `distributed` | coordinator + GPU workers | dataset/topology/device；profiler trace、workload card |
| Atrex-Bench | `accelerator` | generation + isolated evaluator GPU | artifact/device；编译、正确性、性能和 profile |

## 6. 生命周期

Worker 依次执行存在的阶段：

`prepare → warmup → run → normalize → validate → collect → cleanup`

新购机器按“基础 Worker → 选择 Benchmark → 下发 ZIP → prepare → run”的顺序工作。`runtime.provisioning.hostCapabilities` 只列部署前必须存在的基础能力；`provides` 列出由 `prepare` 自动安装或解包的套件软件。`prepare` 必须使用固定摘要依赖、幂等复用 `{cache}`，不能要求用户先登录机器安装 Benchmark。

要求：

- 命令使用 argv 数组，不使用 Shell 字符串；
- `cleanup` 在成功、失败、超时和取消后都尽力执行；
- 未知占位符和越界路径直接失败；
- Adapter 原生失败不得被 normalizer 改写成成功；
- `requiredChecks` 中每个 ID 都必须在 `result.json` 出现；
- 主指标必须在 `metrics.jsonl` 中出现，且单位与方向一致。

## 7. 接入步骤

### 步骤 A：先调查套件

只使用固定源码和权威文档确认：许可证、版本、原生命令、角色、机器数、最低资源、网络/存储副作用、输入数据、输出、正确性、指标、重复策略。无法确认的字段必须留作阻断问题，不能猜。

### 步骤 B：写 Package

1. 从 `docs/examples/benchmark-single-node.yaml` 或 `benchmark-multi-node.yaml` 开始；
2. 写 `benchmark.yaml`；
3. 把套件私有逻辑放进 launcher/容器；
4. 写 deterministic normalizer；
5. 添加小 fixture 覆盖成功、原始输出损坏、正确性失败和缺失证据。

### 步骤 C：本地验证

```powershell
python -c "from pathlib import Path; from looper_core.manifest import load_and_validate_manifest; print(load_and_validate_manifest(Path('benchmarks/<id>/benchmark.yaml'))[1])"
pytest -q tests/test_core_contracts.py tests/test_adapters.py
```

只运行套件 fixture。未经用户授权，不运行不可信代码、昂贵负载、裸设备测试或付费云资源。

### 步骤 D：注册

1. 打开 `/benchmarks/register`；
2. 选择完整 Benchmark ZIP（Stage 0 可选 YAML/JSON）；
3. 阅读自动识别的接口摘要；
4. 若有阻断项，回 Package 修改后重新导入；
5. 点击“登记到目录”。

页面不再提供来源、指标、机器数量或审计勾选的手工覆盖入口。

## 8. 完成标准

- [ ] 来源固定到完整 commit 或 SHA-256，许可证明确；
- [ ] 参数和 workload 身份稳定；
- [ ] 逻辑角色与机器组分开声明；
- [ ] 多机数量、最低机型、放置和链路可机读；
- [ ] 多机 Adapter 声明 required topology 输入；
- [ ] 原生输出被保留，Normalizer 只做确定性转换；
- [ ] 主指标、单位、方向、最小样本和必过检查一致；
- [ ] 容器、依赖、网络、存储和环境证据策略固定；
- [ ] 新机所需套件软件由 managed `prepare` 自动部署，基础能力和部署后能力分开声明；
- [ ] fixture 覆盖失败路径；
- [ ] 注册 ID、版本、package digest、manifest digest 和门禁结果可追溯。

## 9. 版本规则

- 修改参数范围、workload、机器下限、指标、门禁、命令或依赖：提升 Benchmark 版本；
- 修改稳定协议语义：提升 Looper API 版本；
- 不影响行为的厂商信息：放在 `x-extensions`；
- 已登记版本仍作为旧实验的历史证据保留；同一 Benchmark ID 新登记的版本自动成为目录当前版本，场景目录和新建选型只显示这一版，不并排展示旧版本。
- Stage 0 或缺少部署包的更新不会进入新建选型；补齐可执行包和自动准备合同后再发布新版本。
