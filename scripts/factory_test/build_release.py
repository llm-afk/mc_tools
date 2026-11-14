"""Build and verify the workstation executable in the project root."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from .firmware_library import MAX_BYTES, latest_file, read_image
from .station_config import atomic_json
from .storage import atomic_replace
from .verify_release import REQUIRED_CHECKS, verify
from .version import VERSION

ROOT = Path(__file__).resolve().parents[2]
PRINTER_SERVICE = ROOT/'services/tsc-print-service'
FIRMWARE_ROOT = ROOT/'firmware'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files():
    for name in ('README.md', 'pyproject.toml', '.gitignore', '.gitattributes', 'install_drivers.cmd'):
        yield ROOT/name
    for folder in ('motor_platform', 'scripts', 'tests', 'docs'):
        for path in sorted((ROOT/folder).rglob('*')):
            if any(part in ('data', '__pycache__', '.factory-state') for part in path.relative_to(ROOT).parts):
                continue
            if path.is_file() and path.suffix in ('.py', '.spec', '.md', '.png'):
                yield path
    for path in sorted(PRINTER_SERVICE.iterdir()):
        if path.is_file() and path.suffix in ('.js', '.json', '.ps1', '.cmd', '.md', '.txt'):
            yield path


def managed_files():
    paths = set(source_files())
    for folder in (ROOT/'drivers', PRINTER_SERVICE):
        paths.update(path for path in folder.rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts)
    return sorted(paths)


def firmware_record(project):
    project = Path(project)
    found = latest_file(project/'firmware')
    if found is None:
        raise FileNotFoundError('firmware 目录缺少 APP 固件')
    version, path = found
    platform = 'ADM' if path.stat().st_size == MAX_BYTES['ADM'] else 'GD'
    info = read_image(path, version, platform)[1]
    return dict(file=path.relative_to(project).as_posix(), platform=platform,
                version=version, sha256=info['sha256'], crc32=info['crc32'])


def run_hidden(arguments, *, cwd, env, timeout=60):
    return subprocess.run(arguments, cwd=cwd, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=timeout, check=True)


def publish_executable(source):
    # Stage on the project's volume so the final replacement is atomic across drive layouts.
    fd, name = tempfile.mkstemp(prefix='.pmt-executable-', suffix='.tmp', dir=ROOT)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        if digest(temporary) != digest(source):
            raise RuntimeError('EXE 写入后读回不一致')
        atomic_replace(temporary, ROOT/source.name)
    finally:
        temporary.unlink(missing_ok=True)


def build_in(work):
    for name in ('node.exe', 'node_modules', 'LICENSE-node.txt'):
        if not (PRINTER_SERVICE/name).exists():
            raise FileNotFoundError('缺少打印运行环境：'+str(PRINTER_SERVICE/name))
    initial_firmware = firmware_record(ROOT)
    initial = {p.relative_to(ROOT).as_posix(): digest(p) for p in source_files()}
    initial_files = {p.relative_to(ROOT).as_posix(): digest(p) for p in managed_files()}
    temporary = work/'tmp'
    temporary.mkdir()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYINSTALLER_CONFIG_DIR=str(work/'cache'),
               TEMP=str(temporary), TMP=str(temporary))
    checks = work/'regression.json'
    print('Running guarded workstation and component tests...', flush=True)
    subprocess.run([sys.executable, '-B', '-m', 'scripts.factory_test.checks', '--output', str(checks)],
                   cwd=ROOT, env=env, check=True)
    regression = json.loads(checks.read_bytes())
    if regression['sources'] != initial:
        raise RuntimeError('离线检查期间源码发生变化，请重新构建。')
    print(f'Building {VERSION}; log: {work/"build.log"}', flush=True)
    with (work/'build.log').open('w', encoding='utf-8') as log:
        subprocess.run([sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm',
                        '--distpath', str(work/'frozen'), '--workpath', str(work/'work'),
                        str(ROOT/'scripts/factory_test/FactoryTestStandalone.spec')],
                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    executable = work/'frozen/电机产测软件.exe'
    isolated = work/'isolated'
    isolated.mkdir()
    isolated_exe = isolated/executable.name
    shutil.copy2(executable, isolated_exe)
    isolated_env = dict(env)
    for name in ('PYTHONHOME', 'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY'):
        isolated_env.pop(name, None)
    windows = Path(env.get('SystemRoot', r'C:\Windows'))
    isolated_env.update(PATH=os.pathsep.join((str(windows/'System32'), str(windows))),
                        LOCALAPPDATA=str(isolated/'profile'))
    print('Checking isolated EXE and complete simulated workflow...', flush=True)
    check_path = work/'self-check.json'
    run_hidden([str(isolated_exe), '--self-check', str(check_path)], cwd=isolated, env=isolated_env, timeout=120)
    check = json.loads(check_path.read_bytes())
    if (not all(check.get(name) is True for name in REQUIRED_CHECKS) or check['version'] != VERSION
            or check['hardware_or_network_used'] is not False or check['usb_open_attempts'] != 0):
        raise RuntimeError(check)

    firmware_app = firmware_record(ROOT)
    if firmware_app != initial_firmware:
        raise RuntimeError('构建期间 APP 固件已更换，请重新构建。')
    renderer = run_hidden([str(PRINTER_SERVICE/'node.exe'), str(PRINTER_SERVICE/'index.js'), '--self-check'],
                          cwd=PRINTER_SERVICE, env=isolated_env)
    renderer_check = json.loads(renderer.stdout.strip())
    if renderer_check.get('ok') is not True:
        raise RuntimeError(renderer_check)
    node = run_hidden([str(PRINTER_SERVICE/'node.exe'), '--test', '--test-reporter=tap',
                       str(PRINTER_SERVICE/'test_bridge.js')], cwd=PRINTER_SERVICE, env=isolated_env)
    test_count = re.search(r'^# tests ([0-9]+)$', node.stdout, re.M)
    if not test_count:
        raise RuntimeError('打印服务测试未返回数量：'+node.stdout)
    printer_tests = dict(ok=True, tests=int(test_count[1]), hardware_or_network_used=False)
    raw = run_hidden([str(windows/'System32/WindowsPowerShell/v1.0/powershell.exe'),
                      '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                      '-File', str(PRINTER_SERVICE/'raw-print.ps1'), '-CompileOnly'],
                     cwd=PRINTER_SERVICE, env=isolated_env)
    raw_check = json.loads(raw.stdout.strip())
    if raw_check != dict(ok=True, hardwareUsed=False):
        raise RuntimeError(raw_check)
    current_files = {p.relative_to(ROOT).as_posix(): digest(p) for p in managed_files()}
    if current_files != initial_files or firmware_record(ROOT) != initial_firmware:
        raise RuntimeError('构建期间源码或依赖发生变化，请重新构建。')
    manifest = dict(
        version=VERSION, channel='production', project='pmt',
        built_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
        executable=executable.name, sha256=digest(executable), bytes=executable.stat().st_size,
        python=sys.version, isolated_self_check=check, regression=regression, source_files=initial,
        printer_renderer_check=renderer_check, printer_tests=printer_tests,
        printer_raw_helper_compile_check=raw_check, firmware_app=firmware_app,
        files=dict(current_files, **{executable.name:digest(executable)}),
    )
    publish_executable(executable)
    atomic_json(ROOT/'release-manifest.json', manifest)
    result = verify(ROOT)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def build():
    temporary_root = Path(tempfile.gettempdir()).resolve()
    work = Path(tempfile.mkdtemp(prefix='pmt-build-', dir=temporary_root)).resolve()
    if work.parent != temporary_root or not work.name.startswith('pmt-build-'):
        raise RuntimeError('构建临时目录异常')
    try:
        result = build_in(work)
    except BaseException:
        print('Build failed; diagnostic files retained at '+str(work), file=sys.stderr, flush=True)
        raise
    shutil.rmtree(work)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    build()


if __name__ == '__main__':
    main()
