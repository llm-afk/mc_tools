# 位置环扫频

使用与 `mit_control` 相同的 CANable 和 MIT 协议，实机改用独立 C 原生周期执行模块。
原生线程独占 USB 收发并在每次实际发送时计算位置目标，Python 不参与逐周期定时。
零位停稳后，以收到的每包 TPDO5 为单位输出 CSV，不按终端刷新频率采样，不插值、平滑或补齐缺失反馈。

在 `C:\Users\32196\Desktop\mc_tools` 目录运行：

```powershell
# 只预览目标轨迹：不打开 USB，不使能电机，CSV 的反馈列留空。
python -m scripts.position_sweep --preview --start-hz 0.2 --stop-hz 5 --duration 20 --amplitude 1 --kp 20 --kd 1

# 实际运行：SN 可省略，未匹配时只自动选择唯一设备。
python -m scripts.position_sweep --serial <CANable_SN> --id 1 --rate 1000 --start-hz 0.2 --stop-hz 5 --duration 20 --amplitude 1 --kp 20 --kd 1

# 查看适配器 SN（不会使能电机）
python -m motor_platform --list
```

`app.py` 开头的用户配置区只保留 9 项：关节 ID、CANable SN、通信频率、扫频起始频率、
截止频率、扫频时间、单边摆幅、kp、kd。填写后直接运行 `python -m scripts.position_sweep`；命令行可覆盖这些值。
示例的 `kp=20`、`kd=1` 仅为参数示范，实际值以 `app.py` 用户配置区为准。
实机需要 64 位 Windows Python 和 PATH 中的 64 位 MinGW `gcc`；首次运行或 C 源码变化后自动编译，
DLL 缓存在 `build/position_sweep/`。编译在电机使能前完成，失败直接报错，不退回 Python 调度。

SN 匹配时使用指定设备（允许同时连接其他适配器）。SN 留空或匹配不到时重新按当前枚举结果选择：
只有一台就使用并打印实际 SN，没有设备或有多台都报错；枚举失败也直接报错。
此选择规则仅属于扫频脚本，底层 `Canable` 和 `mit_control` 的精确 SN 选择规则不变。
选中设备仍需通过固件版本和能力检查。

## 目录

| 文件 | 用途 |
|---|---|
| `app.py` | 用户配置、命令行入口和运行流程 |
| `native.py` | 原生模块编译加载、线程管理、状态显示 |
| `native_engine.c` | 实机轨迹、USB 收发、反馈记录及保护 |
| `trajectory.py` | 预览与离线模拟使用的轨迹公式 |
| `recording.py` | CSV 字段、命令关联与文件保存 |
| `data/` | 本地测试数据，Git 仅保留 `.gitkeep` |

原生编译缓存位于仓库 `build/position_sweep/`，不提交 Git；回归测试位于 `tests/`。

## 执行流程

1. 创建 CSV、打开 CAN、失能预检，再用全零 MIT 帧使能并读取当前位置；初始电机必须处于静止状态。
2. 从读到的位置开始，用五次平滑曲线移动到固件报告的 `0 rad`。默认目标轨迹最大斜率为 `1 rad/s`，
   时间为 `max(2 s, 1.875 × |初始位置| / home_speed)`；这是目标速度约束，实际运动取决于电机闭环。
3. 在零位等待真实反馈连续 `0.5 s` 满足 `|位置| ≤ 0.05 rad`、`|速度| ≤ 0.1 rad/s`，才开始扫频。
4. 先在起始频率下用 `2 s` 把摆幅从零增加到设定值，然后以完整摆幅做连续线性扫频。
5. 达到截止频率后，在该频率下用 `2 s` 将摆幅收至零；确认零位停稳后失能并等待 ACK。

这里的零位是现有通信坐标中的零位，不执行寻零、机械限位搜索、零偏设置或 EEPROM 写入。
`--amplitude 1` 表示零位两侧各 `1 rad`，即目标范围 `[-1, +1] rad`。
`--duration` 只包含完整摆幅的扫频段，回零、停稳和两个摆幅渐变段另外计时。

目标相位通过频率积分连续计算。在完整扫频段（`0 ≤ t ≤ T`）中：

