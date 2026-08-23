# 多轮自动闭环开发基线（2026-08-23）

> 状态：implementation baseline
> 适用范围：离线或受控 Linux guest 调优；不代表已在腾讯云 CVM 获得优化收益。

## 问题更正

2026-08-23 阿里云 ECS 实录只配置了一个 I/O scheduler 候选，实际证明的是
“单候选安全执行闭环”，不能称为完整自动搜索闭环。执行器已有循环骨架，但该实录的
动态搜索域除基线外只有一个值，且策略明确设置 `max_candidates: 1`、
`no_improvement_limit: 1`，所以一次候选后停止。

## 闭环状态机

每个任务按以下顺序执行：

1. 冻结身份、配置、workload、阶段、工具和统计合同。
2. 测量初始基线，计入 attempt 预算。
3. 根据动态合法域、既有候选和历史结果自动生成唯一候选。
4. 通过安全链施加、读回验证、测量并回滚。
5. 执行可比性、硬门禁、稳健改善和 Pareto/显式决胜规则。
6. 将可行候选的完整目标向量反馈给搜索器；不可行或证据不全候选按 failed trial
   反馈，禁止把表面收益教给搜索器。
7. 每完成显式 `baseline_every_n` 个候选后重测基线；周期基线也消耗 attempt 和
   wall-time 预算，后续候选绑定实际使用的基线 digest。
8. 重复 3–7，直到先触发一个显式停止条件。

## 预算与停止顺序

策略必须显式声明以下参数，不提供隐式任务默认值：

- `max_candidates`：最多完成多少个候选测量轮次。
- `max_attempts`：初始基线、周期基线和候选测量共享的总次数预算。
- `wall_time_seconds`：包含基线、施加、验证、测量和回滚的总墙钟预算。
- `no_improvement_limit`：连续多少个候选未刷新已接受 LCB 后停止。
- `target_improvement`：可选；主目标 LCB 达到该显式目标后停止。
- `baseline_every_n`：每多少个候选重测一次基线。

安全异常优先停止。其余停止原因包括目标达成、搜索空间耗尽、连续无改进、候选预算、
attempt 预算和 wall-time 预算。停止必须输出结构化原因，不能只打印“完成”。

## 时间模型

设一次基线批次耗时为 `T_b`，一次候选完整安全测量耗时为 `T_c`，完成 `N` 个候选、
每 `K` 个候选刷新基线，则近似墙钟为：

\[
T \approx T_b + N T_c + \left\lfloor\frac{N-1}{K}\right\rfloor T_b
\]

阿里云 fio 实录中 `T_b≈91.5s`、`T_c≈91.5s` 仅用于说明量级，不能作为 CVM 默认值。
若 `K=3`、`N=10`，按该实录量级约需 21.4 分钟，实际仍受施加开销、机器争用和
workload 协议影响。

## 当前完成与下一阶段

已实现：多候选生成、历史反馈、动态域去重、周期基线、共享 attempt/wall-time 预算、
停止原因、候选轮次/attempt/baseline 计数和基线 digest 绑定；simulated 专项测试覆盖
候选预算与 attempt 预算停止。

下一阶段按 M2/M3 继续：

1. 为 CPU、内存/NUMA、存储和网络分别建立经目标能力验证的候选域。
2. 实现组件内分阶段搜索与跨组件组合复验，避免一次同时改太多参数而无法归因。
3. 补 workload 的 L0/L1 低开销路由与 L2/L3 触发式采集。
4. 在共享云服务器上先做只读 preflight 和时间预算预估，经执行窗口确认后再跑多轮。
5. 最终必须在腾讯云 CVM 重做四层验证；阿里云、WSL2 和 simulated 结果均不可外推。

## 验收锚点

- `test_engine_generates_multiple_candidates_and_refreshes_baseline`
- `test_periodic_baseline_and_candidates_share_attempt_budget`
- `test_policy_has_no_implicit_search_or_statistics_contract`
- `test_full_demo_runs_general_and_workload_closed_loops`
