"""正式操作界面：工人主屏和管理设置分开。"""
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from .station import STEPS
from .station_config import MATERIALS, HARDWARE_FIELDS, JOINT_NODE_IDS, material_options
from .manual_control import ControlPage
from .firmware_settings import FirmwarePanel
from .version import VERSION_LABEL


SN_INPUT_LENGTHS = {'project':2, 'category':4, 'subcategory':4, 'factory':2, 'production_date':6}


def limit_entry_length(widget, maximum, *, digits=False):
    widget.configure(validate='key', validatecommand=(
        widget.register(lambda proposed:len(proposed)<=maximum and
                        (not digits or not proposed or all(ch in '0123456789' for ch in proposed))), '%P'))


def port_options(saved='auto'):
    try:
        from serial.tools.list_ports import comports
        found=sorted({p.device for p in comports()})
    except ImportError:
        found=[]
    return list(dict.fromkeys(['auto',*found,*([saved] if saved and saved!='auto' else [])]))


def build_settings(self):
    self.settings_window = tk.Toplevel(self.root)
    self.settings_window.title('工位配置')
    self.settings_window.geometry('760x650')
    self.settings_window.configure(background='#FFFFFF')
    self.settings_window.withdraw()
    self.settings_window.protocol('WM_DELETE_WINDOW',self.close_settings)
    self.settings_window.bind('<F1>',self.show_help)
    settings=ttk.Frame(self.settings_window,padding=20)
    settings.pack(fill='both',expand=True)
    tabs = ttk.Notebook(settings)
    tabs.pack(fill='both', expand=True)
    basic, mes, printer = (ttk.Frame(tabs, padding=10) for _ in range(3))
    for pane, title in ((basic, '工位设备'), (mes, '产品 / SN'), (printer, '服务接口')):
        tabs.add(pane, text=title)
    self.vars = {}
    self.fields = []
    self.field_widgets = {}
    self.service_fields = {'mes_url','mes_sn_path','printer_url','printer_name','printer_status_path',
                           'printer_print_path','printer_copies','sn_api_key_header','api_key_env','report_api_key_header',
                           'report_upload_url','upload_config','oss_endpoint','oss_bucket','oss_region','oss_prefix'}
    def field(parent, row, name, label, choices=None, width=30, editable=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', pady=5, padx=(0, 9))
        initial = str(getattr(self.config, name))
        if name=='production_date' and not initial:
            initial='自动当天'
        if name=='adapter_sn':
            initial=''  # Only display a saved SN after confirming it is online.
        value = tk.StringVar(value=initial)
        self.vars[name] = value
        mode = 'normal' if editable or choices is None else 'readonly'
        widget = (ttk.Combobox(parent, textvariable=value, values=choices, state=mode, width=width, exportselection=False)
                  if choices is not None else ttk.Entry(parent, textvariable=value, width=width))
        widget.grid(row=row, column=1, sticky='ew', pady=5)
        if name in SN_INPUT_LENGTHS:
            limit_entry_length(widget,SN_INPUT_LENGTHS[name])
        self.fields.append((widget, mode))
        self.field_widgets[name] = widget
        widget.bind('<<ComboboxSelected>>', lambda _e: self.selection_changed(name))
        widget.bind('<FocusOut>', lambda _e: self.selection_changed(name))
        widget.bind('<Return>', lambda _e: self.selection_changed(name))
        parent.columnconfigure(1, weight=1)
        return widget
    self.devices = field(basic, 0, 'adapter_sn', 'CANable SN', [])
    self.power_port_combo=field(basic,1,'power_port','电源串口',port_options(self.config.power_port))
    self.dyno_port_combo=field(basic,2,'dyno_port','测功机串口',port_options(self.config.dyno_port))
    self.power_port_combo.configure(postcommand=lambda:self.power_port_combo.configure(values=port_options(self.vars['power_port'].get())))
    self.dyno_port_combo.configure(postcommand=lambda:self.dyno_port_combo.configure(values=port_options(self.vars['dyno_port'].get())))
    self.firmware_panel = FirmwarePanel(self, basic, 3)
    sn_group=ttk.LabelFrame(mes,text='工位产品配置 · 临时 SN / MES 取号共用',padding=12)
    sn_group.pack(fill='both',expand=True)
    field(sn_group, 0, 'project', '项目', list(MATERIALS), editable=True)
    self.category = field(sn_group, 1, 'category', '物料分类', material_options(self.config.project), editable=True)
    target_hint = ttk.Label(sn_group, style='Small.TLabel')
    target_hint.grid(row=1,column=2,sticky='w',padx=(8,0))
    def show_product_id(*_):
        node = JOINT_NODE_IDS.get(self.vars['category'].get().strip().upper())
        target_hint.configure(text=f'产测 ID {node}' if node else '产测 ID 未定义')
    self.vars['category'].trace_add('write', show_product_id)
    show_product_id()
    self.subcategory = field(sn_group, 2, 'subcategory', '子分类', material_options(self.config.project,self.config.category), editable=True)
    field(sn_group, 3, 'factory', '工厂', ['JS', 'GF', 'XC'], editable=True)
    hardware=ttk.LabelFrame(sn_group,text='硬件版本 ABCD',padding=(10,6))
    hardware.grid(row=4,column=0,columnspan=2,sticky='ew',pady=6)
    hardware.columnconfigure(1,weight=1)
    self.vars['hardware_version']=tk.StringVar(value=self.config.hardware_version)
    self.hardware_vars={}
    self.hardware_widgets={}
    labels={'A':'A · 结构版本','B':'B · 芯片平台','C':'C · 电机类型','D':'D · 硬件版本'}
    for row,(part,options) in enumerate(HARDWARE_FIELDS.items()):
        ttk.Label(hardware,text=labels[part]).grid(row=row,column=0,sticky='w',padx=(0,10),pady=3)
        code=self.config.hardware_version[row]
        value=tk.StringVar(value=code)
        self.hardware_vars[part]=value
        widget=(ttk.Entry(hardware,textvariable=value) if part in ('A','D') else
                ttk.Combobox(hardware,textvariable=value,state='normal',exportselection=False,
                             values=[key+' · '+name for key,name in options.items()]))
        widget.grid(row=row,column=1,sticky='ew',pady=3)
        limit_entry_length(widget,1,digits=part in ('A','D'))
        self.fields.append((widget,'normal'))
        self.hardware_widgets[part]=widget
        widget.bind('<<ComboboxSelected>>',lambda _e,p=part:self.hardware_changed(p))
        widget.bind('<FocusOut>',lambda _e,p=part:self.hardware_changed(p))
        widget.bind('<Return>',lambda _e,p=part:self.hardware_changed(p))
    date = field(sn_group, 5, 'production_date', '生产日期（空=当天）',
                 ['自动当天',datetime.now().strftime('%y%m%d')], editable=True)
    date.configure(postcommand=lambda:date.configure(values=['自动当天',datetime.now().strftime('%y%m%d')]))
    services=ttk.Notebook(printer)
    services.pack(fill='both',expand=True)
    sn_service,label_service,report_service,oss_service=(ttk.Frame(services,padding=12) for _ in range(4))
    for pane,title in ((sn_service,'SN 取号'),(label_service,'标签打印'),(report_service,'报告上报'),(oss_service,'OSS 存储')):
        services.add(pane,text=title)
    self.secret_vars,self.secret_widgets={},{}
    def secret(parent,row,name,label):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky='w',padx=(0,10),pady=7)
        value=tk.StringVar(value=self.saved_credentials.get(name,''))
        self.secret_vars[name]=value
        widget=ttk.Entry(parent,textvariable=value,show='*',exportselection=False,width=45)
        widget.grid(row=row,column=1,sticky='ew',pady=7)
        parent.columnconfigure(1,weight=1)
        self.secret_widgets[name]=widget
        self.fields.append((widget,'normal'))
    field(sn_service,0,'mes_url','MES 基础地址')
    field(sn_service,1,'mes_sn_path','取号接口路径')
    field(sn_service,2,'sn_api_key_header','Key 请求头名称',['X-Api-Key','Authorization'],editable=True)
    secret(sn_service,3,'sn_api_key','取号 API Key')
    field(sn_service,4,'api_key_env','取号 Key 后备环境变量名')
    ttk.Label(sn_service,text='取号 API Key 留空时读取此环境变量。变量名也留空时，只使用上方填写的 Key。',
              foreground='#64748B',wraplength=570).grid(row=5,column=0,columnspan=2,sticky='w',pady=14)
    field(label_service,0,'printer_url','打印服务地址')
    field(label_service,1,'printer_status_path','状态查询路径')
    field(label_service,2,'printer_print_path','打印接口路径')
    field(label_service,3,'printer_name','Windows 打印机队列名称',[],editable=True)
    field(label_service,4,'printer_copies','每台打印份数（1～50）')
    ttk.Label(label_service,text='内置打印服务随上位机启动，USB 打印机仍需安装对应 Windows 驱动。\n队列名称须与 Windows 一致，USB001 是端口；名称留空使用服务默认队列。\n试打固定打印 TEST-SN-0001 一张，不申请正式 SN。',foreground='#64748B',wraplength=610).grid(
        row=5,column=0,columnspan=2,sticky='w',pady=14)
    self.printer_service_state=tk.StringVar(value='本机打印服务准备启动')
    ttk.Label(label_service,textvariable=self.printer_service_state,foreground='#64748B',wraplength=610).grid(
        row=6,column=0,columnspan=2,sticky='w',pady=(0,8))
    printer_actions=ttk.Frame(label_service)
    printer_actions.grid(row=7,column=0,columnspan=2,sticky='w')
    self.printer_check_button=ttk.Button(printer_actions,text='检查打印服务',command=self.check_printer_service)
    self.printer_check_button.pack(side='left',padx=(0,8))
    self.printer_test_button=ttk.Button(printer_actions,text='试打测试标签',command=lambda:self.check_printer_service(test_print=True))
    self.printer_test_button.pack(side='left',padx=(0,8))
    ttk.Button(printer_actions,text='标签纸 / 打印配置',command=self.open_printer_config).pack(side='left')
    field(report_service,0,'report_upload_url','报告接口完整地址')
    field(report_service,1,'report_api_key_header','Key 请求头名称',['X-Api-Key','Authorization'],editable=True)
    secret(report_service,2,'mes_api_key','报告 API Key')
    ttk.Label(report_service,text='报告接口通常为 /open-api/test-report。\n已有待传报告保持原目标，不会自动跨环境或跨桶补传。',
              foreground='#64748B').grid(row=3,column=0,columnspan=2,sticky='w',pady=14)
    field(oss_service,0,'oss_endpoint','OSS Endpoint')
    field(oss_service,1,'oss_bucket','OSS Bucket')
    field(oss_service,2,'oss_region','OSS Region')
    secret(oss_service,3,'access_key_id','OSS AccessKey ID')
    secret(oss_service,4,'access_key_secret','OSS AccessKey Secret')
    field(oss_service,5,'oss_prefix','OSS 目录前缀（可空）')
    ttk.Label(oss_service,text='OSS 目标需与 MES 后端一致；报告接口提交的是对象 Key。',
              foreground='#64748B').grid(row=6,column=0,columnspan=2,sticky='w',pady=10)
    self.show_service_keys=tk.BooleanVar(value=False)
    def show_keys():
        for widget in self.secret_widgets.values():
            widget.configure(show='' if self.show_service_keys.get() else '*')
    ttk.Checkbutton(printer,text='显示密钥',variable=self.show_service_keys,command=show_keys).pack(anchor='w',pady=(10,2))
    self.credential_notice=tk.StringVar()
    ttk.Label(printer,textvariable=self.credential_notice,foreground='#64748B',wraplength=650).pack(anchor='w',pady=3)
    tools=ttk.Frame(tabs,padding=16)
    tabs.add(tools,text='管理')
    self.auto_control=ttk.Checkbutton(tools,text='启用设备监视',variable=self.auto,command=self.apply_config)
    self.auto_control.pack(anchor='w',pady=7)
    ttk.Label(tools,text='异常工件会自动收尾、保存记录并隔离本次流程。\n以下入口仅供工程人员查阅和补处理。',foreground='#64748B').pack(anchor='w',pady=12)
    records=ttk.Frame(tools)
    records.pack(fill='x',pady=6)
    self.resume_button=ttk.Button(records,text='工程恢复未完成记录',command=self.resume)
    self.resume_button.pack(side='left',padx=(0,6))
    self.resolve_button=ttk.Button(records,text='工程归档异常记录',command=self.resolve_pending)
    self.resolve_button.pack(side='left',padx=(0,6))
    ttk.Button(records,text='打开结果目录',command=self.open_results).pack(side='left')
    environment_heading=ttk.Frame(tools)
    environment_heading.pack(fill='x',pady=(12,6))
    ttk.Label(environment_heading,text='运行环境与服务配置',style='Section.TLabel').pack(side='left')
    ttk.Button(environment_heading,text='复制诊断信息',command=self.copy_environment).pack(side='right')
    self.export_button=ttk.Button(environment_heading,text='导出本次启动日志',command=self.export_session_logs)
    self.export_button.pack(side='right',padx=6)
    self.environment_text=ScrolledText(tools,width=1,height=10,state='disabled',font=('Microsoft YaHei UI',9),
        background='#FFFFFF',foreground='#334155',relief='flat',wrap='word')
    self.environment_text.pack(fill='both',expand=True)
    self.refresh_environment()
    actions=ttk.Frame(settings)
    actions.pack(fill='x',pady=(14,0))
    self.settings_error=tk.StringVar(value=self.config_error)
    notices=ttk.Frame(actions)
    notices.pack(side='left',fill='x',expand=True)
    self.settings_notice=tk.StringVar()
    ttk.Label(notices,textvariable=self.settings_notice,foreground='#64748B',wraplength=480).pack(anchor='w')
    ttk.Label(notices,textvariable=self.settings_error,foreground='#C13F50',wraplength=480).pack(anchor='w')
    ttk.Button(actions,text='保存并关闭',command=self.finish_settings).pack(side='right')


