"""产线桌面入口：python -m scripts.factory_test.gui。"""
import math
import importlib.util
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import json
import time
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

from motor_platform import Canable, MitCommand

ROOT = Path(__file__).resolve().parents[2]
PYTHON_DEPENDENCIES = {
    'serial': 'pyserial>=3.5,<4',
    'reportlab': 'reportlab>=4,<5',
    'fitz': 'PyMuPDF>=1.24,<2',
}


class WindowConsole:
    """通过标准输出把控制面板传到父窗口，停止仍走原脚本的 quit 路径。"""
    cancelled = threading.Event()

    def __init__(self, **kwargs):
        self._panel = '正在连接设备…'
        self._notice = ''
        self.app = self
        self.last_update = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.invalidate(force=True)

    def invalidate(self, force=False):
        now = time.monotonic()
        if force or now - self.last_update >= .2:
            print('@panel ' + json.dumps(self._panel + '\n' + self._notice, ensure_ascii=False), flush=True)
            self.last_update = now

    def poll(self):
        return 'quit' if self.cancelled.is_set() else None


def validate_inputs(serial, torque, node):
    if not serial:
        raise ValueError('请选择 USB 转 CAN 设备。')
    try:
        node = int(node)
        torque = float(torque)
    except ValueError:
        raise ValueError('最大扭矩请输入数字，电机 ID 请输入整数。') from None
    if not 1 <= node <= 9:
        raise ValueError('电机 ID 范围为 1～9。')
    if not math.isfinite(torque) or torque <= 0:
        raise ValueError('最大扭矩必须是大于 0 的有限数值。')
    MitCommand(torque=torque).pack()
    return ['--serial', serial, '--torque', str(torque), '--id', str(node)]


