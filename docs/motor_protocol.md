# Python 电机控制接口

适配 GD 与 ADM 固件共用的 **8 字节 SDO + 0x400 MIT 控制/TPDO5** 线路协议。
电机协议层提供自动使能 MIT 指令、显式失能、状态接收与解析；不包含 OTA、标定或参数修改。
可选的后台调度见 [周期控制](periodic_control.md)。
固件文件未修改。

## 安装与只读识别

Python >= 3.11。协议层只用标准库；终端看板及其交互测试依赖 prompt-toolkit；当前 CANable 后端需要 Windows 的 WinUSB 驱动。
在平台根目录运行，或用 `python -m pip install -e .` 安装后从其他脚本导入。

```powershell
python -m motor_platform --serial 20903498384550052
python -m unittest discover -s tests -v
```

`python -m motor_platform` 只读 USB 版本/能力，不启动 CAN。
也可调用 `Canable.detect()` 枚举当前连接，调用 `Canable(serial_number=...).inspect()` 读取详情。
枚举不到设备时抛错，不回退到任意串口或其他适配器。

2026-09-14 本次会话直接 WinUSB 实测基线：`1D50:606F`，产品 `Candlelight 2.5 - Multiboard`，
序列号 `20903498384550052`，固件 `0x260803`，硬件 `0x200`，HAL `1.2.5`，单通道，
能力 `0xE53B`，CAN 时钟 160 MHz。实际连接时重新读取，不能用这个快照替代设备查询。
当前后端的 `open()` 接受 `0x260618`、`0x260803`、`0x261001`，并检查单通道和所需能力位。
`0x261001` 于 2026-10-10 根据用户设备报告及本地作者 SDK 的 legacy 兼容说明补入；
版本识别和初始化路径已做模拟回归；同日已在 SN `20923378374350012` 上完成原生扫频 CAN 收发、回零及失能 ACK 验证。
短时小摆幅测试覆盖 ±0.1 rad、0.2→2 Hz；这不代表所有负载、频率或固件功能都已验证。
依据：桌面 `canable-sdk/vendor/elmue_canable_sdk/User & Developer Manual.htm` 的 legacy 默认兼容说明，
以及 `SampleApplication Python/Source/Candlelight_def.py` 的请求定义（包括 legacy 可用的请求 22）。

本机 `candle-api==0.0.12` 本次枚举结果为空，因而采用 Python ctypes 直接调用 WinUSB。
使用固件提供的 legacy gs_usb CAN FD 格式，未开启 Elmue blobs 或 USB 硬件时间戳。

## 分层与接口

| 层 | 对外接口 | 负责内容 |
|---|---|---|
| 协议 | `MitCommand`、`mit_frame()`、`control_frame()`、`parse_state()` | 纯编解码，独立于 USB |
| CANable | `detect/inspect/open/send/recv/close` | USB 设备、位定时、帧收发、回显过滤 |
| 电机总线 | `MotorBus(transport)`、`motor(node_id)`、`poll()` | 单一接收入口，按完整 CAN ID 分发 |
| 电机 | `enable()`、`disable()`、`send_mit(command)`、`receive_state()` | 可供未来脚本调用的四项能力 |

其他适配器只需实现 `CanTransport` 的 `send(CanFrame)`、`recv(timeout)`、`close()`。
传给 `MotorBus` 的 transport 应已打开，不能再由其他对象读取同一 transport。
一个 `MotorBus` 可以持有多个电机，重复调用 `motor(1)` 返回同一对象。
以下描述手动模式：操作使用锁串行化，脚本调用 `poll()` 或 `receive_state()` 消费反馈。
启用周期运行时后，则由运行时唯一 I/O 线程接收和分发，脚本读取状态缓存。
SDO 等待期间会分发其他电机的状态，但会阻塞其他控制调用，适合低频使能/失能操作。

## 后续脚本调用示例

以下函数是实际控制操作示例，**导入库不会执行它**。节点号由调用脚本明确提供。
`open()` 配置并启动 CAN；与只读 `inspect()` 不同。

```python
from motor_platform import Canable, MotorBus, MitCommand

def run_commands(node_id, commands):
    can = Canable(serial_number="20903498384550052")
    can.open(bitrate=1_000_000, data_bitrate=4_000_000)
    with MotorBus(can) as bus:
        motor = bus.motor(node_id)
        try:
            # enable() 发送一帧全零 0x400 控制帧；固件自动使能，
            # 收到本节点 TPDO5 且错误码为零后才返回。
            motor.enable(timeout=0.5)
            # commands 的调度频率、物理限幅由调用脚本决定。
            for command in commands:
                motor.send_mit(command)
                state = motor.receive_state(timeout=0.1)
                print(state.position, state.velocity, state.torque)
        finally:
            # 显式尽力发送失能，不等待可能与延迟 ACK 混淆的确认。
            # 返回成功只表示 USB 写入完成；不能证明电机已经失能。
            motor.disable(wait_ack=False)

# 命令对象示例（这里只构造对象，不发送）
command = MitCommand(position=0.0, velocity=0.0, torque=0.0, kp=0, kd=0)
```

正常流程可用 `motor.disable()` 等待 SDO ACK。`close()` 只关闭 CANable 通道和 USB，
不会隐式发送电机失能，也不是电机停机确认。USB 拔出时无法保证失能帧送达。

