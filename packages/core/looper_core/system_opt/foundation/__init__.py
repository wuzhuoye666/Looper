"""L1 配置安全底座 —— 层目录与调用规范。

模块成员：config_manifest.py / state_evidence.py / lease.py / safety.py /
inventory.py / domain.py（原地不动，包仅为层规范入口）。

接口与调用规范：
- 对上（L5/L6/L8）：manifest 解析（parse_config_manifest_yaml）、状态证据
  （state-inventory → authorize-state）、动态域 resolve_domain、
  SafetyController.execute(snapshot→apply→measure→verify→rollback)。
- 调用方向：只允许上层调用本层；本层不 import 任何上层（L4-L8）。
- 规范：一切写操作必须有租约与 fencing token；回退后必须 verify 读回；
  未知所有权 fail-closed；摘要一律 canonical_digest。
- 禁止：本层不做收益评价、不感知组件语义。
"""
