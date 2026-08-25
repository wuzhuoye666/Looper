# System Optimizer 未完成任务队列（2026-08-24）

> 状态：current backlog；最近真实验收代码基线：`system-optimizer-impl@9a32e92`。
> 本文只登记尚未关闭的工作和依赖关系，不代替
> `agent-work-ledger-2026-08-24.md` 的 owner、交付、验收与 push 状态。
> 任何任务正式分配后，必须在 agent 台账另建任务记录。

## 1. 口径与边界

- “实现缺口”表示当前代码或测试可以直接证明能力不存在或覆盖不足。
- “验收缺口”表示代码存在，但没有在目标环境按可获取、可构建、稳定出数、
  区分度四层完成实测；不能用 simulated 或其它云环境结论代替。
- “决策缺口”表示 schema、阈值、TTL、授权或数据口径尚未由任务所有者确认；
  不得由 agent 写入默认值。
- M1/M2 已按现有路线图关闭；本队列不重复打开，除非新证据证明已有出口失效。
- O3、通用缓存和结果复用仍属 M6+；它们不是当前 M3 纵向切片的完成前置。

### 2026-08-24 M3 本地纵向切片收口

- `S4-01`：`7bafd4e` 冻结 v1 四维与公式 v1alpha1；S4-V2、E_m、P/D/A/Q/T 延后，
  不再阻塞在线路由。
- `S3-01`：`9f5c648` 实现 O1 snapshot → MeasurementBatch → S4 v1 → 确定性
  proposal rank；缺失、不可用、身份漂移和未覆盖组件均 fail-closed，不回退文件 rank。
- `L7H-01/02`：合同 `491c855`，实现 `eb4422b`；候选与 hypothesis 两类 schema
  同一 JSONL 严格分派，只缓存身份可比且已执行的业务复测无改善；retention 必须显式输入。
- `M3-INT/M3-PROFILE`：`4f6a4a0` 将上述能力接入 v2 `dynamic-run`，并产出原始/
  通用 Profile 双基线场景报告；`system-opt m3-demo --workspace <empty-dir>` 可跑完整
  synthetic 纵向切片。synthetic 通过不关闭任何真实环境验收门。
- 因此本地 M3 功能链已关闭；当前 M3 外部出口只剩真实目标校准/闭环、REAL-S9，
  O3 仍按既定范围留 M6+。
- `S4-02 local contract`：`f559b1f` 新增显式 MetricContract 审批 bundle、内容寻址落盘和
  回放验证；它不推导 scale，也尚未接真实 CLI。真实逐 metric 数值、解释阈值和审批证据
  仍依赖用户确认与目标实测。

### 2026-08-25 真实 M3 功能闭环校准

- `REAL-M3-01` 已在低规格目标 `8.134.104.213` 运行真实 sysbench 外部负载、O1/O2
  live 采集、v2 风险门禁、THP 写入、业务复测、durable receipt 和拒绝恢复。`never`
  与 `always` 均未达到用户确认的最小效果 0.26698，未接受、未晋升，不形成性能推荐。
- D2 会在一个假设 refute 后阻断唯一剩余假设；为保持硬门禁且不伪造第三假设，两个
  THP 候选分别在独立相位执行。该行为不是安全失败，但多候选穷举流程必须显式考虑。
- 两个相位最终均恢复 `madvise`，无 guard/lease/attention；下载证据经当前代码重算
  dynamic run、receipt chain 和 O1/O2 evidence index 通过。
- 新发现 `DYN-END-01`：`max_windows` 是循环参数而非 gate budget；窗口耗尽时 run 的
  `stop_gate_decision.stop` 仍可为 `false`，仅 note 写“window budget reached”。这与
  “每个相位必须有显式终点”口径不一致，真实运行成功不掩盖该缺口。

### 2026-08-25 下午 Discovery-Success 双臂状态更新

- `DYN-END-01`：设计已交付（`dyn-end-01-window-endpoint-design-2026-08-25.md`），用户确认走
  A 案（gate v1alpha3 + PhaseBudgetV3）；`DYN-END-01I` 由 DeepSeek 执行，需一并修复终点
  `evidence_digest` 绑定 workload digest 而非末窗 digest 的弱绑定。
- `PERF-CAND-01`：真实 accepted candidate 已产生（REAL-DISC-01 相位 1，THP always
  +15.59% 晋升），"寻找候选"入口关闭；后续探索性候选仍按原纪律逐个授权。
