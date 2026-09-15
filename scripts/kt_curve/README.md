# 关节输出扭矩一致性

在平台根目录运行：

```powershell
python -m scripts.kt_curve --id 1 --serial <SN> --torque 30
```

查看 SN：`python -m motor_platform --list`。顶部 `CANABLE_SERIAL`、`TORQUE_FF` 填好后可以省略对应参数。
阻尼器电源默认 24 V、3 A；用 `--voltage 24 --current 3` 覆盖。这里设置的是电源限流，
不是测功机测量节点的供电参数，也不保证线圈实际电流等于限流值。电机使用独立电池。

启动后自动设置阻尼器电源并读回确认，先保持 0.5 秒（顶部 `LOAD_SETTLE_S`），
再使能电机，目标扭矩从零线性增加到指定值。这段阻尼建立等待不计入 5 秒采集。
斜率为最大目标 / 5 秒，应用约每毫秒按单调时钟更新目标，CAN 周期发送目标为 1 kHz。
Windows 调度会有抖动，不追赶补发；采集时间边界固定为 0～5000 ms，连接和停机另计。
到期或输入 `quit` 后保持阻尼，用 1 秒降扭矩到零，失能后关闭电源输出。
反馈超时、电机报错、温度越限或采集溢出时尝试立即失能并关闭电源，不延长故障来执行斜坡。
不使用堵转结束条件：这是固定时长的扭矩扫描。

只生成 `data/<时间>/kt_curve.csv`，最终为两组连续的数据列：

```text
motor_time_ms,motor_torque,motor_temperature_c,driver_temperature_c,motor_torque_smooth,dyno_time_ms,dyno_torque_nm,dyno_torque_smooth_nm
```

电机第 N 帧和测功机第 N 帧并排，各组中间没有交替空行。按行号绘图即可；需要时间轴时，
各组使用自己的 `motor_time_ms` / `dyno_time_ms`，时间原点相同。同一行不代表同时采样。
帧数不同时只在较短组末尾留空。同一 USB 批次的相同时间戳和重复值均保留。最终 CSV 不输出速度、
功率和实验配置列。固件扭矩单位不变；扭矩平滑只是趋势估计。

比较不同关节时建议保持电源设定、最大目标、扭矩方向和传感器标定一致，记录多次扫描
再评估重复性。动态反馈未同步，脚本不直接拿同一行两路数据计算“扭矩误差”。

本脚本已做离线模拟验证；真实装置的扭矩比例、阻尼建立时间和运行结果仍需实测。
