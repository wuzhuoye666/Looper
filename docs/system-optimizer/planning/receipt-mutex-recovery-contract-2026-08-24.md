# Receipt Mutex 崩溃恢复合同（RCP-01-R2）

> 状态：**R2 待主 agent 复审**（不得视为 frozen；RCP-02A 不得在本稿通过前启动）。
> 基线：`origin/system-optimizer-impl@2e8621c`；本稿修订自 `a2fe0ca`（R1）。
> 归属：P0 receipt 正确性链第一环，见 `unfinished-task-queue-2026-08-24.md` §3/§4。
> 本稿只冻结恢复**合同**，不实现代码；不授权作者继续实施 RCP-02A/02B。

---

## 0. 结论摘要（R2 收敛，先读）

`DurableReceiptStore._mutex` 当前用 `O_CREAT | O_EXCL` 创建空 guard 文件、只在正常
`finally` 删除，进程崩溃会永久遗留 guard，导致同一 `(plan_digest, execution_id, operation)`
永久返回 "receipt chain is busy"（`intervention_receipt.py:114-130`）。

R2 冻结为**单一实现、无 fallback**：

- **唯一锁机制 = 进程级 OS advisory lock**：Linux `fcntl.flock(fd, LOCK_EX | LOCK_NB)`；
  Windows `msvcrt.locking(fd, LK_NBLCK, 1)`。锁文件 `.lock` **常驻不删除**，busy 由内核锁
  状态决定而非文件存在性决定，进程崩溃后内核自动释放——从根上消除"孤儿 guard 永久 busy"。
- **不自动退化到 owner guard**：网络文件系统、能力未知、或 advisory lock 不可证明时
  **直接 fail-closed**，不引入第二套锁协议。
- **不引入 stale timeout、TTL、PID liveness、自动孤儿判定**。
- **legacy 空 `.guard` 遗留**由**独立串行包 RCP-02B** 的人工/operator 流程显式恢复，绝不在
  RCP-02A 锁改造中静默处理，绝不自动清理。

---

## 1. R2 相对 R1 的修订点（逐条）

| # | R1 内容 | R2 修订 |
|---|---|---|
| 1 | 方案 A（advisory lock）为首选 + 方案 B（owner guard）为自动 fallback | **删除 owner guard 自动 fallback**，advisory lock 是唯一实现；未知文件系统直接 fail-closed |
| 2 | advisory lock 合同较简略，未明确 fd 生命周期/fork 语义 | **补全合同**：稳定 `.lock` 锚点、只 open 不 unlink、锁 fd 覆盖完整临界区、release=unlock→close、Linux/Windows 语义引用官方文档或标记平台测试确定 |
| 3 | 未区分常驻 lock 与孤儿 guard | **明确 `.lock` 是稳定 inode/path 锚点，允许常驻，不是孤儿 guard**；禁止在线单独删除 |
| 4 | 单一 RCP-02 包 | **拆成串行 RCP-02A（锁改造）→ RCP-02B（legacy guard 恢复）**，不允许同一 Agent 同时实现 |
| 5 | `ReceiptGuardRecord`（owner guard 模型）+ `ReceiptGuardReconciliation` | **删除 `ReceiptGuardRecord`**（不再有 owner guard）；只保留 `ReceiptGuardReconciliation` 并重新设计为 legacy guard 人工恢复证据 |
| 6 | legacy reconcile 顺序未冻结 | **冻结 9 步顺序 + 崩溃缝语义** |
| 7 | 声称"旧 guard 不阻塞无关 scope 正常写入" | **修正**：legacy guard 触发 target-level attention，该 target 所有新写被 `FileTargetGuard` 阻断 |
| 8 | 方案 B fallback 的"临时文件 os.replace + O_EXCL 改名"原子发布 | **删除**（无跨平台 rename-no-replace 原语） |
| 9 | 单一测试矩阵 | **拆分 RCP-02A（12 项）与 RCP-02B（9 项）**，分平台测试 |
| 10 | 核心问题列为"未决" | **收敛**：唯一实现、无 fallback、legacy 独立包、target 全阻断、lock 离线退役清理 |

---

## 2. 当前 mutex 的完整失败链

`intervention_receipt.py:114-130`：

