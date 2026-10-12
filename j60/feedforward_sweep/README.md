# J60-6 前馈扭矩扫频

模仿 `scripts/feedforward_sweep`，保持参数布局、终端交互和 CSV 列顺序。
幅值直接使用关节输出端 N·m，不换算为电流；扫频阶段 `position=velocity=kp=kd=0`。

```powershell
cd C:\Users\32196\Desktop\mc_tools
python -m j60.feedforward_sweep --preview
python -m j60.feedforward_sweep

# 临时覆盖参数
python -m j60.feedforward_sweep --amplitude 0.2 --start-hz 0.1 --stop-hz 20 --duration 30
```

日常参数仍放在 `app.py` 顶部用户配置区：ID、SN、通信频率、起止频率、完整幅值扫频时间、幅值、回零 Kp/Kd。
默认 ID=1、唯一 CANable SN 自动选择、1000 Hz、0.1→20 Hz、完整幅值 30 秒、单边幅值 0.1 N·m、回零 Kp=100/Kd=2。
额外渐入和渐出各 2 秒，因此采集时段共 34 秒，回零和停稳等待另计。
参考脚本目前源码中的幅值 30 是原电机的协议单位，这里采用 0.1 N·m 的独立 J60 默认值。
命令行参数与参考脚本一致：`--id/--serial/--rate/--start-hz/--stop-hz/--duration/--amplitude/--max-travel/--max-velocity/--output/--preview`。
`--serial` 只核对唯一设备，连接零只或多只 CANable 时退出；`--preview` 完全不枚举或连接硬件。

运行流程：读配置/故障码、失能预检、使能、检查初始静止及温度，PD 平滑回到当前坐标零位，
确认位置误差≤0.05 rad、速度≤0.1 rad/s 连续 0.5 秒，再切换到零增益前馈扫频。
回零轨迹为五次平滑曲线，时间 `max(2, 1.875*|初始位置|/5)` 秒；不改电机零点或配置。
回零数据不写 CSV。扫频完成直接失能并检查 ACK，不再次用 PD 回零。

和参考脚本一样，终端每 0.5 秒显示 `Sweep frequency`，Ctrl+C 停止并保存。
CAN 周期发送、相位计算和异步 WinUSB 收发由原生 C 线程完成；Python 负责心跳、显示和结束后 CSV 落盘。
首次真实运行会使用 PATH 中的 64 位 MinGW gcc 构建本脚本自己的 DLL，缓存位于 `build/j60_feedforward_sweep/`。
接口为 J60 1 Mbps 标准经典 CAN，控制 8 字节，WinUSB legacy gs_usb 包 20 字节，FD/BRS 关闭。
每秒另发状态查询，等待状态回复期间继续发送控制帧；没有故障自动清除、重试使能或配置写入。

保护参数仍放在 `app.py` 末尾维护区：默认位移限制关闭（`--max-travel 0`），
可设置相对回零停稳位置的最大位移；速度限制默认 40 rad/s；双温度限制 80 ℃，反馈超时 0.1 秒，
Python 心跳超时 0.25 秒。反馈、USB/CAN、故障码、温度、位移/速度、CSV 内存容量或回零停稳异常时停止并尝试失能。
仅收到 J60 失能 ACK=00 才认为失能已确认。Windows 下 1000 Hz 是发送目标，结束后打印实际频率、最大间隔和跳周期。
扭矩编码允许 ±40 N·m，这是协议范围；J60-6 实际峰值能力以产品参数和供电/电流限制为准。

CSV 位于这个脚本自己的 `data/`：

```text
target_feedforward_torque,kp,kd,position_rad,velocity_rad_s,torque,motor_temperature_c,driver_temperature_c,frequency_hz
```

目标和反馈扭矩单位均为 N·m，逐包保留，Kp/Kd 始终为零。
J60 每帧只回一种温度，另一种使用最近收到的对应温度缓存，尚未收到时留空，不假定两个温度同步更新。
目标按最近完成的 USB 写入与主机接收时间关联；协议没有周期序号，不代表严格逐条回复关联。
正常结束、异常和 Ctrl+C 会保存已采集数据；强制结束进程会丢失内存采集。
文件已存在时拒绝覆盖。预览 CSV 的反馈列留空，不能用作实机结果。

```powershell
python -B -m unittest discover -s j60/feedforward_sweep/tests -v
```

原生 C 模拟测试验证回零/停稳、零增益扫频、J60 打包、异步写入、批量反馈、故障/温度/超时/运动限值、取消和失能 ACK。
尚未实际运行 J60 扫频，接线后的基础 CAN 通信已由用户确认成功。
