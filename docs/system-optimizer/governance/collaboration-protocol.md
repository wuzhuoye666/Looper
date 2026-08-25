# System Optimizer 协作协议

> 状态：current；继承仓库 AGENTS.md，本文件只补充系统优化器专项规则。

## 方向与实现

- 会改变通用/场景边界、评分、字段、阈值或安全语义的决定，必须先讨论后实现。
- 架构重新基线期间，不以已有 M1 类型反向限制产品设计。
- 已有代码可复用，但必须通过新合同和验收重新证明。
- 发现 A 级结果错误或安全问题时立即停、报告并等待确认，不得先修后报。

## 多 Agent 工作树与集成权

- 每个 agent 只能在用户分配的独立 worktree/branch 内写文件；开工写入前必须执行
  `pwd`、`git worktree list`、`git status` 三联自证。路径或分支不符时立即停止。
- agent 不得向其他 worktree 投递文件、清理其他 agent 的临时产物，或在自己的分支
  merge/cherry-pick 主线以外的 agent 分支。跨 agent 协调只走用户中转和仓库台账。
- `system-optimizer-impl` 是唯一集成分支。工作 agent 不 push、不合并；主 agent
  `glm5.3` 负责只读验收、选择性 cherry-pick/重做、全量回归和统一 push。
- 每项任务必须在主 agent 维护的任务登记本（本地未跟踪文件，不入库）登记唯一
  task id、owner、worktree、基线 commit、依赖、写集合、验收命令和合入状态。没有
  登记不得开工；接口或写集合变化先更新台账，不用私聊约定替代记录。
- 工作分支采用依赖感知同步：若任务依赖、共享接口和写集合均未变化，开工前无需为了
  形式上的最新 commit 执行 rebase/merge；依赖或共享文件发生变化时才必须先同步。
  交付时若任务 commit 不是当前主线的可安全选择性集成后代，主 agent 再要求同步或
  重做。报告必须区分任务实际 parent、依赖基线和当前主线，不得把旧 commit 写成
  “当前主线”。主 agent 始终按实际 parent/diff 复核，不采信文字声明。

## 任务状态与交付记录

统一状态机为：

`proposed → assigned → in-progress → delivered → accepted/rework/rejected → integrated → pushed`

- `delivered` 只表示 agent 已交 commit，不表示主线接受。
- `accepted` 必须附主 agent 的代码审查结论和独立测试；`integrated` 必须附主线 commit；
  `pushed` 必须附远端分支与远端可见 commit。
- A 级异常将任务转为 `rework` 或 `rejected`，必须记录最小复现、影响范围和禁止合入
  原因；不能用新增测试全绿覆盖已知反例。
- 每个交付至少报告：实际基线、commit、完整文件列表、依赖变化、测试命令、通过/
  失败/未执行项、未跟踪文件、是否 commit/push/merge、已知限制。
- 主 agent 只集成任务 commit 的必要 diff；不顺带合入工作 agent 的 merge commit、临时
  目录或无关修改。用户已有和 agent 既有未跟踪内容保持不动。

## 测试隔离

- pytest 必须使用本轮独立 basetemp，不能共享默认临时根目录；并发 agent 的
  basetemp 名称必须包含 task id 或 agent id。
- 测试结论必须记录命令、basetemp、pytest 收集 case 数、通过数、失败数和是否存在
  并发进程。测试函数数、参数化 case 数和文件数不得混作一个口径。
- 共享临时根下的失败不能直接定性为 flaky；先用本轮独立 basetemp 隔离复跑，再区分
  环境争用和真实缺陷。
- agent 环境缺 pytest/Ruff 时必须如实写“未执行”，不得安装依赖或借用其他 agent
  worktree 写临时测试；主 agent 在集成工作树独立补验。

## GitHub 网络与推送

- 自 2026-08-24 起，GitHub 网络操作使用用户指定的本地代理端口 `65532`。代理主机和
  协议必须由用户确认后才能写入持久 Git 配置；确认前只允许在单次命令显式传入。
- 只有主 agent 可以执行集成分支 push。工作 agent 的交付报告必须明确“未 push”。

## 数据与指标

- 原始指标先全量保留，再讨论筛选、映射和等价性。
- 同名指标跨论文、benchmark 或 adapter 不默认语义相同。
- 采集开销没有实测前只能写“低开销目标”，不能写“已证明低开销”。
- 用户手动处理的数据在投入下游前先做字节级验证。

## 交付

每个里程碑至少交付：当前合同、实现、测试、运行证据、已知限制和 open decisions。文档完成不等于代码完成，单测通过不等于真实 Linux/CVM 已验证。
