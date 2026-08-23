"""L3 压力器 —— 层目录与调用规范。

模块成员：pressure.py / interference.py（原地不动）。

接口与调用规范：
- 对上：StandardPressureProtocol（phases + stability 合同）、
  calibrate_cv_acceptance_limit（S1.1）、PhasedPressureMeasurementAdapter。
- 调用方向：直接调用现成压力工具（stress-ng/sysbench/fio/iperf3/numactl），
  经 executor 白名单 runner 执行。
- 规范：report-only 禁带 acceptance_limit；hard-gate 必带显式阈值；
  独占窗口前后干扰检查；cleanup 永远执行。
- 禁止：评价收益、判定候选（L8 职责）。
- L4 采集解耦：等 L4 新合同（SO-D016）后压/采分离。
"""
