# Receipt Mutex 崩溃恢复合同（RCP-01）

> 状态：**R1 待主 agent 复审**（不得视为 frozen；RCP-02 不得在本稿通过前启动）。
> 基线：`origin/system-optimizer-impl@2e8621c`。
> 归属：P0 receipt 正确性链第一环，见 `unfinished-task-queue-2026-08-24.md` §3/§4。
> 本稿只冻结恢复**合同**，不实现代码；不授权作者继续实施 RCP-02。

---

## 0. 结论摘要（先读）

`DurableReceiptStore._mutex` 当前用 `O_CREAT | O_EXCL` 创建空 guard 文件、只在正常
`finally` 删除，进程崩溃会永久遗留 guard，导致同一 `(plan_digest, execution_id, operation)`
永久返回 "receipt chain is busy"（`intervention_receipt.py:114-130`）。

推荐**分层方案**，两条腿都**不引入任何隐式 stale timeout / TTL / 自动清理**：

1. **首选：进程级 advisory lock（OS 内核管理，进程退出自动释放）**。Linux 用
   `fcntl.flock(fd, LOCK_EX | LOCK_NB)`；Windows 用 `msvcrt.locking` 对锁文件首字节加
   非阻塞锁。锁文件**常驻不删除**，"busy" 由内核锁状态决定、不是文件存在性决定，因此
   "崩溃遗留"在正确性上被内核消除，不需要 owner 存活探测、不需要超时。
2. **fallback + 旧 `.guard` 兼容：带 owner/process/boot/session 身份的 guard + 显式
   reconciliation**。仅在 OS advisory lock 不可用的文件系统上启用；遗留 guard 一律
   fail-closed + 显式 reconcile，**绝不静默删除、绝不自动清理**。
3. **明确排除：guard expiry / lease（TTL）模型**。receipt 是 immutable
   content-addressed 日志，没有自然"过期"概念；TTL 会误伤一个合法但慢的临界区，且违反
   "未确认的 stale timeout 不得写默认值"（`unfinished-task-queue-2026-08-24.md` L171、
   `d5-i2-runtime-wiring-design-2026-08-24.md` §10）。

---

## 1. 当前 mutex 的完整失败链

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

guard 是**空文件**（`os.close` 后未写任何内容），scope = `(plan_digest, execution_id,
operation)`，identity = `canonical_digest({"execution_id", "plan_digest"})`。调用点只有
`start`（`intervention_receipt.py:397`）和 `advance`（`:435`），且**只包裹 receipt 链的
读-改-写临界区**（check head → 校验 → `_publish_receipt`），**不包裹 backend
apply/rollback/复测**——后者的执行在 `TwoStageSafetyBackedIntervention._run_observed`
（`dynamic_adapters.py:779-821`）里 `execute_observed` 阶段进行，每次 progress 回调再短促
`advance`。因此崩溃遗留 guard 的窗口是"临界区内崩溃"，不是"整个干预执行期崩溃"。

逐场景：

| # | 场景 | 当前行为 | 是否缺陷 |
|---|---|---|---|
| 1 | 正常竞争（同 scope 两 writer 同时到达） | 一个 `O_EXCL` 成功，另一个 `FileExistsError → busy` | ✅ 正确（恰好一个 writer） |
| 2 | writer 在临界区崩溃（`yield` 内进程终止，`finally` 不执行） | guard 永久遗留，同 scope 永久 busy | 🔴 缺陷（本任务目标） |
| 3 | PID 复用（崩溃进程的 pid 被新进程复用） | 无 owner 信息，无从判断；guard 仍在 | 🔴 缺陷（无法判定 owner 死活） |
| 4 | guard 文件损坏 / 半写 / 权限异常 | `os.open` 或 `os.unlink` 抛非 `FileExistsError`，未包装为 `ReceiptStoreError`，上下文丢失 | 🟡 需收紧（异常类型） |
| 5 | guard 内容成功但释放前崩溃（= #2 的同义变体） | 同 #2 | 🔴 缺陷 |
| 6 | 同 execution 重放 / 恢复（进程重启后重新执行同一 window） | `start` 被 guard 或 "already exists" 挡下，无法区分"崩溃遗留"与"链已存在" | 🔴 需明确恢复路径 |
| 7 | 不同 execution / 不同 operation 并发 | identity 不同，guard 路径不同，互不影响 | ✅ 正确 |

