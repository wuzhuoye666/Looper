# System Optimizer 后续 Agent 执行计划（2026-08-25）

> 状态：current handoff plan
> 编制者：glm5.3
> 起始主线：`system-optimizer-impl@d984265`
> 任务状态唯一登记本：`agent-work-ledger-2026-08-24.md`
> 未完成项事实源：`unfinished-task-queue-2026-08-24.md`

本文供后续 agent 在上下文或额度不足时逐项接管。它不重新定义架构，也不把待决策项
写成默认值。执行前必须先读本文件、任务涉及的合同文档和源代码，再按依赖顺序工作。

## 1. 当前可信起点

截至 `d984265`：

- M1/M2 既有安全底座、通用调优切片已落地；M3 本地纵向功能链已经接通。
- `8.134.104.213` 已完成真实 sysbench O0、O1/O2 live、THP 两候选写入、业务复测、
  durable receipt 和拒绝恢复。`never`、`always` 均未达到显式最小效果，不能宣称收益。
- 两个真实相位最终都恢复 `madvise`，无 guard/lease/attention；证据包 sha256：
  `0cd520cd89679cd9d22acf23b9c0ddb6389e273d235c64531a4fa83903fcb28c`。
- 本轮未启用在线 S4、假设 cache 或场景 Profile；没有真实 accepted candidate，
  因此 REAL-S9 仍是 `blocked-by-evidence`。
- 当前最先要修的正确性缺口是 `DYN-END-01`：`max_windows` 耗尽后运行结束，
  `stop_gate_decision.stop` 仍可能为 `false`。

任何 agent 不得重复开发已完成的 D5-I2、S3-01、L7H-02、M3-INT、M3-PROFILE、
RCP-02A，也不得用 synthetic acceptance 冒充真实 accepted candidate。

## 2. 严格串行主路径

```text
DYN-END-01D 结束语义设计
  └─ 用户确认 schema 方案
      └─ DYN-END-01I 实现与回放
          ├─ RCP-02B-D legacy guard 恢复 schema 复审
          │   └─ 用户确认字段
          │       └─ RCP-02B-I 实现
          │           └─ RCP-03 receipt 扫描性能优化
          └─ REAL-L6C-S 成功恢复真机演练
              └─ REAL-L6C-F needs-attention/恢复真机演练（单独授权）

M4-01A typed EnvironmentSnapshot + 只读 API
  └─ M4-02 权限/审批合同（用户确认）
      └─ REAL-SSH + M4-03 远程生命周期
          └─ M5-01/M5-02 发布与演练收口

S4-02R 目标本地逐 metric 校准
  └─ REAL-M3-ONLINE 在线 S4/cache/Profile 真实拒绝路径
      └─ PERF-CAND-01 寻找真实可接受候选（不能保证成功）
          └─ [仅出现 accepted candidate] 第二授权环境
              └─ REAL-S9 跨环境复验/Profile

M6+：只能在上述主路径稳定后启动。
```

推荐一次只交给一个 agent 一个任务 ID。若并行，只有 §8 标为“可并行”的任务允许同时
进行，且必须使用不同 worktree、不同写集合；共享真实目标时仍必须按 lease 串行。

## 3. 第一批：先修真实运行暴露的正确性问题

### DYN-END-01D：显式窗口终点设计

- 类型：docs-only 设计；必须先于实现。
- 依赖：`d984265` 的真实运行证据。
- 阅读：`dynamic_loop.py`、`phase_gate.py`、`cli.py`、动态 loop/CLI/replay tests、
  `contracts/dynamic-session-files.md`。
- 唯一写集合：新增一份 planning 设计文档；不得改代码。
- 必须比较：
  1. gate schema 新版本把窗口预算纳入 `PhaseBudget`；
  2. run schema 单独绑定 `max_windows` 并形成版本化停止证据；
  3. 仅在现 v2 临时返回 `run.max_windows` 停止的兼容代价。
- 推荐方向：优先把窗口预算纳入新 gate schema；CLI 参数必须与合同一致或由合同唯一
  提供。禁止产生未被 digest 绑定的停止阈值。