class FactoryWindow:
    def __init__(self, root):
        self.root = root
        self.process = None
        self.latest_result = None
        self.events = queue.Queue()
        self.scanning = False
        self.dependencies_ready = False
        root.title('电机产线测试 · Demo')
        root.geometry('720x700')
        root.minsize(600, 460)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TLabel', font=('Microsoft YaHei UI', 11))
        style.configure('TButton', font=('Microsoft YaHei UI', 11), padding=9)
        frame = ttk.Frame(root, padding=28)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='电机产线测试', font=('Microsoft YaHei UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='扭矩输出误差  →  电机特性曲线  →  测试报告', foreground='#627381').pack(anchor='w', pady=(6, 24))
        form = ttk.Frame(frame)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        self.serial = tk.StringVar()
        self.torque = tk.StringVar(value='30')
        self.node = tk.StringVar(value='1')
        ttk.Label(form, text='USB 转 CAN').grid(row=0, column=0, sticky='w', padx=(0, 18), pady=8)
        self.devices = ttk.Combobox(form, textvariable=self.serial, state='readonly', width=28)
        self.devices.grid(row=0, column=1, sticky='ew')
        self.refresh = ttk.Button(form, text='刷新设备', command=self.scan)
        self.refresh.grid(row=0, column=2, padx=(10, 0))
        ttk.Label(form, text='最大扭矩（Nm）').grid(row=1, column=0, sticky='w', pady=12)
        self.torque_entry = ttk.Entry(form, textvariable=self.torque)
        self.torque_entry.grid(row=1, column=1, sticky='ew')
        ttk.Label(form, text='电机 ID').grid(row=2, column=0, sticky='w', pady=12)
        self.node_entry = ttk.Spinbox(form, from_=1, to=9, textvariable=self.node)
        self.node_entry.grid(row=2, column=1, sticky='ew')
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(20, 12))
        self.start = ttk.Button(actions, text='开始测试', command=lambda: self.run(False))
        self.start.pack(side='left')
        self.stop = ttk.Button(actions, text='停止测试', command=self.request_stop, state='disabled')
        self.stop.pack(side='left', padx=10)
        self.results = ttk.Button(actions, text='打开结果目录', command=self.open_results,
                                  state='disabled')
        self.results.pack(side='right')
        self.live_panel = ''
        self.log = ScrolledText(frame, height=12, font=('Microsoft YaHei UI', 10), state='disabled',
                                background='#F4F7F9', foreground='#142B3D',
                                insertbackground='#142B3D', selectbackground='#B8D7E6',
                                selectforeground='#142B3D', relief='solid', borderwidth=1,
                                padx=10, pady=8)
        self.log.tag_configure('PASS', foreground='#14815A',
                               font=('Microsoft YaHei UI', 10, 'bold'))
        self.log.tag_configure('FAIL', foreground='#BE3947',
                               font=('Microsoft YaHei UI', 10, 'bold'))
        self.log.pack(fill='both', expand=True, pady=(10, 0))
        self.write_log('正在检查运行依赖…')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)
        self.bootstrap_dependencies()

    def bootstrap_dependencies(self):
        """首次启动自动补齐 Python 依赖，安装过程复用窗口输出区。"""
        self.refresh.config(state='disabled')
        self.start.config(state='disabled')

        def worker():
            missing = [package for module, package in PYTHON_DEPENDENCIES.items()
                       if importlib.util.find_spec(module) is None]
            if not missing:
                self.events.put(('dependencies', None))
                return
            self.events.put(('output', '首次运行，正在自动安装缺少的依赖：' + '、'.join(missing) + '\n'))
            command = [sys.executable, '-m', 'pip', 'install', *missing]
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            process = subprocess.Popen(command, cwd=ROOT, creationflags=flags,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace')
            for line in process.stdout:
                self.events.put(('output', line))
            code = process.wait()
            self.events.put(('dependencies', None if code == 0 else
                             '依赖自动安装失败，请检查网络或 Python pip 是否可用。'))

        threading.Thread(target=worker, daemon=True).start()

    def scan(self):
        if not self.dependencies_ready or self.scanning or self.process:
            return
        self.scanning = True
        self.refresh.config(state='disabled')
        self.start.config(state='disabled')
        def worker():
            try:
                values = sorted(set(d['serial_number'] for d in Canable.detect() if d.get('serial_number')))
                self.events.put(('devices', values))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def write_log(self, value='', *, live=False):
        """唯一输出区：普通消息追加，实时面板在底部原位刷新。"""
        self.log.config(state='normal')
        try:
            self.log.delete('live_start', 'end-1c')
        except tk.TclError:
            pass
        if live:
            self.live_panel = value.rstrip()
        elif value:
            self.insert_output(value.rstrip() + '\n')
        self.log.mark_set('live_start', 'end-1c')
        self.log.mark_gravity('live_start', tk.LEFT)
        if self.live_panel:
            self.insert_output('\n' + self.live_panel + '\n')
        self.log.see('end')
        self.log.config(state='disabled')

    def insert_output(self, value):
        """把 PASS/FAIL 着色加粗，其余输出保持统一深色。"""
        start = 0
        for match in re.finditer(r'\b(?:PASS|FAIL)\b', value):
            self.log.insert('end-1c', value[start:match.start()])
            word = match.group(0)
            self.log.insert('end-1c', word, word)
            start = match.end()
        self.log.insert('end-1c', value[start:])

    def run(self, demo):
        if self.process:
            return
        try:
            args = ['--preview'] if demo else validate_inputs(self.serial.get(), self.torque.get(), self.node.get())
            python = Path(sys.executable)
            if python.name.lower() == 'pythonw.exe':
                python = python.with_name('python.exe')
            self.process = subprocess.Popen(
                [str(python), '-m', 'scripts.factory_test.gui', '--worker', *args],
                cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1'))
            process = self.process
            def read_output():
                for line in process.stdout:
                    self.events.put(('output', line))
                process.stdout.close()
            threading.Thread(target=read_output, daemon=True).start()
            for control in (self.start, self.refresh, self.devices, self.torque_entry, self.node_entry):
                control.config(state='disabled')
            self.stop.config(state='normal')
            self.live_panel = ''
            self.log.config(state='normal')
            self.log.delete('1.0', 'end')
            self.log.config(state='disabled')
            self.write_log(f'> 开始测试  USB={self.serial.get()}  最大扭矩={self.torque.get()} Nm  ID={self.node.get()}')
            self.write_log('正在连接设备…', live=True)
        except Exception as exc:
            messagebox.showerror('无法开始', str(exc), parent=self.root)

    def poll(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == 'output':
                if value.startswith('@panel '):
                    self.write_log(json.loads(value[7:]), live=True)
                elif value.startswith('@resultdir '):
                    self.latest_result = Path(json.loads(value[11:]))
                    self.results.config(state='normal')
                else:
                    self.write_log(value)
                continue
            if kind == 'dependencies':
                if value:
                    self.write_log(value)
                    messagebox.showerror('依赖安装失败', value, parent=self.root)
                    self.refresh.config(state='normal')
                else:
                    self.dependencies_ready = True
                    self.write_log('运行依赖已就绪，正在扫描 USB 转 CAN…')
                    self.scan()
                continue
            self.scanning = False
            self.refresh.config(state='normal' if not self.process else 'disabled')
            if kind == 'devices':
                self.devices['values'] = value
                if self.serial.get() not in value:
                    self.serial.set(value[0] if value else '')
                if not self.process:
                    self.write_log(f'找到 {len(value)} 个设备，请确认参数后开始。' if value else '未发现 USB 转 CAN，请连接设备后点击刷新。')
            elif not self.process:
                self.write_log('设备扫描失败：' + value)
            if not self.process:
                self.start.config(state='normal' if self.serial.get() else 'disabled')
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            self.process.stdin.close()
            self.process = None
            self.stop.config(state='disabled')
            self.live_panel = ''
            self.write_log({0: '测试完成，报告已保存。', 2: '测试结束：指标未通过或测试未完成，请查看结果报告。'}.get(code, '测试异常退出，请查看上方信息。'))
            for control in (self.start, self.refresh, self.torque_entry, self.node_entry):
                control.config(state='normal')
            self.devices.config(state='readonly')
        self.root.after(200, self.poll)

    def request_stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write('stop\n')
                self.process.stdin.flush()
                self.stop.config(state='disabled')
                self.write_log('正在停止，请等待降扭矩和设备关闭完成…')
            except (OSError, ValueError):
                pass

    def open_results(self):
        if self.latest_result is None or not self.latest_result.is_dir():
            messagebox.showinfo('暂无结果', '当前还没有生成测试结果目录。', parent=self.root)
            return
        os.startfile(self.latest_result)

    def close(self):
        if self.process and self.process.poll() is None:
            self.request_stop()
            messagebox.showinfo('正在停止', '已请求停止测试，请等待停机完成后关闭窗口。', parent=self.root)
            return
        self.root.destroy()


def main():
    if '--worker' in sys.argv:
        from . import app
        app.kt.TerminalConsole = WindowConsole
        app.tn.TerminalConsole = WindowConsole
        original_read_sn = app.read_motor_sn
        def read_sn(args):
            if WindowConsole.cancelled.is_set():
                raise RuntimeError('用户停止测试')
            return original_read_sn(args)
        app.read_motor_sn = read_sn
        original_execute = app.execute
        def execute(*args, **kwargs):
            return original_execute(*args, identify=read_sn, **kwargs)
        app.execute = execute
        def read_commands():
            for line in sys.stdin:
                if line.strip() == 'stop':
                    WindowConsole.cancelled.set()
            WindowConsole.cancelled.set()
        threading.Thread(target=read_commands, daemon=True).start()
        from .app import main as run_test
        try:
            result = run_test(sys.argv[sys.argv.index('--worker') + 1:])
            print('@resultdir ' + json.dumps(result['output_directory'], ensure_ascii=False), flush=True)
            code = 0 if result['status'] == 'PASS' else 2
        except BaseException:
            import traceback
            traceback.print_exc()
            code = 1
        raise SystemExit(code)
    root = tk.Tk()
    FactoryWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