关键事实：guard **没有 owner 身份、没有 liveness 证据、没有 expiry、没有 reconciliation
入口**，因此一旦遗留，系统没有任何可证明的依据去安全清理它——这正是必须冻结合同的原因。

---

## 2. 三种方案比较

### 方案 A：OS advisory lock（进程级，内核自动释放）

- 机制：对锁文件加进程级非阻塞排它锁。Linux `fcntl.flock(fd, LOCK_EX | LOCK_NB)`；
  Windows `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`（锁文件至少 1 字节）。
- 崩溃语义：进程退出（含 `kill -9`、崩溃）后内核**自动释放**锁；文件仍在但不表示 busy。
- 优点：**无需 owner 存活探测、无需 TTL、无需 reconciliation 判定孤儿**——内核是正确性
  的信任根。锁文件常驻不删除，消除"删除 vs 持有"的竞态。
- 缺点：跨平台语义差异大（§4）；某些文件系统不承诺（NFS 旧版、FAT、某些 SMB）。
- 是否违反"不设隐式 timeout"：**否**，完全不涉及时间。

### 方案 B：带 owner/process/boot/session 身份的 guard + 显式 reconciliation

- 机制：guard 文件写入 owner 身份（pid + boot_id/session_id + 时间戳 + scope digest）。
  发现遗留 guard 时，读 owner 身份判断其是否存活；已死则走显式 reconcile 后清理。
- 优点：可审计、可绑定证据、跨文件系统一致。
- 缺点：需要 boot_id/session 身份 + 存活探测（pid 复用需 boot_id 消除歧义）+ reconcile
  合同（复杂）；"判定孤儿"本身需要授权与证据，不能自动。
- 是否违反"不设隐式 timeout"：**否**（存活探测基于进程存在性 + boot 身份，不是时间）。

### 方案 C：guard expiry / lease（TTL）模型

- 机制：guard 带 `expires_at`，过期即可接管（复用 `lease.py:TargetLease` 的 TTL 模式）。
- 优点：实现简单、复用现有 lease 概念。
- 缺点：**必须引入 stale timeout/TTL**。receipt 是 immutable 日志，无自然过期语义；一个
  合法但慢的临界区可能被误判过期，导致两个 writer 同时进入——**直接违反
  `unfinished-task-queue-2026-08-24.md` L171 与 d5-i2 设计 §10 的硬约束**。
- 结论：**排除**。

### 比较结论

| 维度 | A（OS advisory lock） | B（owner guard + reconcile） | C（expiry/lease） |
|---|---|---|---|
| 崩溃后自动释放 | ✅ 内核保证 | ❌ 需显式 reconcile | ⚠️ 需 TTL（禁止） |
| 无隐式 timeout | ✅ | ✅ | ❌ 必然要 TTL |
| 跨平台一致性 | ⚠️ 语义差异大 | ✅ 一致 | ✅ 一致 |
| 跨文件系统 | ⚠️ 部分不承诺 | ✅ 本机文件系统即可 | ✅ |
| 可审计/证据绑定 | ⚠️ 锁状态不在文件里 | ✅ 完整证据 | ✅ |
| 复杂度 | 低 | 高 | 中 |

**推荐 A 为首选、B 为受限 fallback，排除 C。**

---

## 3. 推荐方案（分层，无隐式超时）

RCP-02 实现按以下优先级选择，**两条腿都 fail-closed**：

1. **主路径（Linux / 本机文件系统 / 支持 flock 的 POSIX 文件系统）**：对每个 scope 的
   **常驻锁文件** `.{identity}.{operation}.lock` 加 `flock(LOCK_EX | LOCK_NB)`。锁文件在
   store 首次使用时 `mkdir` + 若不存在则以普通 `O_CREAT`（非 EXCL）创建并保留一个字节
   （Windows `msvcrt.locking` 需要非空）。**锁文件从不删除**。获取失败（`EWOULDBLOCK` /
   Windows `OSError EACCES/EDEADLK`）→ `ReceiptStoreError("receipt chain is busy")`。
2. **fallback（不支持 flock 的文件系统被显式检测到）**：退化到方案 B 的 owner guard
   （§6），绝不静默退化——检测到不支持时要么报错、要么显式走 owner guard 并落盘身份。
3. **旧 `.guard` 遗留（本次升级前已存在）**：§7 的 fail-closed 迁移路径，**不自动清理**。

