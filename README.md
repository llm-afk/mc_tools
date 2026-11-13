# PMT · 产线电机测试

PMT 是 Production Motor Test 的缩写。本目录只维护电机产测上位机及其运行、维护和验证依赖。

双击根目录 **电机产测软件.exe** 运行。交付时直接压缩整个 pmt 目录，在另一台 Windows x64 电脑完整解压。

## 目录

    pmt/
    ├─ 电机产测软件.exe       唯一上位机入口
    ├─ scripts/factory_test/   界面、流程、固件、MES、打印、报告、日志与构建
    │  └─ measurements/       内部 KT、T-N 测量内核、采集和数据处理
    ├─ motor_platform/        电机、CANable、测功机、电源及周期通信
    ├─ services/              内置打印服务与离线 Node.js 运行环境
    ├─ drivers/               CANable 与串口设备的 Windows 驱动
    ├─ firmware/              随包 APP 候选与 BOOT 归档目录说明
    ├─ docs/                  使用手册、设备协议和报告接口
    ├─ tests/                 上位机及依赖组件的离线检查
    ├─ install_drivers.cmd    驱动安装入口
    └─ release-manifest.json  版本、文件指纹和验证结果

工位配置、加密密钥、运行日志、新报告和已选固件默认保存在 %LOCALAPPDATA%\MirrorMe\FactoryStation。
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
