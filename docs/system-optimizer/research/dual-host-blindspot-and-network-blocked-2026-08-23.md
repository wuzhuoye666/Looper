# 双机 Guest 盲区实测与网络 peer 阻断实录（2026-08-23）

> 结论一：GPT agent 新 L4 采集器在两台真实 ECS 上完成盲区取证——不可读指标
> 全部显式 unavailable，证实 guest 视角盲区是常态而非例外。
> 结论二：真实网络 peer 闭环被环境阻断（3 号机数据路径平台级故障），
> 会话资产已就绪，环境恢复后可直接执行。

## 1. 双机盲区证据（新 L4 collector，LO4 合同首次真机使用）

| 目标 | cpu | memory | network | storage | numa |
|---|---|---|---|---|---|
| 1 号机 8.138.5.244（8C） | 0 不可读 | 0 | **6 项不可读** | **9 项不可读** | 1 项 |
| 3 号机 8.148.238.132（2C） | 1 项（无 cpufreq） | 0 | 6 项 | 9 项 | 1 项 |

- 不可读项均为显式 unavailable + 探测理由（如 per-NIC sysfs 项、块设备
  每 disk 统计、NUMA 绑定探针），无一项被填 0 或猜测；
- 机器可读快照：`.artifacts/system-opt/guest-blindspot-server{1,3}-20260823.json`；
- 3 号机部署方式：system_opt 包 + 依赖模块逐个上传 + pip
  --break-system-packages pydantic（PEP 668），证明 collector 可最小化部署。

## 2. 网络 peer 闭环：环境阻断取证链

会话资产（已就绪待执行）：`examples/system-optimizer/aliyun-network-cc-*`
（manifest/协议/能力域/授权域/基线，cubic 基线 + bbr/reno 候选，1 号机
78f2f4f 部署已含）。

阻断证据（全部 2026-08-23 实测）：
1. iperf3 控制通道先通后断：初期握手成功（客户端表格头输出），后期
   connect 直接超时；
2. 数据流任何速率都不通：TCP 默认/MSS 1400/反向、UDP 100Kbps~1Mbps
   全部在 3-20 秒测试内零完成；
3. 小包正常：ICMP ping（含 1400 字节大包）0% 丢包、RTT 0.28ms、nc 探通；
4. 3 号机本机 iperf3 自环 7.64 Gbit/s 正常（服务本身健康）；
5. 3 号机无本地防火墙规则（iptables INPUT=ACCEPT）、磁盘 26%/inode 6%、
   无高 CPU 进程；dmesg 的 No space 为陈旧消息。

定性：指向平台侧数据路径（安全组会话/实例网络配置/公网带宽策略），
guest 内无法修复。处置：需要用户在阿里云控制台核查 3 号机的安全组与
网络配置；恢复后按已就绪资产直接执行闭环。

## 3. 服务器原状恢复

1 号机 tuned 已恢复：`systemctl start tuned` → active，
governor 读回 performance（与 CPU 会话前的原始状态一致）。

## 4. 能证明 / 不能证明

能证明：L4 盲区契约在真实双机上按设计工作；网络阻断的环境归属证据链。
不能证明：任何拥塞控制算法在真实 peer 路径上的表现（未测成）；
3 号机网络故障的具体平台侧原因（guest 内不可见）。
