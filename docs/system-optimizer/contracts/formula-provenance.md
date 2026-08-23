# 计分公式溯源与适用边界

> 状态：normative formula registry；S0–S10 已获用户确认并作为实现契约  
> 日期：2026-08-22  
> 目的：登记导师指导、论文原式和项目扩展草案，防止把不同层级、不同场景的公式误拼成一个总分。  
> 实现状态：批准 S0–S10 的公式结构；未批准任何隐式权重、阈值或置信水平。所有数值策略必须由任务合同显式提供。

## 1. 使用规则

每条公式必须标记为以下来源类型之一：

| 类型 | 含义 | 能否直接实现 |
|---|---|---|
| PAPER-ORIGINAL | 论文明确给出的公式或聚合规则 | 只能在相同语义和输入条件下实现 |
| MENTOR-SYNTHESIS | 导师清单综合多篇论文给出的 Looper 建议 | 需要补齐参数和本项目实测校准 |
| PROJECT-CONTRACT | 用户已确认的本项目实现契约 | 可实现；数值参数仍必须显式声明并记录来源 |
| PROJECT-DRAFT | 根据已确认产品思路提出的项目表达 | 用户确认并实测前不得成为默认行为 |
| DESCRIPTIVE-ONLY | 论文只给方法或文字关系，没有可复现完整公式 | 不得自行补权重或阈值 |

以下对象必须分开：

1. 组件内微指标优先级：决定先下钻什么。
2. 瓶颈假设可信度：决定先验证哪条假设。
3. 候选收益：判断改动是否改善组件或业务目标。
4. 可行性、稳定性和风险：决定候选是否允许晋级。

组件内优先级不得进入整体 workload 业务得分。硬门禁不得被任何加权收益补偿。

## 2. 统一符号

| 符号 | 暂定含义 | 注意 |
|---|---|---|
| x | 一个配置候选 | 必须绑定配置、环境和任务身份 |
| m | 一个指标 | 必须有方向、单位、作用域和阶段 |
| B_m | 指标 m 的冻结可比基线 | 不是滚动窗口的隐式替代 |
| C_m(x) | 候选 x 下指标 m 的测量结果 | 必须通过测量有效性检查 |
| U_i | 第 i 个业务或组件效用 | 具体归一化尚未确认 |
| w_i | 第 i 个目标的显式重要性权重 | 来源和版本必须可追溯 |
| P_m | 组件内当前不利压力坐标 | PROJECT-DRAFT，尚无统一公式 |
| D_m | 组件内不利变化坐标 | PROJECT-DRAFT，尚无统一公式 |
| G_k | 第 k 个不可补偿门禁 | 缺失关键门禁视为未通过 |

## 3. 搜索阶段公式实现契约

本节是系统优化器搜索流程的唯一阶段总线。它规定如何计算和晋级，但不内置任何跨任务通用数值阈值。Linux 是真实目标操作系统；Windows 只可作为开发宿主，不能被报告为真实系统优化目标。

### S0：身份与可比性门禁

来源类型：PROJECT-CONTRACT。

候选、基线和复验数据只有在环境、配置、workload、阶段、工具和统计合同身份一致时才可比较：

\[
Comparable(x)=\bigwedge_{i=1}^{N}IdentityMatch_i(x)
\]

任一必需身份字段缺失或不一致时，结果必须标记为不可比较，禁止计算候选收益。

### S1：基线校准

来源类型：PROJECT-CONTRACT。

每个指标必须显式声明中心估计器、波动/不确定性估计器、置信水平、重采样次数、重复协议、实际有效尺度 `scale_m` 和最小有效改善量 `MDE_m`。系统优化器不继承旧实验合同中的隐式默认值。

`MDE_m` 可以来自业务最小收益、基线噪声校准或两者中更严格者，但任务必须记录选择依据和数值。没有合格基线或必需校准参数时停止，不自行填值。

### S1.1：目标机 CV 稳定门禁派生

来源类型：PROJECT-CONTRACT；formula id：
`F-PROJECT-PRESSURE-CV-BOOTSTRAP-UPPER/v1alpha1`。

给定冻结校准样本 `Y={y_1,...,y_n}`，每次有放回抽取同样本量得到 `Y_b`，并且必须从
同一个 `Y_b` 同时计算均值与样本标准差：

