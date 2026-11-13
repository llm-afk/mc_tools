"""MES V1.0 和本机 TSC 协议；HTTP 成功与业务成功分别检查。"""
from datetime import datetime
import json
import os
import re
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from .diagnostics import current_log, log_event, response_evidence, error_details
from .service_credentials import sn_key_for, CredentialStoreError

PRINTER_STATUS_TIMEOUT = 8  # The bundled Windows queue query can take up to six seconds.


class IntegrationError(RuntimeError):
    def __init__(self, message, *, code='INTEGRATION_ERROR', hint='', context=None):
        super().__init__(message)
        self.diagnostic_code, self.diagnostic_hint = code, hint
        self.diagnostic_context = context or {}


class PrintUnknown(IntegrationError):
    """打印可能已经执行，禁止自动重发。"""
    def __init__(self, message, *, code='PRINT_OUTCOME_UNKNOWN', hint='', context=None):
        super().__init__(message, code=code, context=context,
            hint=hint or '打印是否出纸未确认，工位不自动重发；维护人员核对实体标签和打印服务日志后决定补打。')


class PendingConfiguration(IntegrationError):
    def __init__(self, message):
        super().__init__(message, code='CONFIGURATION_MISSING', hint='检查工位配置、目标服务地址和指定的密钥环境变量。')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # A redirect must not silently resubmit a non-idempotent print job.


def json_http(url, payload=None, *, headers=None, timeout=10):
    started = time.perf_counter()
    http_id, method = uuid.uuid4().hex, 'POST' if payload is not None else 'GET'
    phase, response_info = 'prepare_request', {}
    local = urlparse(url).hostname in ('127.0.0.1', 'localhost', '::1')
    log_event('http.request', http_id=http_id, url=url, method=method,
              timeout_s=timeout, payload=payload,
              headers={name:'[REDACTED]' for name in (headers or {})},
              proxy='bypassed_localhost' if local else 'system', automatic_redirects=False)
    try:
        body = None if payload is None else json.dumps(payload).encode('utf-8')
        request = Request(url, data=body, headers={'Content-Type': 'application/json', **(headers or {})})
        opener = build_opener(ProxyHandler({}), _NoRedirect()) if local else build_opener(_NoRedirect())
        phase = 'send_and_wait_headers'
        with opener.open(request, timeout=timeout) as response:
            phase = 'read_response_body'
            response_info = dict(http_status=response.status,
                response_headers=response_evidence(b'',response.headers)['response_headers'])
            raw = response.read()
            evidence = response_evidence(raw, response.headers)
            log_event('http.response', http_id=http_id, url=url, http_status=response.status,
                      elapsed_s=round(time.perf_counter()-started, 4), **evidence)
            try:
                phase = 'decode_json'
                result = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise IntegrationError('接口未返回有效 JSON，原始响应摘要已保存', code='HTTP_INVALID_JSON',
                    hint='检查 URL 是否指向接口而非网页、代理或登录页；按 http_id 对照响应正文和服务日志。',
                    context=dict(http_id=http_id, url=url, **evidence)) from exc
    except HTTPError as exc:
        try:
            evidence = response_evidence(exc.read(), exc.headers)
        except Exception as read_exc:
            evidence = dict(response_read_error=str(read_exc))
        log_event('http.exception', level='ERROR', error=exc, http_id=http_id, url=url, method=method,
                  phase=phase,http_status=exc.code, elapsed_s=round(time.perf_counter()-started, 4), **evidence)
        raise
    except Exception as exc:
        log_event('http.exception', level='ERROR', error=exc, http_id=http_id, url=url, method=method,
                  phase=phase,elapsed_s=round(time.perf_counter()-started, 4), **response_info)
        raise
    if not isinstance(result, dict):
        exc = IntegrationError('接口未返回 JSON 对象', code='HTTP_JSON_SHAPE',
            hint='按 http_id 核对响应正文和接口版本，预期 JSON 对象。',
            context=dict(http_id=http_id, actual_type=type(result).__name__))
        log_event('http.invalid_shape', level='ERROR', error=exc, http_id=http_id, url=url)
        raise exc
    return result


