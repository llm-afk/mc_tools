# 产线报告上传接口与持久队列

本文对应当前上位机；现场操作见 [产测人员使用手册](产测人员使用手册.md)。

## 流程和成功判据

真实测试 PASS 后，还需完成正式 SN 写入核对、标签打印回执及本地报告保存，再进入生产上传队列。
流程最后一步表示可靠提交本地队列；独立后台线程先上传报告 PNG 和两份 CSV 至 OSS，再向 MES 登记。
本地通过、OSS 某文件成功和最终 MES 登记是不同状态。网络失败不修改原测量结果，也不丢弃队列任务。

MES 成功需同时满足 HTTP 200、JSON code=200、匹配的 reportKey 与有效 reportId。duplicated=true 可作为幂等成功。
禁止仅凭 HTTP 200 记为成功；携带密钥的请求不跟随重定向。

## 工位配置

所有现场参数均可在“工位配置 → 服务接口”设置并保存：

- SN 取号：MES 基础地址、接口路径、Key 请求头名称、取号 API Key。
- 报告上报：报告完整 URL、Key 请求头名称、报告 API Key。
- OSS：Endpoint、Bucket、Region、目录前缀、AccessKey ID 和 Secret。

新工位的常用地址：

- SN：https://www.mirrormetech.com/mes-api/open-api/module-sn/batch-generate
- 报告：https://www.mirrormetech.com/mes-api/open-api/test-report
- OSS Endpoint：https://oss-cn-hangzhou.aliyuncs.com
- Bucket：mirrormetech-mes-testreport；Region：cn-hangzhou；前缀：test-report。

默认鉴权头为 X-Api-Key。取号与报告 Key 分开保存，不能默认相同。
密钥默认空，现场通过界面填写后经 Windows 当前用户保护，与公开配置原子保存在 station.json。
已有工位的显式配置优先，不因软件升级覆盖。旧凭据文件和环境变量的兼容读取保留，但不再随项目携带开发机旧配置。

地址预设不代表服务可用或已通过鉴权。已有待传任务封存其 MES / OSS 目标，修改新配置不自动迁移旧任务。

## 文件与 MES 请求

当前工件保存在 %LOCALAPPDATA%\MirrorMe\FactoryStation\data\<会话 UUID>。
reportKey 使用本件正式 SN、测试北京时间和工件标识生成，不能仅靠 UUID 文件夹名猜测。
历史 yyyyMMdd_HHmmss_SN 目录及可选的数字尾号仍支持读取。

本件上传三个文件：report.png、kt/kt_curve.csv、tn/curve.csv；也兼容已发布为目录根部的两个 CSV。
OSS 对象位于同一前缀：

    test-report/{yyyyMM}/{sn}/{reportKey}/
        report.png
        kt_curve.csv
        curve.csv

配置前缀可调整，其余层级由当前工件生成。三个对象分别上传，MES 登记一次，fileCount=3。
MES 请求字段为 sn、reportKey、testTime、files、fileCount；files 是对象 Key，不带域名。
OSS 目标须与后端一致，私有文件的读取地址由后端处理。

## 可靠性与排障

- 请求之前保存待传意图和原始请求；文件以 SHA256 封存，已确认 OSS 对象记录回执。
- 重试沿用原 reportKey、测试时间、文件快照和目标环境，已获回执的 OSS 文件不重复上传。
- OSS 回执丢失时可以向同一 Key 写相同快照，MES 回执丢失后用原 reportKey 幂等登记。
- 文件 / 请求不一致、损坏 JSON 或目标环境变化会阻止沿用旧回执，保留证据等待处理。
- 网络临时失败后台退避重试；认证、服务路由等问题标为需处理并定期复查。重启软件恢复队列。
- 成功回执先持久落盘，再移除待传请求；图片、CSV 和状态记录保留。
- 数据使用 flush / fsync 与原子替换，报告锁覆盖上传，避免多个工作者覆盖同一记录。
- 本地保存 / 渲染中断时，按已有原始数据和检查点恢复，不重新驱动电机或把不合格数据改成 PASS。
- 后台不使用 CAN 锁，不阻塞调试滑块；报告不按日志的 30 天策略自动删除。

upload-state.json 是远端成功证据，upload-queue.json 和 upload-pending.json 等记录负责恢复；旧分离状态格式仍按 storage.metadata 读取。
搬迁现场记录需保留完整 FactoryStation 数据目录及已有旧报告的关联 .factory-state，不能只复制图片。

主页面底部显示待传、需处理和已完成数量。通过“打开报告目录”找到报告，用“导出本次启动日志”取得版本、阶段、服务目标及脱敏错误。
源码 upload.py 的维护 CLI 仍保留供已有报告补传；正常工人不需要运行命令或手工改队列。
历史联调输出和临时凭据备份已从项目清理，离线模拟成功不代表正式云端验收完成。
