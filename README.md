# Looper

Looper 是面向服务器采购与选型的场景化 Benchmark 套件。用户先提出采购问题，选择真实 workload、候选服务器和证据协议；Looper 再以相同 workload、成对时间块和独立 placement 组织运行，输出 SLO 内容量、尾延迟、错误/中止、价格归一化结果和结论强度。原始观测、环境指纹、价格快照、统计策略和分析代码版本保存在同一条可复算证据链中。

当前 Stage 0 已安装两个选型场景契约：BenchBase SmallBank + PostgreSQL 16，以及 DCPerf MediaWiki 单 VM 闭环容量。两者具备固定上游 commit、manifest、结果 adapter、normalization-only 命令和合成 fixture，但执行状态仍是 `stage0-adapter-only`；这些命令验证 upstream output 到标准 worker evidence 的边界，不启动 workload。Web/API 可以保存选型研究草稿，启动会在容器镜像 digest 和匹配 runner 就绪前 fail closed。内置压缩优化器仍作为本地兼容 demo，不代表产品主流程。

P0 评价框架、23 项候选审计、Top 10 landscape、TencentBench 未决项和 CPU 试点协议分别见 `docs/p0-benchmark-evaluation.md`、`docs/p0-candidate-audit.md`、`docs/p0-benchmark-landscape.md`、`docs/p0-tencentbench-gap.md` 和 `docs/cpu-pilot-design.md`。Stage 0 验收边界见 `docs/stage0-acceptance.md`。

## 开发启动

需要 Node.js 20+、pnpm 11 和 Python 3.12+。建议安装 uv；bootstrap 会安装 `.python-version` 指定的 CPython 3.12.11。

```powershell
pnpm install
pnpm setup
pnpm dev
```

依赖由 `pnpm-lock.yaml`、`uv.lock` 和 `requirements.lock` 固定。API 启动时通过 Alembic 将数据库升级到 head。默认地址：

- Web: `http://127.0.0.1:5173`
- API 文档: `http://127.0.0.1:8000/docs`
- 健康检查: `http://127.0.0.1:8000/api/v1/health`

Docker Desktop 不是本地控制面的前置条件。benchmark 命令会执行代码；只有显式信任的内置 demo 使用本地进程 runner，第三方场景必须使用 digest 固定的容器或受控远程 runner。

## 选型研究

Web 的“新建选型研究”按以下对象建立不可变 spec：

1. 采购问题与适用范围。
2. 安装的 scenario benchmark 及其主指标、SLO、goodput 和尾延迟证据规则。
3. 一个或多个候选资源，以及 variant、placement pair 和可选价格快照。
4. 重复数、随机顺序种子、时间预算和推断单位。

单目标结果只能证明场景可用性。单 placement 的成对重复只形成暂定结论；跨多个 placement pair 时，Looper 先在 pair 内聚合，再按 placement cluster 重采样，避免把同一 placement 的重复误当成独立机器。CPU 试点要求三对 placement 时只报告探索性 SKU 证据，不外推到处理器厂商或实例家族。

## 兼容优化 Demo

启动 API、worker 和 Web 后，可运行内置压缩 Pareto demo：

```powershell
.venv\Scripts\looper.exe demo create --start
```

该路径保留 `optimization` mode、候选搜索和 Pareto 分析，用于验证 worker、fencing、CAS 和 evidence bundle。证据包可离线验证：

```powershell
.venv\Scripts\looper.exe evidence verify path\to\bundle.zip
```

本地还提供第一个“测试—修改—复测—保留/恢复”纵向切片：

```powershell
.venv\Scripts\looper.exe demo verified-loop
```

该命令真实运行压缩 Benchmark，持久化修改受控的 active config，使用相同 seed 做基线与候选配对复测，并输出 `accepted`、`rolled_back` 或 `inconclusive`。只有收益置信下界、正确性和压缩率退化门槛同时通过时才保留候选；其他情况恢复基线并读回核验。证据默认写入 `.looper/verified-action`。这是验证 Action/Verification 合同的本地切片，不代表 BenchBase、CVM、OS 或生产应用已具备自动优化能力，完整边界见 `docs/verified-action-loop.md`。

