"""Prepare device drivers asynchronously, before production owns the hardware."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid


def bundle_root():
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]


def spawn_driver_process(*args, **kwargs):
    return subprocess.Popen(*args, **kwargs)


def optional_printer_package(root):
    root = Path(root)
    package = json.loads((root/'drivers/packages.json').read_bytes())['printer']
    path = root/'drivers'/package['bundled']
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != package['sha256']:
        raise ValueError('打印机驱动包 SHA256 不匹配：'+str(path))
    return dict(bundled=path.is_file(), model=package['model'], version=package['version'],
                source=package['source'], sha256=package['sha256'])


def decode_inventory(output):
    value = json.loads(output.lstrip('\ufeff').strip())
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('设备驱动检查响应格式错误')
    if value.get('error'):
        raise RuntimeError(value['error'])
    for key in ('plan', 'devices', 'queues', 'issues'):
        if not isinstance(value.get(key), list):
            raise ValueError('设备驱动检查缺少数组 '+key)
    for task in value['plan']:
        if not isinstance(task, dict) or task.get('kind') not in ('usb', 'printer'):
            raise ValueError('设备驱动安装任务格式错误')
    return value


def plan_key(inventory, revision):
    return json.dumps([revision, inventory['plan']], ensure_ascii=True, sort_keys=True)


class DriverManager:
    def __init__(self, root, snapshot, notify, *, folder=None, popen=None, interval=5, diagnostics=None):
        self.root = Path(root)
        self.folder = Path(folder) if folder else bundle_root()
        self.snapshot, self.callback = snapshot, notify
        self.diagnostics = diagnostics
        self.request = None
        self.popen = popen or spawn_driver_process
        self.interval = interval
        self.stopped = threading.Event()
        self.permission = threading.Event()
        self.thread = None
        self.attempted = set()
        self.last_inventory = None
        self.last_failure = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='device-driver-setup', daemon=True)
        self.thread.start()

    def stop(self):
        # An installation already started must finish and verify. Never terminate it.
        self.stopped.set()

    def command(self, config, mode, receipt=None):
        from .printer_service import local_endpoint
        from .station_config import DEFAULT_PRINTER_NAME
        windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        arguments = [str(windows/'System32/WindowsPowerShell/v1.0/powershell.exe'),
                     '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                     '-File', str(self.folder/'drivers/setup_devices.ps1'), '-Mode', mode,
                     '-DataRoot', str(self.root), '-QueueName', config.printer_name or DEFAULT_PRINTER_NAME,
                     '-UseLocalPrinter', 'true' if local_endpoint(config) else 'false']
        if receipt is not None:
            arguments.extend(('-ResultPath', str(receipt)))
        return arguments

    def launch(self, arguments):
        return self.popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding='utf-8-sig', errors='replace',
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def inspect(self, config):
        process = self.launch(self.command(config, 'Inspect'))
        try:
            output, errors = process.communicate(timeout=35)
        except subprocess.TimeoutExpired:
            process.kill()  # Read-only inventory only. Never terminate an installer.
            output, errors = process.communicate()
            raise TimeoutError('本机设备驱动枚举超过 35 秒；'+errors[-2000:])
        if process.returncode:
            raise RuntimeError('本机设备驱动检查失败；'+output[-4000:]+' '+errors[-4000:])
        return decode_inventory(output)

    def replay_receipt(self, path, seen):
        try:
            if path.stat().st_size > 4*1024*1024:
                raise ValueError('驱动安装回执过大：'+str(path))
            value = json.loads(path.read_bytes())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        for event in value.get('events', []):
            identity = json.dumps(event, ensure_ascii=True, sort_keys=True)
            if identity not in seen:
                seen.add(identity)
                self.notify(dict(state='STAGE', event=event))
        return value

    def install(self, config):
        directory = self.root/'driver-setup'
        directory.mkdir(parents=True, exist_ok=True)
        receipt = directory/('driver-'+uuid.uuid4().hex+'.json')
        process = self.launch(self.command(config, 'Install', receipt))
        seen = set()
        progress_error = None
        try:
            while process.poll() is None:
                try:
                    self.replay_receipt(receipt, seen)
                except (OSError, ValueError) as exc:
                    if str(exc) != progress_error:
                        progress_error = str(exc)
                        self.notify(dict(state='PROGRESS_WARNING', message='暂时无法读取驱动进度，将等待安装程序结束；'+str(exc)))
                time.sleep(.2)
        finally:
            if process.poll() is None:
                # Even a progress-file failure cannot release a running installer.
                process.communicate()
        output, errors = process.communicate()
        result = self.replay_receipt(receipt, seen)
        self.notify(dict(state='RECEIPT', receipt=result, exit_code=process.returncode,
                         stdout=output[-8000:], stderr=errors[-8000:], path=str(receipt)))
        if process.returncode or not result or result.get('final') is not True or result.get('ok') is not True:
            reason = (result or {}).get('error') or errors or output or '安装程序未返回有效的完成回执'
            raise RuntimeError('设备驱动安装未确认完成；'+str(reason))
        if self.diagnostics is not None:
            receipt.unlink(missing_ok=True)
        return result

    def notify(self, value):
        if self.diagnostics is not None:
            stage = value.get('event', {})
            self.diagnostics.station.event('driver.'+stage.get('stage', value['state'].lower()),
                stage.get('message', value.get('message', '')),
                level='ERROR' if value['state']=='FAILED' or stage.get('stage')=='failed' else 'INFO',
                driver_setup=value)
        self.callback(value)

    def run(self):
        # A previous forced exit may leave a receipt. Preserve its evidence first.
        for path in (self.root/'driver-setup').glob('driver-*.json'):
            try:
                receipt = self.replay_receipt(path, set())
                self.notify(dict(state='RECOVERED', message='已恢复上次驱动安装回执。', receipt=receipt))
                if self.diagnostics is not None:
                    path.unlink(missing_ok=True)
            except Exception as exc:
                self.notify(dict(state='FAILED', message='读取历史驱动安装回执失败；'+str(exc)))
        while not self.stopped.is_set():
            request_id = None
            try:
                config, revision = self.snapshot()
                inventory = self.inspect(config)
                if self.stopped.is_set():
                    return
                current = json.dumps([revision, inventory], ensure_ascii=True, sort_keys=True)
                if current != self.last_inventory:
                    self.last_inventory = current
                    self.notify(dict(state='INVENTORY', inventory=inventory, revision=revision,
                                     message='已核对设备驱动、USB 打印端口和 Windows 打印队列。'))
                self.last_failure = None
                from .printer_service import local_endpoint
                signature = plan_key(inventory, (config.printer_name, local_endpoint(config)))
                if inventory['plan'] and signature not in self.attempted:
                    request_id = uuid.uuid4().hex
                    self.permission.clear()
                    self.request = request_id
                    self.notify(dict(state='NEEDED', request_id=request_id, revision=revision,
                                     plan=inventory['plan'], message='发现需要补齐的设备驱动，等待工位空闲。'))
                    while not self.stopped.is_set() and not self.permission.wait(.2):
                        if self.snapshot()[1] != revision:
                            break
                    if self.stopped.is_set() or self.snapshot()[1] != revision:
                        self.notify(dict(state='CANCELLED', request_id=request_id))
                    elif self.permission.is_set():
                        self.attempted.add(signature)
                        result = self.install(config)
                        self.notify(dict(state='DONE', request_id=request_id,
                                         reboot_required=result.get('reboot_required', False),
                                         message='设备驱动安装和回读验证完成。'))
            except Exception as exc:
                message = str(exc)
                if message != self.last_failure or request_id:
                    self.notify(dict(state='FAILED', request_id=request_id, message=message))
                    self.last_failure = message
            finally:
                self.request = None
            self.stopped.wait(self.interval)
