# 动态相位会话文件约定（引擎 ↔ 外部负载侧的唯一界面）

> 状态：v1（2026-08-24，随 `dynamic_adapters.py` 落地）。权威实现是
> `packages/core/looper_core/system_opt/dynamic_adapters.py`（SessionLayout）；
> 本文是给外部负载会话脚本（测试侧）的对齐合同。SO-D020：引擎永不启动负载，
> 只读本约定中的 `windows/`、只写 `control/`。

## 目录布局

```
session-dir/
  workload-contract.yaml     # WorkloadContract（yaml.safe_dump 兼容格式）
  gate-contract.json         # DynamicPhaseGateContract（model_dump_json）
  promotion-contract.json    # PromotionContract
  hypothesis-proposals.yaml  # 声明式竞争假设（rank/change 显式）
  business-policy.json       # BusinessRetestPolicy（全部数值任务显式）
  baseline-batch.json        # 冻结业务基线 MeasurementBatch
  o1-collection-plans.json   # （可选，真机）L4 活体采集计划列表 ComponentCollectionPlan；
                            #   environment_digest 必须绑定本机——外来环境的计划直接拒绝
  windows/<window_id>/
    identity.json            # LoadCommandIdentity（model_dump_json；含 identity_digest 计算
                            #   所需的 tool/argv_digest/declared_duration_seconds）
    o0.txt                   # 外部负载自身输出原文（O0 解析输入）
  control/                   # 引擎写的邮箱：retest-request-*.json、
                            #   intervention-failure-*.json、phase-restoration.json
```

## window_id 命名（引擎侧生成的标识，外部侧照此落盘）

| 类别 | 格式 | 产生方 |
|---|---|---|
| 观察窗 | `window-{n}`（n 从 1 起） | 引擎循环逐窗请求 |
| 干预复测组 | `retest-{hypothesis_id}-run{k}`（k=1..retest_window_count） | 引擎在 `control/retest-request-{hypothesis_id}.json` 里**显式声明 window_ids**，外部侧照单供数 |
| 复验组 | `verify-{window_id}-{v}-run{k}`（v=1..verification_window_count） | 同上，`control/retest-request-{verify_id}.json` |

复测/复验是**组**不是单窗：每组 `retest_window_count` 个连续窗口目录，
每窗贡献一个聚合后的业务值——单窗无分布，不允许假装有置信区间。

## 外部侧（负载会话脚本）职责

1. 按 workload 合同的 `load_command`（argv 由外部侧持有）循环起压；
2. 每完成一轮，把该窗输出原文写入 `windows/<id>/o0.txt`，
   身份写入 `windows/<id>/identity.json`（用
   `looper_core.system_opt.workload.load_argv_digest` 对**实际执行的 argv** 计算
   argv_digest——与合同不同即身份漂移，引擎会停相位）；
3. 轮询 `control/retest-request-*.json`：出现即按其中 window_ids 依序补供
   同身份负载窗口；
4. 台账纪律：外部侧自己的命令台账（N/C 编号 ndjson）与引擎侧分账。

## 引擎侧（dynamic-run）职责

- 只读 `windows/`，只写 `control/`；
- 每窗身份核对（exact match），漂移即停；
- 干预 = L1 安全路径施加配置（keep）→ 请求复测组 → S6/S7 业务裁决；
  拒绝的假设立即用 pre-apply 快照值恢复（恢复失败 = 停相位的安全事件）；
- 相位收尾无条件把配置恢复到相位起点（`control/phase-restoration.json` 留证）；
- 输出 DynamicPhaseRun 证据 JSON（含全部窗口记录、复验观测、晋升证据、
  停止门决策与假设账本 digest）。

## 已知边界（诚实声明）

- 复测窗等待有超时（business-policy.json 的 window_wait_timeout_seconds），
  超时 fail-closed（SessionFileMissing），不会用旧窗口顶替；
- v1 的 O2 探测证据源是观察窗 digest 占位（CLI component_probe），
  真 O2 组件微指标窗口随 G 泳道窗口化采集适配接入；
- 假设提案 v1 是声明式文件（rank 显式），从 O1 在线推导 S4 优先级是后续层；
- `dynamic-run --o1-plans/--o1-window-seconds`（+ `--o2-source live --o2-window-seconds`）
  接入 G 泳道的活体源：任一 collector 不可用 → O1 整体关闭、O2 回退 window-digest
  占位，摘要里如实报告实际运行的模式，不装活体。
