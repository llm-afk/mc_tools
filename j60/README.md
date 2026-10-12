# J60 测试脚本

各个测试脚本分目录存放，数据写入脚本自己的 `data/`：

```text
j60/
├─ mit_control/          # 基础 CAN 控制、实时反馈与原始帧记录
│  └─ data/
├─ feedforward_sweep/    # J60-6 前馈扭矩扫频
│  └─ data/
├─ position_sweep/       # J60 位置扫频
│  └─ data/
├─ canable.py            # 共用 CANable、1 Mbps 经典 CAN 和唯一 SN 自动选择
├─ protocol.py           # 共用 J60 命令和反馈编解码
├─ client.py             # 共用请求、ACK 检查和反馈接收
└─ tests/                # 公共协议及基础控制回归测试
```

在 `mc_tools` 根目录运行：

```powershell
python -m j60.mit_control --probe
python -m j60.mit_control
python -m j60.position_sweep --preview
python -m j60.position_sweep
python -m j60.feedforward_sweep --preview
python -m j60.feedforward_sweep
```

原来的 `python -m j60` 和 `python -m j60 --probe` 保留，转到基础控制入口。
CANable 自动选择唯一连接的设备；零只或多只时提示并退出。

具体配置见各脚本自己的 README。前馈扫频按关节输出端 N·m 填写幅值，不换算电流。
