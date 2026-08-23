"""L2 测量合同 —— 层目录与调用规范。

模块成员：scoring.py / measurement.py（原地不动）。

接口与调用规范：
- 对上：MeasurementBatch/MetricEvidence（L3+L4 的统一输出）、
  bootstrap_improvement（S6）、comparable（S0）、evaluate_hard_gates（S2）、
  diagnostic_priorities（S4 原语）。
- 调用方向：L8/L5 消费；本层不发起测量、不执行命令。
- 规范：digest 双范围命名（metric_evidence / measurement_batch）；
  缺证据、单位不符、样本不足 → 显式失败，绝不以 0 顶替。
- 禁止：解析上游工具原始格式（那是 L3/L4 的事）。
"""