- `REAL-S9`：硬前置"真实 accepted candidate"已满足；仍需第二个授权环境并在该环境重新
  校准 identity/scale，不得复制第一环境阈值。

## 2. 本轮报告复核结论

| 报告项 | 复核结论 | 当前证据/处置 |
|---|---|---|
| receipt mutex guard 崩溃后永久残留 | **确认缺口（P0）** | `intervention_receipt.py::_mutex` 仅以 `O_CREAT|O_EXCL` 建 guard，并只在正常 `finally` 删除；无 owner、liveness、expiry 或 reconciliation 合同 |
| 无线程/进程真实竞争测试 | **确认缺口（P0）** | receipt 测试只有顺序 stale-head/幂等/故障注入，没有线程或独立进程争抢同一 operation 的用例 |
| `_all_receipts()` 每次全局重扫，累计 O(N²) | **确认优化缺口（P1）** | `head()`/`advance()` 会重读全 store；任一 scope 损坏会阻断其它 scope。当前是保守的全局 fail-closed 语义，不能在未冻结真实性边界前直接改为局部忽略 |
| pointer 完全缺失时启动重建无独立测试 | **确认测试缺口（P0）** | 现有 `test_content_before_pointer_crash_recovers_unique_head` 覆盖“pointer 仍指祖先”；没有删除 pointer 后由内容链独立重建的用例 |
| D5-I2-B 尚未接线 | **过时结论，禁止重复开发** | `cli.py:1287/1375/1446` 已生产调用 `DurableReceiptStore`、`TwoStageSafetyBackedIntervention`、`run_dynamic_phase_v2`；`dynamic_adapters.py` 已调用 `execute_observed`；`8e657e5`/`2065a77` 已推远端 |
| L5 组件优化器仍拥有终裁 | **过时结论，转文档同步** | `tuning.py` 与 `component/__init__.py` 已明确 `accepted` 只是组件晋级建议，L8 `engine.evaluate_candidate` 终裁；`engine/loop.py` 已实际消费 verdict |
| S4 逐 metric scale、P/D/A/Q/T 迁移、E_m 完整版 | **仅剩真实校准/后续 V2 提案（P1/P3）** | v1 四维与公式已冻结；在线路由使用任务显式 scale；P/D/A/Q/T、confidence 改名和 E_m 不进入本轮 |
| 从 O1 在线推导 S4 路由 | **本地实现完成** | `online_routing.py` 已生产 digest 绑定 S4 vector/rank/evidence；真实目标输入和校准归 S4-02 |
| L7 refuted 假设第二条目类型 | **本地实现完成** | 同 JSONL 独立 schema、完整 identity、显式 retention 和业务复测准入已接 v2 loop/CLI；O2 typed refutation 来源仍延后 |
| O3 时间盒 trace | **确认延后项（P3/M6+）** | 需单独授权、时间盒、开销证据和工具能力；禁止常开 |
| F-MENTOR-002/003 | **确认校准缺口（P2）** | 公式前置条件已登记，任务参数与目标环境数据未完成 |
| L6c 真实目标退化演练 | **确认验收缺口（P1，可独立实测）** | 纯逻辑、CLI、回放和 failure injection 已完成；真实目标尚未演练 |
| S9 跨环境验证 | **确认验收缺口（外部前置）** | 需要至少一个真实已接受候选和两个授权环境；当前五组件实测为零接受，不能伪造样本 |
| ssh-remote 后端 | **确认验收缺口（P2）** | 接口存在，目标环境失联/回滚/attention 行为未验收 |
| L7 存储位置、TTL、重激活 C 案、数值校准 | **拆分处理** | 本地文件存储已实现；TTL/保留期仍是决策缺口；重激活 C 案与通用过程优化留 M6+；各阈值按目标任务单独校准 |
| M4/M5/M6 完全未开始 | **表述不准确** | M4 已有 CLI/local-linux 切片；M5 已有多类实录、replay 与证据验证器；M6 的候选负缓存被提前实现。剩余出口仍很多，见 §4 |

## 3. 依赖图：并行支线与必须串行链

