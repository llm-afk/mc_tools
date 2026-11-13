# 产测上位机源码维护

正式入口为项目根目录的电机产测软件.exe，源码入口为 python -m scripts.factory_test。
日常操作见 [产测人员使用手册](../../docs/产测人员使用手册.md)；该手册随 EXE 内置，可从顶部 Help 或 F1 查看。

## 模块职责

- desktop_entry.py：启动参数、单实例锁、启动失败诊断和 EXE 离线自检入口。
- gui.py、operator_view.py：Tk 主窗口、工位设置和异步事件处理。
- help_view.py：只读、可检索的离线使用手册，不连接设备或切换工作模式。
- station.py：11 步产测状态机、工件落盘、恢复和异常归档。
- maintenance.py、device_status.py：来料探测、身份核对、写入、校准和设备健康状态。
- firmware.py、firmware_library.py、firmware_settings.py：GD / ADM APP 检查、OTA 和持久固件选择。
- measurements/kt_curve、measurements/tn_curve：两项实际测量内核，由工位传入设备、目标与输出目录。
- measurements/console.py：测量进度事件与取消信号；不再依赖交互式终端。
- measurements/csv_streams.py、measurements/smoothing.py：离线匹配与平滑，不占用控制循环。
- manual_control.py、waveform.py：调试会话、实时目标、状态监视和绘图。
- integrations.py、printer_service.py：MES 取号、打印客户端及内置本机打印服务生命周期。
- report.py、delivery.py、upload.py、upload_queue.py、storage.py：指标、报告、持久队列和 OSS / MES 登记。
- station_config.py、service_credentials.py：配置与 Windows 当前用户保护的密钥。
- diagnostics.py、environment.py、network_status.py、lifecycle.py：诊断、脱敏、本机网络状态、环境摘要、日志导出和进程互斥。

motor_platform 提供实际通信协议、驱动与调度；services/tsc-print-service 提供打印服务和离线运行环境。
这些是上位机依赖。旧实验脚本、独立 TUI、旧报告重建入口和历史实验输出不再随项目保留。

## 运行边界

产测每件必须点击开始；设备监视只负责识别，不自动开始校准或测试。
连接调试后以 1 kHz 为目标监视状态，未使能时发送监视命令；使能后实时目标参与发送。
绘图使用独立刷新节奏，保留画布并压缩可见点，不能用画面刷新率推断 CAN 线上周期。
切换产测 / 调试先停止和释放当前模式，再异步进入下一模式；关闭窗口等待所属线程收尾。

只有一只 CANable 时自动选择，多只时人工选择；列表由实际 USB 枚举更新，不用离线固定 SN 占位。
电源自动探测先确认被动测功机串口，避免对未知设备发送 SCPI。

SN 各字段集中在“产品 / SN”。硬件 A / D 为数字输入，B / C 为可编辑选项；临时号为 12 个 0 + ABCD + 10 个 0。
A / D 不是硬件版本白名单；型号对应的已确认测量扭矩仍由平台 / 电机 BC 选择，避免借用错误测试参数。

校准和停机只依赖固件可观测的协议结果；已校准标志不一定证明新一轮已完成。
缺少可验证状态时保存异常并阻止后续动作，失能未确认会明确提示断开工装电源。
当前 GD / ADM 的 APP OTA 协议共用；BOOT 仅归档，由烧录工装处理。

## 指标与保存

KT 指标为 max|平滑测功机扭矩 - 实际下发目标| / 最大目标 × 100%，当前 LIMITS 中标准为 5%。
这包含增益、零偏和非线性误差，不能解释为仅相对最佳拟合线的非线性。
T-N 记录特性并校验流程 / 数据完整性，不设置额外的数值合格门限。
测功机帧与电机帧按接收时间一对一匹配，20 ms 内无匹配则留空，平滑在各自完整采样上先完成。

新工件使用用户数据目录下 data/<会话 UUID>，保存工件状态、SN 请求标识、阶段结果、报告和回执。
报告队列先上传 3 个文件至 OSS，再向 MES 登记。收到匹配 reportKey / reportId 的成功回执后才标记云端成功。
配置地址和密钥不等于远端验收通过；未知写入 / 打印结果不会盲目自动重放。
旧报告格式的读取和补传兼容仍保留，已有现场记录不因源码整理被改写。

## 诊断

真实日志在用户数据目录 logs/production。识别到电机即起连接日志；持续离开约 2 秒后结束。
预期重启 / 短暂丢包不立即拆日志。station 日志记录软件、配置和后台服务事件。
日志按 30 天保留，清理跳过活动文件；报告和工件记录不自动删除。

日志包含版本、连接 / 工件 / 请求标识、阶段开始结束和耗时、异常堆栈、协议索引、通信统计、服务请求与回执摘要。
启动和状态变化同步显示在主页面日志，带毫秒时间与告警颜色；枚举、加载、服务、队列和开测条件都有摘要。硬件读数微小变化不重复刷屏，后台保留独立详细证据。
network_status.py 通过隐藏的 PowerShell / .NET 本机属性读取接口、IP、网关和 DNS，独立线程运行；单次最多等待 5 秒，30 秒检查一次，变化时显示、稳定时仅低频写后台。不会 ping、解析业务域名或向生产接口试发请求，也不将本机连网当作云端成功。
密钥从配置加载后注册脱敏；禁止把真实 Key 写入源码、手册、构建清单或诊断包。
现场优先使用“工位配置 → 管理 → 导出本次启动日志”和“复制诊断信息”。

## 验证与构建

    python -B -m scripts.factory_test.checks
    python -B -m scripts.factory_test.build_release
    python -B -m scripts.factory_test.verify_release

检查拦截真实 USB、串口和网络。构建检查源码稳定性、回归、打印服务测试、隔离环境 EXE、完整模拟流程及 Help 内嵌手册，最后核对文件 SHA256。
编译和检查在系统临时目录完成，更新根目录 EXE 与 release-manifest.json 后清理临时产物。构建失败时保留临时诊断位置。
每次交付更新 version.py 与 pyproject.toml，标题、角落、日志、Windows 元数据和发布清单共用版本。
整包交付由用户压缩整个 pmt 文件夹完成，不生成独立部署目录或自动 ZIP。

这些离线结果不代表真实校准、OTA、物理出纸或生产云端已验收，实机联调须分别确认。