```python
@contextmanager
def _mutex(self, plan_digest, execution_id, operation):
    identity = self._execution_digest(plan_digest, execution_id).removeprefix("sha256:")
    path = self.root / f".{identity}.{operation.value}.guard"
    try:
        descriptor = os.open(_native_path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise ReceiptStoreError("receipt chain is busy") from error
    try:
        os.close(descriptor)
        yield
    finally:
        if os.path.exists(_native_path(path)):
            os.unlink(_native_path(path))
```

guard 是**空文件**，scope = `(plan_digest, execution_id, operation)`，identity =
`canonical_digest({"execution_id", "plan_digest"})`。调用点只有 `start`
（`intervention_receipt.py:397`）和 `advance`（`:435`），且**只包裹 receipt 链的读-改-写
临界区**，不包裹 backend apply/rollback/复测（后者在
`TwoStageSafetyBackedIntervention._run_observed` `dynamic_adapters.py:779-821` 的
`execute_observed` 阶段进行）。

| # | 场景 | 当前行为 | 是否缺陷 |
|---|---|---|---|
| 1 | 正常竞争（同 scope 两 writer 同时到达） | 一个 `O_EXCL` 成功，另一个 `FileExistsError → busy` | ✅ 正确 |
| 2 | writer 在临界区崩溃（`yield` 内进程终止） | guard 永久遗留，同 scope 永久 busy | 🔴 缺陷（本任务目标） |
| 3 | PID 复用 | 无 owner 信息，无从判断 | 🔴 缺陷（无 liveness 证据） |
| 4 | guard 文件损坏/权限异常 | 非 `FileExistsError` 未包装，上下文丢失 | 🟡 需收紧异常类型 |
| 5 | guard 内容成功但释放前崩溃（=#2） | 同 #2 | 🔴 缺陷 |
| 6 | 同 execution 重放/恢复 | `start` 被 guard 或 "already exists" 挡下，无法区分 | 🔴 需明确恢复路径 |
| 7 | 不同 execution/operation 并发 | identity 不同，互不影响 | ✅ 正确 |

---

## 3. 冻结方案：单一 OS advisory lock（无 fallback）

RCP-02A 的锁实现**只有一条路径**：

1. **主实现 = OS advisory lock**：
   - Linux：`fcntl.flock(fd, LOCK_EX | LOCK_NB)`；
   - Windows：`msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`。
2. **只承诺经过验收的本地文件系统**（本机 ext4/xfs/btrfs/NTFS 等）。
3. **网络文件系统、能力未知、或 advisory lock 不可证明时直接 fail-closed**：在 store 初始化
   或首次获取锁时显式探测（见 §4.7），探测失败即抛 `ReceiptStoreError`，**不写入、不降级**。
4. **不自动退化到 owner guard**：不存在第二套锁协议。
5. **不引入 stale timeout、TTL、PID liveness、自动孤儿判定**。

理由：owner guard fallback 引入 boot/session/PID reuse/reconcile 第二套锁协议，且**不能解决
未知分布式文件系统上的互斥真实性**——一个无法证明 advisory lock 生效的文件系统，同样无法
证明 O_EXCL/owner guard 生效。因此唯一诚实的语义是"要么本机文件系统的内核锁，要么拒绝"。

---

## 4. Advisory lock 合同（RCP-02A 必须逐条满足）

### 4.1 稳定锁锚点

- 每个 `(plan_digest, execution_id, operation)` 使用**稳定** `.{identity}.{operation}.lock`
  锚点，identity = `canonical_digest({"execution_id", "plan_digest"})` 的 hex。
- `.lock` 文件在 store 首次使用时以普通 `O_CREAT`（**非 EXCL**）创建，若不存在则写入一个
  哨兵字节（Windows `msvcrt.locking` 需要文件非空，见 §4.6）。

### 4.2 只 open，不 unlink

- **lock 文件只 `open`，正常 release 时不 `unlink`**。锁绑定到 inode（open file description，
  OFD）；一旦删除并重建同名文件，后续 `open` 得到新 inode，锁互斥被绕过。因此 `.lock` 是
  不可变的路径锚点，`O_EXCL` guard 时代"删除即释放"的语义**被废弃**。

### 4.3 busy 由内核锁状态决定

- busy 判定 = 内核锁获取失败（Linux `EWOULDBLOCK` / Windows `LK_NBLCK` 抛 `OSError`），
  **不由文件存在性决定**。`.lock` 文件存在但无活跃锁时，后续 `flock` 正常成功。

### 4.4 锁后重验 receipt head（防 stale-head）