**为什么锁文件必须常驻、不能被删除**：`flock` 的锁绑定到 inode（open file description），
一旦有人删除并重建同名文件，后续 `open` 得到新 inode，锁互斥被绕过。因此锁文件是
**不可变的路径锚点**，只 `open` 不 `unlink`；旧 `O_EXCL` guard 的"删除即释放"语义在方案 A
中**被废弃**。

---

## 4. Windows 与 Linux 语义（必须写进 RCP-02 验收）

| 维度 | Linux（`fcntl.flock`） | Windows（`msvcrt.locking`） |
|---|---|---|
| 进程退出后锁是否自动释放 | ✅ 内核释放（进程终止即释放） | ✅ 内核释放 |
| fd 关闭是否释放锁 | ✅ 同一进程任一 fd 关闭即释放该 flock | ⚠️ `msvcrt.locking` 是字节范围锁，`_locking` 需要显式 `LK_UNLCK` 解锁；但进程退出仍自动释放 |
| 锁文件是否允许删除 | ⚠️ 允许删除但会破坏互斥（inode 失效），RCP-02 禁止删除 | 同左 |
| 锁文件最小字节 | 可为空（flock 不要求内容） | **必须 ≥1 字节**（RCP-02 写一个哨兵字节） |
| fork 语义 | flock 与 open file description 绑定，fork 后父子共享同锁（**不产生互斥**） | 不适用 |
| 同进程内重入（同一线程再次 `_mutex`） | flock 非重入（同 fd 重复 flock 可能死锁/直接失败） | 同左 |

**明确不承诺 / 需 fail-closed 的文件系统边界**：

- NFS：`flock` 在 NFSv3 及更早**不可靠**（历史上映射到 fcntl POSIX lock，语义漂移）；
  NFSv4 支持但依赖服务器实现。**RCP-02 不得在无法证明语义的文件系统上宣称正确性**；检测到
  网络文件系统时走方案 B 的 owner guard 或直接拒绝，**不承诺自动正确**。
- FAT/exFAT、部分 SMB 挂载、内存盘 tmpfs 之外的非 POSIX 文件系统：不承诺。
- 上述边界必须在 RCP-02 以**显式能力探测 + 文档记录**落盘，不得静默假设本地盘语义。

---

## 5. 恢复授权（方案 B fallback 与旧 guard 共用）

> 方案 A 主路径无"孤儿"概念（内核保证），本节的授权只约束**方案 B 的 owner guard** 与
> **旧 `.guard` 遗留**。

- **谁能判断 guard 已孤儿**：只有显式 reconcile 流程（operator 或主 agent 授权的对账
  入口），**不是任何写进程的自动逻辑**。写进程遇到遗留 guard 时只有两个动作：
  `busy`（owner 存活）或 `needs-attention`（无法判定），**不得自行清理**。
- **owner/process/boot/session 身份**：guard 内容必须携带 `pid`、`boot_id`（Linux
  `/proc/sys/kernel/random/boot_id` 或等价）、`session_id`（Windows 会话/`os.getppid` 链不
  足时用进程启动时间 + 随机 nonce）、`hostname`、`created_at`。`pid` 复用用 `boot_id` +
  `process_start_time` 消除歧义（同一 boot 内 pid 才可能复用；跨 boot 由 boot_id 区分）。
- **是否必须有 reconciliation evidence**：**是**。判定"owner 已死"必须同时满足
  (a) owner 进程在相同 boot_id/session 下不存在（存活探测）、(b) 链状态可验证（见下）。
  任一不满足 → fail-closed + needs-attention。
- **evidence digest 如何绑定**：reconciliation 记录必须内容寻址，digest 覆盖
  `plan_digest`、`execution_id`、`operation`、`旧 guard 的身份字段`（pid/boot_id/session_id/
  created_at）、`guard 文件字节 digest`、`判定时刻`、`reconciler 身份`。旧 guard 文件本身在
  reconcile 成功前必须被字节级保存（先复制取证，再清理），reconciliation digest 绑定被清理
  的那份字节。
- **什么情况只能 fail-closed**：owner 存活探测不确定、boot/session 身份缺失、链状态无法
  唯一验证、reconciliation 证据缺失/不完整、或任何"删除可信 receipt"的风险 → 一律
  fail-closed，转既有 `lease.py` 的 needs-attention / `TargetReconciliation` 路径，等待人工。

---

