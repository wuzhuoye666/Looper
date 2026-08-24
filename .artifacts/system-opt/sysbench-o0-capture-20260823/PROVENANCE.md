# sysbench O0 夹具采集记录（2026-08-23）

- 主机：8.134.104.213（阿里云 ECS 2vCPU / 1.6GB，即初始调优全链会话用机）
- sysbench 版本：1.0.20（LuaJIT 2.1.0-beta3，Ubuntu 22.04 系统包）
- 采集方式：`ssh root@8.134.104.213 "<cmd>" > <file> 2>&1` 直接重定向，
  本地零转录（前一次手工誊抄版本作废——存在缩进与拼行错误）
- 采集命令：
  - `sysbench cpu --threads=2 --time=5 run` → `sysbench-cpu-threads2-time5.txt`
  - `sysbench memory --threads=2 --time=5 --memory-block-size=1M --memory-total-size=2G run` → `sysbench-memory-threads2-1M-2G.txt`
- 用途：O0 解析器（sysbench 文本输出）真实夹具，供 hypothesis 测试与 DeepSeek 泳道的
  sysbench 解析器任务钉数值。数值本身是一次性快照，不可跨机器比较。