- 获取锁后、执行任何链修改前，**必须重新读取并验证该 scope 的 receipt head**（复用现有
  `head()` / `verify_chain()`），确认当前持有的是最新链头，防止"拿到锁时链已被前一个崩溃
  前的 writer 推进"导致的 stale-head 写入。这与现有 `advance` 的
  "cannot advance a stale receipt head"（`intervention_receipt.py:452`）合并为一处强校验。

### 4.5 锁 fd 覆盖完整临界区

- 锁 fd 必须在 `_mutex` 的 `yield` 之前获取、在 `yield` 退出后释放，**覆盖完整临界区**
  （check head → 校验 → `_publish_receipt`）。
- 临界区内**不得再次获取同一 scope 的锁**（flock 对同 OFD 重复 flock 会改变锁语义；对独立
  OFD 会 EWOULDBLOCK 死锁，见 §4.8）。当前 `start`/`advance` 不嵌套调用 `_mutex`，RCP-02A
  必须保持这一性质并加注释/断言。

### 4.6 Windows 哨兵字节

- Windows `msvcrt.locking` 是字节范围锁，锁文件**至少 1 字节**。RCP-02A 创建 `.lock` 时写
  入一个固定哨兵字节；Linux `flock` 不要求内容，但为统一，两平台都写该哨兵字节。

### 4.7 文件系统能力探测

- RCP-02A 在 store 初始化或首次获取锁时做**显式能力探测**：尝试对 `.lock` 加锁/解锁，并在
  同一进程用**第二个独立 fd** 再尝试加锁，确认会得到 busy（互斥真实生效）。探测失败、或
  检测到网络/未知文件系统（无法证明 flock 语义）时**直接 fail-closed**，不写入。
- 能力探测结果不得缓存为"永久可用"；每次 store 实例化重探（低成本）。

### 4.8 Linux 语义（引用官方文档，不凭印象）

依据 `flock(2)`（Linux man-pages）：

- flock 锁与**打开文件描述（OFD）**关联，不是与进程或 fd 关联。
- `fork()` 后子进程继承父进程的 OFD 与 fd 表副本：**父子共享同一把锁，不互斥**。
- `dup(2)`/`dup2(2)`/`fcntl(F_DUPFD)` 复制的 fd 引用同一 OFD：**共享锁**。
- 对同一文件的**两次独立 `open()` 得到两个 OFD，彼此互斥**：因此同一进程对同一文件二次
  `open` + `flock(LOCK_EX|LOCK_NB)` 会返回 `EWOULDBLOCK`。
- 锁在**所有引用该 OFD 的 fd 都关闭**时释放（或进程退出时释放）。
- `flock` 与 `fcntl` 的 POSIX record lock **互不交互**（两套独立锁）。
- NFS：自 Linux 2.6.12 起 flock 在 NFS 上由 fcntl POSIX lock 模拟（历史语义漂移）；**RCP-02A
  不承诺 NFS**，见 §4.7。

**标记为必须由平台测试确定**（不凭印象写进验收）：同进程内重复 `open` 互斥的精确行为、fork
后父子共享锁的跨实现差异、`LOCK_NB` 失败返回 `EWOULDBLOCK` 还是 `EAGAIN` 的移植差异——这些由
RCP-02A 的真实 Linux 测试钉死，不写为文档断言。

### 4.9 Windows 语义（引用官方文档 + 真实进程测试）

依据 Microsoft `msvcrt.locking` 文档：

- `msvcrt.locking(fd, mode, nbytes)` 对 fd 锁 `nbytes` 字节；`LK_NBLCK` 为**非阻塞**尝试，失败
  立即抛 `OSError`。
- 锁是字节范围锁，文件需可写且长度 ≥ `nbytes`。
- 进程退出后锁由操作系统释放。

**标记为必须由平台测试确定**：`LK_NBLCK` 失败的异常类型/errno、同一进程二次 `open` 互斥
行为、fork/子进程继承语义、锁文件被删除后的行为——由 RCP-02A 的真实 Windows 测试钉死。

### 4.10 release 顺序

- release 固定为 **unlock → close**（先显式 `LK_UNLCK`/`LOCK_UN` 解锁，再 `close` fd），
  不依赖"close 即释放"的隐式语义（Windows 尤其需要显式 `LK_UNLCK`）。

---

## 5. 常驻 `.lock` 文件不是孤儿 guard

- **`.lock` 是稳定 inode/path 锚点，允许常驻**；它是锁的载体，不是"持锁标记"，因此不需要
  "清理"来释放锁。
