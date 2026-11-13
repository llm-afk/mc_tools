# Offline-capable Windows single-file build; production credentials are never bundled.
from pathlib import Path
import runpy
from PyInstaller.utils.win32.versioninfo import (VSVersionInfo, FixedFileInfo,
    StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct)

root = Path(SPECPATH).parents[1]
app_version = runpy.run_path(str(root/'scripts/factory_test/version.py'))['VERSION']
version_tuple = tuple(int(part) for part in app_version.split('.'))+(0,)
version_info = VSVersionInfo(ffi=FixedFileInfo(filevers=version_tuple,prodvers=version_tuple),kids=[
    StringFileInfo([StringTable('080404b0',[
        StringStruct('CompanyName','镜识科技'),StringStruct('ProductName','产测与电机调试'),
        StringStruct('FileDescription','电机产测软件'),StringStruct('OriginalFilename','电机产测软件.exe'),
        StringStruct('FileVersion',app_version),StringStruct('ProductVersion',app_version)])]),
    VarFileInfo([VarStruct('Translation',[0x0804,1200])])])
a = Analysis(
    [str(root/'scripts/factory_test/desktop_entry.py')],
    pathex=[str(root)],
    datas=[(str(root/'scripts/factory_test/assets'), 'scripts/factory_test/assets'),
           (str(root/'docs/产测人员使用手册.md'), 'docs')],
    hiddenimports=['serial.tools.list_ports_windows', 'fitz', 'oss2', 'requests'],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'matplotlib', 'numpy', 'scipy', 'pandas'],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name='电机产测软件',
          console=False, disable_windowed_traceback=False, version=version_info)
