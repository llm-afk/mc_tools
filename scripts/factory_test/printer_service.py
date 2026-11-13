"""Own the supplied Node TSC service without taking ownership of external services."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit

from .integrations import json_http
from .station_config import atomic_json, DEFAULT_PRINTER_NAME


def bundle_paths():
    if getattr(sys,'frozen',False):
        folder=Path(sys.executable).parent/'services'/'tsc-print-service'
        return folder,folder
    folder=Path(__file__).resolve().parents[2]/'services'/'tsc-print-service'
    return folder,folder


def local_endpoint(config):
    parts=urlsplit(config.printer_url)
    return parts.scheme=='http' and parts.hostname in ('localhost','127.0.0.1','::1')


def service_config_path(root):
    return Path(root)/'printer-service'/'config.json'


def spawn_process(*args,**kwargs):
    return subprocess.Popen(*args,**kwargs)


def child_environment(runtime):
    names={'SYSTEMROOT','WINDIR','SYSTEMDRIVE','PATH','TEMP','TMP','USERPROFILE','APPDATA',
        'LOCALAPPDATA','HOMEDRIVE','HOMEPATH','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMDATA',
        'COMSPEC','NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE'}
    result={name:value for name,value in os.environ.items() if name.upper() in names}
    result['NODE_PATH']=str(Path(runtime)/'node_modules')
    return result


def prepare_config(config, root, source=None):
    source=Path(source) if source else bundle_paths()[0]
    path=service_config_path(root)
    template=path if path.exists() else source/'config.json'
    document=json.loads(template.read_text(encoding='utf-8-sig'))
    if not isinstance(document,dict) or not isinstance(document.get('printer'),dict) or not isinstance(document.get('label'),dict):
        raise ValueError('打印服务配置缺少 printer 或 label 对象，请核对配置文件：'+str(template))
    document=deepcopy(document)
    parts=urlsplit(config.printer_url)
    document['httpPort']=parts.port or 80
    document['printer']['type']='windows'
    document['printer']['name']=config.printer_name or document['printer'].get('name') or DEFAULT_PRINTER_NAME
    prefix=parts.path.rstrip('/')
    document['httpRoutes']=dict(status=prefix+config.printer_status_path,print=prefix+config.printer_print_path)
    document['printerDiscovery']=dict(serialScan=False,networkScan=False,windowsPrinterScan=True)
    atomic_json(path,document)
    return path


class LocalPrinterService:
    def __init__(self, config, root, *, diagnostics=None, notify=lambda value:None,
                 previous=None, http=json_http, popen=None, paths=None):
        self.config,self.root=config,Path(root)
        self.diagnostics,self.notify,self.previous=diagnostics,notify,previous
        self.http,self.popen=http,popen or spawn_process
        self.source,self.runtime=paths or bundle_paths()
        self.stop_event=threading.Event()
        self.thread=self.process=self.reader=None
        self.process_lock=threading.Lock()
        self.external=False
        self.error=None

    def event(self, name, message='', *, error=None, level='INFO', **details):
        if self.diagnostics:
            self.diagnostics.current.event('printer_service.'+name,message,level=level,error=error,**details)

    def publish(self, state, detail, **values):
        self.notify(dict(state=state,detail=detail,external=self.external,
            pid=self.process.pid if self.process is not None else None,**values))

    def start(self):
        if self.thread is not None and self.thread.is_alive(): return
        self.thread=threading.Thread(target=self.run,name='printer-service-owner',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        # EOF is the managed child's graceful shutdown signal. A crashed parent
        # also closes this pipe. Never signal or terminate an external process.
        with self.process_lock:
            if self.process is not None and self.process.stdin is not None:
                try: self.process.stdin.close()
                except (OSError,ValueError): pass

    def _read_output(self, process):
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    item = None
                if isinstance(item,dict) and item.get('event')=='printer.job.result':
                    self.event('job',pid=process.pid,print_job_id=item.get('requestId'),result=item,
                               level='INFO' if item.get('success') is True else 'ERROR')
                else:
                    self.event('output',line.rstrip(),pid=process.pid)
        except (OSError,ValueError) as exc:
            self.event('output_failed',error=exc,level='WARNING',pid=process.pid)

    def _existing(self, health_url):
        try:
            reply=self.http(health_url,timeout=3)
        except HTTPError:
            return True  # A different/older service is listening; do not replace it.
        except (URLError,OSError) as exc:
            # A refused connection permits a start. A timeout/unknown transport
            # fault does not prove the port is unused.
            from .diagnostics import error_details
            if error_details(exc)['code']=='CONNECTION_REFUSED': return False
            raise
        return isinstance(reply,dict)

    def run(self):
        try:
            if self.previous is not None:
                while self.previous.thread is not None and self.previous.thread.is_alive():
                    if self.stop_event.wait(.1): return
            if self.stop_event.is_set(): return
            if not local_endpoint(self.config):
                self.external=True
                self.publish('EXTERNAL','使用配置的外部打印服务')
                self.event('external',url=self.config.printer_url)
                return
            parts=urlsplit(self.config.printer_url)
            origin=urlunsplit((parts.scheme,parts.netloc,'','',''))
            health_url=origin+'/api/health'
            self.publish('STARTING','正在检测本机打印服务…')
            if self._existing(health_url):
                self.external=True
                self.publish('EXTERNAL','使用外部打印服务（由外部进程管理）')
                self.event('reused',url=origin,owned=False)
                return
            node=self.runtime/'node.exe'
            script=self.source/'index.js'
            if not node.is_file() or not script.is_file() or not (self.runtime/'node_modules').is_dir():
                raise FileNotFoundError('内置打印服务或 Node.js 运行环境不完整，请完整复制 pmt 项目目录')
            config_path=prepare_config(self.config,self.root,self.source)
            self.publish('STARTING','正在启动本机打印服务…')
            env=child_environment(self.runtime)
            arguments=[str(node),str(script),'--config',str(config_path),'--port',str(parts.port or 80),
                '--host','::1' if parts.hostname=='::1' else '127.0.0.1','--managed']
            with self.process_lock:
                if self.stop_event.is_set(): return
                self.process=self.popen(arguments,cwd=self.source,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                process=self.process
            self.event('spawned',pid=process.pid,arguments=arguments,config_path=str(config_path),owned=True)
            self.reader=threading.Thread(target=self._read_output,args=(process,),name='printer-service-output',daemon=True)
            self.reader.start()
            deadline=time.monotonic()+15
            while not self.stop_event.is_set():
                if process.poll() is not None:
                    raise RuntimeError(f'打印服务提前退出，代码 {process.returncode}；请查看 printer_service.output 日志')
                try:
                    reply=self.http(health_url,timeout=.6)
                    data=reply.get('data',{})
                    if data.get('service')!='tsc-print-service' or data.get('pid')!=process.pid:
                        raise RuntimeError('打印端口由其他进程响应，未取得该进程的所有权')
                    self.publish('RUNNING','本机打印服务已启动，正在确认打印机队列')
                    self.event('ready',pid=process.pid,url=origin,physical_printer_checked=False)
                    break
                except (URLError,OSError):
                    if time.monotonic()>=deadline: raise TimeoutError('本机打印服务启动等待超时')
                    self.stop_event.wait(.15)
            while not self.stop_event.wait(.5):
                if process.poll() is not None:
                    raise RuntimeError(f'打印服务异常退出，代码 {process.returncode}')
        except Exception as exc:
            self.error=str(exc)
            self.event('failed',error=exc,level='ERROR',url=self.config.printer_url)
            if not self.stop_event.is_set(): self.publish('ERROR',self.error)
        finally:
            self.stop()
            if self.process is not None:
                # Allow pending RAW jobs/HTTP replies to settle; no force kill
                # or replay. The GUI remains responsive while waiting for us.
                self.process.wait()
                if self.reader is not None: self.reader.join()
                self.process.stdout.close()
                self.event('stopped',pid=self.process.pid,exit_code=self.process.returncode,owned=True)
