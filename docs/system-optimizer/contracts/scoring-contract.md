# 优先级、评分与采纳契约

> 状态：normative draft  
> 原则已确认；具体公式、权重和统计参数 open。

论文原式、导师综合式和项目扩展草案的逐项来源见 formula-provenance.md。该文档是公式登记表，不代表其中所有公式均已获准实现。

## 四种结果

| 结果 | 回答的问题 | 输出方式 |
|---|---|---|
| component metric priority | 选定组件内部先调查哪个指标 | 二维分层加持续性/可信度证据 |
| hypothesis confidence | 先验证哪条瓶颈假设 | 证据列表和状态，不冒充因果概率 |
| candidate utility | 配置是否改善业务或组件目标 | 相对可比基线的目标向量 |
| feasibility/evidence/risk | 候选能否晋级和部署 | 独立门禁和等级，不揉进性能分 |

组件内二维优先级 MUST NOT 进入整体 workload 业务得分。

## 先门禁，后收益

候选只有在以下项目通过后才进入收益比较：

1. 配置 preflight、apply 和 readback verify 成功。
2. workload 正确性和结果完整性通过。
3. 安全和硬 SLO 未违反。
4. 环境、阶段和基线可比。
5. 关键指标齐全且测量证据充分。

门禁失败的候选标记 infeasible，并保存原因。不得用吞吐或其他收益抵消错误率、正确性或安全失败。

## 通用调优

通用调优优先输出组件结果向量：CPU、内存与 NUMA、存储、网络、稳定性和跨组件退化。每个组件由经确认的 component objectives 评价。

组件局部 best observed 必须组合复测后才能晋升为通用 Profile。第一阶段不设置隐式固定跨组件总权重。

## workload 调优

workload manifest 声明：

- 主要业务目标及方向。
- 硬 SLO、正确性和安全门禁。
- 可选次级目标和成本。
- 同分项或 Pareto/字典序政策。

系统微指标默认只参与诊断、解释和门禁。只有 workload 合同明确把资源或能耗定义为业务成本时，才能进入 candidate utility。

## 比较基准

结果必须明确区分：

- vs frozen baseline：最终报告的总体提升。
- vs incumbent：是否替换当前最佳候选。
- vs general profile：场景调优相对通用基础的增量。

分数只在环境指纹、配置合同、workload 与数据集、阶段、工具版本、统计协议和评分版本可比时比较。跨环境只能另做迁移验证，不默认横向排序。

## 不确定性

每个收益结果必须携带重复次数、中心估计、离散或置信信息以及数据覆盖率。小于基线噪声的表面提升不能自动认定为有效优化。

最小有效提升、置信方法、置信水平、重复次数和接受规则均需基线校准并经用户确认。历史 LCB95 方案处于 reopened，不是当前默认。

## 结果定性

- observed：获得一次或多次原始测量。
- best observed：本次预算内最好，但未必重复验证。
- validated：通过已确认重复和稳定性规则。
- portable：跨时间或同规格目标验证。
- deployable：另通过安全、所有权和发布审批。

不得把 best observed 写成全局最优或 deployable。

## 缺失与显示分

- 关键目标或门禁缺失时不生成可比较综合分。
- 可选诊断缺失可以继续，但 evidence coverage 必须可见。
- 内部引擎保存目标向量、门禁和证据；未来 UI 可以生成展示分，但必须显示分项、基线和评分版本。
- 展示分不得跨机器或 workload 解释为绝对性能分。