```text
P0 receipt 正确性链（必须串行）
RCP-01 锁恢复合同冻结（已完成）
  └─ RCP-02A advisory lock + 线程/进程竞争 + pointer 缺失测试（已完成）
      └─ RCP-02B legacy guard attention/reconciliation
          └─ RCP-03 scoped index/增量验证性能设计与实现

P1 M3 功能闭合（两支可并行，汇合点串行）
S4-01 schema/公式版本决策（完成）─┬─ S4-02 显式 scale 注入与目标校准（真实目标待做）
                                  └─ S3-01 O1→S4 在线路由生产者（完成）─┐
L7H-01/02 假设缓存合同+实现（完成）──────────────────────────┤
                                                                 └─ M3-INT（本地完成）
                                                                     └─ M3-PROFILE（本地完成）

真实环境验收支线（有授权时可彼此并行）
REAL-L6C 真实退化演练
REAL-SSH ssh-remote 能力/失联/恢复验收
REAL-S9 = 真实 accepted candidate + 至少两个环境 → 跨环境晋升验证

P2 M4 平台链（接口可并行设计，安全语义必须先于写 API）
M4-01 API/事件/EnvironmentSnapshot 合同
  └─ M4-02 权限与人工审批
      └─ M4-03 远程目标/多节点生命周期
          └─ M4-04 可选 UI

P2 M5 交付链
运行手册/迁移/schema 清单可并行起草
  └─ 依赖 P0、M3-INT、选定的 M4 出口和真实 failure drills 后统一验收

P3 M6+（必须等 M1–M5 功能与证据合同稳定）
O3 时间盒 trace；通用采集缓存；中间测量/结果复用；增量下钻；
重激活 C 案；L7 TTL/跨环境可信度扩展；ECDF/Z 与 E_m 后续提案。
```

依赖解释：

1. RCP-03 不得抢在 RCP-02 前做。当前“全 store 损坏即全局拒绝”是安全语义；
   若先局部索引，可能把损坏隔离误写成静默忽略。
2. S3-01 只依赖 S4 schema/显式输入合同，不必等待每个目标环境的全部 scale
   校准完成；但 M3-INT 的真实验收必须携带该目标的校准证据。
3. L7H 与 S3 在线路由可以由不同 agent 并行；只有 M3-INT 同时依赖两者。
4. REAL-S9 不是普通代码任务。没有真实 accepted candidate 时保持 blocked-by-evidence，
   不用 simulated acceptance 冒充跨环境证据。
5. O3 明确留在 M6+，不得为了“补齐 O0–O3 名称”提前常开 trace。

## 4. 任务队列

### P0：先消除安全/事实错误

| ID | 类型 | 任务 | 依赖 | 可并行性与写集合 | 验收门 |
|---|---|---|---|---|---|
| RCP-01 | 决策/设计 | 冻结 receipt mutex 崩溃恢复合同；**不设置隐式 stale timeout** | D5-I2-A/B/C 已完成 | `accepted-design`：R2 单一 advisory lock、未知文件系统拒绝、新旧 writer 禁止混跑；legacy 恢复拆到 02B | `f54abd6` + `6296d11`；仅 RCP-02A 已获实施授权 |
| RCP-02A | 实现/测试 | advisory lock；同 scope 线程/独立进程竞争、持锁进程退出、不同 scope 并行、pointer 完全缺失重建；legacy guard 全 store fail-closed | RCP-01 | `accepted-a`：`2d479b8` + `990d087`；独占 `intervention_receipt.py` 与新增 concurrency tests | Windows 聚焦 48、System Optimizer 639、全仓 972；WSL2 `flock` 独立 open/fork/terminate 原语实测；完整 Linux pytest 待 CI/真实 Linux 环境补跑，不阻塞 RCP-02A 代码接收 |
| RCP-02B | 实现/测试 | legacy guard 发现、target attention、内容寻址 reconciliation evidence、显式 operator 恢复 | RCP-02A；schema 逐字段复审 | 独占 receipt store + CLI + 专属 tests | 证据先于删除；所有崩溃缝可幂等重试；完整恢复后才清 attention |
| DOC-01 | 文档 | 同步 `implementation-map.md`、`overall.md`、`workload-tuning.md` 和 2026-08-24 rebaseline：D5-I2 已接线、risk quota 已生产消费、L5 已降级、M3 真实剩余边界 | 无 | `accepted-docs`：`e0552f4` + `3c443ff`；仅架构/规划文档 | 已保留历史快照并以 addendum 更新；未改公式/阈值；在线路由缺口已准确收敛为 O1 evidence→S4 vector→ranked proposals 生产者 |

