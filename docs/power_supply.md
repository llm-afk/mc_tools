# UDP6721 电源组件

参考此前跑通的 `nodes/power_supply/node.py`，沿用 UNI-T UDP6721 的 RS-232 SCPI 命令：
9600 baud、8N1、无流控、ASCII、LF 结尾。组件与 `motor` 平级，串口端口由上层脚本传入。

## 安装与使用

```powershell
python -m pip install -e .
```

连接、读取和关闭不主动改变输出开关；脚本决定是否使能、是否在结束时切断电源。
每个串口一个 `PowerSupply` 实例，不加载全局配置、不扫描其他端口、不启动后台服务。

```python
from motor_platform import PowerSupply

# COM26 只是旧脚本的示例，按当前连接填写。
with PowerSupply(port="COM26") as power:
    print(power.identity)
    state = power.read_state()
    print(state.voltage, state.current, state.power, state.output, state.mode)
```

下面会实际设定并开启输出，电压和电流限值应按当前实验选取：

```python
with PowerSupply(port="COM26") as power:
    try:
        power.disable()
        power.set_voltage(12.0)
        power.set_current(1.0)
        power.enable()
        sample = power.measure()
        print(sample.voltage, sample.current, sample.power)
    finally:
        power.disable(verify=False)  # 尽力发送 OUTP OFF；不代表已读回确认。
```

## 接口与确认语义

| 接口 | 线路命令 | 返回/确认 |
|---|---|---|
| `open()` / 上下文进入 | `*IDN?` | 检查型号 UDP6721，不改变输出 |
| `set_voltage(V)` | `SOUR:VOLT V` + `VOLT?` | 两位小数编码并读回核对 |
| `set_current(A)` | `SOUR:CURR A` + `CURR?` | 三位小数编码并读回核对，设置限流 |
| `enable()` / `disable()` | `OUTP ON/OFF` + `OUTP?` | 读回设备输出开关状态 |
| `disable(verify=False)` | `OUTP OFF` | 仅串口写入，无确认 |
| `measure()` | `MEAS:VOLT?`、`MEAS:CURR?` | 新的电压、电流；功率计算为 V×I |
| `read_state()` | 上述两条及 `VOLT?`、`CURR?`、`OUTP?`、`OUTP:CVCC?` | 全部重新读取，不沿用本地设置缓存 |
| `set_ovp(V)` / `set_ocp(A)` | `SOUR:VOLT:PROT` / `SOUR:CURR:PROT` | 沿用原实现，只有写入完成，无读回 |
| `remote()` / `local()` | `SYST:REM` / `SYST:LOC` | 只有写入完成 |
| `close()` | 关闭串口 | 不改变输出，不自动返回本地模式 |

`PowerMeasurement` / `PowerState` 为不可变快照，单位 V/A/W，`received_at` 为
`perf_counter()` 时间。多个字段由顺序查询取得，不是同一瞬间采样。
模式每次 `read_state()` 都重新查询，不把启动时 CV/CC 当作长期状态。

## 调度与错误处理

- 电源是同步、低速串口设备。`measure()` 用两条查询，`read_state()` 用六条；
  上层选择轮询频率。原实现约 10 Hz 是旧硬件实测条件下的目标，不承诺所有配置均能达到。
- 电源查询不可放到电机 250 ms 租约续期循环中。组合实验应在独立线程轮询电源，
  将不可变采样交给电机应用；不把串口挂入 CAN 的 I/O 线程。
- 同一实例的整组查询、设置和读回由锁串行保护，防止不同线程串包。
- 空响应、非法数字、未知输出/模式及读回不一致均报错，不替换成 0 或成功。
- 不完整响应、串口写失败后会拒绝继续普通事务，避免把迟到响应认成下一条应答。
  脚本应处理错误并关闭会话；重新连接不自动恢复输出目标。异常时仍允许显式尽力失能。
- 仅检查数值有限、非负及线路小数精度，具体型号额定范围和当前实验限制由脚本负责。

分层：`components/power_supply/protocol.py` 保存数据与解析，`client.py` 提供设备接口，
`transports/serial_scpi.py` 负责串口生命周期和行事务。`pyserial` 只在实际打开串口时导入。
旧实现迁移后已完成离线测试及下述空载实机验证。

## 2026-09-14 空载实机验证

用户确认已断开全部负载后测试，端口 COM26，设备标识：
`Uni-Trend,UDP6721,AWP7225150007,V1.10`。

- 初始输出开启，32 V / 5.352 A，实测 32 V、0 A、CV。
- 原设定值写入并读回一致；连续 30 组 V/I 查询成功，约 2.987 秒（包含每组 50 ms 等待），电压 32.00–32.01 V，电流 0 A。
- 关闭输出读回 OFF，1 秒后实测 0 V。
- 输出关闭时设为 12 V / 1 A，读回一致；开启后实测 11.99 V、0 A、CV。
- 再次关闭，恢复 32 V / 5.352 A 并开启；最终实测 31.99 V、0 A、CV，输出状态 ON。
- 关闭串口前已核对最终状态。关闭串口不切断输出。

以上验证身份、读数、电压/限流设定读回及输出开关；未验证带载限流、CC 转换、OVP/OCP 触发或长期稳定性。
## 上位机中的自动探测

在工位配置中选择电源串口或 auto。自动识别查询串口的 *IDN?，匹配 UDP6721；
测功机串口先被识别并排除。组件使用 detect_power_supplies() 和 resolve_power_port()，
测试流程负责设置、输出与收尾。

## 查询频率复核

同一设备只读实测：完整状态目标 8 Hz 时约 7.88 Hz；只读 V/I 目标 15 Hz 时约 14.93 Hz，目标 20 Hz 时约 18.82 Hz（各30组）。无间隔轮询曾出现一次 MEAS:CURR? 超时，不采用无限速查询。

采用 V/I 目标15 Hz、完整状态约每秒刷新后，连续30.018秒采集422组，实际14.06 Hz，完整状态27次，最大查询耗时133.98 ms，无超时。最终保持输出OFF、32 V / 1 A设定、实测0 V / 0 A。本结果是短时空载查询验证，不代表长期带载稳定性。
