# DYN-200 测功机组件

USB 转 RS-485，**115200 baud、8N1、无流控**，通信模式 0 的 6 字节 HEX 主动上报。
组件只接收，不发送轮询、模式切换或控制命令。串口必须接在自动收发方向控制的 USB-RS485 适配器上。

```powershell
python -m pip install -e ".[dynamometer]"
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

## 独立接收测试脚本

```powershell
python -m pip install -e ".[console,dynamometer]"
python -m scripts.dynamometer_test
# 指定串口：
python -m scripts.dynamometer_test --port COM22
# 仅被动探测：
python -m scripts.dynamometer_test --scan
# 采集10秒，无全屏界面：
python -m scripts.dynamometer_test --port COM22 --duration 10 --no-console
```

默认持续运行，115200 8N1；`status` 刷新提示，`quit` 或 Ctrl+C 退出。
`--display-hz` 仅控制界面刷新频率，不控制设备上报速度。`--torque-scale`、`--speed-scale` 为缩放分母。
没有候选或存在多个候选时要求指定端口；脚本不发送任何设备指令。

CSV 写到 `scripts/dynamometer_test/data/`，逐帧保留原始量化值和重复值，包含本地记录序号、
主机接收时间、扭矩、转速和功率。本地序号不是设备帧序号。退出时停止接收后取出最后一批缓存；
缓存溢出丢弃数会显示在统计中，不能把它误认为无条件完整记录。

## 2026-09-14 实机验证

当前 USB-RS485 适配器枚举为 COM9。使用 115200、8N1 被动探测成功，设备连续上报
`00 00 00 00 00 24`；CRC 有效，对应零扭矩和零转速。

运行 `python -m scripts.dynamometer_test --port COM9 --duration 3 --no-console --display-hz 2`
共记录 3002 帧，最终统计为 998.34 Hz、18017 接收字节、0 重同步丢弃字节、
0 缓存溢出、无接收故障。18017 字节包含退出时解析器中不足一帧的 5 个待接收字节。

首次自动探测失败的原因是串口层固定请求 4096 字节，Windows CH340 驱动等待较大的请求完成，
超过了 0.3 秒探测窗口。现改为读取驱动当前已有字节；缓冲为空时只等待 1 字节。
修改后 `python -m scripts.dynamometer_test --scan` 自动识别 COM9。

本次确认当前 8N1 接收、CRC 流解析、自动探测和约 1 kHz 主动上报。设备处于静止零值，
仍未验证扭矩/转速缩放、符号、动态跟踪或长期零丢帧。