- **数量有界**：`.lock` 数量最多与 receipt operation scope 同阶（每
  plan+execution+operation 一个），不是无限临时文件泄漏。
- **禁止单独在线删除某个 `.lock`**：在线删除会制造旧 inode/新 inode 双 writer 绕过互斥。
- **只能在 receipt store 已离线、确认无 writer、准备归档/删除整个 store 时统一清理**。
- **store retention 属后续显式生命周期输入**，不在 RCP-02 设置默认 TTL/保留期。
- 测试**不得要求 `.lock` 文件不存在**；只要求"当前可重新获取且无活跃锁"。

---

## 6. 必须保持的既有 receipt 语义（RCP-02A 不得破坏）

1. immutable content-addressed nodes（`intervention_receipt.py:111-112`、`143-152`）。
2. candidate/recovery pointer 独立（`:105-109`）。
3. predecessor 单 successor（`:286-299`、`:195-198`）。
4. pointer 可落后于唯一内容链头（`:322-329`）。
5. 不自动 replay backend apply/rollback（`d5-i2-...-design` §4.3、§11.8）。
6. post-apply 非终态 → needs-attention（`cli.py:1287-1324`）。

---

## 7. legacy `.guard` 恢复证据（重新设计，RCP-02B）

旧 `.guard` 是**空文件**，字节上不含 plan_digest/execution_id/operation 的明文，但**文件名
本身编码了 identity 与 operation**：`.{identity_hex}.{operation}.guard`，其中 identity_hex =
`execution_digest` 的 hex（`canonical_digest({"execution_id", "plan_digest"})`）。

因此恢复证据**只能直接声明并绑定可观察事实**，不能从空文件反推 plan/execution：

`ReceiptGuardReconciliation`（`looper.receipt-guard-reconciliation/v1alpha1`）字段：

1. `guard_filename`：guard 的**规范化纯文件名**（严格 `.`+64hex+`.`+operation+`.guard`）。
2. `execution_digest`：从 `guard_filename` 解析出的 identity hex，还原为
   `sha256:<64hex>`。
3. `operation`：从 `guard_filename` 解析（`candidate` | `recovery`）。
4. `guard_sha256`：guard 文件**字节级 sha256**（空文件也有确定 digest，用于绑定被清理的那份）。
5. `receipt_root`：receipt store root 的身份（路径 + 若可得其可观测身份）。
6. `discovered_at`：发现时间。
7. `target_id`：关联目标（operator/CLI 提供）。
8. `operator_id`：operator/reconciler 身份。
9. `writer_quiescence`：operator 的**显式声明**"旧版本 writer 已全部停止"（布尔 + 声明文本）。
10. `chain_head_digest`：相关 receipt chain/head digest（若存在；无法建立关联时为 null）。
11. `outcome`：恢复结果（`ORPHAN_CONFIRMED` | `NEEDS_ATTENTION`）。
12. `reason`：结果与理由。

**plan_digest + execution_id 关联规则**：

- 若 operator 提供 `plan_digest` + `execution_id`：**必须重算
  `execution_digest = canonical_digest({"execution_id", "plan_digest"})`，并等于 guard 文件名
  里的 execution digest**；不匹配 → fail-closed。
- 若无法建立关联（文件名解析失败、plan/execution 未知、或链缺失）：**不得清理**，标记
  target needs-attention。

---

## 8. legacy reconcile 顺序（冻结，RCP-02B）

1. CLI 先确认目标处于 needs-attention，或通过 `FileTargetGuard.mark_needs_attention` 建立
   attention。
2. operator **显式确认旧版本 writer 已全部停止**（quiescence 声明）。
3. 在新 advisory lock 下读取 legacy guard（防止与任何仍在写的旧进程竞态）。
4. 验证 guard 文件名、字节 sha256、相关 receipt 链/head。
5. **先原子持久化内容寻址 `ReceiptGuardReconciliation` 证据**（tmp + `os.replace`，复用
   `_atomic_write` 模式）。
6. **再删除 legacy `.guard`**。
7. 删除后重验 receipt 链。
8. 通过既有 `TargetRecoveryEvidence` 清除 attention（复用 `lease.py:253-263` 的
   `clear_attention` 边界）。
9. 任一步失败保持或重新进入 needs-attention。

**崩溃缝语义**：

- **evidence 已写、guard 未删**：允许幂等重试（evidence 是内容寻址，重复写同一内容幂等）。
- **guard 已删、attention 未清**：依据已落盘 evidence 继续恢复（evidence 先于删除是前提）。
- **不允许先删 guard 后写 evidence**：先写证据是硬序。

