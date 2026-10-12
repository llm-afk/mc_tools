# 临时前馈扫频

先用 PD 控制回零并停稳，扫频阶段固定 `position=velocity=kp=kd=0`，直接向 MIT 前馈扭矩字段下发连续正弦扫频。
不是安培指令，不做转矩常数换算。复用位置扫频的原生异步 USB 执行模块。

```powershell
python -m scripts.feedforward_sweep --preview
python -m scripts.feedforward_sweep
```

日常参数集中在 `app.py` 顶部用户配置区，前馈单边幅值默认 `AMPLITUDE=0.1`，可直接修改源码后无参数运行。
命令行参数可临时覆盖对应值。保护参数集中在文件末尾的内部维护区。
其他日常参数为 `--id/--serial/--rate/--start-hz/--stop-hz/--duration`。
默认通信 1000 Hz、0.1→10 Hz、完整幅值 30 秒，渐入渐出各 2 秒。

回零复用位置扫频的五次平滑轨迹和停稳判据，增益在源码顶部 `HOME_KP=100`、`HOME_KD=2` 配置。
回零目标峰值速度为 `HOME_SPEED_RAD_S=5` rad/s，轨迹时间为 `max(2, 1.875×|初始位置|/5)` 秒，随后确认停稳。
回零停稳后切换至零增益前馈扫频并开始采集，回零数据不写 CSV；渐出结束直接失能并等待 ACK。
前馈扫频默认不限制位移：`MAX_TRAVEL_RAD=0`（`--max-travel 0`）。需要行程限制时可设为正数，单位 rad，相对回零停稳位置计算。
速度达到 `--max-velocity`（默认 40 rad/s）仍会停机。温度、反馈超时和应用心跳保护仍生效。

CSV 位于本目录 `data/`，逐包保存前馈目标、kp/kd、位置、速度、反馈扭矩、双温度和扫频频率。
终端每 0.5 秒显示频率。命令与反馈按主机时间关联，不代表协议有逐条应答序号。
正常结束、异常和 Ctrl+C 后落盘；强制杀进程会丢失内存数据。`--output` 可指定文件名。