## 线路格式

所有 ID 为标准 11 位，默认 CAN FD + BRS，数据采用小端。
节点号允许 `0..127`，0 在此固件是普通节点号，并非广播。

| 操作 | CAN ID | 载荷 |
|---|---|---|
| 失能 | `0x600 + node` | `2B 02 20 00 02 00 00 00` |
| 控制字 ACK | `0x580 + node` | `60 02 20 00 00 00 00 00` |
| MIT 指令并自动使能 | `0x400 + node` | 16 字节 `<fffHH`：position, velocity, torque, kp×100, kd×100 |
| 状态 | `0x190 + node` | 16 字节 `<fffhh`：position, velocity, torque, motor_temp×10, board_temp×10 |
| 带错误状态 | `0x190 + node` | 20 字节：上述 16 字节 + uint16 error + 未定义的 2 字节 |

- SDO 使用对象索引 `0x2002`，写 2 字节（CS `0x2B`），保留/子索引字节固定为 0。
  接收端以帧 ID、8 字节长度、索引和子索引匹配应答。`0x80` 为拒绝，但不能假定含标准 CANopen abort code。
- `enable()` 不写使能控制字：它发送全零 `0x400 + node`，GD/ADM 的兼容入口都会自动使能，
  并等待本节点 `0x190 + node` 回传。回传证明控制入口已处理该帧；协议仍没有独立功率级使能位。
- `disable()` 写对象 `0x2002=2` 并等待 SDO ACK。SDO 没有事务号；失能超时后不能把迟到 ACK
  当作下一次事务的确认。`disable(wait_ack=False)` 仅作异常收尾，不能证明帧已经到达电机。
- MIT 的 position 为 rad，velocity 为 rad/s，torque 为固件使用的力矩值（标称 Nm）。
  `kp/kd` 的线路分辨率为 0.01：API 中 `kp=20.0` 编码为 uint16 `2000`，固件得到 20.00。
- 编码校验有限 float32 和增益线路范围；这只是表示范围，不是电机允许的工作范围。
  具体电机的角度、速度、力矩和增益限值应由后续脚本配置。
- TPDO5 的 ID 基址 `0x190` 非 0x80 对齐，必须用完整的 `0x190 + node` 比较，不能 `id & 0x7F` 取节点。
- 20 字节状态的 error 只解析字节 16..17；18..19 没有被固件赋值，不解释其内容。
  状态中的 `error_code` 已被固件屏蔽 `ERR_HEARTBEAT_TIMEOUT=0x0080`，0 不代表完整错误寄存器为零。
- `MotorState.received_at` 为主机 `time.perf_counter()` 接收时间；`last_state` 是缓存，可能过期。
  `receive_state()` 等待下一个状态而不返回缓存，也不发送“状态查询”帧。
  当前固件收到 `0x400 + node` 后回传 TPDO5；两者没有序号，无法把反馈严格关联到某一条 MIT 指令。

## CANable 位定时与验证边界

默认仲裁段 1 Mbit/s、数据段 4 Mbit/s、BRS，对应本仓库 `canfd_init()`。
默认采样点请求为 75% / 64%；数据段固件注释写 60%，但寄存器配置算得 64%，以寄存器为准。
CANable 按实读时钟、seg/brp/sjw 范围搜索精确波特率并取最接近的采样点，不硬编码 100/160 MHz。
它不改变板上终端电阻配置。USB 收发过滤 TX echo，保留错误帧供上层报错。

WinUSB 管道读取使用端点最大包长的整数倍缓冲，使用挂起的异步读取；应用超时保留挂起请求，关闭时取消并等待完成：
[Microsoft WinUsb_ReadPipe 文档](https://learn.microsoft.com/en-us/windows/win32/api/winusb/nf-winusb-winusb_readpipe)。
底层结构依据桌面 `CANable-2.5-firmware-Slcan-and-Candlelight/Firmware/Candlelight/`
的 `candlelight_def.h`、`control.c`、`buffer.c` 核对。

离线测试覆盖 `0x400 + node` 协议黄金字节、状态扩展、非法输入、SDO 应答/超时、多节点分发、
USB DLC/回显/分片/合包、设备时序求解及启动失败释放。
历史实机记录（2026-09-14）：ID 1 使用 `0x401`、tau=0.2 完成 1 秒实机运动，
MIT 收发 985/985，约 981.7 Hz，错误码为零，最终失能收到 ACK。
详见 [实机记录](hardware_validation_20260914.md)。
# 电机SN只读接口

`bench.motor(id).read_serial_number()` 在 `bench.start()` 前调用，不发送使能或写入命令。
依次读取 `0x2004..0x200A`：请求ID `0x600+id`，数据 `40 idx_lo idx_hi 00 00 00 00 00`；
响应ID `0x580+id`，4字节读取命令字 `0x43`，有效载荷为最后4字节。
七段按索引顺序拼接28字节，按固件小端内存顺序得到26位ASCII字符串及末尾零填充。
`read_serial_bytes()` 提供未解码的28字节；无效/未编程SN、SDO错误和超时均报错。
核对来源：`c1_driver/Firmware_app/Source/com_can.c` 的SDO分支及 `od.c` 的OD表、Parse_Branch_From_SN。
该SN是电机身份，CANable的USB SN仍由上层创建传输实例时指定。