def mes_payload(config, request_id, *, production_date=None):
    date = production_date if production_date is not None else config.production_date or datetime.now().strftime('%y%m%d')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,64}', request_id):
        raise ValueError('requestId 格式错误')
    if config.fixture_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', config.fixture_id):
        raise ValueError('fixtureId 格式错误')
    if not re.fullmatch(r'\d{6}', date):
        raise ValueError('生产日期必须为 YYMMDD')
    datetime.strptime(date, '%y%m%d')
    if not re.fullmatch(r'[A-Z0-9]{4}', config.hardware_version):
        raise ValueError('硬件版本必须为四位大写字母或数字')
    payload = dict(requestId=request_id, projectCode=config.project,
                materialCategory=config.category, materialSubCategory=config.subcategory,
                factoryCode=config.factory, hardwareVersion=config.hardware_version,
                productionDate=date, quantity=1)
    if config.fixture_id:
        payload['fixtureId'] = config.fixture_id
    return payload


class MesClient:
    def __init__(self, config, http=json_http, pause=time.sleep):
        self.config, self.http, self.pause = config, http, pause

    def allocate(self, payload, cancelled=lambda: False):
        started = time.perf_counter()
        try:
            key = sn_key_for(self.config)
        except CredentialStoreError as exc:
            raise PendingConfiguration(str(exc)) from exc
        if not key:
            raise PendingConfiguration('请在工位配置中填写取号 API Key；也可沿用环境变量：' + self.config.api_key_env)
        if current_log():
            current_log().owner.register_secret(key)
        url = self.config.mes_url.rstrip('/') + self.config.mes_sn_path
        if urlparse(url).scheme != 'https' and urlparse(url).hostname not in ('localhost', '127.0.0.1'):
            raise IntegrationError('远程 MES 必须使用 HTTPS',code='MES_ENDPOINT_INVALID',
                hint='检查取号基础地址，远程 MES 使用 HTTPS；本机联调地址可使用 HTTP。')
        for attempt in range(4):  # 首次请求加最多三次重试，全程复用保存的 payload。
            log_event('mes.allocate.attempt', attempt=attempt+1, url=url, payload=payload)
            if cancelled():
                raise InterruptedError('已停止；取号状态可由原 requestId 恢复')
            try:
                reply = self.http(url, payload, headers={self.config.sn_api_key_header: key}, timeout=10)
            except HTTPError as exc:
                log_event('mes.allocate.http_error', level='WARNING', error=exc, attempt=attempt+1, http_status=exc.code)
                if exc.code < 500:
                    raise IntegrationError(f'MES HTTP {exc.code}，请检查配置',code='MES_HTTP_REJECTED',
                        hint='核对 HTTP 状态、目标接口地址、鉴权和服务返回正文；保留原 requestId。',
                        context=dict(http_status=exc.code,request_id=payload['requestId'])) from exc
                retry = True
            except (TimeoutError, URLError, OSError) as exc:
                log_event('mes.allocate.transport_error', level='WARNING', error=exc, attempt=attempt+1)
                retry = True
            else:
                log_event('mes.allocate.reply', attempt=attempt+1, response=reply)
                if not isinstance(reply, dict):
                    raise IntegrationError('MES 应答不是 JSON 对象', code='MES_RESPONSE_INVALID',
                        hint='核对取号服务版本和本次 requestId，停止写号，保留原请求恢复。')
                if reply.get('code') == 200:
                    data = reply.get('data')
                    if not isinstance(data, dict) or data.get('requestId') != payload['requestId']:
                        raise IntegrationError('MES 应答 requestId 不匹配，停止写号',code='MES_REQUEST_ID_MISMATCH',
                            hint='核对本次取号请求和响应中的 requestId，禁止写入不属于本件的 SN。',
                            context=dict(expected_request_id=payload['requestId'],data=data))
                    sns = data.get('sns')
                    if type(data.get('count')) is not int or data['count'] != 1 or not isinstance(sns, list) or len(sns) != 1:
                        raise IntegrationError('MES 应答数量不符，停止写号',code='MES_COUNT_MISMATCH',
                            hint='核对 quantity=1、data.count=1 和 sns 中恰好一个 SN。',context=dict(data=data))
                    sn = sns[0]
                    if not isinstance(sn, str) or not re.fullmatch(r'[A-Z0-9]{26}', sn):
                        raise IntegrationError('MES 返回的 SN 格式不符合电机 26 位协议',code='MES_SN_FORMAT',
                            hint='检查服务返回的 SN 长度和编码；不会按下拉框枚举限制业务型号。',context=dict(sn=sn))
                    if sn[12:16] != payload['hardwareVersion']:
                        raise IntegrationError('MES SN 硬件版本与测试配置不符，停止写号',code='MES_SN_HARDWARE_MISMATCH',
                            hint='核对本件冻结的硬件 ABCD 与返回 SN 第 13～16 位。',
                            context=dict(expected=payload['hardwareVersion'],received=sn[12:16],sn=sn))
                    prefix = ''.join(payload[key] for key in ('projectCode', 'materialCategory',
                        'materialSubCategory', 'factoryCode', 'hardwareVersion', 'productionDate'))
                    if not sn.startswith(prefix):
                        raise IntegrationError('MES SN 项目、物料、工厂或日期与取号请求不符',code='MES_SN_PREFIX_MISMATCH',
                            hint='按本件保存的产品参数和生产日期逐字段核对响应 SN。',context=dict(expected_prefix=prefix,sn=sn))
                    log_event('mes.allocate.confirmed', request_id=payload['requestId'], sn=sn,
                              duplicated=data.get('duplicated'), attempt=attempt+1,
                              elapsed_s=round(time.perf_counter()-started, 4))
                    return sn
                retry = reply.get('code') == 500 and reply.get('msg') in (
                    '系统异常，请稍后重试', '流水号发号冲突，请重试')
                if not retry:
                    raise IntegrationError(f'MES 业务错误 {reply.get("code")}：{reply.get("msg", "未知错误")}',
                        code='MES_BUSINESS_REJECTED',
                        hint='检查服务返回的参数或鉴权原因；本类业务错误不自动重试，保留原 requestId。',
                        context=dict(request_id=payload['requestId'], response=reply, attempt=attempt+1))
            if retry and attempt < 3:
                log_event('mes.allocate.retry', '沿用原 requestId 等待重试', attempt=attempt+1,
                          delay_s=1.5, request_id=payload['requestId'])
                self.pause(1.5)
        raise IntegrationError('MES 取号重试已用尽；保留原 requestId，不能重新生成请求',
            code='MES_RETRIES_EXHAUSTED', hint='检查各次 HTTP 错误和服务日志，恢复后复用原 requestId，禁止重新取新号。',
            context=dict(request_id=payload['requestId'], attempts=4))


