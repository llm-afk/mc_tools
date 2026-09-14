# mc_tools：硬件组件与实验脚本平台

脚本决定使用哪些电机 ID、CANable、MIT 目标和通信频率。组件层不保存实际启用的电机名单。
目前实现电机、原生 USB CANable 与 UDP6721 RS-232 电源；设备由各脚本选择并组合。

```text
mc_tools/
├─ scripts/
│  ├─ mit_control/
│  │  ├─ app.py               # ID、频率配置和控制流程
│  │  ├─ console.py           # 终端反馈看板与命令编辑
│  │  └─ data/                # 本脚本的反馈 CSV
│  └─ （以后增加 tn_curve、kt_test 等实际实验流程）
├─ motor_platform/
│  ├─ components/motor/        # 协议、使能/失能、MIT、状态解析
│  ├─ transports/canable.py    # 原生 WinUSB CAN FD
│  ├─ core/                    # CAN 帧、输入校验、通用记录
│  ├─ bench.py                 # 组装脚本指定的硬件实例
│  └─ runtime.py               # 多电机共享的周期收发调度
├─ utils/                     # 共享终端看板与命令编辑
├─ tests/                      # 离线测试
└─ docs/                       # 协议与接口说明
```

每个应用脚本占一个独立目录。它产生的数据默认写入自己目录内的 `data/`，不会再混放到平台根目录。

## ID 1 MIT 实时控制台

在平台根目录（包含 `pyproject.toml` 的目录）执行，需 Python 3.11 或更高版本：

```powershell
python -m pip install -e ".[console]"

# 打开 ID 1 的持续运行看板；退出时失能并等待 ACK
python -m scripts.mit_control --id 1 --rate 1000

# 若需要定时结束，可显式设置秒数
python -m scripts.mit_control --id 1 --rate 1000 --duration 5
```

脚本先做失能预检，再发送全零 `0x400+ID` 控制帧；收到 TPDO5 后认为自动使能入口已处理，随后进入看板：

- 当前下发的 position、velocity、torque、kp、kd
- 返回的位置、速度、力矩、双温度、错误码及反馈年龄
- 目标/实际发送频率、TX/RX、跳周期数和最大延迟
- 电机使能状态、运行时间、链路故障和命令输入行

同一终端上方持续刷新反馈，下方保留命令编辑区。方向键 ↑↓ 切换历史，←→、Home/End 编辑，Esc 清空。界面独立线程运行，刷新不改动输入内容，也不阻塞控制租约续期。

看板运行时直接输入命令并回车，后台通信调度会继续运行：

```text
set 1.0 0 0.2 20 1    # 同时更新 position velocity torque kp kd
stop                   # 五项清零，保持使能
rate 500               # 修改该电机的周期发送频率
disable                # 停止周期 MIT，并等待失能 ACK
enable                 # 重新使能并恢复当前目标
status                 # 刷新状态提示
help
quit                    # 失能并退出
```

`--id 1` 决定本脚本控制哪个电机。`--duration` 默认值为 0，代表持续运行到 `quit` 或 Ctrl+C。
`--no-console` 用于固定参数的自动化运行。position 单位为 rad，velocity 单位为 rad/s，torque 按固件标称单位；
kp/kd 以实际增益填写，协议按 0.01 分辨率编码为 uint16。`--id/--serial/--rate` 会覆盖 `app.py` 中的默认配置。

应用每约 10 ms 刷新一次控制租约，CAN 周期目标默认 1 kHz；终端默认 10 Hz 刷新。CSV 保存应用读取到的
新反馈快照，并非完整逐 CAN 帧日志。退出、Ctrl+C 或异常时尝试失能；ACK 失败会作为错误报告。

## 脚本内选择多个电机

```python
from motor_platform import Bench, MitCommand

MOTOR_IDS = [1, 2]  # 只属于当前脚本
with Bench(motor_ids=MOTOR_IDS, serial="20903498384550052", rate_hz=1000) as bench:
    with bench.start() as control:
        m1 = control.motor(1)
        m2 = control.motor(2)
        m1.set_rate(1000)
        m2.set_rate(500)
        # 实例创建不等于使能；脚本按流程调用 enable/set_mit/state/disable。
```

每个电机一个实例，多实例共享同一条 CANable 总线和收发线程。未列出的 ID 不建立实例。多个脚本不能同时
独占同一个 CANable；多电机控制应放在同一个应用脚本和平台会话中。

`scripts/` 只放调用下层组件完成实际任务的应用流程，每项任务建立独立目录，例如
`mit_control/`、`tn_curve/`、`kt_test/`。协议编码、USB 检查和调度正确性归入组件测试，不作为应用脚本。
新增硬件组件放到 `motor_platform/components/`。
协议见 `docs/motor_protocol.md`，周期接口见 `docs/periodic_control.md`。

## 安装与验证

只复用底层组件：`python -m pip install -e .`；终端脚本及完整测试使用 `.[console]`。
打包同时包含组件和应用入口，不打包本地测试 CSV。

```powershell
python -m unittest discover -s tests -v
```

测试使用模拟 CAN 和键盘输入，不使能真实电机。`docs/hardware_validation_20260914.md`
保存历史硬件证据；测试数据留在各脚本自己的 `data/` 内。

## 电源组件

`PowerSupply(port="COM26")` 与电机组件独立，支持设置电压/限流、输出开关和新鲜状态读取。
端口由应用脚本指定，连接和关闭不会自动改变输出。安装：`python -m pip install -e ".[power]"`。
详见 [电源接口与组合使用说明](docs/power_supply.md)。

电源终端验证：先安装 `python -m pip install -e ".[console,power]"`，再运行 `python -m scripts.power_control`。默认自动探测；`--scan` 仅识别，详情见电源说明。
