# PMT · 产线电机测试

PMT 是 Production Motor Test 的缩写。本目录只维护电机产测上位机及其运行、维护和验证依赖。

双击根目录 **电机产测软件.exe** 运行。仓库包含正式 EXE、打印运行环境和固件，Windows x64 电脑完整 clone 后即可运行，也可以压缩整个 pmt 目录交付。

## 目录

    pmt/
    ├─ 电机产测软件.exe       唯一上位机入口
    ├─ scripts/factory_test/   界面、流程、固件、MES、打印、报告、日志与构建
    │  └─ measurements/       内部 KT、T-N 测量内核、采集和数据处理
    ├─ motor_platform/        电机、CANable、测功机、电源及周期通信
    ├─ services/              内置打印服务与离线 Node.js 运行环境
    ├─ drivers/               设备驱动、驱动清单和统一自动安装程序
    ├─ firmware/              APP 固件，按文件名数字版本自动选取
    ├─ docs/                  使用手册、设备协议和报告接口
    ├─ tests/                 上位机及依赖组件的离线检查
    ├─ install_drivers.cmd    驱动安装入口
    └─ release-manifest.json  版本、文件指纹和验证结果

工位配置、加密密钥、运行日志和新报告默认保存在 %LOCALAPPDATA%\MirrorMe\FactoryStation。
APP 直接放在根目录 EXE 旁的 firmware 文件夹；更换该文件后自动重新检查版本。
旧的独立实验脚本、终端工具、历史实验输出和重复部署目录已清理。

## 文档

- [产测人员使用手册](docs/产测人员使用手册.md)：软件顶部 Help 或 F1 也可离线查看。
- [源码维护说明](scripts/factory_test/README.md)
- [报告上传接口](docs/factory_report_upload.md)

## 源码运行与构建

需要 Python 3.11 或更高版本。现场运行 EXE 不需要安装 Python。

    python -m pip install -e ".[build]"
    python -m scripts.factory_test
    python -B -m scripts.factory_test.checks
    python -B -m scripts.factory_test.build_release
    python -B -m scripts.factory_test.verify_release

构建在系统临时目录完成编译和自检，更新根目录 EXE 与清单后清理临时文件。
现场产品、设备、服务地址和密钥在“工位配置”中设置并记忆。

软件启动及插入设备后会检查驱动，缺少时等待工位空闲再安装。Windows 可能要求管理员授权。已正常工作的驱动保持原样，安装后回读设备绑定和队列，不发送测试标签。停止并关闭上位机后，根目录 install_drivers.cmd 可用于工程人员重试，详情写入工位日志。

本机交付目录已附带官方 T-4502 PRO 12.4 驱动，可离线安装。厂商许可要求公开镜像另获授权，因此公开仓库暂不包含该原始安装包；clone 后首次需要它时，从厂商官网获取并校验哈希和数字签名，缓存在用户数据目录。需要离线交付时压缩本机整个 pmt 目录，保留 drivers/tsc_printer/Generic_12.4.0.exe。

## 同步更新

开发电脑通过离线构建生成最新 EXE 和发布清单，将源码与正式运行文件一起提交、推送到 main。

其他电脑首次获取项目时执行以下命令。

    git clone https://github.com/llm-afk/pmt.git

后续更新先正常关闭上位机，再在 PowerShell 中进入已有目录执行。

    cd "$env:USERPROFILE\Desktop\pmt"
    git pull --ff-only

更新后重新运行根目录 EXE，核对标题栏版本。工位设置、加密密钥、日志和报告保存在当前 Windows 用户目录，继续沿用本机已保存的配置。