\[
CV_b=\frac{s(Y_b)}{|\bar{Y_b}|},\qquad
Limit_{CV}=Q_{confidence}(\{CV_b\}_{b=1}^{B})
\]

置信水平、重采样次数、随机种子、样本数、目标作用域和环境可迁移边界必须显式记录。
`Limit_CV` 只用于相同目标环境和相同测量协议的后续批次；不得从阿里云、WSL 或 loopback
外推到腾讯云 CVM/NIC。均值为零、样本少于 3、关键身份改变或派生上界为零时 fail-closed。
该门禁只判断测量批次是否足够稳定，不等于候选收益 MDE，也不证明候选有效。

同节登记 S1.1 的点估计 CV 报告公式 id：`F-PROJECT-PRESSURE-CV/v1alpha1`，定义
`CV=s(Y)/|\bar Y|`（`evaluate_measurement_stability` 的 report 口径）。它只用于报告
单批次的样本变异系数，不派生 `Limit_CV` 门限；门限派生只认
`F-PROJECT-PRESSURE-CV-BOOTSTRAP-UPPER/v1alpha1`。

### S2：不可补偿门禁

来源类型：PROJECT-CONTRACT，与 F-MENTOR-001 对齐。

\[
Feasible(x)=\bigwedge_{k=1}^{K}G_k(x)
\]

false、missing、timeout、unknown 均不通过。收益、成本或其他软目标不能补偿门禁失败。

### S3：workload 组件路由

来源类型：PROJECT-CONTRACT。

组件路由保存业务症状、同阶段组件压力、时间关系和证据覆盖，不压成全局加权分。一个症状允许路由到多个竞争组件假设；关联状态不写成因果结论。

### S4：组件内二维优先级

来源类型：PROJECT-CONTRACT。

对适合比例比较且基线不在近零区的指标，不利变化为：

\[
D_m=d_m\frac{C_m-B_m}{|B_m|}
\]

- minimize 指标取 \(d_m=+1\)。
- maximize 指标取 \(d_m=-1\)。
- \(D_m>0\) 表示不利变化。

当前压力必须由指标合同显式提供 `PressureTransform_m`；容量利用率、SLO 超限距离、目标距离和同阶段分布位置不得互相冒充。组件内排序保留向量：

\[
Priority_m=(P_m,D_m,Persistence_m,Confidence_m)
\]

第一版采用确定性的 Pareto 层和词典序决胜，不将该向量加权为跨组件总分。target、range、近零、负值、计数器回绕和跨阶段指标必须使用各自合同中的绝对尺度或超限距离。

实现契约（fail-closed，无隐式分母）：target/range 指标的不利变化为
`D_m=(distance(C_m)-distance(B_m))/scale_m`，`scale_m` 缺失时 fail-closed；
压力变换 `PressureTransform_m`（EXCESS/DEFICIT/TARGET_DISTANCE/RANGE_EXCESS）同样要求显式 `scale_m`，
不再以 `abs(reference)` 或 `1.0` 作为隐式分母。

### S5：动态合法搜索域

来源类型：PROJECT-CONTRACT。

\[
\mathcal D_{task}=\mathcal D_{declared}\cap\mathcal D_{capability}\cap
\mathcal D_{authorized}\cap\mathcal D_{dependency}\cap\mathcal D_{risk}
\]

候选生成器只能在该交集中工作。任一交集项无法验证时，该配置不进入搜索域，但仍保留采集状态和失败原因。

### S6：方向感知候选改善量

来源类型：PROJECT-CONTRACT。

令 `scale_m` 为任务显式声明的有效尺度：

\[
I_m(x)=
\begin{cases}
\dfrac{C_m(x)-B_m}{scale_m},&maximize\\
\dfrac{B_m-C_m(x)}{scale_m},&minimize
\end{cases}
\]

target 指标使用到目标距离的减少量，range 指标使用到合法区间超限距离的减少量。必须同时保存原始值、估计值、不确定性、公式 ID、公式版本和输入摘要。

`I_m(x)` 的符号已方向归一化（改善为正）：后续搜索生成器与 Pareto 排名一律把改善量当作最大化目标，不得再次叠加指标自身方向（否则方向被双重编码）。

