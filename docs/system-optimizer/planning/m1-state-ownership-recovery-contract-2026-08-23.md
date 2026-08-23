# M1 配置状态、所有权与崩溃恢复合同（2026-08-23）

> 状态：implemented + Alibaba ECS KVM accepted；腾讯云 CVM 仍待独立复验
> 范围：离线或受控 Linux guest；不代表生产常驻控制器。

## 本次关闭的两个实现缺口

### 1. 持久化与所有权不能再由调用方随意塞字典

旧 `ManifestInventoryCollector` 接受 `persistent_values` 和 `ownership` 字典，但 CLI
没有真实发现链，字段在实际执行中基本不可用，也无法证明来源。

现在使用 `ConfigurationStateEvidence`：

- 同时绑定 target、manifest digest、环境指纹和采集时间。
- 保存显式 source scope、每个可解析 assignment、原文件 SHA-256、行号和 raw value。
- 对 manifest 每项给出 persistent/ownership disposition；缺记录即 unknown。
- source 文件只匹配 target 或 manifest 显式 `persistent_keys`。例如
  `/proc/sys/vm/swappiness` 与 `vm.swappiness` 的关系必须写入 manifest，采集器不猜测。
  重复声明保留全部证据并标 conflict，不臆测优先级。
- inventory 和 optimization run 均保存 state evidence digest。

自动采集发现单一外部声明时标记 `external-writer`，不会因为“当前值看起来像默认值”而
判为无人所有。操作者如确认受控目标可写，必须运行 `authorize-state`，逐项把运行时
写所有权授予具体 actor。声明绑定原 evidence digest；未列项目继续 fail-closed。

## 2. 过期租约不能再接受任意 reconciliation digest

过期租约接管证据现在内嵌：

- 原租约完整 digest。
- 现场读取的完整 actual `ConfigSnapshot`。
- 操作者提供的完整 expected `ConfigSnapshot`。
- outcome、reason 和时区明确的时间戳。

只有 target 相同、两个快照完整、内容 digest 相等且原租约绑定正确时，才允许
`matched-snapshot` 接管。CLI `reconcile-expired-lease` 现场读 actual；不一致会记录
needs-attention。`recover-attention` 只有在新的 actual 与 operator-approved snapshot
完整一致、且 recovery 绑定原 attention evidence 时才清除阻断。

现场复读严格采用 expected/approved snapshot 的 item 集合，并验证这些 item 都属于当前
manifest。这样既不遗漏历史事务实际修改的项目，也不会因为 manifest 后续包含额外未修改项
而产生集合不相等的假冲突。

## 命令链

1. `state-inventory`：读取所有显式列出的配置来源，输出原始 assignment 和逐项状态。
2. `authorize-state`：操作者逐项授权指定 actor，输出新的不可混淆 evidence digest。
3. `inventory`：把 current/desired/effective/persistent/ownership 与环境指纹统一报告。
4. `manual` 或 `run`：强制输入状态证据；target、manifest、当前主机指纹或 actor 不符即拒绝。
5. `reconcile-expired-lease`：过期租约接管前现场快照对账。
6. `recover-attention`：人工批准恢复状态后，现场复读一致才解除阻断。

## 明确未完成

- 不自动推断 sysctl.d、tuned、systemd、发行版脚本之间的最终优先级或语义等价关系。
- M1 官方候选的 `6 × 3 + IRQ + MTU = 20` 是文档计数；CVM 逐项存在性、权限、
  可写性、动态合法域和回滚仍必须重新实测。
- Alibaba ECS 已完成过期租约、快照集合不一致 fail-closed、attention 恢复、匹配后接管、
  真实 apply、故意 rollback failure、needs-attention 和 operator recovery 演练。这里模拟的
  crash 是遗留过期租约，不是向真实优化器进程发送 kill 信号；腾讯云 CVM 仍需独立复验。
- 本地全量 293 case 中出现与本次变更无关的 cloud confirmation token 测试失败；
  它在独立 basetemp 下发生，因此不能再只归因于共享 pytest 临时根目录。根因未分析，
  不在本 M1 修改中静默修复。
