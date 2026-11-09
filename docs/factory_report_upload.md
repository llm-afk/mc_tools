# 产线报告上传

## 当前正式上位机

原有 OSS 三文件上传、MES 登记、持久队列、哈希封存、环境绑定和幂等回执均已接入。
当前版本使用 UUID 工件目录，不搬动历史数据；按最终正式 SN、测试北京时间和会话生成兼容原协议的 reportKey。
基础测试 PASS 之后，必须完成正式 SN 写入、打印确认和 PNG 保存，才允许进入生产上传队列。
流程最后一步表示可靠提交上传队列，远端上传由独立线程执行；网络问题不将测试 PASS 改成 FAIL。

工位配置的“服务接口”中，可设置报告 MES 完整地址、鉴权头名称、报告 API Key，
以及 OSS Endpoint、Bucket、Region、AccessKey ID/Secret 和对象目录前缀。
服务修改点击“保存并关闭”后生效；密钥经 Windows 当前用户加密后与公开配置原子保存在 station.json，明文不进入日志或发布包。
旧上传配置仍支持 `access_key_id`、`access_key_secret`、`mes_api_key` 和 `mes_url`，可作为空白密钥字段的兼容来源。
新工位的报告接口默认 `https://www.mirrormetech.com/mes-api/open-api/test-report`，
取号默认同域名 `/mes-api/open-api/module-sn/batch-generate`；上传模块的新任务默认值也统一为正式地址。
已有 station.json 中的显式地址优先。报告地址显式清空时读取外置文件的 mes_url；两处均为空则保留本地待传。
地址预设不等于远端已验收；待传记录继续按各自封存的 MES/OSS 地址校验，不自动迁移。
原版历史报告及其回执不自动迁移到新环境。以下联调记录是历史证据，不能视为当前正式云端已验收。

工位数据位于 `%LOCALAPPDATA%\MirrorMe\FactoryStation\data\<会话UUID>\`。
该目录内的 upload-state.json 是远端成功依据；upload-queue.json、pending/ 和 upload-pending.json 负责恢复待传。
完整搬迁时保留整个工位数据目录。模拟报告不会被后台扫描进生产上传队列。

正常生产：真实测试 PASS → 报告和待传记录可靠落盘 → 自动返回第一步，可继续下一台。独立后台任务上传 3 个文件到 OSS → 向 MES 登记 → 保存成功回执 → 报告档案标记完成。测试通过与MES上报完成是两个独立状态。

窗口不显示上传按钮，保留 **查看测试数据**。测试工作线程完成停机和本地报告保存后，将报告加入持久队列，不等待网络。工程联调、失败补传通过下方命令行工具指定已有报告目录，无需连接电机、校准或测试 PASS。

## 同一报告文件夹

本地目录名 `yyyyMMdd_HHmmss_26位SN` 决定 `reportKey`、SN 和测试时间（北京时间）；兼容目录末尾附加数字序号；新产线报告追加64位随机数的十进制尾号，降低跨工位同秒同SN的冲突风险。三个本地文件是：

- `report.png`
- `kt/kt_curve.csv`（兼容目录根部的 `kt_curve.csv`）
- `tn/curve.csv`（兼容目录根部的 `curve.csv`）

已本地预检 `20260915_205754_00000000000001100000000000`：目录名 42 位，SN 26 位，测试时间为 `2026-09-15 20:57:54`，三个文件齐全。目录名符合 `reportKey` 的 `[A-Za-z0-9_]{10,128}` 限制；SN 是否已登记到 MES 仍需接口校验。

OSS 默认 Bucket 为 `mirrormetech-mes-testreport`，区域为杭州；工位设置可以修改地址、桶、区域和目录前缀。
默认三个文件共同位于：

```
test-report/{yyyyMM}/{sn}/{reportKey}/
    report.png
    kt_curve.csv
    curve.csv
```

OSS 的文件夹通过共同 Key 前缀组织；三个文件分别 PutObject，MES 只登记一次报告，`fileCount=3`。请求字段严格为 `sn`、`reportKey`、`testTime`、`files`、`fileCount`；`files` 为三个对象 Key，不带域名或 HTTPS 前缀。私有文件的读取链接由 MES 生成。
因此 OSS 目标修改须与 MES 后端配置保持一致。待传记录封存原 OSS 目标，不能用新桶复用旧桶的回执；
目录前缀修改也不会绕过文件/请求封存检查。原文件和凭据保留供恢复原设置后继续补传。

OSS 使用官方 Python SDK 的 HTTPS/V4 签名上传，参考 [阿里云简单上传文档](https://www.alibabacloud.com/help/en/oss/developer-reference/simple-upload-1)。MES 地址按对接文档配置为 `https://www.mirrormetech.com/mes-api/open-api/test-report`。

## 本机配置与直接试传

密钥保存在 `%LOCALAPPDATA%\MotorFactoryTest\upload-config.json`，不提交到仓库、不打进 EXE、不输出到日志。每台产线电脑由维护人员配置。源码版可从运维 CSV 和协议文档导入：