```text
f(t)   = f_start + (f_stop - f_start) * t / T
phi(t) = phi_start + 2*pi*(f_start*t + (f_stop-f_start)*t*t/(2*T))
p(t)   = amplitude * sin(phi(t))
```

`phi_start` 承接前面的摆幅渐入段，阶段之间位置和速度连续。
前馈扭矩始终为 `0`。默认 `--velocity-mode zero` 将 MIT 目标速度也保持为 `0`，用位置输入测试位置闭环，
此时 kd 提供速度阻尼。加 `--velocity-mode derivative` 可同时下发轨迹的 `dp/dt`（包含摆幅渐变的导数）；
这会改变 MIT 控制输入，比较测试时应保持该选项一致。kp/kd 沿用现有协议的实际增益和 `0.01` 编码分辨率。

## 常用参数

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `--id` | 1 | 关节 ID |
| `--serial` | None | 优先选择的 SN；未匹配时只自动选择唯一设备 |
| `--start-hz` / `--stop-hz` | 0.1 / 10 | 位置正弦起始 / 截止频率，Hz；相等时为定频正弦 |
| `--duration` | 30 | 完整摆幅扫频时间，s |
| `--amplitude` | 1 | 单边摆幅，rad |
| `--kp` / `--kd` | 以用户配置区为准 | 实际增益；kp 至少 0.01，kd 可以为 0 |
| `--rate` | 1000 | CAN 周期发送目标频率，Hz |

回零、渐入渐出、超时、温度等参数集中在 `app.py` 底部的内部维护区，通常无需修改。
下面的高级命令行选项保留兼容，但不再出现在普通 `--help` 中；`--preview`、`--output` 是运行工具选项。

| 内部参数 / 工具选项 | 默认值 | 含义 |
|---|---:|---|
| `--home-speed` | 1 | 回零目标最大移动速度，rad/s |
| `--ramp` | 2 | 摆幅渐入和渐出各自的时间，s |
| `--settle` | 0.5 | 零位连续停稳时间，s |
| `--zero-tolerance` / `--zero-velocity` | 0.05 / 0.1 | 停稳的位置 / 速度容差，rad / rad/s |
| `--home-timeout` | 10 | 目标回零完成后，额外等待实际停稳的最大时间，s |
| `--max-motor-temp` / `--max-driver-temp` | 80 / 80 | 停机温度阈值，°C |
| `--output` | 本目录 `data/时间_截止频率_摆幅_kp_kd.csv` | CSV 路径；已有文件会拒绝覆盖 |
| `--preview` | 关闭 | 只导出目标预览；文件默认以 `-preview.csv` 结尾 |

`--rate` 与 `--stop-hz` 是不同参数。例如发送 1000 Hz、扫到 5 Hz，意味着最快每个正弦周期期望有 200 个命令点。
校验要求 `stop_hz < rate/2`，且为了现有 100 ms 反馈看门狗，`rate ≥ 20 Hz`。
目标轨迹的速度需求随频率增加，完整摆幅段最大斜率上界为 `2*pi*amplitude*stop_hz`；
例如 ±1 rad、5 Hz 对应约 `31.4 rad/s`。可据电机性能降低摆幅或截止频率。

## CSV 内容

自动文件名示例：`20261010-143000-123456_10_1_100_1.csv`。
依次为时间戳、截止频率（Hz）、单边摆幅（rad）、kp、kd；采用本次运行的配置值。
预览以 `-preview.csv` 结尾。
指定 `--output` 时使用指定路径和文件名。

确认回零并停稳后才开始写数据，回零过程不保存。后续摆幅渐入、扫频、渐出、零位收尾及失能期间的反馈均保留。
如果回零前中止或失败，文件只有表头。每包实际反馈写一行，不插值、不平滑、不补造缺失包。
CSV 包含以下 9 列，顺序固定；终端每 0.5 秒打印当前扫频目标频率：