- 交付门：v1/v2 历史 digest/loader 兼容表、状态迁移表、负向测试矩阵、精确写集合。
- 停止点：等待用户确认方案，不得直接实现。

可直接给 agent 的提示词：

> 执行 DYN-END-01D，基线以当前 `origin/system-optimizer-impl` 为准。先做 pwd、
> git worktree list、git status 三联自证；完整阅读交接计划和动态 gate/loop/CLI/replay。
> 只写一份 docs-only 版本设计，比较 gate 新版本、run 新版本和临时兼容三案，推荐但不
> 替用户拍板。必须解决 max_windows 未进 digest、运行结束却 stop=false、旧证据回放、
> CLI 参数重复来源四个问题。不要改生产代码、测试或阈值；单 commit，不 push/merge。

### DYN-END-01I：显式窗口终点实现

- 依赖：DYN-END-01D 获用户确认。
- 独占写集合：设计确认的 `phase_gate.py`、`dynamic_loop.py`、必要 loader/CLI、专属 tests。
- 禁止：放宽 D2、多改一个窗口、修改业务 SLO/LCB 公式、迁移旧 evidence。
- 必测反例：0/1/上限窗口边界；上限与 SLO/安全/风险预算同窗竞争时保持固定优先级；
  v1/v2 历史 payload 与 digest 不漂移；CLI 异常和相位恢复不回归。
- 完成定义：任何正常返回的新版 run 都有 `stop=true` 的可回放决定，并绑定明确合同字段、
  阈值和最后证据 digest。

### RCP-02B-D / RCP-02B-I：legacy guard 显式恢复

- 设计依赖：RCP-02A 已完成；实现必须等 schema 字段逐项确认。
- 设计读取：`receipt-mutex-recovery-contract-2026-08-24.md`、`intervention_receipt.py`、
  `lease.py`、CLI attention/recovery。
- 必须冻结字段：target、receipt root、legacy guard identity、发现时间、关联 plan/execution/
  operation、可证明的链头、operator actor、决策、证据 digest、恢复终态。
- 实现独占写集合：`intervention_receipt.py`、`cli.py`、新 reconciliation model、专属 tests。
- 硬约束：证据持久化成功后才能删除 legacy guard；不能用 TTL 猜 stale；不能自动重放
  backend 写；链不唯一/内容损坏/身份不明继续 attention；完整恢复后才 clear attention。
- 完成定义：所有 crash seam 幂等；Windows/Linux 专属测试；历史 advisory lock 语义不退化。

### RCP-03：receipt store 扫描性能

- 依赖：RCP-02B-I 完成。
- 目标：避免每次 `head/advance` 全 store 重读造成累计 O(N²)，但启动时全局完整性审计
  仍必须保留。
- 独占写集合：`intervention_receipt.py`、专属 benchmark/tests；先设计后实现。
- 禁止：把其它 scope 的损坏静默忽略、缓存未经 digest 绑定的可变对象、弱化孤儿/分叉检查。
- 完成定义：提交前后相同规模基准；复杂度改善；全部篡改、断链、分叉、孤儿反例仍拒绝。

## 4. 第二批：真实故障与远程能力验收

### REAL-L6C-S：真实退化后成功恢复

- 依赖：DYN-END-01I；目标、last-good、S8 向量、`u_regression` threshold 和写授权由用户
  显式提供。
- 默认不得复用 2026-08-25 的 0.26698；那是 M3 业务噪声界，不是 L6c 阈值。
- 只允许在目标授权的配置项上制造退化；先快照、再触发、再由生产 L6c CLI 恢复。
- 四层证据：输入可获取、命令可构建、重复触发稳定、恢复前后可区分。
- 完成定义：request/outcome/rollback 固定索引回放通过，readback 等于 last-good，lease
  释放，无 attention；所有原始命令和输出落未跟踪 artifacts。

### REAL-L6C-F：needs-attention 与显式恢复

