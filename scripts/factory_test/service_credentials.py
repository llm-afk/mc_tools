"""Windows-user protected service credentials, separate from public station settings."""
import ctypes as C
from ctypes import wintypes as W
import base64
import json
import os
from pathlib import Path


FIELDS = ('sn_api_key', 'mes_api_key', 'access_key_id', 'access_key_secret')
MAGIC = b'MirrorMe-Factory-Credentials-v1\0'
PROTECTED_FIELD = 'protected_credentials'


class CredentialStoreError(RuntimeError):
    pass


def _crypt(data, decrypt=False):
    if os.name!='nt':
        raise CredentialStoreError('本机凭证保护需要 Windows')
    class Blob(C.Structure):
        _fields_ = [('size',W.DWORD),('data',C.POINTER(C.c_ubyte))]
    buffer = C.create_string_buffer(data)
    source = Blob(len(data),C.cast(buffer,C.POINTER(C.c_ubyte)))
    target = Blob()
    crypt = C.WinDLL('crypt32',use_last_error=True)
    kernel = C.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [C.c_void_p],C.c_void_p
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    method.argtypes = [C.POINTER(Blob),C.c_void_p,C.POINTER(Blob),C.c_void_p,C.c_void_p,W.DWORD,C.POINTER(Blob)]
    method.restype = W.BOOL
    if not method(C.byref(source),None,None,None,None,1,C.byref(target)):
        raise C.WinError(C.get_last_error())
    try:
        return C.string_at(target.data,target.size)
    finally:
        kernel.LocalFree(C.cast(target.data,C.c_void_p))


class CredentialStore:
    def __init__(self,path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return dict.fromkeys(FIELDS,'')
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(document,dict):
                raise ValueError('invalid settings document')
            if PROTECTED_FIELD not in document:
                return dict.fromkeys(FIELDS,'')
            data = base64.b64decode(document[PROTECTED_FIELD],validate=True)
            if not data.startswith(MAGIC):
                raise ValueError('unsupported credential file')
            value = json.loads(_crypt(data[len(MAGIC):],decrypt=True))
            if not isinstance(value,dict) or any(not isinstance(value.get(k,''),str) for k in FIELDS):
                raise ValueError('invalid credential fields')
            return {k:value.get(k,'') for k in FIELDS}
        except (OSError,ValueError,UnicodeError,TypeError) as exc:
            raise CredentialStoreError('已保存的服务密钥无法读取或解密，请在工位配置中重新填写并保存') from exc

    @staticmethod
    def encode(values):
        if any(not isinstance(values.get(k,''),str) for k in FIELDS):
            raise ValueError('服务密钥须为文本')
        data = MAGIC+_crypt(json.dumps({k:values.get(k,'') for k in FIELDS},ensure_ascii=False).encode('utf-8'))
        return base64.b64encode(data).decode('ascii')

    def save(self,values):
        from .station_config import atomic_json
        document = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        document[PROTECTED_FIELD] = self.encode(values)
        atomic_json(self.path,document)


def values_for(config):
    path = getattr(config,'credentials_file','')
    return CredentialStore(path).load() if path else dict.fromkeys(FIELDS,'')


def sn_key_for(config):
    return values_for(config)['sn_api_key'] or os.environ.get(config.api_key_env,'')
