"""Firmware maintenance on the engineering settings page."""
from dataclasses import replace
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .diagnostics import log_scope
from .firmware_library import (FirmwareError, change_app, package_firmware, platform_for, selections)


class FirmwarePanel:
    def __init__(self, owner, parent, row):
        self.owner = owner
        self.thread = None
        self.current = None
        self.previous = None
        self.refresh_pending = False
        self.target = tk.StringVar()
        self.note = tk.StringVar()
        box = ttk.LabelFrame(parent, text='驱动器固件', padding=12)
        box.grid(row=row, column=0, columnspan=2, sticky='ew', pady=(14, 4))
        ttk.Label(box, textvariable=self.target, wraplength=620).pack(anchor='w')
        buttons = ttk.Frame(box)
        buttons.pack(anchor='w', pady=(8, 4))
        self.choose = ttk.Button(buttons, text='更换 APP…', command=self.pick_app)
        self.choose.pack(side='left')
        self.undo = ttk.Button(buttons, text='恢复上一版', command=self.restore, state='disabled')
        self.undo.pack(side='left', padx=6)
        ttk.Button(buttons, text='打开 BOOT 目录', command=lambda:owner.open_firmware_directory(boot=True)).pack(side='left')
        self.message = ttk.Label(box, textvariable=self.note, wraplength=620, foreground='#64748B')
        self.message.pack(anchor='w')

    def status(self, text, *, error=False):
        self.note.set(text)
        self.message.configure(foreground='#AE3644' if error else '#64748B')

    def refresh(self):
        if self.thread is not None:
            self.refresh_pending = True
            return
        self.run('inspect')

    def pick_app(self):
        owner = self.owner
        if self.thread is not None or owner.busy or owner.closing:
            return
        config = replace(owner.config)
        try:
            platform = platform_for(config)
        except FirmwareError as exc:
            self.status(str(exc), error=True)
            return
        # The file dialog can dispatch Tk events while it is open. Recheck the station afterwards.
        options = {} if self.current else dict(initialdir=str(package_firmware()/'候选'/platform))
        name = filedialog.askopenfilename(parent=owner.settings_window,
            title=f'为 {platform} 工位选择并启用 APP', filetypes=[('APP 固件', '*.bin')], **options)
        if name:
            self.run('import', config=config, source=Path(name))

    def restore(self):
        if self.previous is not None:
            self.run('restore', restore=self.previous)

    def run(self, action, *, config=None, **arguments):
        owner = self.owner
        config = config or replace(owner.config)
        if self.thread is not None or owner.closing:
            return
        if action != 'inspect':
            with owner.lock:
                if owner.busy or owner.configuring or owner.start_requested or owner.resume_requested:
                    self.status('工位正在启动或执行任务，结束后可换版。', error=True)
                    return
                if (config != owner.config or owner.vars['firmware_directory'].get().strip() != config.firmware_directory
                        or owner.hardware_vars['B'].get().partition('·')[0].strip() != config.hardware_version[1]):
                    self.status('平台或目录已修改，请先保存工位配置，再选择 APP。', error=True)
                    return
                owner.configuring = True
                owner.config_revision += 1
                owner.invalidate_readiness()
        self.choose.configure(state='disabled')
        self.undo.configure(state='disabled')
        self.status('正在读取工位固件…' if action == 'inspect' else '正在校验文件并切换工位目标…')
        target = owner.diagnostics.station.bind(phase='firmware_library')

        def work():
            result = dict(config=config, action=action, current=None, previous=None, error='')
            with log_scope(target):
                try:
                    if action == 'inspect':
                        current, previous = selections(config)
                        result.update(current=current, previous=previous)
                        if current:
                            current.load(platform_for(config))
                    else:
                        current, previous = change_app(config, **arguments)
                        result.update(current=current, previous=previous)
                except Exception as exc:
                    result['error'] = str(exc)
                    target.event('firmware.library.failed', level='ERROR', error=exc, action=action,
                                 directory=config.firmware_directory, **{k:str(v) for k,v in arguments.items()})
            owner.events.put(('firmware_library', result))

        self.thread = threading.Thread(target=work, name='firmware-library', daemon=True)
        self.thread.start()

    def complete(self, result):
        self.thread = None
        action, failure, config = result['action'], result['error'], result['config']
        owner = self.owner
        if action != 'inspect':
            with owner.lock:
                owner.configuring = False
        key = (config.hardware_version[1], config.firmware_directory)
        if key != (owner.config.hardware_version[1], owner.config.firmware_directory):
            self.refresh_pending = False
            self.refresh()
            return
        self.choose.configure(state='normal')
        if action == 'inspect' or not failure:
            self.current, self.previous = result['current'], result['previous']
            try:
                platform = platform_for(config)
            except FirmwareError:
                platform = '当前平台'
            if self.current:
                fingerprint = f'　SHA256 {self.current.sha256[:12]}' if self.current.sha256 else ''
                self.target.set(f'{platform} · 目标 APP {self.current.version}\n{self.current.path.name}{fingerprint}')
            else:
                self.target.set(f'{platform} · 尚未选择 APP')
        undo_text = f'恢复至 {self.previous.version}' if self.previous else '恢复上一版'
        if self.previous and self.current and self.previous.version == self.current.version:
            undo_text += f' · {self.previous.sha256[:8]}'
        self.undo.configure(text=undo_text,
                            state='normal' if self.previous else 'disabled')
        notice = '下次开测使用所选 APP。电机版本达到或高于目标时直接继续。'
        if failure:
            self.status(('换版失败。' if action != 'inspect' else '') + failure, error=True)
        elif not self.current:
            self.status('点击「更换 APP」选择随包固件或 U 盘中的发布文件。')
        else:
            self.status(notice if action == 'inspect' else '工位目标已保存。' + notice)
        if self.refresh_pending:
            self.refresh_pending = False
            if not failure:
                self.refresh()