- 依赖：REAL-L6C-S；这是 A 类故障注入，必须再次取得用户授权。
- 必须使用可逆故障，不得破坏 SSH、根文件系统或用户 worker；注入点优先选择证据发布/
  verify 的可恢复失败，而不是让机器失联。
- 完成定义：原始异常上下文保留；attention 先产生；恢复命令引用 attention evidence；
  readback 正确后才清 attention；失败链和恢复链都可回放。

### REAL-SSH：ssh-remote 后端

- 依赖：至少一个授权远程目标；若与 L6c 共用 `8.134.104.213`，必须严格串行。
- 覆盖：capability、allowlist/writable root、正常读写回滚、连接中断、过期 lease、
  attention/recovery。禁止自动购买、关机、销毁或修改防火墙。
- 断连测试必须由用户确认可接受的断连注入方式；不能以“拔掉当前唯一控制通道”作为默认。
- 完成定义：四层实测 + failure drill，且所有配置和服务恢复到起点。

## 5. 第三批：校准、在线路由和真实收益入口

### S4-02R：逐 metric 目标本地校准

- 依赖：`priority_calibration.py` 的本地审批合同已完成。
- 每个 metric 分开确认：来源、单位、方向、scale 推导、采样数、异常口径和解释阈值。
- 先拉目标原始 O1 全量，再由用户确认 metric 子集；不可把本轮业务吞吐 scale 当成
  CPU/memory/network O1 scale。
- 产出：CalibrationEvidence、ApprovalRecord、digest 文件、固定索引、回放结果。
- 不可测项写 unavailable；无显式批准的 bundle 不得接真实动态运行。

### REAL-M3-ONLINE：在线 S4/cache/Profile 真实拒绝路径

- 依赖：S4-02R 批准 bundle；DYN-END-01I。
- 目标：在真实目标启用 `--online-routing` 和显式 retention，证明 O1→S4→ranked
  proposals、业务拒绝→L7 cache 的生产路径；没有 promotion 时不得启 `--scenario-profile`。
- 完成定义：routing evidence/index、恰一条合法 refuted-hypothesis cache、receipt、
  collection replay、最终恢复全部通过。

### PERF-CAND-01：寻找真实可接受候选

- 这是探索性校准，不保证成功，也不得为了“跑通 accepted”降低 MDE/SLO 或挑选结果。
- 每次候选前必须由用户确认 workload、scale、MDE、样本数、风险和可写配置集合。
- 若所有候选拒绝，应以“保持默认配置”收口；这仍是有效结果。
- 只有真实 S7 accepted 后，才允许生成同环境验证窗口和讨论场景 Profile。

### REAL-S9：跨环境晋升

- 硬前置：真实 accepted candidate + 第二个独立授权环境；否则保持 blocked。
- 在第二环境重新校准 identity/scale，不能复制第一环境阈值；至少满足合同要求的观测数、
  distinct time blocks 和 environments。
- 任一失败观测都必须进入 `failed_observations`；不能删样本换取 promotion。

## 6. 第四批：M4 平台与 M5 发布

### M4-01A：typed EnvironmentSnapshot 与只读 API

- 依赖：现有 `m4-01-api-event-environment-contract-2026-08-24.md` 复审通过。
- 先冻结 DB migration、旧/新 digest 名和 operation-local event sequence。
- 实现只读 operation/evidence/event API 与 typed snapshot 双写；旧记录不回填、不改 digest。
- 禁止真实写 API、后台自动执行和权限绕过。
- 完成定义：migration up/down、旧记录读取、新旧双写、事件顺序、分页/权限负测与 API tests。

### M4-02 / M4-03

- M4-02 先设计角色、审批、审计、confirmation token 和 backend enablement；用户逐项确认后
  才实现写 API。
- M4-03 依赖 M4-02 与 REAL-SSH，用平台任务映射远程 lease/fencing/attention；不得另建
  第二套执行/恢复状态机。

### M5-01 / M5-02

- 固化可复现部署 bundle（源码、schemas、benchmarks、adapters、third-party lock、测试清单
  和整体 digest），避免历史上云端缺目录。
