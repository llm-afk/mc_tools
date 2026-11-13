"""Local environment summary; never probes hardware or production services."""
import os
from pathlib import Path
import platform
import sys

from .version import VERSION_LABEL


CREDENTIAL_LABELS = {'sn_api_key': '取号 API Key', 'mes_api_key': '报告 API Key',
                     'access_key_id': 'OSS AccessKey ID', 'access_key_secret': 'OSS AccessKey Secret'}


def credential_status(config, saved, *, environ=None, credential_error=''):
    environ = os.environ if environ is None else environ
    result = {}
    for name in CREDENTIAL_LABELS:
        if credential_error:
            result[name] = '无法读取已保存密钥'
        elif saved.get(name):
            result[name] = '已保存'
        elif name == 'sn_api_key' and environ.get(config.api_key_env):
            result[name] = '已配置环境变量'
        else:
            result[name] = '未填写'
    return result


def environment_summary(config, saved, *, config_path, storage_root, dependency_failures=None,
                        credential_error='', environ=None):
    """Describe effective saved settings, exposing only the presence of credentials."""
    from .upload import selected_config_path
    from .firmware import firmware_root
    credentials = credential_status(config, saved, environ=environ, credential_error=credential_error)
    try:
        legacy = Path(config.upload_config).expanduser() if config.upload_config else selected_config_path()
        legacy_state = '检测到文件（未校验内容）' if legacy.is_file() else '未检测到文件'
    except OSError:
        legacy_state = '无法访问，请检查路径和权限'
    dependencies = ('检查中' if dependency_failures is None else
                    '缺少或无法加载：' + '；'.join(dependency_failures) if dependency_failures else '已通过本机依赖加载检查')
    root = Path(storage_root)
    lines = [f'上位机 {VERSION_LABEL} · {platform.system()} {platform.release()} · {platform.machine()}',
             '运行方式：' + ('独立 EXE，无需安装 Python' if getattr(sys, 'frozen', False) else 'Python 源码'),
             '程序：' + sys.executable,
             '运行依赖：' + dependencies,
             '配置文件：' + str(config_path),
             '数据目录：' + str(root),
             '驱动器固件目录：'+str(firmware_root(config)),
             '日志：每次电机连接单独记录，保留 30 天；报告不自动删除。',
             '', '以下为已保存配置；未联网验证服务可达性或密钥有效性。',
             'SN 取号：' + config.mes_url.rstrip('/') + config.mes_sn_path,
             '标签打印：' + config.printer_url.rstrip('/') + config.printer_status_path,
             '打印提交：' + config.printer_url.rstrip('/') + config.printer_print_path,
             '打印机队列：' + (config.printer_name or '使用打印服务默认队列') + f'；每台 {config.printer_copies} 份',
             '打印链路：上位机 → HTTP 打印服务 → Windows 驱动/队列 → USB 打印机。',
             '本机服务：随上位机后台启动，退出时自动收尾；项目目录自带 Node.js。',
             '标签配置：' + str(root/'printer-service/config.json'),
             '报告登记：' + (config.report_upload_url or '使用原上传配置文件中的地址'),
             'OSS：' + config.oss_endpoint + ' / ' + config.oss_bucket + ' / ' + config.oss_region,
             'OSS 目录：' + (config.oss_prefix or '桶根目录')]
    lines.extend(CREDENTIAL_LABELS[name] + '：' + value for name, value in credentials.items())
    lines.append('旧版上传凭据：' + legacy_state)
    lines.append('报告/OSS 留空字段可兼容旧版凭据文件；取号 Key 与报告 Key 分别配置。')
    if credential_error:
        lines.append('密钥读取提示：' + credential_error)
    return '\n'.join(lines)