```powershell
python -m scripts.factory_test.upload --credentials-csv '运维凭证.csv' --protocol-doc 'HTTP协议文档.md'
python -m scripts.factory_test.upload '完整报告目录' --attempts 1
```

配置字段为 `access_key_id`、`access_key_secret`、`mes_api_key`。GUI队列每轮尝试一次；临时网络错误按5、15、30、60、120秒退避，随后每300秒继续重试，不因次数耗尽丢弃任务。认证、SN未登记、服务路由等问题显示“需处理”，每600秒复查，问题修复后自动继续。CLI `--attempts` 为单次命令的有限重试次数，仍为1～10次。

必须同时满足 HTTP 200、JSON `code=200`、回执 `reportKey` 匹配且有 `reportId` 才算 MES 成功。`duplicated=true` 是成功。禁止将 HTTP 200 单独作为成功依据；HTTP 重定向不携带密钥跟随。

## 失败保留及补传

- 所有网络请求前先保存 `pending/{reportKey}.json`，内容为原始 MES 请求。
- `upload-state.json` 记录三个文件的 SHA256、每个 OSS 成功回执、尝试记录和最终 MES 回执。
- 命令行再次指定同一报告目录即可补传。已收到 OSS 成功回执的文件不重复上传；MES 重试始终使用同一 `reportKey`。
- OSS ACK 丢失时，重试向同一 Key 写相同的文件快照，不需要该写入账号的读取或删除权限。
- 文件或请求发生变化时拒绝沿用旧上传记录，要求使用新的报告目录。
- 成功后仅移除待传请求 JSON；PNG、CSV 和上传回执始终保留。

## 自动恢复与操作提示

- `upload-queue.json` 持久保存状态、尝试次数、下次尝试时间和最后原因。软件运行时后台检查；关闭期间不联网，下次启动继续。
- EXE 默认保存到 `EXE所在目录/MotorFactoryTest/data`；该目录不可写时启动时回退到 `%LOCALAPPDATA%/MotorFactoryTest/data` 并提示，运行中不再切换。源码版仍保存到 `scripts/factory_test/data`。升级时保留数据文件夹。
- 启动及运行中每30秒扫描当前数据根目录、同版本源码数据目录、本机 `%LOCALAPPDATA%/MotorFactoryTest/data` 的直接子目录；EXE 版还扫描旁边的 `MotorFactoryTest/data` 和旧 `data` 目录。真实PASS报告即使尚未来得及建立队列也会自动补入；不递归扫描联调子目录，不上传模拟报告、失败或未完成的测试。
- `report-input.json` 在PNG渲染前保存测试结果检查点。进程在渲染/保存清单期间中断时，后台可重新分析已有CSV并生成报告，不重新驱动电机测试。缺失或不合格数据不会被强行改成PASS。
- 数据写入先flush/fsync，再原子替换JSON；不同进程由操作系统文件锁互斥，进程崩溃后锁自动释放。单份报告锁覆盖完整上传，队列和CLI不能同时覆盖同一份记录。
- `upload-state.json` 是上传结果的依据；成功回执先落盘，再删除pending请求。重启时修复队列、summary和commissioning的镜像状态，已成功报告不重复联网提交。请求重放保留原reportKey、测试时间、文件字节和哈希。
- 网络失败仅在状态变化时记日志，底部持续显示待上传数量。SN/凭证/本地记录等问题显示红色需处理数量及原因。成功显示绿色并记录MES报告编号，不弹确认框，也不改变下一台电机的状态。
- 本地报告保存失败保持当前电机流程，提示磁盘/权限问题并每30秒重试；不能把未保存完整的数据当作已可靠归档。缺文件、哈希不一致、损坏的JSON保留原状并阻止上传，不自动覆盖证据；其他报告仍继续处理。
- 网络上传线程不占CAN通信锁，不参与滑块或电机控制。所有报告文件持续保留，当前不自动删除历史数据，需要维护磁盘容量。
- 队列永久绑定MES地址。默认仍为云端URL；本地验证使用独立报告目录和显式 `--mes-url http://192.168.1.203:8091/open-api/test-report`。禁止把本地成功当作云端成功；更换环境需要明确安排迁移，不能直接篡改旧回执。

## 2026-09-23 实际联调记录

使用用户指定目录 `20260915_205127_00000000000001100000000000`，SN 按原目录保留。

三个 OSS 文件上传均成功，已有 ETag/Request ID 保存在原目录的 `upload-state.json`。两次按文档地址提交 MES，均返回业务 `code=500`：

```
No static resource open-api/test-report for request '/open-api/test-report'.
```

当前阻塞点是 MES 未找到该接口资源，需要 MES 维护人员核对实际部署和路由。接口恢复后直接重试原目录即可；本次尚不能记为完整上传成功。

