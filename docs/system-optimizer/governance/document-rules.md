# System Optimizer 文档规范

> 状态：normative draft；适用于 docs/system-optimizer 下的当前文档。

## 状态词

- confirmed：用户已明确确认，可以成为实现输入。
- draft：有逻辑依据的设计建议，仍需评审。
- open：存在会改变行为的未决选择，不得落成实现默认值。
- unverified：尚无本项目实测证据。
- partial implementation：存在代码，但未满足完整新合同。
- superseded：曾经有效，已由后续决定替代。
- legacy：只供追溯，不是当前合同。

## 规范强度

- MUST：不满足会导致结果错误、安全风险或违反已确认边界。
- SHOULD：推荐行为；偏离时必须记录理由和影响。
- MAY：可选能力，不得被文档暗示为已经实现。

只有 confirmed 决定可以直接产生 MUST 产品行为。draft 和 open 不得伪装成默认值。

## 事实与推断

每项外部事实、测试数字、指标单位和能力声明必须能指向论文原文、仓库文件、命令输出或实测证据。没有证据时写 unverified 或 hypothesis。

以下内容必须分开：

- 论文宣称与本项目实测。
- synthetic fixture 与真实 workload。
- 接口已经编码与真实目标已经验证。
- 相关性与因果结论。
- best observed 与全局最优。

## 默认值与数据卫生

阈值、权重、统计方法、缺失处理、字段映射、样本计数和去重规则在写入代码前，必须记录选择逻辑、未验证情况和影响范围，并经用户确认。

指标资料遵守先拉全量再筛选。相同名称、ID 或 URL 不构成语义等价或内容重复的充分证据。

缺失关键目标或门禁指标时不得静默重分配权重。数据量、覆盖率或 merge 发生影响结果的异常时，按项目 A 级异常停止和报告。

## 变更记录

架构决定变化时必须：

1. 在 decision-log.md 保留原决定。
2. 标记 reopened 或 superseded。
3. 写明变化原因和影响模块。
4. 更新当前入口和受影响规范。
5. 代码尚未同步时显式写 partial implementation 或 contract mismatch。

重要聊天内容只记录技术结论、理由、反例、替代方案和未决项，不把未经确认的即时建议写成产品事实。
