"""工位配置；密钥使用 Windows 当前用户保护的密文保存。"""
from dataclasses import asdict, dataclass, fields
from datetime import datetime
import json
import math
import os
import re
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from .storage import atomic_replace

DEFAULT_MES_BASE_URL = 'https://www.mirrormetech.com/mes-api'
DEFAULT_REPORT_URL = DEFAULT_MES_BASE_URL + '/open-api/test-report'
DEFAULT_OSS_ENDPOINT = 'https://oss-cn-hangzhou.aliyuncs.com'
DEFAULT_OSS_BUCKET = 'mirrormetech-mes-testreport'
DEFAULT_OSS_REGION = 'cn-hangzhou'
DEFAULT_OSS_PREFIX = 'test-report'


# Hardware ABCD uses the user's dictionary; test limits remain a separate supported profile.
HARDWARE_A = {}
HARDWARE_B = {'1': 'GD 平台', '2': 'ADM 平台'}
HARDWARE_C = {'1': 'C2_jieke', '2': 'C2_xinzhi', '3': 'C2_PRO_jieke',
              '4': 'C2_PRO_xinzhi', '5': 'A2_jieke', '6': 'A2_xinzhi'}
HARDWARE_D = {}
HARDWARE_FIELDS = {'A': HARDWARE_A, 'B': HARDWARE_B, 'C': HARDWARE_C, 'D': HARDWARE_D}
TEST_TORQUE_BY_BC = {'11': 30.0, '22': 30.0, '13': 50.0, '14': 50.0}


def hardware_torque_limit(hardware):
    """Torque belongs to platform/motor BC; A/D revisions are not a whitelist."""
    return TEST_TORQUE_BY_BC.get(hardware[1:3]) if isinstance(hardware,str) and len(hardware)==4 else None
MATERIALS = {
    'C2': {'HIPX': ['LFRR', 'LRRF'], 'HIPY': ['LFXX', 'RFXX', 'LRXX', 'RRXX'],
           'KNEE': ['0STD', '0PRO'], 'BATY': ['8S2P', '8S5P'], '0LEG': ['LXXX', 'RXXX']},
    'A2': {'HIPX': ['LFRR', 'LRRF'], 'HIPY': ['LFRR', 'LRRF'],
           'KNEE': ['LFRR', 'LRRF'], 'WHEL': ['LFRR', 'LRRF'],
           'BATY': ['0STD'], 'NAVI': ['0001']},
}


def hardware_parts(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z0-9]{4}', value):
        raise ValueError('硬件版本必须为四位英文字母或数字 ABCD')
    return dict(zip(HARDWARE_FIELDS,value))


def hardware_input_value(part, value):
    """Accept a one-character code or the descriptive entry from the same dropdown."""
    code = value.strip().partition('·')[0].strip().upper()
    if part not in HARDWARE_FIELDS or not re.fullmatch(r'[A-Z0-9]', code):
        raise ValueError(f'硬件 {part} 位须输入单个英文字母或数字')
    return code


def material_options(project, category=None):
    """The built-in dictionary supplies suggestions, not a whitelist for text input."""
    known = MATERIALS.get(project, {})
    if category is None:
        return list(known) or list(dict.fromkeys(code for values in MATERIALS.values() for code in values))
    return list(known.get(category, [])) or list(dict.fromkeys(
        code for values in MATERIALS.values() for items in values.values() for code in items))


def temporary_sn(hardware_version):
    """26 ASCII characters: hardware ABCD at [12:16], all other positions are '0'."""
    hardware_parts(hardware_version)
    return '0'*12+hardware_version+'0'*10