## 6. 必须保持的既有 receipt 语义（RCP-02 不得破坏）

以下不变量逐条冻结，RCP-02 的锁改造**只换锁机制、不改链语义**：

1. **immutable content-addressed nodes**：`<receipt_digest_hex>.json`，digest 可重算
   （`intervention_receipt.py:111-112`、`_read_receipt:143-152`）。
2. **candidate / recovery pointer 独立**：`<execution_digest>.<operation>.current.json`
   （`:105-109`），互不覆盖。
3. **predecessor 单 successor**：分叉（同 predecessor 两个不同 digest）fail-closed
   （`:286-299`、`:195-198`）。
4. **pointer 可落后于唯一内容链头**：内容已发布、pointer 仍指合法祖先 = 可恢复缝，链头
   优先、下次写重发 pointer（`:322-329`）。
5. **不自动 replay backend apply/rollback**：durable receipt 是执行到达哪一安全边界的
   证据，不是 crash 自动回滚日志（`d5-i2-...-design` §4.3、§11.8）。
6. **post-apply 非终态 → needs-attention**：重启发现 `APPLY_STARTED` 及之后但尚未
   `OPERATION_TERMINAL` 的 head → 阻止新执行 + attention + 转 lease/state reconcile，不自动
   恢复（`cli.py:1287-1324`）。

锁改造不触碰以上任何一条；RCP-02 的竞争正确性必须与这些不变量的既有测试并存全绿。

---

## 7. 旧 `.guard` 兼容策略（禁止静默删除）

现状遗留物是**空 guard 文件**（无 owner、无 schema、无证据），字节上无法判定 owner 死活，
也无法绑定 reconciliation 证据。策略：

- **不允许静默删除**：任何启动扫描或写进程发现 `.*.guard` 文件时，绝不 `unlink`。
- **legacy guard 判定 = fail-closed + 人工 reconcile**：因无 owner 身份，无法自动证明其
  孤儿；唯一安全动作是**转 needs-attention**（绑定该 guard 文件的字节 digest），由
  operator 显式决定"确认为孤儿后清理"或"该 scope 已死、跳过"。
- **版本化迁移（不做自动迁移）**：RCP-02 引入的新锁机制使用**新文件名**（方案 A 用
  `.*.lock`，方案 B 用带 schema 的 `.*.guard.json`），**不复用旧 `.guard` 裸名**，因此旧
  `.guard` 与新锁机制天然无冲突；旧 `.guard` 只会被识别为 legacy 遗留物进入人工流程。
- **升级后的启动行为**：启动扫描（`cli.py` 已有的非终态 receipt 扫描）额外检测 `.*.guard`
  文件；发现即产出一条明确诊断 + needs-attention，**不阻塞无关 scope 的正常写入**（旧
  guard 只影响它自己那个 scope 的后续 `_mutex`）。

---

## 8. RCP-02 冻结的 API 与写集合

### 8.1 预计修改的类 / 函数

- `intervention_receipt.py::DurableReceiptStore._mutex`：换成方案 A 的常驻锁文件
  `flock`/`msvcrt.locking`（主路径），保留 `ReceiptStoreError("receipt chain is busy")`
  对外的异常语义不变。
- 新增 `intervention_receipt.py` 内的锁辅助（可选私有）：`_open_lock_file`（常驻、非 EXCL、
  Windows 写哨兵字节）、`_acquire_process_lock` / `_release_process_lock`（Linux flock /
  Windows msvcrt 的薄封装，含 `EWOULDBLOCK/EACCES/EDEADLK` → busy 的映射）。
- `intervention_receipt.py::DurableReceiptStore.__init__`：不新增公开参数（不注入 TTL）。

### 8.2 是否新增 GuardRecord / Reconciliation 模型

- **是，新增 `ReceiptGuardRecord`**（`looper.receipt-guard-record/v1alpha1`）：仅在**方案 B
  fallback** 写盘，字段 = `schema_version`、`scope_plan_digest`、`scope_execution_id`、
  `scope_operation`、`owner_pid`、`owner_boot_id`、`owner_session_id`、`owner_hostname`、
  `created_at`；`digest` 覆盖上述全部字段。方案 A 主路径**不写**该模型（锁状态在内核，不在
  文件）。