只读检查 `https://www.mirrormetech.com/mes-api/v3/api-docs`，公开 OpenAPI 清单也未包含 `/open-api/test-report`；可见的 report 路由为生产执行报告等其他业务，不能替换使用。这与两次真实 POST 的未找到资源响应一致。

### 本地 MES 验证

用户确认云端服务尚未开启后，改为验证 `http://192.168.1.203:8091`。其 `/v3/api-docs` 已包含 `POST /open-api/test-report`，使用 `X-Api-Key`，直连地址不加 `/mes-api` 前缀。

复用上述报告的三个 OSS 成功回执，核对原文件 SHA256 和请求体一致后，实际提交到 `http://192.168.1.203:8091/open-api/test-report`。返回业务 `code=500`：`模组SN未在生产系统中找到，请确认已执行「关节程序烧入+SN写入」: 00000000000001100000000000`。

本地接口已可达并进入 SN 业务校验，尚未获得成功报告编号。需要在本地 MES 登记此测试 SN，或使用已登记 SN 的对应报告继续验证。本次结果保存在原报告目录的 `local-mes-validation.json`；生产 `upload-state.json`、待传请求、PNG 和 CSV 均保持不变，应用默认地址仍为云端地址。

用户随后指定 `C2KNEE0PROJS01412699990001` 并明确允许生成模拟报告。生成目录为 `scripts/factory_test/data/mes-local-validation/20260923_162447_C2KNEE0PROJS01412699990001`，PNG 标注模拟样张，两个 CSV 的 `data_source` 标注为接口联调模拟数据。三个文件均已实际上传到 OSS 同一报告前缀；本地 MES 仍返回 `code=500`，提示该 SN 未在生产系统中找到。没有获得 MES 报告编号，未改变产线完成状态。该目录内 `local-mes-validation.json`、`upload-state.json` 和 `pending/` 保存验证结果、三个 OSS 回执和待传请求。登记 SN 后可复用现有 OSS 文件，仅补报本地 MES。

用户要求再次尝试后，使用同一 `reportKey` 和三个已核验的 OSS 回执，仅补报本地 MES，成功收到 HTTP 200、业务 `code=200` 和匹配的报告回执：`reportId=12`、`duplicated=false`。本地模拟报告上传链路已跑通；成功回执已落盘，对应 pending JSON 已移除，PNG 和两个 CSV 保留。此结果仅验证局域网服务，默认云端地址和真实电机产线完成状态未改变。

### 可靠性真实联调（临时 SN 01/02/03）

2026-09-23 使用用户授权的模拟报告，对同一本地服务进行验证：

- SN 01 重放原报告：`reportId=12`、`duplicated=true`，未产生第二条报告。
- SN 02 第一个OSS文件成功后人为注入一次连接中断，新建Uploader实例恢复后只上传剩余两个文件，MES报告编号13。
- SN 03 在MES实际接受后人为丢弃成功回包，新建Uploader实例重试后返回同一报告编号14、`duplicated=true`，OSS文件重传次数为0。

证据：`scripts/factory_test/data/mes-local-validation/reliability-validation.json`，具体目录中另有文件哈希及上传回执。这些是模拟报告和受控故障注入，不代表真实电机测试或生产云端服务已经部署。

### 新版可迁移发布目录（2026-09-23）

EXE、upload-config.json、data、.factory-state 与源码同级。新报告只含 report.png、kt_curve.csv、curve.csv；summary、渲染恢复点、上传状态与 OS 锁位于 .factory-state/data/<报告名>/。OSS Key 和 MES reportKey 不变。旧报告使用原位置的锁和回执，避免与旧版本同时处理时失去互斥。新格式整体搬迁后复用原哈希、环境绑定、OSS 回执和 MES 幂等键；不要仅迁移 data 而丢失状态目录。

### 当前联调地址配置

用户确认继续使用局域网 MES。新版从 EXE 同级 `upload-config.json` 的 `mes_url` 读取地址，当前值及缺省值均为 `http://192.168.1.203:8091/open-api/test-report`，重启后生效。三个凭证字段仍保留。配置无效时停止上传并在日志提示，测试数据继续保留；启动日志显示配置路径与实际上报地址。旧版无 endpoint 的历史回执仍按原云端环境解释，不因默认值变化而认作局域网成功。

原默认云端地址明确返回 `No static resource open-api/test-report` 的失败任务，在所有历史尝试均为此类明确拒绝、没有成功回执且文件哈希/请求一致时，可自动补报上述局域网服务；保留 OSS 回执和 reportKey，记录地址修复审计，原子写状态后再写队列，崩溃后可续接。成功、超时结果不明、其他服务器地址和修改过的文件不自动迁移。

本次只读 OpenAPI 检查返回 HTTP 200，存在 POST 接口；重放此前 SN03 联调报告返回 reportId=14、duplicated=true，未调用 OSS PUT。证据保存在 `build/factory_test/mes-config-live-check.json`。这验证的是开发电脑至局域网服务的连通与幂等行为，不替代新产线电脑的实际网络验证。
