# DYN-200 测功机组件

USB 转 RS-485，**115200 baud、8N1、无流控**，通信模式 0 的 6 字节 HEX 主动上报。
组件只接收，不发送轮询、模式切换或控制命令。串口必须接在自动收发方向控制的 USB-RS485 适配器上。

```powershell
python -m pip install -e .
```

## 脚本接口

```python
from motor_platform import Dynamometer

with Dynamometer(port="COM22") as dyno:  # 上层配置实际端口；此处只是示例
    first = dyno.receive_state(timeout=1.0)
    print(first.torque, first.rpm, first.speed, first.power)
    latest = dyno.state(max_age=0.1)       # 最新状态，过期报错
    samples = dyno.drain()                # 取走所有尚未消费的帧，适合记录
    print(dyno.stats())
```

每台设备一个实例、一个串口接收线程。`state()` 读取缓存不阻塞电机控制；
`receive_state()` 等待调用后的新帧，不重复返回旧值。无数据不会自动重连或伪造采样。
`drain()` 返回尚未消费的队列，包括启动时收到的帧；读取 `state()` 不消费队列。
默认队列容量 4096，可由脚本设置 `capacity`；溢出丢弃最旧样本并累计 `dropped_samples`。

可选 `detect_dynamometers()` 被动扫描端口，至少收到三帧 CRC 有效数据才列为候选。
没有型号查询响应，候选只能证明线路格式匹配；多个候选由脚本或用户选择。

## 线路与单位

| 字节 | 解释 |
|---|---|
| D1..D2 | 扭矩幅值 uint16，大端 |
| D3 bit7 | 扭矩负号，1 表示负扭矩 |
| D3 bit6..0 + D4 | 无符号 15-bit 转速 |
| D5..D6 | 前四字节的 CRC16/Modbus，低字节先发，初值 FFFF，多项式 A001 |

没有帧头，通过相邻两帧 CRC 获取同步；损坏后逐字节重新寻找边界。
串口分片、合包均按流解析，不以一次 read 当作一帧。
CRC 不是绝对的身份标识，偶然误匹配仍有可能；`discarded_bytes` 是跳过的字节数，不是丢帧数。

沿用旧脚本缩放：`torque = signed_raw / torque_scale`，默认 `torque_scale=100`（Nm）；
`rpm = raw_speed / speed_scale`，默认 `speed_scale=100`，`speed = rpm * π / 30`（rad/s）。
旧代码标记缩放待校准，仍需实机确认；脚本可传入不同缩放。
`power = torque * speed` 保留符号。线路没有转速方向位，不能据此判定完整机械功率流向；
需要幅值时应用取 `abs(state.power)`。

不添加 EMA 滤波、随机抖动或自动调零；保留原始量化和重复值，避免把处理值当作新测量。
状态同时保留 `raw_torque`（带符号）和 `raw_speed`。

## 频率与验证

1 kHz 为设备标称上报目标，不是主机定时器生成的发布频率。
115200、8N1 下 6 字节帧约占 60 bit，1 kHz 需要约 60 kbit/s，带宽上可容纳；
实际速率必须连接设备测量。`actual_rx_hz` 是本次会话 CRC 有效帧数除以主机接收运行时间。

`received_at` 是主机读到这一批字节的 `perf_counter()` 时间，同一批帧可能同时间戳；
协议没有序号或采样时间，不伪造均匀 1 ms 时间戳，也不能据此证明设备零丢帧或采样抖动。

本次只完成旧实现迁移与离线测试，尚未用当前 8N1 配置进行实机验证。

## 上位机使用

在工位配置中选择测功机串口或 auto，自动产测流程负责连接、采集和收尾。