**不能直接复用 `reconcile-expired-lease`**（`cli.py:461`）：lease reconciliation 与 receipt
guard reconciliation 是**不同证据合同**（前者对账配置快照，后者对账空 guard 文件）；只能
**复用 `FileTargetGuard` 的 attention/recovery 边界**（`mark_needs_attention` /
`clear_attention`），不复制其 lease TTL/reconciliation 语义。

---

## 9. 作用域与 target attention（修正 R1 矛盾）

- mutex 互斥作用域 = **单 plan+execution+operation**（§4.1）。
- legacy guard 被发现后，CLI 标记的是 **target-level attention**（
  `FileTargetGuard.mark_needs_attention`）。
- **一旦 target attention 建立，该目标所有新写都会被 `FileTargetGuard` 阻断**
  （`lease.py:164-168` 的 `assert_writable`）。
- 因此**不能声称"旧 guard 只影响它自己那个 scope、无关 scope 正常写入"**：target attention
  是全目标阻断。
- **其它 target、其它 session 不受影响**（attention 按 target_id 隔离，lease 按 target_id 隔离）。

---

## 10. RCP-02A / RCP-02B 写集合（串行）

### RCP-02A：未来崩溃安全锁

- 依赖：本稿（RCP-01-R2）通过。
- 写集合：
  - `packages/core/looper_core/system_opt/intervention_receipt.py`
  - 新增 receipt concurrency 测试（`tests/test_system_opt_intervention_receipt_concurrency.py`
    或等价）。
- 内容：advisory lock（§4）、local filesystem 支持边界探测、线程/进程竞争、持锁进程强制
  退出、不同 scope 并行、stale-head 重验、pointer 完全缺失重建测试。
- 不可改：`intervention.py`、`safety.py`、`dynamic_adapters.py`、`dynamic_loop.py`、`cli.py`、
  `lease.py`、任何既有测试、云端证据与 `.artifacts/`。

### RCP-02B：legacy `.guard` 显式恢复

- 依赖：RCP-02A。
- 写集合：
  - `packages/core/looper_core/system_opt/intervention_receipt.py`
  - `services/api/looper_api/cli.py`
  - receipt/CLI 专属测试
  - 必要的新恢复证据模型（`ReceiptGuardReconciliation`，§7）
- 内容：legacy guard 发现、`ReceiptGuardReconciliation` 模型、冻结 9 步 reconcile 顺序
  （§8）、target attention 边界复用。

**RCP-02A 与 RCP-02B 必须串行，不允许一个 Agent 同时实现**（02A 的锁改造是 02B 恢复流程
的安全前提，02B 的 CLI 入口依赖 02A 的锁已落地）。

---

## 11. 测试矩阵

### RCP-02A（至少覆盖，分平台）