def state_root():
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'MirrorMe' / 'FactoryStation'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass
class StationConfig:
    simulation: bool = False
    automatic: bool = True
    adapter_sn: str = ''
    power_port: str = 'auto'
    dyno_port: str = 'auto'
    target_id: int = 3
    model: str = '221'
    fixture_id: str = ''
    project: str = 'C2'
    category: str = 'KNEE'
    subcategory: str = '0STD'
    factory: str = 'XC'
    hardware_version: str = '0221'
    production_date: str = ''  # 空白代表本次取号当天，不能记住旧的自动日期。
    mes_url: str = DEFAULT_MES_BASE_URL
    api_key_env: str = 'MIRRORME_MES_API_KEY'
    printer_url: str = 'http://127.0.0.1:9876'
    printer_name: str = ''
    calibration_timeout: float = 120.0
    report_upload_url: str = DEFAULT_REPORT_URL  # 显式清空后仍可沿用外置配置的 mes_url。
    upload_config: str = ''     # 可指定旧版上传配置文件，不在工位配置中复制密钥。
    credentials_file: str = ''  # 包含 DPAPI 密文的工位配置文件，不保存明文密钥。
    mes_sn_path: str = '/open-api/module-sn/batch-generate'
    printer_status_path: str = '/api/status'
    printer_print_path: str = '/api/print'
    oss_endpoint: str = DEFAULT_OSS_ENDPOINT
    oss_bucket: str = DEFAULT_OSS_BUCKET
    oss_region: str = DEFAULT_OSS_REGION
    oss_prefix: str = DEFAULT_OSS_PREFIX
    sn_api_key_header: str = 'X-Api-Key'
    report_api_key_header: str = 'X-Api-Key'
    printer_copies: int = 1

    @property
    def base_sn(self):
        return temporary_sn(self.hardware_version)

    @property
    def torque(self):
        if not self.test_profile_available:
            raise ValueError('当前平台和电机组合尚未配置产测参数')
        return hardware_torque_limit(self.hardware_version)

    @property
    def test_profile_available(self):
        return hardware_torque_limit(self.hardware_version) is not None

    def validate(self, *, live=False):
        if type(self.target_id) is not int or not 1 <= self.target_id <= 127:
            raise ValueError('工位目标 ID 必须为 1～127 的整数')
        hardware_parts(self.hardware_version)
        if self.hardware_version[1:] != self.model:
            raise ValueError('MES 硬件版本 BCD 必须与工位型号一致')
        for name,width in (('project',2),('category',4),('subcategory',4),('factory',2)):
            value = getattr(self,name)
            if not isinstance(value,str) or not re.fullmatch(r'[A-Z0-9]{'+str(width)+'}',value):
                raise ValueError(f'{name} 须为 {width} 位英文字母或数字')
        if self.production_date:
            if not isinstance(self.production_date, str) or not re.fullmatch(r'[0-9]{6}', self.production_date):
                raise ValueError('生产日期须为 YYMMDD，或留空使用当天')
            try:
                datetime.strptime(self.production_date, '%y%m%d')
            except ValueError:
                raise ValueError('生产日期不是有效的日历日期') from None
        if not math.isfinite(self.calibration_timeout) or self.calibration_timeout <= 0:
            raise ValueError('校准超时必须为有限正数')
        for name in ('mes_url','printer_url','report_upload_url','oss_endpoint'):
            value = getattr(self,name)
            if value:
                try:
                    parts = urlsplit(value)
                    port = parts.port
                    valid = (parts.scheme in ('http','https') and parts.hostname and
                             not parts.username and not parts.password and not parts.query and not parts.fragment
                             and not any(c.isspace() for c in value))
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError(f'{name} 请填写有效的 HTTP / HTTPS 地址，不要在地址中填写密钥')
        for name in ('mes_sn_path','printer_status_path','printer_print_path'):
            value = getattr(self,name)
            parts = urlsplit(value)
            if not value.startswith('/') or value.startswith('//') or parts.scheme or parts.netloc or parts.query or parts.fragment or any(c.isspace() for c in value):
                raise ValueError(f'{name} 请填写以 / 开头的接口路径')
        if any(not isinstance(getattr(self,name),str) or not getattr(self,name).strip() for name in ('oss_bucket','oss_region')):
            raise ValueError('OSS Bucket 和 Region 不能为空')
        if any(part in ('.','..') for part in self.oss_prefix.split('/')) or any(c in self.oss_prefix for c in '\\?#'):
            raise ValueError('OSS 目录前缀请填写相对对象目录，不使用 .、..、反斜杠或 URL 参数')
        for name in ('sn_api_key_header','report_api_key_header'):
            value=getattr(self,name)
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+",value) or value.lower() in ('host','content-type','content-length','connection'):
                raise ValueError(f'{name} 请填写有效的 API Key 请求头名称')
        if type(self.printer_copies) is not int or self.printer_copies<1:
            raise ValueError('打印份数必须为正整数')
        if live:
            if not self.test_profile_available:
                raise ValueError('当前平台和电机组合尚未配置产测参数，不能开始实测')
            if not self.mes_url:
                raise ValueError('请先配置 MES 取号地址')


def load_config(path=None):
    path = Path(path) if path else state_root() / 'station.json'
    if not path.exists():
        return StationConfig()
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('工位配置格式错误')
    names = {f.name for f in fields(StationConfig)}
    values = {k: v for k, v in raw.items() if k in names}
    if 'protected_credentials' in raw:
        values['credentials_file'] = str(path.resolve())
    if 'hardware_version' in values:
        hardware_parts(values['hardware_version'])
        values['model'] = values['hardware_version'][1:]
    config = StationConfig(**values)
    config.validate()
    return config


def save_config(config, path=None, *, credentials=None):
    config.validate()
    from .service_credentials import CredentialStore, PROTECTED_FIELD
    path = Path(path) if path else state_root()/'station.json'
    document = asdict(config)
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,UnicodeError):
            import shutil
            backup = path.with_name(path.name+'.invalid-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            shutil.copy2(path,backup)
            previous = {}
        if isinstance(previous,dict) and PROTECTED_FIELD in previous:
            document[PROTECTED_FIELD] = previous[PROTECTED_FIELD]
    if credentials is not None:
        document[PROTECTED_FIELD] = CredentialStore.encode(credentials)
    # Public settings and encrypted credentials are committed in a single replacement.
    atomic_json(path,document)
