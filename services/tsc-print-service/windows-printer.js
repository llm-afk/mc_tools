// Integration adapter for the supplied TSC service. Shell-free, Unicode-safe.
const { execFile } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { randomUUID } = require('node:crypto');
const powershell = path.join(process.env.SystemRoot || 'C:\\Windows',
  'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');

function runScript(name, args = [], timeout = 15000) {
  return new Promise((resolve, reject) => {
    execFile(powershell, ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
      '-File', path.join(__dirname, name), ...args],
    { encoding: 'utf8', windowsHide: true, timeout, maxBuffer: 1024 * 1024 }, (error, stdout, stderr) => {
      if (error) { error.message += '\n' + String(stderr || '').trim(); reject(error); }
      else {
        try { resolve(JSON.parse(stdout.replace(/^\uFEFF/, '').trim())); }
        catch (cause) { reject(new Error(`PowerShell JSON invalid: ${stdout.slice(0, 1000)}`, { cause })); }
      }
    });
  });
}

async function listWindowsPrinters() {
  const rows = await runScript('windows-status.ps1', [], 6000);
  if (!Array.isArray(rows)) throw new Error('Windows printer query did not return an array');
  return rows;
}

function printerState(printers, name, lastCheck = new Date().toISOString()) {
  const found = printers.find(p => String(p.name).toLowerCase() === String(name).toLowerCase());
  const base = { mode: 'windows', printerName: found ? found.name : name,
    found: !!found, allPrinters: printers.map(p => p.name), lastCheck,
    statusSource: 'Windows Print Spooler', physicalOutputConfirmed: false };
  if (!found) return { ...base, online: false, reason: `Windows 未找到打印队列“${name}”，请安装对应 USB 打印驱动并核对队列名称。` };
  const errors = { 4: '缺纸', 6: '耗材不足', 7: '打印机开盖', 8: '卡纸', 9: '打印机离线', 11: '输出槽已满' };
  const reason = found.workOffline || found.printerStatus === 7 ? 'Windows 报告打印机离线' : errors[found.detectedErrorState];
  return { ...base, ...found, printerName: found.name, online: !reason, reason: reason || null };
}

async function sendToPrinter(name, bytes, timeout = 15000) {
  const file = path.join(os.tmpdir(), `tsc-${randomUUID()}.bin`);
  let enteredHelper = false;
  try {
    await fs.writeFile(file, bytes); enteredHelper = true;
    return await runScript('raw-print.ps1', [file, name], timeout);
  } catch (error) {
    return { success: false, outcome: enteredHelper ? 'UNKNOWN' : 'FAILED',
      message: String(error.message), errorCode: error.code || null };
  } finally { await fs.unlink(file).catch(() => {}); }
}
module.exports = { listWindowsPrinters, printerState, sendToPrinter };