实现契约（fail-closed，无隐式分母）：target/range 指标的 `I_m(x)` 使用到目标/区间距离的减少量除以显式 `scale_m`；`scale_m` 缺失时 fail-closed，不再使用隐式 `1.0` 分母。

### S7：稳健接受条件

来源类型：PROJECT-CONTRACT，与 F-MENTOR-002 对齐。

主目标候选的最小接受条件为：

\[
Accept(x)=Comparable(x)\land Feasible(x)\land
LCB_{confidence}(I_{primary}(x))>MDE_{primary}
\]

置信水平、重采样方法、重复次数和 `MDE` 必须由任务显式给出。只看到均值上升不能晋级。

`bootstrap_improvement` 产出的 `ImprovementEvidence` 统一标注公式 id
`F-PROJECT-S6-S7/v1alpha1`：它覆盖 S6 的点估计（`improvement_value`）与 S7 的
bootstrap 置信下界（`LCB=Q_{\alpha/2}(\{I_b\}_{b=1}^{B})`），是 S6/S7 的组合证据
标签，不含任何新的权重或阈值。

### S8：结果向量与排名

来源类型：PROJECT-CONTRACT。

通用调优保留：

\[
U_{general}(x)=(U_{cpu},U_{memory},U_{storage},U_{network},U_{stability},U_{regression})
\]

workload 调优保留：

\[
U_{workload}(x)=(U_{primary},U_{secondary},Cost,Risk,EvidenceCoverage)
\]

排名顺序固定为：硬门禁 → Pareto 层 → 显式任务决胜规则。F-MENTOR-003 的 `J(x)` 仅允许作为同一 Pareto 层内的可选决胜器，且全部权重与惩罚系数必须显式提供；缺失时保持并列，不补默认值。

### S9：晋升与组合复验

来源类型：PROJECT-CONTRACT。

`best observed` 只有通过任务声明的重复、跨时间或跨环境复验后才可晋升为 `validated`。通用调优的单组件最优组合后必须重新测量；不能用各组件独立收益相加推断组合收益。

### S10：停止条件

来源类型：PROJECT-CONTRACT。

停止原因必须是以下显式状态之一：目标达到、候选域耗尽、接受/拒绝证据已充分、显式连续无有效改善策略触发、候选/尝试/时间预算耗尽、安全门禁触发、用户取消、测量错误或恢复失败。

连续次数、预算、置信参数和目标阈值全部属于任务输入。缺少所需参数时在 preflight 停止，禁止“一直跑”，也禁止实现层偷偷填默认值。

## 4. 导师指导的 Looper 三层结构