def build_view(self):
    root=self.root
    root.title('镜识科技 · 产测与电机调试 '+VERSION_LABEL)
    root.geometry('1180x900')
    root.minsize(1050,800)
    root.configure(background='#FFFFFF')
    style=ttk.Style(root)
    style.theme_use('clam')
    style.configure('.',background='#FFFFFF')
    style.map('.',background=[('disabled','#FFFFFF'),('active','#FFFFFF')])
    style.configure('TLabel',font=('Microsoft YaHei UI',10),background='#FFFFFF',foreground='#334155')
    style.configure('TFrame',background='#FFFFFF')
    style.configure('Page.TFrame',background='#FFFFFF')
    style.configure('Card.TFrame',background='#FFFFFF')
    style.configure('Card.TLabel',background='#FFFFFF',foreground='#334155',font=('Microsoft YaHei UI',10))
    style.configure('Small.TLabel',background='#FFFFFF',foreground='#8491A4',font=('Microsoft YaHei UI',9))
    style.configure('Muted.TLabel',background='#FFFFFF',foreground='#8491A4',font=('Microsoft YaHei UI',9))
    style.configure('Title.TLabel',background='#FFFFFF',foreground='#17283D',font=('Microsoft YaHei UI',22,'bold'))
    style.configure('Section.TLabel',background='#FFFFFF',foreground='#24364D',font=('Microsoft YaHei UI',11,'bold'))
    style.configure('TButton',font=('Microsoft YaHei UI',10),padding=(14,8),background='#FFFFFF',foreground='#334155',bordercolor='#D8E2EE',lightcolor='#FFFFFF',darkcolor='#FFFFFF')
    style.map('TButton',background=[('disabled','#FFFFFF'),('pressed','#FFFFFF'),('active','#FFFFFF')],foreground=[('disabled','#A4AFBD')])
    style.configure('Footer.TButton',padding=(12,3))
    style.configure('Primary.TButton',font=('Microsoft YaHei UI',12,'bold'),padding=(26,13),background='#2463DC',foreground='white',borderwidth=0)
    style.map('Primary.TButton',background=[('disabled','#CCD6E4'),('active','#174EBA')],foreground=[('disabled','#F8FAFC')])
    style.configure('TCheckbutton',background='#FFFFFF',font=('Microsoft YaHei UI',10))
    style.map('TCheckbutton',background=[('disabled','#FFFFFF'),('active','#FFFFFF')])
    style.configure('TLabelframe',background='#FFFFFF',bordercolor='#E2E8F0')
    style.configure('TLabelframe.Label',background='#FFFFFF',foreground='#334155')
    style.configure('TNotebook',background='#FFFFFF',bordercolor='#E2E8F0')
    style.configure('TNotebook.Tab',background='#FFFFFF',foreground='#334155',padding=(10,6),lightcolor='#FFFFFF',darkcolor='#FFFFFF')
    style.map('TNotebook.Tab',background=[('selected','#FFFFFF'),('active','#FFFFFF')],foreground=[('selected','#1D4ED8')])
    style.configure('TEntry',fieldbackground='#FFFFFF',foreground='#334155',bordercolor='#D8E2EE',lightcolor='#FFFFFF',darkcolor='#FFFFFF')
    style.map('TEntry',fieldbackground=[('disabled','#FFFFFF'),('readonly','#FFFFFF')],foreground=[('disabled','#A4AFBD')])
    style.configure('TCombobox',background='#FFFFFF',fieldbackground='#FFFFFF',foreground='#334155',bordercolor='#D8E2EE',arrowcolor='#64748B',lightcolor='#FFFFFF',darkcolor='#FFFFFF')
    style.map('TCombobox',background=[('disabled','#FFFFFF'),('active','#FFFFFF'),('readonly','#FFFFFF')],fieldbackground=[('disabled','#FFFFFF'),('readonly','#FFFFFF')],selectbackground=[('readonly','#FFFFFF')],selectforeground=[('readonly','#334155')],foreground=[('disabled','#A4AFBD')])
    root.option_add('*TCombobox*Listbox.background','#FFFFFF')
    root.option_add('*TCombobox*Listbox.foreground','#334155')
    root.option_add('*TCombobox*Listbox.selectBackground','#FFFFFF')
    root.option_add('*TCombobox*Listbox.selectForeground','#1D4ED8')
    style.configure('Flow.Treeview',font=('Microsoft YaHei UI',10),rowheight=28,background='#FFFFFF',fieldbackground='#FFFFFF',borderwidth=0)
    style.configure('Flow.Treeview.Heading',font=('Microsoft YaHei UI',9),background='#F8FAFC',foreground='#8491A4',relief='flat')
    style.configure('Progress.Horizontal.TProgressbar',background='#2563EB',troughcolor='#E8EDF5',borderwidth=0,thickness=5)
    def fixed_label(parent, variable, font, lines, color='#8491A4'):
        # Height is reserved in font lines, so status text cannot move the sections below.
        label=tk.Label(parent,textvariable=variable,font=font,height=lines,width=1,
                       anchor='nw',justify='left',background='#FFFFFF',foreground=color,
                       borderwidth=0,highlightthickness=0,padx=0,pady=0)
        label.bind('<Configure>',lambda event:label.configure(wraplength=max(1,event.width)))
        return label

    shell=ttk.Frame(root,padding=(24,10,24,5))
    shell.pack(fill='both',expand=True)
    self.page_menu=tk.Menu(root,tearoff=False)
    self.page_menu.add_command(label='自动产测',command=lambda:self.switch_page('production'))
    self.page_menu.add_command(label='电机调试',command=lambda:self.switch_page('control'))
    self.page_menu.add_command(label='工位配置',command=self.show_settings)
    self.page_menu.add_command(label='Help',command=self.show_help)
    root.bind('<F1>',self.show_help)
    root.config(menu=self.page_menu)
    self.nav_buttons={}
    self.page_holder=ttk.Frame(shell)
    self.page_holder.pack(fill='both',expand=True)
    production=ttk.Frame(self.page_holder)
    self.production_page=production
    production.pack(fill='both',expand=True)
    production.columnconfigure(0, weight=1)
    production.rowconfigure(3, weight=1)  # Give spare height to the log after fitting all process steps.
    device_row=ttk.Frame(production)
    device_row.grid(row=0,column=0,sticky='ew',pady=(0,6))
    self.device_widgets={}
    for i,(name,title) in enumerate((('can','CAN 适配器'),('motor','待测电机'),('dyno','测功机'),('power','电源'),('printer','标签打印机'))):
        card=ttk.Frame(device_row,style='Card.TFrame',padding=(14,6))
        card.grid(row=0,column=i,sticky='nsew',padx=(0,8 if i<4 else 0))
        device_row.columnconfigure(i,weight=1,uniform='devices')
        ttk.Label(card,text=title,style='Small.TLabel').pack(anchor='w')
        state=tk.StringVar(value='检测中')
        badge=ttk.Label(card,textvariable=state,width=1,style='Card.TLabel',font=('Microsoft YaHei UI',11,'bold'),foreground='#94A3B8')
        badge.pack(fill='x',pady=(4,2))
        detail=tk.StringVar(value='等待设备状态')
        fixed_label(card,detail,('Microsoft YaHei UI',9),2).pack(fill='x')
        self.device_widgets[name]=(state,badge,detail)
    hero=ttk.Frame(production,style='Card.TFrame',padding=(18,6))
    hero.grid(row=1,column=0,sticky='ew')
    self.banner=tk.StringVar(value='正在检查工位，请稍候')
    self.banner_label=fixed_label(hero,self.banner,('Microsoft YaHei UI',20,'bold'),1,'#24364D')
    self.banner_label.grid(row=0,column=0,sticky='ew')
    self.hint=tk.StringVar(value='正在加载配置和设备监视。')
    fixed_label(hero,self.hint,('Microsoft YaHei UI',9),2).grid(row=1,column=0,sticky='ew',pady=(3,0))
    hero.columnconfigure(0,weight=1)
    self.stop=ttk.Button(hero,text='开始测试',width=12,style='Primary.TButton',command=self.start_or_stop,state='disabled')
    self.stop.grid(row=0,column=1,rowspan=2,padx=(20,0))
    self.connection=tk.StringVar(value='等待识别来料 ID')
    self.current_sn=tk.StringVar(value='—')
    self.formal_sn=self.current_sn
    self.sn_phase=tk.StringVar(value='SN · 等待识别')
    flow=ttk.Frame(production,style='Card.TFrame',padding=(18,8,18,4))
    flow.grid(row=2,column=0,sticky='ew',pady=(6,0))
    flow_title=ttk.Frame(flow,style='Card.TFrame')
    flow_title.pack(fill='x',pady=(0,10))
    ttk.Label(flow_title,text='生产进度',style='Card.TLabel',font=('Microsoft YaHei UI',12,'bold')).pack(side='left')
    self.progress_text=tk.StringVar(value='0 / '+str(len(STEPS)))
    ttk.Label(flow_title,textvariable=self.progress_text,width=9,anchor='e',style='Small.TLabel').pack(side='right')
    self.progress=ttk.Progressbar(flow,style='Progress.Horizontal.TProgressbar',maximum=len(STEPS))
    self.progress.pack(fill='x',pady=(0,12))
    self.steps=ttk.Treeview(flow,columns=('step','status'),show='headings',style='Flow.Treeview',height=len(STEPS),selectmode='none')
    self.steps.heading('step',text='步骤')
    self.steps.heading('status',text='状态')
    self.steps.column('step',width=310,stretch=True)
    self.steps.column('status',width=95,stretch=False,anchor='center')
    self.steps.pack(fill='both',expand=True)
    for index,(name,label) in enumerate(STEPS,1):
        self.steps.insert('','end',iid=name,values=(f'{index:02d}   {label}','等待'))
    for state,color in (('DONE','#138365'),('FAILED','#C13F50'),('UNKNOWN','#B27813'),('PENDING','#B27813'),('RUNNING','#2563EB')):
        self.steps.tag_configure(state,foreground=color)
    log_box=ttk.Frame(production,style='Card.TFrame',padding=(12,0))
    log_box.grid(row=3,column=0,sticky='nsew',pady=(6,0))
    ttk.Label(log_box,text='日志',style='Small.TLabel').pack(anchor='w',pady=(0,2))
    self.log=ScrolledText(log_box,height=1,state='disabled',font=('Consolas',9),bg='#FFFFFF',fg='#334155',relief='flat',borderwidth=0)
    self.log.tag_configure('WARNING',foreground='#A16207')
    self.log.tag_configure('ERROR',foreground='#B91C1C')
    self.log.pack(fill='both',expand=True)
    footer=ttk.Frame(production)
    footer.grid(row=4,column=0,sticky='ew',pady=(3,0))
    self.version_label=ttk.Label(footer,text=VERSION_LABEL,width=8,anchor='e',style='Small.TLabel')
    self.version_label.pack(side='right',padx=(10,0))
    self.open_report_button=ttk.Button(footer,text='打开报告目录',style='Footer.TButton',command=self.open_test_report)
    self.open_report_button.pack(side='right')
    self.open_error_log_button=ttk.Button(footer,text='打开日志目录',style='Footer.TButton',command=self.open_error_log)
    self.open_error_log_button.pack(side='right',padx=(0,8))
    self.upload_status=tk.StringVar(value='报告上传：离线预览' if self.config.simulation else '报告上传：准备中')
    ttk.Label(footer,textvariable=self.upload_status,width=1,anchor='w',style='Small.TLabel').pack(side='left',fill='x',expand=True)
    self.control_page=ControlPage(self,self.page_holder)
    build_settings(self)
