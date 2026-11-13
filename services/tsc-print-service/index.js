// Based on the supplied 2026-10-22 TSC service; factory integration revision 2.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const windows = require('./windows-printer');
const { generateLabelBMP } = require('./label-image');
const { buildBatchTSPL } = require('./tspl-builder');

function sendJSON(res, status, body) {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type' });
  res.end(JSON.stringify(body));
}

async function readJSON(req) {
  let size = 0, chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 1024 * 1024) throw new Error('请求正文超过 1 MiB');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function createService(config, adapters = {}) {
  const list = adapters.listWindowsPrinters || windows.listWindowsPrinters;
  const send = adapters.sendToPrinter || windows.sendToPrinter;
  const render = adapters.generateLabelBMP || generateLabelBMP;
  const statusPath = config.httpRoutes?.status || '/api/status';
  const printPath = config.httpRoutes?.print || '/api/print';
  let tail = Promise.resolve(), activeJobs = 0;

  let snapshot, reading;
  async function queues(fresh) {
    const now = performance.now();
    if (!fresh && snapshot && now - snapshot.finished < 6000) return snapshot;
    if (!reading) {
      snapshot = undefined;
      reading = Promise.resolve().then(list).then(rows => {
        const finished = performance.now();
        snapshot = { rows, finished, checkedAt: new Date().toISOString(), queryMs: Math.round(finished-now) };
        return snapshot;
      }).finally(() => { reading = undefined; });
    }
    return reading;
  }

  async function status(name, fresh = false) {
    try {
      const sample = await queues(fresh);
      const result = windows.printerState(sample.rows, name, sample.checkedAt);
      return { ...result, labelWidth: config.label.width, labelHeight: config.label.height,
        dpi: config.label.dpi, service: 'tsc-print-service', queryMs: sample.queryMs,
        sampleAgeMs: Math.round(performance.now()-sample.finished) };
    } catch (error) {
      console.error('[状态] Windows 打印机查询失败:', error.stack || error.message);
      return { online: false, printerName: name, found: false, allPrinters: [],
        errorCode: 'WINDOWS_PRINTER_QUERY_FAILED', reason: '无法查询 Windows 打印队列，请检查打印后台处理服务及权限。',
        error: error.message, physicalOutputConfirmed: false };
    }
  }

  async function print(body, requestId, queuedMs) {
    const name = body.printerName || config.printer.name;
    const copies = body.copies ?? 1, results = [];
    const state = await status(name, true);
    let succeeded = 0;
    for (const item of body.items) {
      const jobs = [];
      let successes = 0, unknown = false, message = '';
      for (let number = 0; number < copies; number++) {
        let result, renderMs, submitMs;
        if (!state.online) result = { success: false, outcome: 'FAILED', message: state.reason };
        else {
          try {
            const start = performance.now();
            const bytes = await render(item.wifiName || '', item.sn, config.label);
            const rendered = performance.now();
            renderMs = Math.round(rendered-start);
            result = await send(name, bytes);
            submitMs = Math.round(performance.now()-rendered);
          } catch (error) {
            result = { success: false, outcome: 'FAILED', message: error.message };
          }
        }
        jobs.push(result);
        if (result.success === true) { successes++; succeeded++; }
        if (result.outcome === 'UNKNOWN') unknown = true;
        if (!result.success && !message) message = result.message || '服务未说明失败原因';
        console.log(JSON.stringify({ event: 'printer.job.result', requestId, sn: item.sn,
          printerName: name, portName: state.portName, copy: number + 1, queuedMs, renderMs, submitMs,
          queryMs: state.queryMs, ...result }));
        // Never submit another copy after an uncertain outcome.
        if (unknown) break;
      }
      results.push({ sn: item.sn, copies, success: successes === copies && !unknown,
        outcome: unknown ? 'UNKNOWN' : successes === copies ? 'SUBMITTED' : 'FAILED',
        message: message || 'Windows 打印队列已接收；实际出纸待现场确认',
        submittedCopies: successes, jobs, physicalOutputConfirmed: false });
      if (unknown) {
        for (const skipped of body.items.slice(results.length)) results.push({ sn: skipped.sn,
          copies, success: false, outcome: 'FAILED', message: '前序标签结果未知，本条未提交' });
        break;
      }
    }
    return { success: succeeded > 0, data: { requestId, total: body.items.length * copies,
      success: succeeded, results, physicalOutputConfirmed: false } };
  }

  async function route(req, res) {
    const url = new URL(req.url, 'http://localhost');
    if (req.method === 'OPTIONS') return sendJSON(res, 204, {});
    if (req.method === 'GET' && url.pathname === '/api/health') return sendJSON(res, 200,
      { success: true, data: { service: 'tsc-print-service', bridgeRevision: 2, pid: process.pid,
        managed: process.argv.includes('--managed'), activeJobs, nodeVersion: process.version } });
    if (req.method === 'GET' && url.pathname === statusPath) return sendJSON(res, 200,
      { success: true, data: await status(url.searchParams.get('printerName') || config.printer.name,
        url.searchParams.get('fresh') === '1') });
    if (req.method === 'GET' && url.pathname === '/api/config') return sendJSON(res, 200,
      { success: true, data: { service: 'tsc-print-service', printer: config.printer,
        label: config.label, layout: config.layout, httpRoutes: { status: statusPath, print: printPath } } });
    if (req.method === 'GET' && url.pathname === '/api/printers') return sendJSON(res, 200,
      { success: true, data: { windows: (await queues(false)).rows } });
    if (req.method === 'POST' && (url.pathname === printPath || url.pathname === '/api/preview')) {
      let body;
      try { body = await readJSON(req); }
      catch (error) { return sendJSON(res, 400, { success: false, message: error.message }); }
      if (!Array.isArray(body.items) || !body.items.length || body.items.some(item =>
        !item || typeof item.sn !== 'string' || !item.sn)) return sendJSON(res, 400,
        { success: false, message: 'items 必须包含非空 SN 文本' });
      const copies = body.copies ?? 1;
      if (!Number.isInteger(copies) || copies < 1 || copies > 50) return sendJSON(res, 400,
        { success: false, message: 'copies 必须为 1～50 的整数' });
      if (url.pathname === '/api/preview') return sendJSON(res, 200,
        { success: true, data: { tspCommands: buildBatchTSPL(body.items, body.label || config.label),
          count: body.items.length, label: config.label } });
      activeJobs++;
      const clientId = req.headers['x-request-id'];
      const requestId = typeof clientId === 'string' && /^[a-zA-Z0-9_-]{1,80}$/.test(clientId) ? clientId : randomUUID();
      const enqueued = performance.now();
      const job = tail.then(() => print(body, requestId, Math.round(performance.now()-enqueued)));
      tail = job.catch(() => {});
      try { return sendJSON(res, 200, await job); }
      finally { activeJobs--; }
    }
    sendJSON(res, 404, { success: false, message: '未知接口' });
  }
  const server = http.createServer((req, res) => route(req, res).catch(error => {
    console.error('[请求] 异常:', error.stack || error.message);
    sendJSON(res, 500, { success: false, message: error.message });
  }));
  server.waitForJobs = async () => { while (activeJobs > 0) await tail; };
  return server;
}