来源：[导师清单“三、建议融入 Looper 的统一目标函数”](<../../../../面向腾讯云 IaaS 测评与 Looper 的 10 篇优先复现论文.md#三建议融入-looper-的统一目标函数>)。

### F-MENTOR-001：不可补偿硬门禁

来源类型：MENTOR-SYNTHESIS。

导师要求正确性、业务质量、反 fallback、可用性、RPO/RTO、安全、容量、p99/SLO、数据持久性和大面积 workload 退化先形成硬门禁。

项目中的逻辑表达草案为：

\[
Feasible(x)=\bigwedge_{k=1}^{K}G_k(x)
\]

只要任一必需门禁为 false 或关键证据缺失，候选就不进入收益排序。该布尔表达是对导师文字规则的项目化表示，不是论文原式。

### F-MENTOR-002：稳健性门禁

来源类型：MENTOR-SYNTHESIS。

导师建议候选满足：重复收益置信下界为正、跨宿主机/跨日方向基本一致、换计分公式不明显掉队、尾部和波动不恶化、结果不依赖偶然缓存/频率/热状态。

当前没有批准统一的稳健性布尔公式。需要先确定环境集合、重复设计、最小有效提升和统计估计器。

### F-MENTOR-003：Pareto 层内稳健效用

来源类型：MENTOR-SYNTHESIS。

导师给出的建议形式为：

\[
J(x)=\sum_i w_i\operatorname{LCB}_{95}(\Delta U_i(x))
-\lambda_1\operatorname{CVaR}_{99}(L_x)
-\lambda_2CV_x
-\lambda_3R^{worst}_{env}(x)
-\lambda_4Cost_x
-\lambda_5Energy_x
\]

适用范围：已经通过硬门禁和稳健性门禁的候选，在 Pareto 层内按业务政策决胜。

当前不可直接实现，原因包括：

- ΔU 的归一化未定义。
- w 的数据来源、时间窗和版本未定义。
- LCB95 与 CVaR99 是否适合每类 workload 未实测。
- L、CV 和最坏环境退化的统计样本未定义。
- 五个 λ 未校准。
- 缺失 Cost/Energy 的处理未确认。

禁止将该式用于组件内微指标排序，也禁止在门禁失败时计算 J 后补偿失败。

## 5. 论文原始计分公式

### 4.1 Benchmark 可靠性审计

来源：[Are Performance-Optimization Benchmarks Reliably Measuring Coding Agents?](../../../../papers/fulltext/2607.01211.md)。

#### F-PAPER-REL-001：统一 runtime change

来源类型：PAPER-ORIGINAL。

当 speedup 为

\[
s=\frac{T_{base}}{T_{ref}}
\]

论文转换为 runtime change：

\[
RC=\frac{1}{s}-1
\]

负值表示 reference 比 base 快。它用于跨 benchmark 对齐运行时间变化方向，不是 Looper 的最终收益公式。

#### F-PAPER-REL-002：OPT@1

\[
OPT@1(m)=100\cdot\frac{\#\text{reference-level successes}}{N}
\]

正确且达到/超过参考补丁才计一次成功，低于参考但正确的结果没有部分分。适合审计二元参考级达成率，不适合表达连续的小幅收益。

#### F-PAPER-REL-003：SpeedUp Ratio 与调和平均

\[
SR_{m,i}=\frac{speedup_{m,i}}{speedup_{ref,i}}
\]

\[
HM(m)=\frac{N}{\sum_i1/\max(SR_{m,i},0.001)}
\]

论文证明该聚合会使极低 SR 任务获得很高杠杆，排名会随计分规则改变。Looper 对它的主要采纳是“审计任务杠杆和排名敏感性”，不是照搬 0.001 floor。

### 4.2 CCL-Bench

来源：[CCL-Bench 1.0](../../../../papers/fulltext/2605.06544.md)。

#### F-PAPER-CCL-001：资源翻倍效用

来源类型：PAPER-ORIGINAL。

\[
Utility(r)=\frac{T-T_{2\times}(r)}{T}\times100\%
\]

T 是 baseline step time，T 的下标 2×(r) 表示只将资源 r 翻倍后的模拟 step time，其他条件固定。

适用范围：回答“升级某项资源是否带来端到端收益”。它适合验证资源瓶颈假设，不适合比较任意不同类型的系统配置，也不能替代真实配置干预。

CCL-Bench 还明确展示 compute-communication overlap 变高但 step time 反而变差的反例，因此代理微指标不能直接替代业务结果。

### 4.3 Atrex-Bench

来源：[Are LLM-Generated GPU Kernels Production-Ready?](../../../../papers/fulltext/2607.14541.md)。

#### F-PAPER-ATREX-001：单 shape roofline achievement

来源类型：PAPER-ORIGINAL。

\[
S_j=\frac{T_{roofline,j}}{T_{cand,j}}\in(0,1]
\]

T_roofline 来自固定参考语义，不从候选 profile 推导。大于 1 被视为单位、测量或 bound 错误，不做截断。

#### F-PAPER-ATREX-002：单 operator 聚合

\[
S_i=
\begin{cases}
\operatorname{median}\{S_j:j=(i,s)\text{ correct}\},&\text{存在正确 shape}\\
0,&\text{不存在正确 shape}
\end{cases}
\]

失败 operator 记零而不是从分母删除，防止缺失失败被静默忽略。

#### F-PAPER-ATREX-003：生产重要性加权

\[
S_{agg}=\sum_iw_iS_i\in[0,1]
\]

w_i 是 operator 在生产 wall-time 中的占比。该式直接支持“生产重要性权重”，但只在权重来源真实、脱敏、版本冻结且覆盖范围明确时可迁移。

注意：DCPerf 支持生产 workload 校准，但其公开总分使用几何平均；生产时间权重的直接论文公式来源是 Atrex，不能写成 DCPerf 原式。

### 4.4 DCPerf

来源：[DCPerf fulltext](../../../../papers/fulltext/DCPerf_fulltext.md)。

#### F-PAPER-DCPERF-001：套件几何平均

来源类型：PAPER-ORIGINAL。当前本地全文以文字明确几何平均规则，下面是该规则的等价数学展开，不是额外权重设计。

DCPerf 先将每个 benchmark 相对已知 baseline 归一化，再以几何平均形成 overall score。其数学展开是：

\[
Score_{overall}=\left(\prod_{i=1}^{n}Score_i^{norm}\right)^{1/n}
\]

该式适合正值、已归一化的套件分数。DCPerf 同时报告 SLO 下 RPS、Perf/Watt 和 Perf/$，说明不同业务目标可能互相不一致。

禁止把此几何平均解释为生产时间份额加权，也不应把硬 SLO 作为一个可以被其他 benchmark 补偿的低分项。

### 4.5 CloudyBench

来源：[CloudyBench](../../../../papers/fulltext/CloudyBench_ICDE25.md)。

#### F-PAPER-CLOUDY-001：P-Score

\[
P=\frac{\overline{TPS}}{Cost_{cpu}+Cost_{mem}+Cost_s+Cost_{io}+Cost_{net}}
\]

#### F-PAPER-CLOUDY-002：E1-Score

\[
E1=\frac{\overline{TPS}}{\widetilde{Cost_{cpu}}+\widetilde{Cost_{mem}}+\widetilde{Cost_{io}}}
\]

#### F-PAPER-CLOUDY-003：故障恢复类指标

\[
F=\frac{1}{k}\sum_{i=1}^{k}(t_s^i-t_f^i)
\]

\[
R=\frac{1}{k}\sum_{i=1}^{k}(t_r^i-t_s^i)
\]

F 是恢复服务所需时间，R 是服务恢复后恢复到目标 TPS 所需时间；两者越小越好。

#### F-PAPER-CLOUDY-004：E2、C 与 T

\[
E2=\frac{1}{\lambda}\sum_{i=1}^{\lambda}\frac{TPS_i-TPS_{i-1}}{\delta}
\]

\[
C=\frac{\overline{T_{insert}}+\overline{T_{update}}+\overline{T_{delete}}}{\lambda}
\]

\[
T=\frac{\sqrt[m]{\prod_{i=1}^{m}TPS_i}}{\sum_{i=1}^{m}Cost_i}
\]

#### F-PAPER-CLOUDY-005：O-Score

\[
O=SF\cdot\lg\left(\frac{P\cdot T\cdot E1\cdot E2}{R\cdot F\cdot C}\right)
\]

论文允许通过乘积和对数统一比较七个维度。导师清单明确不建议把 O-Score 原样作为 Looper 唯一奖励，因为正确性、可用性、RPO/RTO、复制延迟和 SLO 不应被吞吐与成本补偿。

Looper 可借鉴 Resource Unit Cost 和分维度设计，但必须先门禁，再做 Pareto/业务决胜。

### 4.6 IO500

来源：[Statistical Characterization of IO500 Submission Data](../../../../papers/fulltext/2605.02194.md)。

#### F-PAPER-IO500-001：带宽分数

\[
Score_{BW}=(S_{ior\mbox{-}easy\mbox{-}w}\cdot S_{ior\mbox{-}easy\mbox{-}r}\cdot S_{ior\mbox{-}hard\mbox{-}w}\cdot S_{ior\mbox{-}hard\mbox{-}r})^{1/4}
\]

#### F-PAPER-IO500-002：元数据分数

\[
Score_{MD}=(S_{md\mbox{-}easy\mbox{-}w}\cdot S_{md\mbox{-}easy\mbox{-}s}\cdot S_{md\mbox{-}hard\mbox{-}w}\cdot S_{md\mbox{-}hard\mbox{-}s}\cdot S_{find})^{1/5}
\]

#### F-PAPER-IO500-003：总分

\[
Score_{overall}=\sqrt{Score_{BW}\cdot Score_{MD}}
\]

论文同时证明 aggregate score 会隐藏 close-time、stonewall wear-down straggler 和逐客户端不平衡。Looper 只能把几何平均作为概览，持久化、close/fsync、尾部和最慢客户端需要独立证据或门禁。

### 4.7 MESS

来源：[MESS 提取文本](../../../../papers/fulltext/a_paper_pdf.txt)。

#### F-PAPER-MESS-001：memory stress score

来源类型：DESCRIPTIVE-ONLY。

论文说明 stress score 范围为 0–1，并由 memory latency 与 bandwidth-latency curve inclination 的加权和计算：

\[
Stress_{memory}=WeightedSum(Latency,CurveInclination)
\]

引用段没有给出可直接复刻的权重和归一化细节，因此不得自行填写系数。

必须区分：CurveInclination 表示带宽变化导致延迟变化的敏感度，不是指标随时间的上升比例。它可以启发内存组件内的“当前压力 + 饱和敏感度”，不能直接成为所有组件的统一动态公式。

论文在自身 HPCG/Extrae 环境报告 10ms 默认采样、低于 1% 开销；Looper 必须在自己的目标环境重新验证，不能继承这个开销结论。

### 4.8 VGO

来源：[Variability-Guided Performance Optimization](../../../../papers/fulltext/VGO_ICPE2026.md)。

#### F-PAPER-VGO-001：没有统一总分

来源类型：DESCRIPTIVE-ONLY。

VGO 观察 mean、median、mode、p95、SD、CV 和完整分布，用统计分类器找与 normal/tail 或不同 mode 相关的低层指标，再施加 mitigation 并重测分布。

其中常用：

\[
CV=\frac{SD}{Mean}
\]

VGO 没有给出“高值 × 最大变化”的统一优先级公式，也没有证明关联就是因果。它直接支持：

- 低层指标与业务分布区域关联。
- 优先验证 feature importance 较高的因素。
- 干预后必须重测完整分布。
- 均值、尾部和波动可能是不同甚至相反目标。

VGO 自己也指出长 workload 重复数百次成本高，可使用代表性片段、代理 workload 或等负载迭代作为样本；具体替代策略必须验证代表性。

### 4.9 TailBench++ 与 SPEC CPU2026

TailBench++ 提供动态多客户端/多服务端尾延迟测试能力，但当前本地全文没有给出导师所称 SLO-Goodput 的唯一原始公式。SLO-Goodput 是导师基于尾延迟场景提出的 Looper 综合建议，不能写成 TailBench++ 论文原式。

SPEC CPU2026 使用几何平均 score，并展示对 solo profile 按 retired instructions 或 cycles 加权形成一阶预测；论文同时说明这种加权预测不是精确线性插值。它适合代理 workload 组合和通用 CPU 表征，不直接决定 workload 场景业务分数。

## 6. 项目公式草案

本节只记录已讨论的数学接口，不批准具体权重和阈值。

### F-PROJECT-001：方向感知相对不利变化

来源类型：PROJECT-DRAFT。

只在 B_m 非零、远离近零区且指标适合比例比较时，候选表达为：

\[
D^{rel}_m=d_m\cdot\frac{C_m-B_m}{|B_m|}
\]

其中：

- minimize 指标取 d_m=+1，上升为不利。
- maximize 指标取 d_m=-1，下降为不利。
- D 大于 0 表示向坏方向变化。

该式不适用于 target、range、近零、可为负、计数器回绕或跨 workload 阶段指标。近零判定不能擅自填 epsilon。

### F-PROJECT-002：组件内二维坐标

来源类型：PROJECT-DRAFT，核心分工已确认，变换函数未确认。

\[
P_m(t)=PressureTransform_m(x_m(t),Reference_m,Scope_m,Phase_m)
\]

\[
D_m(t)=AdverseChangeTransform_m(x_m(t),B_m,Direction_m,Phase_m)
\]

二维结果 (P_m,D_m) 只在同一组件内部决定下钻顺序：

| P | D | 含义 |
|---|---|---|
| 高 | 大 | 正在恶化的高压热点 |
| 高 | 小 | 持续稳定瓶颈 |
| 低 | 大 | 新出现风险，先验证持续性 |
| 低 | 小 | 当前证据弱 |

建议第一版保存二维坐标、持续性和不确定性，不急于压成一个标量。高/低阈值、PressureTransform 和 AdverseChangeTransform 都是 open decision。

### F-PROJECT-003：瓶颈假设证据

来源类型：PROJECT-DRAFT。

暂不定义伪精确的“因果概率”。假设结果保存：业务症状、时间关系、组件压力、微指标、持续性、竞争假设、可控配置和干预结果。

状态只允许：observed association、supported hypothesis、intervention-supported、unresolved。

### F-PROJECT-004：通用调优结果向量

\[
U_{general}(x)=(U_{cpu},U_{memory},U_{storage},U_{network},U_{stability},U_{regression})
\]

来源类型：PROJECT-DRAFT。

第一阶段不设置固定跨组件总权重。各组件局部 best observed 必须组合复测；任一安全、正确性或跨组件硬退化门禁失败则不可晋升。

### F-PROJECT-005：workload 候选结果向量

\[
U_{workload}(x)=(U_{primary},U_{secondary},Cost,Risk,EvidenceCoverage)
\]

来源类型：PROJECT-DRAFT。

具体维度由 workload manifest 声明。系统微指标不自动进入 U_primary；只有 workload 合同明确声明资源或能耗为业务成本时才进入候选效用。

比较必须分别保存：相对 frozen baseline、相对 incumbent、相对 general profile。

### F-PROJECT-006：L5 条件判定分布统计量与置信界

来源类型：PROJECT-CONTRACT；formula id：`F-PROJECT-CONDITION-BOOTSTRAP/v1`。

L5 公式映射的 `when` 条件可声明分布统计量（median / mean / p95 / cv）与置信模式
（point / lcb95 / ucb95）。置信模式为 lcb95/ucb95 时，用 bootstrap 有放回重采样该统计量，
取 5%/95% 分位作为下/上界；样本数低于规则的 `minimum_samples`，或置信模式/分布统计量
缺乏分布证据（collector 快照只有点值）时，条件判为“未决”且规则不触发（fail-closed，不猜）。
该公式只用于 L5 内部的条件触发判定，不进入整体业务得分，也不替代 S7 的 LCB 接受判据。

## 7. 来源纠错与禁止表述

| 禁止表述 | 正确表述 |
|---|---|
| DCPerf 论文使用生产时间份额加权 | DCPerf 做生产校准并以归一化 benchmark 几何平均；Atrex 直接使用生产 wall-time 权重 |
| MESS 的 slope 就是时间上升率 | MESS slope 是 bandwidth-latency 曲线斜率；时间变化是项目扩展 |
| VGO 给出了统一动态优先级公式 | VGO 给出分布分类、feature association、mitigation 和复测方法，没有该统一公式 |
| TailBench++ 论文定义了唯一 SLO-Goodput 公式 | 尾延迟测试来自论文；SLO-Goodput 是导师综合建议 |
| 导师 J(x) 是某篇论文原式 | J(x) 是导师将可靠性、VGO、Atrex、CloudyBench 等综合出的 Looper 建议 |
| 几何平均可以覆盖所有门禁 | 几何平均只能聚合适当的正值分数，不能补偿正确性、SLO 或持久化失败 |
| 指标上升就是变坏 | 必须按 maximize、minimize、target、range 或 diagnostic-only 判断 |

## 8. 公式进入实现的前置条件

任一公式进入代码前 MUST 完成：

1. 确认来源类型和 formula_id。
2. 验证输入字段语义、单位、作用域和 workload 阶段。
3. 保存原始值，派生值引用公式版本和输入 digest。
4. 明确基线、近零、缺失、异常值和计数器重置处理。
5. 用目标环境实测采集开销和重复分布。
6. 对权重、阈值、lambda、置信方法和停止参数说明依据并经用户确认。
7. 添加方向、近零、缺失、门禁不可补偿和跨阶段拒绝测试。
8. 验证更换合理聚合公式时排名是否稳定，并报告 task/metric leverage。

在这些条件完成前，本文的 PROJECT-DRAFT 和 MENTOR-SYNTHESIS 公式只能用于规划与实验设计。

## 9. 当前结论

- 导师三层结构是当前总体评分架构的权威方向输入。
- 论文提供多个局部可复用公式，但没有一条可以覆盖通用调优、动态下钻、workload 业务收益和安全门禁。
- 组件内二维优先级是受 MESS/VGO 启发的项目扩展，尚无最终标量公式。
- workload 最终收益必须由场景合同定义，不能由系统微指标替代。
- 最终统一排序公式只有在门禁、指标语义、生产权重和统计校准完成后才能确认。