### P1：关闭 M3 剩余功能与高优先级真实验收

| ID | 类型 | 任务 | 依赖 | 可并行性与写集合 | 验收门 |
|---|---|---|---|---|---|
| RCP-03 | 性能/安全 | 设计 scope-local 索引或单次扫描快照，消除每次 advance 全局 O(N) 重验；明确其它 scope 损坏时是否仍全局阻断 | RCP-02 | 与 M3 功能 lane 并行；独占 receipt store/tests | 基准证明复杂度改善；篡改/断链/分叉/孤儿仍 fail-closed；不得降低启动全局审计强度 |
| DYN-END-01 | 正确性 | 把 `max_windows` 耗尽建模为可回放的显式停止决定，或将窗口预算纳入版本化 gate 合同；禁止返回“运行已结束但 stop=false” | v1 digest/loader 兼容评审 | 独占 `dynamic_loop.py`/`phase_gate.py` 与专属 tests；不得顺带放宽 D2 | v1 历史回放零漂移；v2 到窗上限必须 `stop=true` 且引用合同字段/证据 digest；CLI 仍无条件恢复 |
| S4-01 | 决策/合同 | 冻结 v1；V2/P-D-A-Q-T/E_m 延后 | 已完成 | `7bafd4e`；v1 四维、公式 v1alpha1 和旧入口不变 | 无旧 digest 漂移；无默认 scale/阈值 |
| S4-02 | 校准 | 本地审批/持久化/回放合同已完成 `f559b1f`；对每个实际 metric/目标环境生成 scale 与解释阈值校准证据仍待实测 | S4-01、真实目标授权、用户确认推导依据 | 可按 metric/环境并行 | 四层实测；未获取数据记 unavailable，不用论文数字填充；真实接线前必须验证 bundle |
| S3-01 | 实现 | O1 evidence → S4 v1 → 确定性 ranked proposal | 已完成 `9f5c648` | 新模块/tests；v1/v2 声明回放入口保留 | digest 全绑定；数据不足 fail-closed；不回退文件 rank |
| L7H-01 | 决策/合同 | refuted hypothesis schema/身份/准入/retention | 已完成 `491c855` | docs-only 合同 | v1 仅业务复测来源；TTL 无默认值 |
| L7H-02 | 实现/回放 | 假设负缓存同文件分派、原子持久化、读取/失效和 loop bridge | 已完成 `eb4422b` | negative-cache + bridge + tests | 同身份命中、任一身份变化 miss、坏行 fail-closed、发布后才更新内存 |
| M3-INT | 集成 | 在线路由与假设缓存接 v2 dynamic loop/CLI | 本地完成 `4f6a4a0` | receipt/恢复链原样复用 | synthetic 接受/拒绝两条 E2E 通过；真实 workload 验收仍开放 |
| M3-PROFILE | 交付模型 | 场景 Profile + 原始/通用 Profile 双基线报告 | 本地完成 `4f6a4a0` | `scenario_profile.py` + CLI/demo/tests | 绑定环境/workload/公式/run/promotion/candidate；不自动启用或跨环境复用 |
| REAL-L6C | 真实验收 | 在已授权目标注入运行期退化，验证 S8 threshold → S9 last-good → L1 恢复/attention/回放 | 目标、阈值、last-good 证据、变更授权 | 可与代码 lane 并行；不共享同一目标租约 | 成功与失败各一条实录；所有命令/原始证据落盘；不能用 simulated 替代 |
| REAL-SSH | 真实验收 | 验收 ssh-remote 的 capability、命令边界、断连、过期 lease、恢复和 attention | 至少一个授权远程目标 | 可与 REAL-L6C 并行，但必须不同目标或串行租约 | 四层实测 + 失联 failure drill；禁止自动购买/销毁实例 |
| REAL-S9 | 真实验收 | 对真实 accepted candidate 做跨环境复验 | 真实 accepted candidate、至少两个授权环境 | 外部证据门；可与文档 lane 并行 | S9 identity/time-block/environment 合同全部满足；无 accepted candidate 时保持未执行 |

### P2：M4、M5 与导师公式校准