- **是，新增 `ReceiptGuardReconciliation`**（`looper.receipt-guard-reconciliation/v1alpha1`）：
  `schema_version`、`guard_file_digest`（被清理那份 guard 的字节 digest）、`guard_record`
  （若可解析）、`plan_digest`、`execution_id`、`operation`、`outcome`
  （`ORPHAN_CONFIRMED` / `NEEDS_ATTENTION`）、`reconciled_at`、`reconciler`；`digest` 覆盖全
  字段。**不自动产生**，只由显式 reconcile 入口写入。
- **复用 FileTargetGuard 概念但不复制其隐式行为**：复用"guard + owner + 显式对账"的概念，
  **不复用** `TargetLease` 的 `expires_at`/`ttl_seconds` 与 `acquire` 的"过期即接管"隐式行为
  （`lease.py:180-225`）——receipt guard 无 TTL。

### 8.3 哪些字段进 digest

- 锁 scope 身份：`plan_digest`、`execution_id`、`operation`（沿用 `_execution_digest`）。
- `ReceiptGuardRecord.digest`：owner 身份全字段（pid/boot_id/session_id/hostname/created_at）
  + scope 全字段。
- `ReceiptGuardReconciliation.digest`：§5 所述绑定全字段（含旧 guard 文件字节 digest）。

### 8.4 原子发布顺序（方案 B fallback）

1. `mkdir` store root；
2. 原子写 `ReceiptGuardRecord` 到**临时 guard 内容文件**（tmp + `os.replace`）；
3. 以 `O_EXCL` 把 guard 内容文件原子改名为正式 guard 路径（发布即持锁，内容先于存在）；
4. 失败路径按"内容成功但改名失败"处理：fail-closed + 清理自己刚写的临时文件，**不删他人
   的 guard**；
5. 释放 = `unlink` 正式 guard 路径（只有 owner 才能，因 O_EXCL 命名空间保证唯一）。

（方案 A 主路径无发布顺序：锁文件常驻，只 open+flock，无内容、无 unlink。）

### 8.5 异常类型与调用方处理

- 对外**保持** `ReceiptStoreError("receipt chain is busy")` 语义（`start`/`advance` 调用方
  `dynamic_adapters.py:779-821` 已按此 fail-closed 传播为 `DynamicInterventionError`）。
- 新增诊断异常（内部/可选）：`ReceiptGuardOrphaned`（发现 legacy/orphan guard，需人工）——
  调用方**不得捕获后继续写**，必须 fail-closed 并转 attention。
- 崩溃后重试的调用方语义：`start` 遇到"锁已释放但链已存在"仍按现有 "receipt execution
  already exists" 处理（`intervention_receipt.py:398-399`），与锁恢复无关。

### 8.6 写集合（RCP-02）

- 可修改：`packages/core/looper_core/system_opt/intervention_receipt.py` + 新增
  `tests/test_system_opt_intervention_receipt_concurrency.py`（或并入既有 receipt 测试）。
- 不得修改：`intervention.py`、`safety.py`、`dynamic_adapters.py`、`dynamic_loop.py`、
  `cli.py`、`lease.py`、任何既有测试、`unfinished-task-queue-2026-08-24.md`、
  `agent-work-ledger-2026-08-24.md`、云端证据与 `.artifacts/`。

---

## 9. RCP-02 测试矩阵

> 验收门：恰好一个 writer 成功；loser 明确 busy；崩溃后只能按冻结合同恢复；内容/指针链仍
> 完整；Windows/Linux 语义一致。docs-only 阶段不要求 pytest。

| # | 用例 | 预期 |
|---|---|---|
| 1 | 同 scope 两线程竞争 | 恰好一个 `_mutex` 进入，另一个 `ReceiptStoreError("busy")` |
| 2 | 同 scope 两独立进程竞争 | 同上（用 `multiprocessing` 或 subprocess，不共享 fd） |
| 3 | 持锁进程强制退出（`kill`/`terminate`） | 锁自动释放；第二个进程随后可正常进入；**无孤儿 guard** |
| 4 | 不同 execution 并行 | 互不阻塞（guard/lock 按 identity+operation 隔离） |
| 5 | candidate 与 recovery 并行（同 execution） | 互不阻塞（operation 不同） |
| 6 | PID 复用 / 伪造 owner（方案 B） | 依赖 boot_id/session 判定，无法证明 owner 死亡 → fail-closed |
| 7 | 损坏 / 半写 guard（方案 B） | fail-closed，不清理，转 attention |
| 8 | legacy 空 `.guard` 遗留 | fail-closed + 人工 reconcile，不静默删除 |
| 9 | pointer 完全缺失但内容链完整 | 沿前驱链重建唯一 head（现有 `test_content_before_pointer_crash_recovers_unique_head` 覆盖 pointer 指祖先；**新增** pointer 删除后的重建用例） |
| 10 | pointer 指祖先 | 唯一内容链头优先，下次写重发 pointer |
| 11 | 内容链分叉 / 断链 | fail-closed（分叉 = 同 predecessor 两个 successor；断链 = 缺 predecessor） |
| 12 | 恢复失败不得删除可信 receipt | reconcile 失败/不确定时，已存在 receipt 文件一个都不删 |
| 13 | 测试结束无孤儿 guard | 每个用例 teardown 断言 `.*.guard` / `.*.lock` 无遗留（主路径锁文件常驻属正常，须区分"锁文件"与"遗留 guard"） |

