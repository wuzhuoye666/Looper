# 主工作树旧副本清理方案（2026-08-23）

> 状态：approved sequence, deletion not executed  
> 目标旧树：`E:\wujiahao\CProjectAllStudies\TencentMiniProject\Looper`  
> 权威新树：`E:\wujiahao\CProjectAllStudies\TencentMiniProject\Looper-system-optimizer`

## 当前只读盘点

- 旧树分支：`system-optimizer...origin/main`；仍有未提交 System Optimizer 文件。
- 新树分支：`system-optimizer-impl...origin/main`；是本轮唯一写入目标。
- 旧树 4 个测试文件；新树本轮修复前 8 个测试文件。
- 旧树相关源文件/文档/测试逐文件 SHA-256 预盘点没有发现 `old-only` 文件；
  相同文件中既有 identical，也有新树已演进的 different。这个结果只是提交前快照，
  不能代替提交后的最终确认。
- `__pycache__`/`.pyc` 是生成物，不作为独有业务内容。

## 必须按序执行

1. 在新树完成代码、文档、隔离测试和 WSL2 证据重采。
2. 将新树提交到 `system-optimizer-impl`，记录 commit SHA；不 push、不建 PR，除非用户另行要求。
3. 以该 commit 为权威基准，重新列出旧树所有相关文件，范围至少包括：
   `README.md`、`contracts.py`、`cli.py`、`docs/system-optimizer*`、
   `examples/system-optimizer`、`system_opt` 包、`system_opt_support.py` 和全部
   `test_system_opt_*.py`。
4. 对每个旧文件做 SHA-256；哈希不同的文件再做内容 diff。不能只凭路径、文件名或
   Git 状态判断重复。
5. 若发现旧树独有内容：立即停止清理，列出文件、独有片段和拟迁移位置，等待用户确认；
   不自动合并。
6. 只有所有旧文件都被判定为“与新 commit 相同”或“内容已被新 commit 明确覆盖”，
   才形成精确删除清单，再请求/取得该次删除授权。
7. 删除只针对确认过的旧副本与生成物；保留旧树中任何无关改动和正在运行的开发栈。
8. 删除后复核两个工作树的 `git status`，并运行新树隔离回归；记录删除目标、结果和
   是否可恢复。

## 本轮边界

本轮不删除主工作树文件。原因不是认为旧副本有价值，而是清理的前置条件明确要求
“新树先提交 + 提交后逐文件确认”，目前尚未走完该序列。
