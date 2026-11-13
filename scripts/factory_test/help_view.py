"""Offline operator manual; opening it never changes a device or station mode."""
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import tkinter as tk
from tkinter import ttk

from .version import VERSION_LABEL


@dataclass(frozen=True)
class Chapter:
    title: str
    body: str


def load_manual():
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
    text = (base/'docs/产测人员使用手册.md').read_text(encoding='utf-8-sig')
    chapters = []
    title, lines = None, []
    for line in text.splitlines():
        if line.startswith('## '):
            if title is not None:
                chapters.append(Chapter(title, '\n'.join(lines).strip()))
            title, lines = line[3:].strip(), []
        elif title is not None:
            lines.append(line)
    if title is not None:
        chapters.append(Chapter(title, '\n'.join(lines).strip()))
    if not chapters or any(not chapter.title or not chapter.body for chapter in chapters):
        raise ValueError('使用手册内容不完整，请核对软件包。')
    return tuple(chapters)


class HelpWindow:
    def __init__(self, parent):
        self.chapters = load_manual()
        self.visible = []
        self.current = None
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title('使用手册 · '+VERSION_LABEL)
        self.window.configure(background='#FFFFFF')
        self.window.geometry('1050x740')
        self.window.minsize(820, 560)
        self.window.protocol('WM_DELETE_WINDOW', self.hide)
        self.window.bind('<Escape>', self.hide)
        self.window.bind('<Control-f>', self.focus_search)
        self.window.bind('<Control-F>', self.focus_search)
        self.window.bind('<F1>', self.focus_search)
        frame = ttk.Frame(self.window, padding=20)
        frame.pack(fill='both', expand=True)
        heading = ttk.Frame(frame)
        heading.pack(fill='x', pady=(0, 13))
        ttk.Label(heading, text='产测人员使用手册', font=('Microsoft YaHei UI',16,'bold'),
                  foreground='#24364D').pack(side='left')
        ttk.Label(heading, text=VERSION_LABEL, foreground='#64748B').pack(side='right')
        search = ttk.Frame(frame)
        search.pack(fill='x', pady=(0, 12))
        ttk.Label(search, text='查找').pack(side='left', padx=(0,8))
        self.query = tk.StringVar(self.window)
        self.search = ttk.Entry(search, textvariable=self.query)
        self.search.pack(side='left', fill='x', expand=True)
        ttk.Button(search, text='清空', width=6, command=lambda:self.query.set('')).pack(side='left',padx=(8,0))
        self.count = tk.StringVar(self.window)
        ttk.Label(search, textvariable=self.count, foreground='#64748B', width=13,anchor='e').pack(side='right',padx=(8,0))
        footer = self.footer = ttk.Frame(frame)
        footer.pack(side='bottom',fill='x',pady=(12,0))
        ttk.Label(footer,text='选择左侧章节，或输入关键词查找 · Ctrl+F 查找 · Esc 关闭',
                  foreground='#64748B').pack(side='left')
        ttk.Button(footer,text='关闭',command=self.hide).pack(side='right')
        panes = ttk.Panedwindow(frame, orient='horizontal')
        panes.pack(fill='both', expand=True)
        navigation = ttk.Frame(panes, width=245)
        navigation.pack_propagate(False)
        panes.add(navigation, weight=0)
        self.topics = tk.Listbox(navigation, background='#FFFFFF',foreground='#334155',
            selectbackground='#EAF1FF', selectforeground='#1D4ED8', highlightthickness=1,
            highlightbackground='#D8E2EE', borderwidth=0, activestyle='none', exportselection=False,
            font=('Microsoft YaHei UI',10), width=27)
        topics_scroll = ttk.Scrollbar(navigation,orient='vertical',command=self.topics.yview)
        self.topics.configure(yscrollcommand=topics_scroll.set)
        topics_scroll.pack(side='right',fill='y')
        self.topics.pack(side='left',fill='both',expand=True)
        self.topics.bind('<<ListboxSelect>>', self.select)
        article = ttk.Frame(panes,padding=(18,0,0,0))
        panes.add(article,weight=1)
        self.body = tk.Text(article,wrap='word',state='disabled',background='#FFFFFF',
            foreground='#334155',borderwidth=0,highlightthickness=0,padx=2,pady=2,
            font=('Microsoft YaHei UI',11),spacing1=2,spacing3=7,exportselection=False)
        body_scroll = ttk.Scrollbar(article,orient='vertical',command=self.body.yview)
        self.body.configure(yscrollcommand=body_scroll.set)
        body_scroll.pack(side='right',fill='y')
        self.body.pack(fill='both',expand=True)
        self.body.tag_configure('title',font=('Microsoft YaHei UI',15,'bold'),foreground='#24364D',spacing3=16)
        self.body.tag_configure('heading',font=('Microsoft YaHei UI',12,'bold'),spacing1=9,spacing3=9)
        self.body.tag_configure('strong',font=('Microsoft YaHei UI',11,'bold'))
        self.body.tag_configure('code',font=('Consolas',11))
        self.body.tag_configure('match',background='#FFF1B8',foreground='#24364D')
        self.query.trace_add('write',lambda *_:self.filter())
        self.filter()

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.search.focus_set()

    def hide(self, event=None):
        self.window.withdraw()
        return 'break'

    def focus_search(self, event=None):
        self.search.focus_set()
        self.search.selection_range(0,'end')
        return 'break'

    def filter(self):
        query = self.query.get().strip().casefold()
        previous = self.current
        self.visible = [i for i,chapter in enumerate(self.chapters)
                        if not query or query in (chapter.title+'\n'+chapter.body).casefold()]
        self.topics.delete(0,'end')
        for index in self.visible:
            self.topics.insert('end',self.chapters[index].title)
        self.count.set(f'{len(self.visible)} / {len(self.chapters)} 章' if query else f'{len(self.chapters)} 个章节')
        if not self.visible:
            self.current = None
            self.body.configure(state='normal')
            self.body.delete('1.0','end')
            self.body.insert('end','未找到相关章节\n','title')
            self.body.insert('end','试试“打印”“日志”“升级”或“上传”，也可以清空关键词浏览全部章节。')
            self.body.configure(state='disabled')
            return
        row = self.visible.index(previous) if previous in self.visible else 0
        self.topics.selection_set(row)
        self.topics.activate(row)
        self.topics.see(row)
        self.select()

    def select(self, event=None):
        selected = self.topics.curselection()
        if not selected or selected[0]>=len(self.visible):
            return
        self.current = self.visible[selected[0]]
        chapter = self.chapters[self.current]
        self.body.configure(state='normal')
        self.body.delete('1.0','end')
        self.body.insert('end',chapter.title+'\n','title')
        for line in chapter.body.splitlines():
            if line.startswith('### '):
                self.body.insert('end',line[4:]+'\n','heading')
                continue
            for part in re.split(r'(\*\*[^*]+\*\*|`[^`]+`)',line):
                if part.startswith('**') and part.endswith('**'):
                    self.body.insert('end',part[2:-2],'strong')
                elif part.startswith('`') and part.endswith('`'):
                    self.body.insert('end',part[1:-1],'code')
                else:
                    self.body.insert('end',part)
            self.body.insert('end','\n')
        query = self.query.get().strip()
        first, start = None, '1.0'
        if query:
            while True:
                match = self.body.search(query,start,stopindex='end',nocase=True)
                if not match:
                    break
                end = f'{match}+{len(query)}c'
                self.body.tag_add('match',match,end)
                first = first or match
                start = end
        self.body.configure(state='disabled')
        self.body.yview_moveto(0)
        if first:
            self.body.see(first)