---

## 10. RCP-02 与 RCP-03 边界

- **RCP-02 只解决并发与崩溃正确性**：锁机制、owner 身份、崩溃判定、显式 reconcile、
  legacy guard 迁移。**不优化任何扫描复杂度。**
- **`_all_receipts()` 全局 O(N) 重扫 → O(N²) 累计属于 RCP-03**（
  `unfinished-task-queue-2026-08-24.md` L24、§4 RCP-03）。
- **不得在 RCP-01/RCP-02 中改变"其它 scope 损坏是否全局阻断"的安全语义**：当前"任一
  scope 损坏即全局 fail-closed"是保守安全语义；局部索引/忽略是 RCP-03 在冻结真实性边界后
  才可决策的事，RCP-01/RCP-02 一律不碰。

---

## 11. 未决问题（需主 agent / 用户裁决，本稿不擅自定值）

1. 方案 A 主路径的**文件系统能力探测**边界：不支持 flock 时是"显式走方案 B"还是"直接拒绝"？
   本稿倾向"显式走方案 B 并落盘身份"，但**不替主 agent 拍板**。
2. 方案 B 的 owner 存活探测具体实现（Linux `boot_id` + `/proc/<pid>` vs 便携封装），是否
   允许引入极小的纯存在性探测（**不含任何时间/超时**）。
3. `ReceiptGuardRecord` / `ReceiptGuardReconciliation` 的 schema 版本与字段是否需要用户
   逐字段过目（按公式/字段登记纪律）。
4. legacy `.guard` 遗留的 operator 清理入口是否复用 `cli.py` 的 reconcile-expired-lease 命令
   （`cli.py:461`）还是新增独立 `reconcile-orphan-guard` 命令。

以上问题均**不得**由 RCP-02 实现者以默认值解决；需先在本稿复审时收敛。

---

## 12. 引用校验记录（本稿重新 grep 核实，非沿用旧行号）

- `intervention_receipt.py`：`_mutex` 114-130、`_atomic_write` 132-141、`_read_receipt`
  143-152、`_pointer_path` 105-109、`_content_path` 111-112、`_publish_receipt` 367-387、
  `start` 389-413（mutex@397、already-exists@398-399）、`advance` 415-454（mutex@435）、
  `head` 265-329（content-before-pointer seam@322-329）、`_all_receipts` 163-199。
- `intervention.py`：`InterventionExecutionReceiptV2` 201-291、`ReceiptStageV2` 178-186、
  `ReceiptOperation` 188-190。
- `lease.py`：`FileTargetGuard` 117-253、`_mutex` 135-147、`TargetLease` 28-38（expires_at@33）、
  `acquire` 180-225（TTL/reconciliation）、`TargetReconciliation` 53-87。
- `dynamic_adapters.py`：`TwoStageSafetyBackedIntervention._run_observed` 779-821
  （start@788-795、execute_observed@798-806）、`_observer` 764-774。
- `cli.py`：`DurableReceiptStore` 引用 1287、启动扫描 1289-1324（non-terminal post-apply
  receipt 阻断）、`attention_sink` 1367、注入 1383-1384、`reconcile-expired-lease` 461。
- 设计来源：`d5-i2-runtime-wiring-design-2026-08-24.md` §4.2/§4.3/§8.3/§10/§11；
  `unfinished-task-queue-2026-08-24.md` §2（L22-24）、§3（RCP 链）、§4（RCP-01/02/03）、
  §6（L171 无默认超时）。
