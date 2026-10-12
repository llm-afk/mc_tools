# J60 基础 CAN 通信验证

这个目录保存 J60 基础控制脚本和反馈数据，CSV 写入 j60/mit_control/data/。入口模仿 `scripts/mit_control`，复用
`motor_platform/transports/canable.py` 的设备枚举、WinUSB 读写、gs_usb 帧编码、TX echo 过滤和关闭流程，
以及 `utils/terminal.py` 的终端看板和命令编辑。共用的 `j60/canable.py` 只适配经典 CAN 启动：1 Mbps、11 位标准帧、FD/BRS 关闭。

## 运行

在 `mc_tools` 根目录执行，使用 Python 3.11 或更高版本：

```powershell
cd C:\Users\32196\Desktop\mc_tools

# 查看 CANable 序列号，仅枚举 USB，不启动 CAN。
python -m motor_platform --list

# 接线后的第一步：读取配置和故障码，不使能、不发运动指令。
python -m j60.mit_control --probe

# 五个参数默认全为 0；使能后持续收发，quit/Ctrl+C 失能并退出。
python -m j60.mit_control --id 1 --rate 1000

# 固定参数运行 5 秒，不打开终端看板。
python -m j60.mit_control --id 1 --rate 1000 --duration 5 --no-console
```

CANable SN 自动枚举：只有一只时直接使用；没有或有多只时打印提示并退出，不启动 CAN。
J60 默认 ID 为 1，可加 `--id <0..15>`。不需要填写 SN；`--serial <SN>` 仅用于额外核对唯一适配器的 SN，
即使指定该参数，连接多只适配器时也会退出。
也可以在这个目录执行 `python app.py --probe` 或 `python app.py`。
若环境尚未安装终端组件，在平台根目录运行 `python -m pip install -e ".[console]"`。

上电后先确认供电、CAN_H/CAN_L、共地和总线终端电阻。这个脚本使用同一 CANable 的原生 WinUSB 接口，
不要同时运行占用该适配器的其他脚本。仅支持共享 CANable 层已支持的 Candlelight 固件。

## 交互命令

```text
set p v tau kp kd    # 输出端 rad、rad/s、Nm；kp/kd 按实际增益填写
stop                 # 五个参数清零，保持使能
rate 500             # 改变目标控制发送频率，允许 1..1000 Hz
disable              # 停止控制帧，发失能并检查 ACK
enable               # 检查故障码，发使能并恢复当前目标
status               # 查询故障码；不会暂停正常的周期控制
help
quit                 # 发失能并检查 ACK，关闭 CANable
```

位置控制需要给合适的 `kp/kd`；速度控制令 `kp=0`；纯扭矩控制令 `kp=kd=0`。
驱动器按 `tau = kp*(p_des-p_fb) + kd*(v_des-v_fb) + tau_ff` 计算目标扭矩。
默认的零扭矩目标不等于失能；真正失能请用 `disable` 或退出。

## 首次验证看什么

`--probe` 应收到配置 `0x311` 和状态码 `0x2F1`（ID=1），打印实际 CAN 超时、故障码和原始回复。
控制启动前先读配置/状态、检查故障为零，再做一次失能预检，然后使能；使能/失能回复必须是 DLC=1、Byte0=00。
默认不会清除故障、改 CAN 超时、改 ID、校准、设零点或保存配置。

ID=1 的运行报文：

| 操作 | 主机 ID / DLC | 回复 ID / DLC |
| --- | --- | --- |
| 使能 | 0x041 / 0 | 0x051 / 1 |
| 控制 | 0x081 / 8 | 0x091 / 8 |
| 故障查询 | 0x2E1 / 0 | 0x2F1 / 2，手册版本 |
| 失能 | 0x021 / 0 | 0x031 / 1 |

全零物理控制参数的 8 字节编码为 `FF 7F FF 1F 00 00 FF 7F`。
手册位置例子 `p=3.14, v=0, tau=0, kp=50, kd=5` 的编码为 `0B 8A FF 9F 0C 19 FF 7F`，
它仅用于核对打包，并非默认下发目标。

运动控制位宽依次为 `p:16, v:14, kp:10, kd:8, tau:16`，小端打包；
反馈为 `p:20, v:20, tau:16, temp_flag:1, temp:7`。
位置/速度/扭矩映射范围为 ±40，kp 为 0..1023，kd 为 0..51；这些是编码范围，实际能力按电机型号判断。
温度标志 0 为驱动器、1 为电机，每帧只有一种温度；看板分别缓存并显示两种温度及各自年龄。

正常运行时，控制 TX 和运动 RX 计数应持续增加，位置/速度/扭矩随实际运动改变。
默认每秒另发一次故障查询，异步等待该回复，等待期间继续发送控制帧。
反馈超过 `max(--feedback-timeout, 2/rate)` 秒未更新（默认 0.1 秒），或故障码非零、状态查询超时、USB/CAN 出错时，
停止测试并尝试失能；只有收到 ACK=00 才报告已确认失能，失败时以非零退出码报告 `Disable UNCONFIRMED`。
发送失败不会自动重发或重新使能。错误未排除时不能继续正常控制。

如果报 `CAN error frame ID=0x24` 且数据为 `00 20 00 00 00 00 80 00`，它是适配器的
CAN 错误报告：没有收到总线 ACK，发送端已错误被动，TX 错误计数为 128；不是 J60 的应用故障码。
先核对关节供电、CAN_H/CAN_L、共地、终端电阻和 1 Mbps 总线。
启动日志显示实际枚举到的 SN、USB 固件、位时序以及配置查询的发送/期望回复 ID。

手册 CAN 配置查询中的超时字段单位为 ms；调试界面对 CAN_Timeout 的说明另使用 25 us 计数。
脚本记录查询原值，未转换或写入调试界面的计数。实际超时行为需要结合目标固件确认。
Windows 的 1000 Hz 是发送目标；脚本显示实际平均频率，错过周期会跳过，不补发追赶。

## 状态码版本差异

V1.0.7 手册：DLC=2、小端；公开 SDK 的状态 DLC 常量为 5，而其错误数据 union 为 8 字节，
状态解析表达式又采用大端。脚本允许 2/5/8 字节回复，仅解释前两个字节，保留整个原始回复，额外字节不猜测含义。
默认按手册使用 `--status-endian little`，核对实际固件后可显式改为 `--status-endian big`。
故障 bit0..6 为过压、欠压、过流、电机过温、驱动器过温、CAN 超时、温感线断连；其他非零位也会阻止控制。

## 数据与离线测试

每次运行生成 `data/<时间>-j60.csv`；也可使用 `--output <路径>`，文件已存在时拒绝覆盖。
CSV 记录实际发送成功的 USB 请求及目标关节回复的 ID、DLC、原始数据、FD/BRS 标志和主机单调时钟时间，
运动回复另解码位置/速度/扭矩和该帧温度。记录的 TX 是 USB 写入成功，电机执行要以匹配回复和实际行为确认。
J60 帧没有周期序号，CSV 的时间为主机收发时间，不是电机时戳或严格的一一周期关联。

```powershell
python -B -m unittest discover -s j60/tests -v
```

离线测试覆盖打包/反馈、ACK 与目标 ID 匹配、经典 CAN 启动、只读探测、异常停机和模拟完整收发流程。
真实 J60/CANable 收发、温度切换和 1 kHz 频率等待接线后验证。

依据：J60 产品手册 V1.0.7 第 25-31 页；
[官方 SDK](https://github.com/DeepRoboticsLab/Deep_Motor_SDK) 的 `sdk/can_protocol.h`、`sdk/deep_motor_sdk.h`。