class PrinterClient:
    def __init__(self, config, http=json_http):
        self.config, self.http = config, http

    def ready(self, *, timeout=PRINTER_STATUS_TIMEOUT, fresh=False):
        base = self.config.printer_url.rstrip('/') + self.config.printer_status_path
        printer_name = self.config.printer_name.strip()
        params = {'printerName': printer_name} if printer_name else {}
        if fresh:
            params['fresh'] = '1'
        url = base+'?'+urlencode(params) if params else base
        urls = [url]
        if fresh and printer_name:
            urls.append(base+'?'+urlencode({'printerName':printer_name}))
        if params:
            urls.append(base)
        started = time.perf_counter()
        log_event('printer.status.start', url=url, printer_name=printer_name, timeout_s=timeout)
        try:
            for url in urls:
                try:
                    reply = self.http(url,timeout=timeout)
                    break
                except HTTPError as exc:
                    if exc.code!=404 or url==base: raise
                    log_event('printer.status.legacy_query_fallback',rejected_url=url,printer_name=printer_name)
            log_event('printer.status.reply', url=url, response=reply,
                      elapsed_s=round(time.perf_counter()-started, 4))
            data = reply.get('data') if isinstance(reply, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get('online'), bool):
                raise IntegrationError('打印状态应答缺少布尔值 data.online', code='PRINTER_STATUS_INVALID',
                    hint='检查 /api/status 应答和打印服务版本；不能将不完整应答视为设备就绪。',
                    context=dict(url=url, response=reply))
            if data.get('errorCode') == 'WINDOWS_PRINTER_QUERY_FAILED':
                raise IntegrationError('Windows 打印队列状态待确认', code='PRINTER_QUEUE_QUERY_FAILED',
                    hint='查看队列查询错误、Windows 打印后台处理服务及当前用户权限。',
                    context=dict(url=url, printer_name=printer_name, response=reply))
            if data['online'] is not True:
                reason = data.get('message') or data.get('reason') or reply.get('message') or reply.get('msg')
                raise IntegrationError('打印服务已连接，打印机未就绪' + (f'：{reason}' if reason else ''), code='PRINTER_OFFLINE',
                    hint='核对打印服务返回信息，并检查 USB、Windows 打印机名称/驱动及设备状态；online=false 未说明具体硬件原因。',
                    context=dict(url=url, printer_name=printer_name, response=reply))
            actual_name = data.get('printerName')
            if printer_name and isinstance(actual_name, str) and actual_name.strip() and actual_name.strip().casefold() != printer_name.casefold():
                raise IntegrationError(f'打印服务检查的队列与配置不一致：{actual_name}', code='PRINTER_TARGET_MISMATCH',
                    hint='核对服务默认打印机及 printerName 参数支持，状态查询与标签提交必须使用同一台打印机。',
                    context=dict(url=url, expected_printer=printer_name, actual_printer=actual_name, response=reply))
            if reply.get('success') is False:
                raise IntegrationError('打印服务返回失败状态', code='PRINTER_STATUS_REJECTED',
                    hint='检查打印服务状态应答和服务日志；online=true 不能覆盖明确的失败状态。',
                    context=dict(url=url, response=reply))
        except Exception as exc:
            failure = self.status_error(exc, url)
            if failure is not exc:
                failure.__cause__ = exc
            log_event('printer.status.failed', '打印探活失败，本次尚未提交标签', level='ERROR', error=failure,
                      cause=error_details(exc), printer_name=printer_name,
                      url=url, elapsed_s=round(time.perf_counter()-started, 4), print_submitted=False)
            if failure is not exc:
                raise failure from exc
            raise
        log_event('printer.status.ready', '服务报告打印机就绪', url=url,
                  printer_name=printer_name, reported_printer_name=actual_name,
                  target_verified=bool(printer_name and isinstance(actual_name,str) and actual_name.strip()),
                  physical_label_checked=False)
        return reply

    def status_error(self, error, url):
        """Translate a status-query failure, retaining the transport cause in logs."""
        if isinstance(error, IntegrationError):
            return error
        details = error_details(error)
        local = urlparse(url).hostname in ('localhost', '127.0.0.1', '::1')
        if isinstance(error, HTTPError):
            message = f'打印状态接口 HTTP {error.code}'
            hint = ('检查状态查询路径，当前服务没有此接口。' if error.code == 404 else
                    '检查打印服务响应正文、接口路径与服务日志。')
            code = 'PRINTER_STATUS_HTTP'
        elif details['code'] == 'CONNECTION_REFUSED':
            message = '本机打印服务未启动或地址/端口不符' if local else '打印服务拒绝连接，请检查服务和地址/端口'
            hint = '先检查打印服务进程及监听端口，再检查服务地址；安装 USB 打印驱动不会自动启动 HTTP 服务。'
            code = 'PRINTER_SERVICE_UNREACHABLE'
        elif details['code'] == 'TIMEOUT':
            message = '打印状态查询超时'
            hint = '打印期间服务可能暂时无响应；检查服务日志及队列状态，超时不能判断是否已出纸。'
            code = 'PRINTER_STATUS_TIMEOUT'
        elif isinstance(error, (URLError, OSError)):
            message, hint, code = '打印服务连接失败', details['hint'], 'PRINTER_STATUS_TRANSPORT'
        else:
            return error
        return IntegrationError(message, code=code, hint=hint,
            context=dict(url=url, printer_name=self.config.printer_name, cause=details, print_submitted=False))

    def print_sn(self, sn, *, job_id=None):
        context = getattr(current_log(),'context',{})
        job_id, started = job_id or context.get('print_job_id') or uuid.uuid4().hex, time.perf_counter()
        copies = self.config.printer_copies
        if type(copies) is not int or not 1 <= copies <= 50:
            raise IntegrationError('打印份数必须为 1～50 的整数（打印接口规定）', code='PRINT_COPIES_INVALID',
                hint='在工位配置中调整打印份数；本次尚未提交标签。',
                context=dict(copies=copies, print_submitted=False))
        timeout = max(20,3*copies)
        payload = dict(items=[dict(sn=sn)], copies=copies)
        if self.config.printer_name.strip():
            payload['printerName'] = self.config.printer_name.strip()
        context = dict(print_job_id=job_id, sn=sn, url=self.config.printer_url.rstrip('/') + self.config.printer_print_path,
                       printer_name=self.config.printer_name, copies=copies)
        log_event('printer.submit', payload=payload, timeout_s=timeout, automatic_retry=False,
                  expected_label_format='CODE128 50x15mm', **context)
        try:
            reply = self.http(context['url'], payload, headers={'X-Request-Id': job_id}, timeout=timeout)
        except Exception as exc:
            log_event('printer.unknown', '打印结果未确认，不重发任务', level='ERROR', error=exc,
                      elapsed_s=round(time.perf_counter()-started, 4), outcome='UNKNOWN', **context)
            raise PrintUnknown('打印响应未确认，请人工核对出纸；禁止自动补打', context=context) from exc
        log_event('printer.reply', response=reply, elapsed_s=round(time.perf_counter()-started, 4), **context)
        data = reply.get('data') if isinstance(reply, dict) else None
        results = data.get('results') if isinstance(data, dict) else None
        invalid = (not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict)
                   or results[0].get('sn') != sn or not isinstance(results[0].get('success'), bool))
        if invalid:
            exc = PrintUnknown('打印应答格式异常或未逐条匹配 SN，请核对实体标签',
                code='PRINT_ACK_INVALID', context=dict(**context, response=reply))
            log_event('printer.ack.invalid', level='ERROR', error=exc, **context)
            raise exc
        item = results[0]
        if item.get('outcome')=='UNKNOWN':
            exc=PrintUnknown('打印服务报告提交结果未知；请核对实体标签及 Windows 作业记录',
                code='PRINT_SERVICE_OUTCOME_UNKNOWN',context=dict(**context,response=reply))
            log_event('printer.item.unknown',level='ERROR',error=exc,outcome='UNKNOWN',**context)
            raise exc
        if item['success'] is False:
            exc = IntegrationError('标签打印失败：' + str(item.get('message', '服务未说明原因')),
                code='PRINT_ITEM_FAILED', hint='服务明确报告本条 SN 失败；按原始 message 检查打印机/服务，不重新申请 SN。',
                context=dict(**context, response=reply))
            log_event('printer.item.failed', level='ERROR', error=exc, outcome='FAILED', **context)
            raise exc
        # Optional counters/copies may be omitted; contradictory receipts cannot prove success.
        inconsistent = any(key in data and (type(data[key]) is not int or data[key] not in (1,copies))
                           for key in ('total', 'success'))
        inconsistent |= all(key in data for key in ('total','success')) and data['total']!=data['success']
        inconsistent |= 'copies' in item and (type(item['copies']) is not int or item['copies'] != copies)
        inconsistent |= 'success' in reply and reply['success'] is not True
        if inconsistent:
            exc = PrintUnknown('打印成功条目与总数、份数或顶层状态矛盾，请核对实体标签',
                code='PRINT_ACK_CONFLICT', context=dict(**context, response=reply))
            log_event('printer.ack.conflict', level='ERROR', error=exc, **context)
            raise exc
        log_event('printer.confirmed', '本条 SN 的打印服务应答已确认', outcome='API_CONFIRMED',
                  physical_label_checked=False, elapsed_s=round(time.perf_counter()-started, 4), **context)
        return item
