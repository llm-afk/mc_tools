"""Build the production application and its matching deployment ZIP."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile
from .version import VERSION, VERSION_LABEL


ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files():
    yield ROOT/'pyproject.toml'
    for name in ('motor_platform', 'scripts', 'utils', 'tests', 'docs'):
        for path in sorted((ROOT/name).rglob('*')):
            if any(part in ('data', 'drivers', '__pycache__', '.factory-state') for part in path.relative_to(ROOT).parts):
                continue
            if path.is_file() and (path.suffix in ('.py', '.pyw', '.spec', '.cmd', '.ps1', '.md', '.c', '.h')
                                   or (path.suffix == '.png' and 'assets' in path.parts)):
                yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'dist/电机产测软件')
    args = parser.parse_args()
    output = args.output.resolve()
    archive = output.with_name(output.name+'-部署包.zip')
    if output.exists() or archive.exists():
        raise FileExistsError(f'发布目录或 ZIP 已存在，禁止覆盖：{output} / {archive}')
    initial_sources = {p.relative_to(ROOT).as_posix():digest(p) for p in source_files()}
    work = ROOT/'build/factory_test'/uuid.uuid4().hex[:12]
    work.mkdir(parents=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYINSTALLER_CONFIG_DIR=str(work/'cache'))
    temporary = work/'tmp'
    temporary.mkdir()
    env.update(TEMP=str(temporary), TMP=str(temporary))
    log = work/'build.log'
    print(f'Building current source; log: {log}', flush=True)
    with log.open('w', encoding='utf-8') as stream:
        subprocess.run([sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm',
                        '--distpath', str(work/'frozen'), '--workpath', str(work/'work'),
                        str(ROOT/'scripts/factory_test/FactoryTestStandalone.spec')],
                       cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True)
    executable = work/'frozen/电机产测软件.exe'
    isolated = work/'isolated'
    isolated.mkdir()
    isolated_exe = isolated/executable.name
    shutil.copy2(executable, isolated_exe)
    check = work/'self-check.json'
    isolated_env = dict(env)
    for name in ('PYTHONHOME', 'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY'):
        isolated_env.pop(name, None)
    windows = Path(env.get('SystemRoot', r'C:\Windows'))
    isolated_env.update(PATH=os.pathsep.join((str(windows/'System32'), str(windows))),
                        LOCALAPPDATA=str(isolated/'profile'))
    print('Checking isolated EXE, white UI and complete simulated workflow...', flush=True)
    subprocess.run([str(isolated_exe), '--self-check', str(check)], cwd=isolated, env=isolated_env,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True, timeout=120)
    result = json.loads(check.read_text(encoding='utf-8'))
    if (not all(result.get(name) is True for name in ('ok', 'frozen', 'production_defaults', 'single_production_ui',
                                                   'adapter_auto_selection', 'adapter_dropdown_live', 'adapter_online_choices_only',
                                                   'adapter_manual_selection', 'adapter_unplug_clears_selection',
                                                   'adapter_single_auto_connect','mode_handoff_async','debug_auto_connect',
                                                   'gui_event_processing_bounded','debug_monitor_on_connect',
                                                   'debug_settings_accessible','version_display','waveform_retained',
                                                   'waveform_time_axis_stable','waveform_target_solid','slider_wheel',
                                                   'hardware_revision_entries','revision_whitelist_removed',
                                                   'debug_enable_without_sn_whitelist','firmware_decimal','legacy_config_field_removed',
                                                   'live_targets_default','send_controls_removed','position_wheel_step_0_1',
                                                   'waveform_thin','waveform_60fps'))
            or result.get('version') != VERSION
            or result.get('hardware_or_network_used') is not False or result.get('usb_open_attempts') != 0):
        raise RuntimeError(result)
    current_sources = {p.relative_to(ROOT).as_posix():digest(p) for p in source_files()}
    if current_sources != initial_sources:
        raise RuntimeError('构建期间源码发生变化，停止发布；请从当前源码重新构建。')
    release = work/'release'/output.name
    release.mkdir(parents=True)
    shutil.copy2(executable, release/executable.name)
    shutil.copytree(ROOT/'scripts/factory_test/drivers', release/'drivers')
    for path in source_files():
        target = release/'源码'/path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    copied_sources = {p.relative_to(release/'源码').as_posix():digest(p)
                      for p in (release/'源码').rglob('*') if p.is_file()}
    if copied_sources != initial_sources:
        raise RuntimeError('配套源码与构建开始时的快照不一致，停止发布。')
    (release/'install_drivers.cmd').write_bytes(
        b'@echo off\r\nsetlocal\r\nfltmc >nul 2>nul\r\nif errorlevel 1 (\r\n'
        b'  echo Run this file as administrator to install USB drivers.\r\n  pause\r\n  exit /b 1\r\n)\r\n'
        b'pnputil.exe /add-driver "%~dp0drivers\\*.inf" /subdirs /install\r\n'
        b'if errorlevel 1 (\r\n  echo Windows reported a driver installation error.\r\n  pause\r\n  exit /b 1\r\n)\r\n'
        b'echo Driver processing finished. Reconnect USB devices and open the application.\r\npause\r\n')
    (release/'使用说明.txt').write_text(
        '电机产测软件（Windows x64）\n\n'
        '完整解压后，双击“电机产测软件.exe”运行，无需安装 Python。\n'
        '本包只提供一个正式入口，没有测试版或预览版。\n'
        '顶部菜单：自动产测 / 电机调试 / 工位配置。\n'
        f'上位机版本：{VERSION_LABEL}；标题栏、右下角、EXE 属性和日志版本保持一致，每次修改发布时递增。\n'
        'CANable SN 下拉只列在线适配器：只有一只时自动选中连接，多只时由工程人员选择；每 0.5 秒刷新，无设备时留空。\n'
        '切换页面会自动停止原模式、等待连接释放，再启动新模式；进入电机调试会自动连接，连接不使能电机。\n'
        '调试连接成功后后台按目标 1 kHz 发送零目标 RPDO5 并接收状态，未使能也显示波形和温度；停止电机后继续监视。\n'
        '默认实时参数，已移除模式选择和下发按钮；滑块、滚轮、文本输入直接更新待发送目标。\n'
        '未使能可预设参数，点击使能后使用当前界面值；停止后保留预设，状态采集帧仍固定零目标。\n'
        '后台持续发送最新目标，连续编辑不排队补发旧值；橙色曲线为待发目标，日志/显示 CSV 同时记录实际发送目标及使能状态。\n'
        '显示采样目标 100 Hz，绘图目标 60 帧/秒；反馈蓝色、目标橙色，均为细实线，暂停显示不停止通信。\n'
        '横坐标显示时间 / s，刻度固定并统一小数精度；位置滚轮及滑块拖动步进 0.1 rad。\n'
        '参数滑块及输入框可用鼠标滚轮调整；固件版本显示为十进制。\n'
        '硬件 A/D 为一位数字输入，B/C 保留可编辑下拉；结构/硬件版本不作为使能白名单。\n'
        '调试中可打开工位配置；保存时自动停止、释放连接，再以新配置自动连接，不自动重新使能。\n'
        '产线流程：接线 → 点击开始 → 全自动执行 → 取下并贴标 → 换下一台 → 再点击开始。\n'
        '已预设对接文档中的正式服务地址，密钥在“工位配置 → 服务接口”中填写。\n'
        'SN 取号：https://www.mirrormetech.com/mes-api/open-api/module-sn/batch-generate\n'
        '报告上报：https://www.mirrormetech.com/mes-api/open-api/test-report\n'
        '本机打印：http://127.0.0.1:9876（状态 /api/status，打印 /api/print）\n'
        'OSS：https://oss-cn-hangzhou.aliyuncs.com；Bucket mirrormetech-mes-testreport；Region cn-hangzhou；前缀 test-report。\n'
        '本包不携带开发机配置、密钥、历史报告或旧版上传配置。\n\n'
        '临时 SN 按工位硬件版本自动生成：12 个字符 0 + ABCD + 10 个字符 0，共 26 位。\n'
        '正式 SN 的项目、分类、工厂、硬件版本和日期共用工位产品配置，由 MES 分配流水号。\n'
        '在“产品 / SN”中可以下拉选择，也可以直接输入字段代码；硬件 ABCD 每位支持单字符输入。\n'
        '输入框按各字段位数限制长度，超长键入或粘贴会直接拦截，不按下拉枚举限制手填编码。\n'
        '新增取号/打印/校准完整流程仍待实机联调。\n'
        '报告上报已恢复原 OSS/MES 后台队列，网络异常不改变电机测试结果，重启后继续补传。\n'
        '工位配置“服务接口”可设置取号/打印/报告地址、接口路径、API Key 请求头名称和密钥。\n'
        'OSS 支持 Endpoint、Bucket、Region、AccessKey ID/Secret、目录前缀；打印支持名称和每台份数。\n'
        '点击“保存并关闭”后生效并记忆；密钥按 Windows 当前用户加密保存，换电脑/用户后应重新录入。\n'
        '界面 Key 优先，也可复用旧版环境变量和外置 upload-config.json；没有新增超时或重试配置。\n'
        '新工位默认使用上述正式地址；现场可修改。已有配置不会被新默认值静默覆盖。\n'
        '报告地址显式留空时可读取外置配置的 mes_url；已有报告仍绑定原 MES/OSS 目标。\n'
        '上报成功须收到匹配的 MES reportId / reportKey 回执，本地通过和上报成功分别显示。\n'
        '服务地址预设和离线自检不代表正式服务或实体设备已经联调通过。\n\n'
        '配置与数据：%LOCALAPPDATA%\\MirrorMe\\FactoryStation\\\n'
        'station.json 保存工位配置；data\\ 保存工件记录与报告。\n'
        '右下角“打开日志目录”和“打开报告目录”打开各自文件夹，由现场人员选择具体文件。\n'
        'logs/production/ 每个电机从识别连接到确认断开保存一份日志。\n'
        '日志包含配置、步骤耗时、异常堆栈、协议维护、取号/打印与上报诊断，凭据脱敏。\n'
        '失败可按阶段、错误编号及排查方向定位；HTTP 错误正文、服务 request-id、SDO 原始帧和上传文件哈希均保留诊断信息。\n'
        '打印不完整或矛盾的应答按结果未知处理，不自动补打；详细错误编号见源码/scripts/factory_test/README.md。\n'
        '后台补传关联原连接日志；无电机时的工位事件按天保存。\n'
        '日志保留 30 天，启动时及运行中每小时自动清理到期日志，正在记录的连接日志除外。\n'
        '自动清理不删除报告、CSV、工件记录、upload-state.json、upload-queue.json 或 pending/。\n'
        '本包不迁移旧版历史报告或改变旧回执环境，不包含任何上传凭证。\n'
        '新电脑缺少驱动时，可由工程人员以管理员身份运行 install_drivers.cmd。\n\n'
        '源码构建：python -m scripts.factory_test.build_release --output <新的发布目录>\n'
        '源码\\ 内为本次构建配套源码；release-manifest.json 记录文件哈希和独立自检结果。\n',
        encoding='utf-8-sig')
    manifest = dict(version=VERSION, channel='production', built_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                    executable=executable.name, sha256=digest(executable), bytes=executable.stat().st_size,
                    python=sys.version, isolated_self_check=result,
                    files={p.relative_to(release).as_posix(): digest(p) for p in sorted(release.rglob('*')) if p.is_file()})
    (release/'release-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    staged_zip = work/'deployment.zip'
    with zipfile.ZipFile(staged_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as package:
        for path in sorted(release.rglob('*')):
            if path.is_file():
                package.write(path, (Path(output.name)/path.relative_to(release)).as_posix())
    with zipfile.ZipFile(staged_zip) as package:
        if package.testzip() is not None:
            raise RuntimeError('ZIP integrity check failed')
        for name, expected in manifest['files'].items():
            if hashlib.sha256(package.read(output.name+'/'+name)).hexdigest() != expected:
                raise RuntimeError(f'ZIP hash mismatch: {name}')
    output.parent.mkdir(parents=True, exist_ok=True)
    release.rename(output)
    staged_zip.rename(archive)
    print(f'Release: {output}\nZIP: {archive}\nEXE: {manifest["bytes"]/1024/1024:.1f} MiB; SHA256: {manifest["sha256"]}', flush=True)


if __name__ == '__main__':
    main()