- 完成安装/升级/降级迁移说明、三命令 runbook、证据归档/验证命令、已知限制。
- 汇总动态 receipt、L6c、远程失联等成功/失败演练；每条失败必须有 restored 或
  needs-attention 终态。
- 发布出口不得声称 REAL-S9，除非 §5 的真实前置确实满足。

## 7. M6+ 延后项

以下任务不得为了“清零 backlog”提前启动：

- O3 时间盒 perf/eBPF/抓包；
- 通用采集缓存、中间测量和结果复用；
- 增量下钻/快速探针选择；
- workload 分布漂移重激活 C 案；
- E_m、环境内 ECDF/Z、S4-V2 P/D/A/Q/T；
- L7 跨环境信任和通用 TTL。

启动条件：M1–M5 功能、证据和恢复合同稳定，并拥有足够同环境真实分布；工具权限、
开销阈值、retention/TTL 和数据合规均由用户明确确认。

## 8. 可并行矩阵

| 任务 | 可与谁并行 | 不可并行原因 |
|---|---|---|
| DYN-END-01D/I | M4-01A 文档/DB lane | 不可与任何修改 dynamic loop/gate/CLI 的任务并行 |
| RCP-02B-D/I | S4-02R、M4-01A | 不可与 RCP-03；共享 receipt/CLI 时必须串行 |
| S4-02R | RCP lane、M4-01A | 与真实 M3 共用目标时必须串行 lease |
| REAL-L6C | 本地 docs/API lane | 不可与 REAL-SSH/REAL-M3 在同目标同时执行 |
| M4-01A | DYN/RCP/校准 lane | 不可与其它 DB migration/API 写集合任务并行 |
| M5 文档 | 本地实现 lane | 最终签收必须等待真实演练，不得提前标完成 |

## 9. 每个 agent 的统一开工与交付模板

开工必须报告：

1. `pwd`、`git worktree list`、`git status` 三联输出；
2. 自己的 worktree/branch、实际 HEAD、依赖 commit 是否存在；
3. 本任务写集合、禁止写集合、是否依赖其它未合入任务；
4. 将任务登记为 `assigned`，未登记不得写代码；
5. 不因工作树干净而强制 rebase；只有依赖变化或交付前远端前进才同步。

交付必须报告：

1. commit、parent、完整文件列表；
2. 逐条需求对应到实现和测试；
3. 聚焦测试、System Optimizer、全仓、Ruff、py_compile、`git diff --check` 的实际结果；
4. 未执行项和原因，不得伪报；
5. A/B/C 异常、真实环境残留、无法测得项；
6. 只提交本任务文件，不提交 `.artifacts/`，不 push/merge；
7. 主 agent 验收后由主 agent 使用 `127.0.0.1:65532` 代理统一 push。

通用提示词前缀：

> 你负责本文指定的单一任务 ID。先完整阅读
> `docs/system-optimizer/planning/agent-handoff-execution-plan-2026-08-25.md`、
> `agent-work-ledger-2026-08-24.md` 和任务引用文件；先理清依赖和架构再动手。
> 严守自己的 worktree，开工三联自证。未经用户确认不得新增默认 threshold、scale、TTL、
> schema 迁移或真实写范围。只提交、不 push/merge；交付时按 §9 完整报告。

## 10. 主 agent 验收顺序

每个交付按固定顺序验收：

1. 验证 parent/依赖/写集合，没有删除或覆盖其它 agent 主线能力；
2. 先审合同和 fail-closed 反例，再看正向路径；
3. 使用主线环境独立复跑聚焦测试；必要时再跑 System Optimizer 与全仓；
4. 真机任务先检查最终配置、服务、进程、lease/attention/guard，再验 digest/replay；
5. 更新 ledger/backlog/architecture 状态；
6. 单次代理参数 fetch/push，push 后确认本地与远端 commit 一致。

任何出现结果错误、主键/身份不一致、目标状态无法恢复、attention 无证据或证据链无法
回放的情况均为 A 类异常：立即停止、保留现场、报告用户，不得自行清理或继续下游任务。