完成一个 `optimization` 实验后，实验详情页会读取 Benchmark manifest 中的 `postBenchmarkActions` 低风险白名单并显示“优化并重新测试”。点击后，Looper 以原实验最佳可行配置为基线，只修改一个声明参数，创建关联的复测实验；复测完成后显示建议保留、保留原配置或证据不足。原实验和原始证据不会被覆盖。当前 Web 流程只支持 `benchmark-parameter` 动作并输出配置决策，不会把配置自动部署到生产目标。

候选资源页的“连接外部机器”支持通过 SSH 密码、临时私钥或 API 进程 SSH Agent 连接 Linux 主机，自动读取主机名、系统、内核、架构、CPU 与内存，并部署绑定到该目标的 Looper Worker。Worker 通过 SSH 反向隧道访问本机控制平面，测试程序、指标和证据分别在远端执行、采集并回传；连接凭据不会写入数据库。

## 多云目录与受保护购买

云资源市场通过腾讯云 CVM、阿里云 ECS、火山引擎 ECS 和百度智能云 BCC 官方 SDK 提供目录检索和报价能力。凭证只存在于 API 进程环境，不写入数据库、API 响应或浏览器。

真实购买默认关闭，必须同时满足全局开关、Provider allowlist、独立 operator token、独立确认签名密钥、有效精确报价、金额上限、人工确认文本和确认时重新询价。提交前会持久化稳定 client token；结果不明确时进入 `unknown` 且绝不自动重试。完整边界见 `docs/cloud-market.md`。

Stage 0 和本次 CPU 试点设计没有创建、启动或购买任何云资源。任何腾讯云执行仍需要后续明确的阶段预算与资源确认。

## 上游源码治理

第三方来源由 `third_party/sources.lock.yaml` 控制。只有许可证、纳入状态和精确 commit 均通过策略检查的来源可以下载：

```powershell
.venv\Scripts\looper.exe source list
.venv\Scripts\looper.exe source resolve dcperf
.venv\Scripts\looper.exe source fetch dcperf
```

下载归档位于被忽略的 `.looper/upstreams`；许可证不明、`NOASSERTION`、商用或 metadata-only 来源会 fail closed。

## 核心原则

- 采购问题和真实 workload 是场景定义，不是 tuning recipe。
- goodput 只统计成功提交的工作；abort、rollback、retry、timeout 和 error 不得混入容量。
- 正确性、环境一致性和 SLO 是不可补偿硬门禁。
- 公平比较使用公共校准、相同协议和成对时间块，不为单个目标定制调优。
- Attempt 与原始 Observation 追加且不可变；分析绑定输入 digest、策略 digest 和代码版本。
- 尾延迟必须保留 histogram 或 raw evidence，并披露样本量。
- 单机可用性、单 placement 暂定结果和跨 placement 结论使用不同证据等级。
- TencentBench 当前是 `unresolved-internal-baseline`，公开检索缺失不等于内部能力缺失。

架构和契约详见 `docs/architecture.md`、`docs/benchmark-contract.md`、`docs/experiment-contract.md`、`docs/operations.md` 和 `docs/upstreams.md`。

阶段 1 已落地统一实验与 Trace 数据底座：CCL-style、BenchBase、DCPerf 三种原始输出经各自 Adapter 归一为同一 Evidence Contract，Trace 离线重算无需重新运行 Benchmark。设计、数据分层、新 Adapter 编写指南与当前边界见 `docs/unified-evidence-contract.md`。

VGO 变异导向波动分析器（Variability Analyzer）已作为公共组件上线：与 BenchTrust 平行挂在统一实验数据之上，输出分布统计、稳定性结论、快/慢模式识别、慢运行关联线索、方差来源归因、控制变量 A/B 建议与分布级配置比较（"均值改善但尾部恶化"会显式标出）。所有 Benchmark 共用，无需各自实现。设计、指标契约与边界见 `docs/variability-analyzer.md`，前端入口为实验详情页"波动分析"标签。
