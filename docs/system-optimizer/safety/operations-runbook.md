# System Optimizer 运维与验收手册（M5-01 最小收口版）

> 当前状态：synthetic M3 可运行；真实动态闭环已两轮关闭（REAL-M3-01 功能校准
> 2026-08-25 晨、REAL-DISC-01 双臂发现运行 2026-08-25 午后含首个真实 accepted candidate
> 与晋升）；DYN-END-01（显式窗口终点，gate v1alpha3）与 RCP-02B（legacy guard 显式恢复）
> 已实现并全测通过；REAL-L6C、REAL-SSH、REAL-S9 尚未关闭。本手册不会把 readiness 或
> collector smoke test 写成真实闭环成功。

## 1. 三命令本地验收

以下命令必须在仓库根目录、以 editable 方式安装当前工作树及开发依赖的 Python 环境执行，
`<workspace>` 必须是空目录：

```powershell
python -m looper_api.cli system-opt validate --manifest <manifest.yaml> --policy <policy.yaml>
python -m looper_api.cli system-opt m3-demo --workspace <workspace>
python -m pytest tests/test_system_opt_dynamic_cli.py tests/test_system_opt_online_routing.py tests/test_system_opt_scenario_profile.py --basetemp=.artifacts/m5-focused
```

若环境未 editable install，必须显式把当前工作树的 `services/api`、`packages/core` 和
`packages/benchmark-sdk` 放入 `PYTHONPATH`，避免误调用其它 worktree 的已安装包。每次运行
保留命令、commit、Python/OS、target、UTC、退出码和 pytest summary。

## 2. 真实运行前门禁

1. 保存 `EnvironmentSnapshot`、manifest、state evidence、workload、pressure protocol 和 policy
   digest，确认都属于同一目标。
2. 逐项核对 backend capability、可执行文件 allowlist、writable roots、所有权交接和回读。
3. 确认目标无未清 attention、非终态 post-apply receipt、过期 lease 或 legacy guard。
4. 明确 owner、lease TTL、窗口数、采样窗、S4 scale、阈值、保留期和允许写集合；这些参数
   都没有项目默认值。
5. local-linux 必须显式 `--enable-real` 和既有确认字符串；远程写 API 当前不存在。

readiness 只证明接口可读/可构建，不代表动态 A/B、收益、回滚或跨环境晋升通过。

## 3. 运行中观察

- `control/` 中的 receipt、dynamic collection/routing、scenario profile 和 recovery evidence
  只能由对应原子发布入口产生；不要手工改摘要文件。
- 每窗最多执行一个 change；风险预算在执行前检查；`apply_started` 后的失败按 durable receipt
  恢复，不以 CLI 退出码单独判断是否写过配置。
- 发现 identity drift、证据图损坏、非终态 post-apply receipt 或 attention 时停止新运行，
  保存现场，不删除 guard/lease/pointer。

## 4. 失败与恢复

| 现象 | 处置 |
|---|---|
| apply 前失败 | 保留原异常链和失败证据；确认没有 `apply_started` 后再重试 |
| apply 后异常 | 以 receipt/L1 结果判断 rollback；不得仅重跑命令 |
| rollback 失败或回读不一致 | 标记 needs-attention，禁止同目标新 lease，人工核对真实状态 |
| receipt/evidence 篡改、分叉、孤儿 | fail-closed，复制完整 control 目录供离线 replay |
| SSH 断连 | 不推断远端状态；等待 REAL-SSH 合同下的重连、回读和 attention 流程 |
| 动态退化 | 仅用 S9 promoted last-good 和显式 threshold 执行 L6c recovery |

清 attention 必须有完整恢复证据，不得通过删除 attention 文件绕过。

## 5. 证据归档

归档至少包含：git commit、环境 snapshot、所有输入合同、原始 collector/workload 输出、
content-addressed evidence、固定索引、receipt 全链、attention/lease 终态、命令和日志。
`.artifacts/` 默认不提交 Git；若要长期保存，先确认脱敏、存储位置和保留期。

### 5.1 REAL-DISC-01 双臂发现运行（2026-08-25，已归档）

