# 双机 NUMA 拓扑探测（2026-08-23）

> 结论：两台可用阿里云 ECS 均为单 NUMA node，NUMA 组件在本目标对上保持
> `unavailable`，不做任何绑定类候选搜索。

## 逐机证据

| 目标 | 机型线索 | vCPU | lscpu NUMA 输出 |
|---|---|---:|---|
| 8.138.5.244（iZ7xvcxcry1ejfjcd1d7igZ） | 8 vCPU / 14 GiB，Ubuntu 24.04，6.8.0-137-generic | 8 | `NUMA node(s): 1`，`NUMA node0 CPU(s): 0-7` |
| 8.148.249.35（iZ7xv0jzed4460ga4dblfeZ） | 24 vCPU / 45 GiB，Ubuntu 24.04，6.8.0-137-generic | 24 | `NUMA node(s): 1`，`NUMA node0 CPU(s): 0-23` |

两机 lscpu 均直接报告单节点；无需进一步 numactl 绑定验证——绑定探针的前提
（≥2 个 node）不成立。

## 对 M2 NUMA 组件的含义

- 组件状态保持 [M2 组件合同](../planning/m2-component-pressure-contract-2026-08-23.md)
  表中的 `unavailable`：本轮新增的 24 vCPU 大规格机同样单节点，"找一台双节点机"
  的计划在当前可用目标池内不可行。
- 关闭条件不变：获得 ≥2 NUMA node 的目标机后，重跑拓扑/绑定探针并另行校准。
- 单节点事实本身已作为证据记录，不推断"该机型族全部单节点"（未验证）。