| ID | 类型 | 任务 | 依赖/顺序 | 说明 |
|---|---|---|---|---|
| M4-01 | 合同/实现 | 现状盘点和只读优先设计已完成 `a365713`；System Optimizer API、事件投影、EnvironmentSnapshot typed 双写实现待做 | 先冻结 DB migration/digest 命名/事件序列；统一版本后实现 |
| M4-02 | 安全 | 操作者权限、审批、真实 backend enablement | 依赖 M4-01；必须先于任何远程写 API |
| M4-03 | 平台 | 远程目标与可选多节点生命周期 | 依赖 M4-02、REAL-SSH；不得绕过 lease/fencing/attention |
| M4-04 | 展示 | 可选 UI | 依赖稳定 API/事件；UI 不得启用未授权真实 backend |
| M5-01 | 交付 | 运维/恢复/归档/schema/限制骨架已完成 `a365713`；发布版、迁移脚本和真实演练证据待做 | 最终签收依赖 P0、M3-INT、选定 M4 出口和真实演练 |
| M5-02 | 演练 | 跨组件、动态 receipt、L6c、远程失联 failure drill 汇总 | 依赖对应实现/目标；每个失败必须有恢复或 needs-attention 终态 |
| CAL-MENTOR | 校准 | F-MENTOR-002/003 参数和目标环境证据 | 与真实校准活动并行；未经数据和用户确认保持禁用 |

### P3：M6+ 延后队列

| ID | 任务 | 启动门 |
|---|---|---|
| O3-TRACE | 显式授权的时间盒 perf/eBPF/抓包采集，携带 disabled→enabled 开销证据 | M1–M5 稳定 + 工具/权限/数据合规授权 |
| CACHE-GENERAL | 采集缓存、中间测量和候选结果复用 | 身份、TTL、失效、跨环境可信度合同获确认 |
| DRILL-INCREMENTAL | 增量下钻和快速探针选择 | 有足够同环境真实特征数据 |
| REACT-C | workload 分布漂移重激活 C 案 | 有校准分布；资格仍不等于自动重启 |
| S4-EM/ECDF | E_m、环境内 ECDF/Z 等后续评分提案 | 有同环境/同协议/同 metric 校准分布并完成公式版本登记 |

## 5. 下一批可执行工作

### 本地可并行（互不共享写集合）

1. **M4-01A 实现准备**：复审 typed DB 字段、旧/新 digest 命名和 operation-local event
   sequence；确认后实现 snapshot 双写及只读 API，不开真实写 API。
2. **M5-01 发布化**：现有骨架补精确打包入口、migration/rollback 和真实演练证据；
   不得把待执行真机项写成完成。
3. **RCP-02B schema 复审**：只逐字段复审 legacy guard reconciliation/attention 模型；
   用户确认前不实施。
4. **S4-02 真实输入确认**：任务包已完成；由用户确认目标、逐 metric scale/reference
   推导依据、采样/异常口径和解释阈值后执行，不填默认值。

### 真实目标可并行（必须不同目标，或共享目标时同 lease 串行）

1. REAL-L6C：成功恢复与 needs-attention 各一条退化演练。
2. REAL-SSH：远程 capability、断连、过期 lease、恢复/attention failure drill。
3. S4-02：按目标和 metric 执行四层校准；无法获取的指标如实记 unavailable。

### 必须串行的里程碑出口

真实 target-local scale/reference → 真实 M3 动态闭环 → 产生真实 accepted candidate
→ 第二授权环境复验 → REAL-S9/场景 Profile 跨环境边界报告。没有真实 accepted candidate
时 REAL-S9 保持 blocked-by-evidence。之后再选择 M4 安全出口并统一收口 M5；O3、通用缓存、
结果复用和增量下钻继续留 M6+。

## 6. 分派纪律

- 每个 agent 开工仍需 `pwd`、`git worktree list`、`git status` 三联自证。
- 不因为工作树当前干净就强制 rebase；只有依赖提交变化或交付前主线已前进时才同步。
- 同一批次中不能把两个 agent 分配到相同写集合；汇合文件由主 agent 在依赖验收后接管。
- 真机任务必须记录目标、授权、显式参数、命令、原始输出和无法测得项。
- 所有 GitHub fetch/push 由主 agent 使用 `127.0.0.1:65532` 代理统一执行。
- 未确认的 stale timeout、TTL、scale、阈值、保留期、跨环境信任均不得写默认值。