| # | 用例 | 预期 |
|---|---|---|
| 1 | 同 scope 两线程竞争 | 恰好一个进入，另一个 `ReceiptStoreError("busy")` |
| 2 | 同 scope 两独立进程竞争 | 同上（`multiprocessing`/subprocess，不共享 fd） |
| 3 | 持锁进程 `terminate` 后可重新获取 | 锁自动释放；无遗留 busy |
| 4 | 不同 execution 并行 | 互不阻塞（`.lock` 按 identity+operation 隔离） |
| 5 | candidate 与 recovery 并行（同 execution） | 互不阻塞 |
| 6 | 锁内 head 重验 | 拿到锁后重读 head，stale-head 写入被拒 |
| 7 | pointer 完全缺失 | 沿前驱链重建唯一 head |
| 8 | pointer 指祖先 | 唯一内容链头优先，下次写重发 pointer |
| 9 | 分叉/断链 | fail-closed |
| 10 | `.lock` 常驻但无活跃锁 | 测试结束 `.lock` 存在（允许），且可重新获取、无活跃锁 |
| 11 | Windows 长路径 | `_native_path` 的 `\\?\` 前缀下锁正常 |
| 12 | Linux 与 Windows 分平台 | **不得在单平台伪报另一平台通过**；两平台各有真实进程测试 |

### RCP-02B（至少覆盖）

| # | 用例 | 预期 |
|---|---|---|
| 1 | legacy 空 guard 首次发现 | 标记 target attention，不自动清理 |
| 2 | target attention 阻断新 lease | 该 target 新写被 `FileTargetGuard` 阻断 |
| 3 | 未提供 operator quiescence 声明 | 拒绝清理 |
| 4 | execution digest 重算不一致 | fail-closed，不清理 |
| 5 | evidence 先落盘 | 删除 guard 前 evidence 已存在 |
| 6 | evidence 后 guard 删除失败 | 可幂等重试 |
| 7 | guard 删除后 attention 清理失败 | 依已落盘 evidence 继续 |
| 8 | 相关 receipt 链损坏 | 不删除 guard，保持 needs-attention |
| 9 | 完整恢复后 attention 才能清除 | `clear_attention` 只在全部步骤成功后调用 |

---

## 12. RCP-02A / RCP-02B / RCP-03 边界

- **RCP-02A 只解决并发与崩溃正确性**：advisory lock + local filesystem 边界 + 竞争/崩溃
  测试。不碰 legacy guard、不碰 CLI。
- **RCP-02B 只解决 legacy `.guard` 显式恢复**：发现、证据、reconcile 顺序、attention 边界。
- **`_all_receipts()` 全局 O(N) 重扫 → O(N²) 属于 RCP-03**（
  `unfinished-task-queue-2026-08-24.md` L24、§4 RCP-03）。
- **不得在任何一包中改变"其它 scope 损坏是否全局阻断"的安全语义**：当前"任一 scope 损坏即
  全局 fail-closed"是保守安全语义，局部索引/忽略是 RCP-03 在冻结真实性边界后的事。

---

## 13. 已收敛的决策（不再是未决问题）

R2 以下核心问题**已冻结，RCP-02A/02B 实现者不得再以"待定"为由自行选择**：

1. **advisory lock 是唯一实现**；未知/网络文件系统直接 fail-closed，不探测降级。
2. **不做 owner guard fallback**；不引入 boot/session/PID liveness/自动孤儿判定。
3. **legacy 恢复由独立串行 RCP-02B 的 CLI/operator 流程完成**，不在 RCP-02A 处理。
4. **target attention 是全目标阻断**；发现 legacy guard 后该 target 所有新写被阻断。
5. **常驻 `.lock` 只在 store 离线退役时统一清理**；不设默认 TTL/保留期。

---

## 14. 剩余外部问题（可保留，不阻塞 RCP-02A）

1. **store retention 生命周期**：receipt store 何时归档/退役、`_all_receipts` 与 `.lock` 的
   离线清理时机，属后续显式生命周期输入，不在 RCP-02A/02B 设默认。
2. **`ReceiptGuardReconciliation` 的 schema 字段定稿**：需主 agent / 用户逐字段过目（按
   字段登记纪律），但字段语义已由 §7 冻结。
3. **平台语义的官方依据锚定**：Linux `flock(2)` 与 Windows `msvcrt.locking` 的 fork/重入/
   异常行为，已在本稿引用官方文档并标记"必须由 RCP-02A 真实进程测试钉死"；不阻塞设计，但
   是 RCP-02A 验收的硬前置。

---

## 15. 引用校验记录（本稿重新 grep 核实，非沿用旧行号）

- `intervention_receipt.py`：`_mutex` 114-130、`_atomic_write` 132-141、`_read_receipt`
  143-152、`_pointer_path` 105-109、`_content_path` 111-112、`_publish_receipt` 367-387、
  `start` 389-413（mutex@397）、`advance` 415-454（mutex@435、stale-head@452）、
  `head` 265-329（content-before-pointer seam@322-329）、`_all_receipts` 163-199。
- `intervention.py`：`InterventionExecutionReceiptV2` 201-291、`ReceiptStageV2` 178-186、
  `ReceiptOperation` 188-190。
- `lease.py`：`FileTargetGuard` 117-253、`_mutex` 135-147、`assert_writable` 164-168、
  `mark_needs_attention` 238-251、`clear_attention` 253-263、`TargetLease` 28-38。
- `dynamic_adapters.py`：`TwoStageSafetyBackedIntervention._run_observed` 779-821。
- `cli.py`：`DurableReceiptStore` 引用 1287、启动扫描 1289-1324、`attention_sink` 1367、
  注入 1383-1384、`reconcile-expired-lease` 461。
- 设计来源：`d5-i2-runtime-wiring-design-2026-08-24.md` §4.2/§4.3/§8.3/§10/§11；
  `unfinished-task-queue-2026-08-24.md` §2（L22-24）、§3（RCP 链）、§4（RCP-01/02/03）、
  §6（L171 无默认超时）。