async function main() {
  const value = (name, fallback) => {
    const index = process.argv.indexOf(name);
    return index < 0 ? fallback : process.argv[index + 1];
  };
  const filename = value('--config', path.join(__dirname, 'config.json'));
  const config = JSON.parse(fs.readFileSync(filename, 'utf8').replace(/^\uFEFF/, ''));
  if (process.argv.includes('--self-check')) {
    const bytes = await generateLabelBMP('', 'TEST-SN-0001', config.label);
    const size=`SIZE ${config.label.width || 50}mm,${config.label.height || 15}mm`;
    if (!Buffer.isBuffer(bytes) || !bytes.subarray(0, 100).toString().includes(size))
      throw new Error('Label renderer self-check failed');
    console.log(JSON.stringify({ ok: true, bytes: bytes.length, nodeVersion: process.version, label: config.label }));
    return;
  }
  const port = Number(value('--port', config.httpPort || 9876));
  const host = value('--host', '127.0.0.1');
  if (!['127.0.0.1', '::1', 'localhost'].includes(host)) throw new Error('本机打印服务仅绑定回环地址');
  const server = createService(config);
  server.on('error', error => { console.error('[服务] 启动失败:', error.stack); process.exitCode = 1; });
  server.listen(port, host, () => console.log(JSON.stringify({ event: 'printer.service.started',
    host, port, pid: process.pid, printerName: config.printer.name, config: filename, label: config.label })));
  let stopping = false;
  function stop(reason) {
    if (stopping) return;
    stopping = true;
    console.log('[服务] 正在停止:', reason);
    server.close(async () => { await server.waitForJobs(); console.log('[服务] 已停止'); process.exit(0); });
  }
  process.on('SIGINT', () => stop('SIGINT'));
  process.on('SIGTERM', () => stop('SIGTERM'));
  if (process.argv.includes('--managed')) {
    for (const stream of [process.stdout, process.stderr]) stream.on('error', error => {
      if (error.code !== 'EPIPE') throw error;
    });
    process.stdin.on('end', () => stop('上位机已关闭输入管道'));
    process.stdin.resume();
  }
}

if (require.main === module) main().catch(error => { console.error(error.stack); process.exitCode = 1; });
module.exports = { createService };