| 列 | 含义 |
|---|---|
| `target_position_rad` | 下发位置，rad，按线协议 float32 值保存 |
| `kp` | 下发位置增益 |
| `kd` | 下发速度增益 |
| `position_rad` | 上报位置，rad |
| `velocity_rad_s` | 上报速度，rad/s |
| `torque` | 上报扭矩，单位沿用固件 |
| `motor_temperature_c` | 电机温度，°C |
| `driver_temperature_c` | 驱动器温度，°C |
| `frequency_hz` | 与下发位置对应的扫频目标频率，Hz；渐入为起始频率、渐出为截止频率、零位保持为 0 |

目标按主机收包时间关联最近一次成功提交 USB 的命令。协议没有命令序号，不能严格一对一确认请求和应答。
内部仍使用单调时间做命令关联、反馈超时和停稳判断，但不将时间戳、序号、阶段、错误码写入 CSV。
错误码仍参与故障停机并在终端报告。预览 CSV 同样为 9 列，仅从零位开始输出目标，五个反馈列留空，文件名以 `-preview.csv` 结尾。
`frequency_hz` 是目标正弦的瞬时频率，不是 CAN 收发频率，也不是从电机反馈估算的实际振动频率。

Windows/USB 发送不是硬实时。CSV 行号只表示收包顺序，不能把相邻行视为严格相隔 1/rate 秒。
终端最终统计包含实际 TX/RX 频率、跳周期和发送间隔；CSV 不含时间，不能据此精确还原间隔抖动或做时间对齐的频响分析。
`max_tx_gap_s` 是最大连续周期 USB 写成功时间间隔，`tx_gaps_over_5ms` 是超过 5 ms 的次数。
`max_usb_write_s` 是 USB 写调用的最大主机耗时（包括最后的失能请求），不能直接当作 CAN 线上延迟。
周期命令使用异步 WinUSB OUT，IN/OUT 各有独立事件和 OVERLAPPED；任意时刻最多一笔 OUT 未完成。
等待写完成时继续接收反馈，完成后校验实际长度并更新发送统计，短写/超时直接停机。
`max_usb_submit_s` 是异步写提交调用耗时，`max_usb_write_s` 包含提交至主机观察到完成的时间。
退出前先取消或完成尚未结束的 OUT，再发送失能；不重用仍由 USB 持有的缓冲区。
实现依据：[WinUSB 异步写入约定](https://learn.microsoft.com/en-us/windows/win32/api/winusb/nf-winusb-winusb_writepipe)。
原生线程用 QPC 绝对时间安排发送，延迟后跳过过期周期、不追赶连发；最多允许 4 个未回包命令。
使用高于普通线程的优先级，临近发送时轮询异步接收事件，无 Python 回调、GIL、运行时分配或写盘。
每包反馈及当时命令存入预分配数组，失能并收到 ACK、通信线程退出后由 Python 输出 CSV。
普通异常和 Ctrl+C 也保存已缓存的数据；原生线程在 Python 心跳中断 250 ms 后独立停止并发送失能。
强制结束进程或断电无法保存内存数据。内存上限为 500,000 包，达到上限报错停机并保存此前数据，不静默丢包。
原生路径不再修改 Python GIL/垃圾回收设置。它会增加 CPU 占用；USB 写阻塞和 Windows 抢占仍可能造成间隔抖动。
原有 Python 运行时仅用于非 USB 的离线模拟，`mit_control` 的执行架构未改变。

## 停止与离线验证

Ctrl+C、错误码、温度超限、100 ms 无新鲜反馈、回零超时、队列溢出或通信异常都会尝试立即失能，
保留已收到的数据；异常退出不继续执行回零运动。失能确认失败会明确输出，不把尝试发送当作成功。
正常运行有 250 ms 应用租约，应用阻塞后后台会停止周期发送并尝试失能。

```powershell
python -m unittest discover -s tests -p 'test_position_sweep.py' -v
python -m unittest discover -s tests -p 'test_runtime*.py' -v
python -m unittest discover -s tests -p 'test_native_sweep.py' -v
```

模拟测试覆盖实际 `MotorBus` / `ControlRuntime` 收发路径、每包 CSV、正常收尾、反馈中断、瞬时错误、
温度故障、回零失败、Ctrl+C 和失能失败。它们不替代真实电机的扫频和通信频率验证。
