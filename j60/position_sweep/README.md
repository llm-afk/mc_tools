# J60 位置扫频

在 `C:\Users\32196\Desktop\mc_tools` 运行：

```powershell
python -m j60.position_sweep --preview
python -m j60.position_sweep
python -m j60.position_sweep --stop-hz 5 --duration 20 --amplitude 0.2 --kp 100 --kd 2
```

日常修改 `app.py` 开头的 9 项配置，与 `scripts/position_sweep` 对齐：
ID=1、SN 自动选择唯一设备、通信 1000 Hz、扫频 0.1→10 Hz、30 秒、±1 rad、KP=100、KD=2。
零台或多台 CANable 提示退出。数据默认保存本目录 `data/`；历史数据不迁移。

先以 1 rad/s 目标峰值速度缓慢回零，连续停稳 0.5 秒，再渐入 2 秒、线性扫频 30 秒、渐出 2 秒，保持零位置直到连续停稳 0.5 秒，再失能。
默认速度目标和前馈扭矩均为 0。Ctrl+C、反馈超时、状态故障或温度达到 80°C 会停止并尝试失能，停机后保存反馈。

位置单位 rad，为关节输出端。J60 KP 分辨率 1、KD 分辨率 0.2，按协议向下量化；CSV 记录量化后的目标位置和增益。
零位置编码有约 -0.00061 rad 的量化偏差。预览反馈列为空，不连接设备。
CSV 列与参考一致：target_position_rad,kp,kd,position_rad,velocity_rad_s,torque,motor_temperature_c,driver_temperature_c,frequency_hz。
每包反馈一行；目标为最近一次 USB 写完成的控制目标，协议无序号，不能保证与反馈逐请求对应。

复用 J60 的 CANable、请求与协议模块；原生 C 收发线程独立于前馈脚本。经典 CAN 1 Mbps、标准帧、DLC=8。
首次运行需要 PATH 中有 64 位 MinGW gcc；DLL 缓存至 `build/j60_position_sweep`。
离线验证不代表已完成实机位置扫频。
