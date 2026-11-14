"""Read-only APP status for the executable's flat firmware folder."""
from dataclasses import replace
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk

from .diagnostics import log_scope
from .firmware_library import firmware_root, load_target


class FirmwarePanel:
    def __init__(self, owner, parent, row):
        self.owner = owner
        self.thread = None
        self.refresh_pending = False
        self.target = tk.StringVar()
        self.note = tk.StringVar()
        self.box = ttk.LabelFrame(parent, text='APP 固件', padding=12)
        self.box.grid(row=row, column=0, columnspan=2, sticky='ew', pady=(14, 4))
        ttk.Label(self.box, textvariable=self.target, wraplength=620).pack(anchor='w')
        ttk.Button(self.box, text='打开固件目录', command=owner.open_firmware_directory).pack(anchor='w', pady=(8, 4))
        self.message = ttk.Label(self.box, textvariable=self.note, wraplength=620, foreground='#64748B')
        self.message.pack(anchor='w')
        self.box.after(0, self.refresh)
        self.box.after(1000, self.auto_refresh)

    def auto_refresh(self):
        if self.owner.closing:
            return
        if self.box.winfo_ismapped():
            self.refresh()
        self.box.after(1000, self.auto_refresh)

    def refresh(self):
        owner = self.owner
        if owner.closing:
            return
        if self.thread is not None:
            self.refresh_pending = True
            return
        config = replace(owner.config)
        target = owner.diagnostics.station.bind(phase='firmware_folder')

        def work():
            result = dict(config=config, info=None, error='')
            with log_scope(target):
                try:
                    result['info'] = load_target(config)[1]
                except Exception as exc:
                    result['error'] = str(exc)
            owner.events.put(('firmware_library', result))

        self.thread = threading.Thread(target=work, name='firmware-folder', daemon=True)
        self.thread.start()

    def complete(self, result):
        self.thread = None
        owner = self.owner
        if owner.closing:
            return
        if result['config'].hardware_version[1] != owner.config.hardware_version[1]:
            self.refresh_pending = False
            self.refresh()
            return
        info = result['info']
        if info:
            self.target.set(f"{info['platform']} · 目标 APP {info['target_version']}\n{Path(info['file']).name}")
        else:
            self.target.set('未找到可用 APP')
        self.note.set(result['error'] or f'替换 {firmware_root()} 中的 APP 即可。版本较低时升级，相同或更高时跳过。')
        self.message.configure(foreground='#AE3644' if result['error'] else '#64748B')
        if self.refresh_pending:
            self.refresh_pending = False
            self.refresh()
