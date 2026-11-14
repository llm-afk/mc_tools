# 工位集成说明

来源为同事提供的 tsc-print-service 1.0.0 及 20261022 部署说明，原目录保持不变。
保留 CODE128/BMP/TSPL、50×15 mm 默认标签和 Windows RAW 打印链路，加入：

- 状态路由兼容查询参数，按指定 Windows 队列精确匹配，并返回离线、缺纸等 Windows 状态。
- Windows 查询失败保留错误，区分服务存活、队列就绪、作业提交与实际出纸。
- 通过参数数组启动隐藏 PowerShell，支持中文/空格路径和 Unicode 队列名。
- RAW helper 校验完整写入、返回阶段、Win32 错误、作业编号和字节数，未知结果不自动重发。
- 串行提交打印请求；未知结果停止剩余份数。副本只保留逐条成功信息，不猜测出纸。
- 增加只读 /api/health；上位机启动时用 --managed，关闭 stdin 后等待正在处理的请求与作业退出。
- 不创建计划任务，不自动打印测试标签，不打开 CANable、串口或扫描局域网。

正常使用请运行上位机，当前用户的标签配置位于 %LOCALAPPDATA%\MirrorMe\FactoryStation\printer-service\config.json。
修改标签规格后重启上位机；名称及服务地址/路径在工位配置内保存。
start.cmd 仅用于工程手动调试，手动服务由其窗口管理，上位机不会结束外部进程。
Node.js 24.17.0 与第三方声明随项目目录提供；依赖使用交付包中已有的 Windows x64 预编译版本。
node index.js --self-check 只检查标签渲染；raw-print.ps1 -CompileOnly 只编译 helper，不访问打印机。
test_bridge.js 使用内存请求及模拟打印后端，不监听端口或发送实际打印任务。