- 目标：`47.104.25.156`（阿里云 u1-c1m1.2xlarge 8C8G，按量，平台下单）；代码 `8439654`
  部署于 `/opt/looper-discovery/`；负载 `stress-ng --vm 8 --vm-bytes 600M --timeout 60s`。
- 用户显式授权：THP madvise→always / madvise→never 双相位、MDE 0.02、SLO 45131.14、
  相位结束无条件恢复。
- 结果：相位 1 always +15.59% 晋升（复测中位 51649 vs 基线 44684，CV 0.65%）；相位 2
  never −1.31% 多窗破退化界 safety-triggered 拒绝。双相位 THP 均恢复 madvise，零
  guard/lease/attention，receipt 全 terminal。
- 证据：`.artifacts/discovery-8vcpu-20260825/`（未跟踪）；两相位 tar 包 sha256
  `02b77f20893f2ac56b985bdee4bb272d29ab33db0a25c908c5c5b4477bb83dbe` /
  `4598915084fdc300d7d5d2fde5011289a21f32881f7ee822103bdbb34f85d3a4` 双端一致；
  当前代码回放验证 ALL-PASS（`downloaded-verification.json`）。
- 诚实边界：SLO 为用户批准的介入触发器而非机器缺陷声明；无先验宣称（MEMORY 旧记录
  "never=−3.8%" 无 artifact 支撑已废弃）；单主指标为会话设计选择。

## 6. schema 与迁移承诺

- 已发布 v1/v1alpha1 模型保持显式分派，不就地改变 digest payload。
- 新字段改变 canonical payload 时必须升 schema；旧 JSON 不静默回填。
- gate 合同三版本并存（v1alpha1/v1alpha2/v1alpha3），`load_dynamic_phase_gate` 按
  schema_version 分派；v3 窗口预算唯一来源是合同 `PhaseBudgetV3.max_windows`，CLI
  `--max-windows` 对 v3 禁止、对 v1/v2 必填（DYN-END-01I，含 R2 真实工件回归）。
- RCP-02B 已实现：`looper.receipt-guard-reconciliation/v1alpha1` 证据模型 +
  `system-opt reconcile-legacy-guard` CLI；发现 legacy guard 全 store fail-closed，
  恢复走冻结 9 步顺序，证据先落盘后删 guard（schema 字段待用户逐字段复审，
  GAP-R02B-1/2 已在代码与登记本标记）。
- DB typed EnvironmentSnapshot 双写、M4 API/event 仍未实现，迁移脚本和回滚程序
  因此仍是开放项。
- candidate/hypothesis cache 共用 JSONL 但按 schema 分派；retention 必须显式提供。

## 7. 当前已知限制

- 腾讯云 CVM 尚无完整真实动态闭环，阿里云结果不可外推。
- O2 PMU 取决于虚拟化透传；不可获取时必须记录 unavailable。
- 单 NUMA 节点不能验证跨 NUMA 区分度。
- REAL-S9 需要真实 accepted candidate 和第二授权环境，当前证据门未满足。
- O3 trace、通用缓存/结果复用、E_m/S4-V2 属 M6+。
- M4 目前只有 CLI/local backend 切片，没有权限化远程写 API 或 UI。

## 8. 发布前剩余动作

当前定位为**最小收口版**：三命令验收入口已锁定，真实闭环证据（REAL-M3-01 /
REAL-DISC-01）已归档并可离线回放，已知限制如实列出。剩余动作为后续版本内容，
不阻塞当前交付：

1. REAL-L6C / REAL-SSH / REAL-S9 真实演练（各需用户显式授权目标与参数）。
2. EnvironmentSnapshot typed migration 与只读 API（M4-01A）。
3. 每个 schema 的兼容矩阵、升级和回滚登记。
4. REAL-DISC-01 证据包离线回放入口：
   `python .artifacts/discovery-8vcpu-20260825/verify_discovery.py
   .artifacts/discovery-8vcpu-20260825/downloaded-evidence`（需
   `PYTHONPATH=services/api;packages/core;packages/benchmark-sdk`）。
