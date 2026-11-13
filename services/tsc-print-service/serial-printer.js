/**
 * 串口打印机通信模块
 *
 * 通过串口 (COM口) 连接到 USB TSC 打印机，发送 TSPL 指令。
 * 依赖 serialport 库。
 */

const { SerialPort } = require('serialport');
const { ReadlineParser } = require('serialport');

/**
 * 列出所有可用串口
 *
 * @returns {Promise<Array<{path: string, manufacturer?: string, pnpId?: string}>>}
 */
async function listSerialPorts() {
  try {
    const ports = await SerialPort.list();
    return ports.map((p) => ({
      path: p.path,
      manufacturer: p.manufacturer || '',
      serialNumber: p.serialNumber || '',
      pnpId: p.pnpId || '',
      vendorId: p.vendorId || '',
      productId: p.productId || '',
    }));
  } catch (err) {
    console.error('[串口扫描] 失败:', err.message);
    return [];
  }
}

/**
 * 通过串口发送 TSPL 指令到打印机
 *
 * @param {object} options
 * @param {string} options.path - 串口路径 (如 COM3)
 * @param {number} options.baudRate - 波特率 (默认 9600)
 * @param {string} tspCommands - TSPL 指令文本
 * @param {number} timeout - 超时 ms
 * @returns {Promise<{success: boolean, message: string}>}
 */
function sendToSerialPrinter(options, tspCommands, timeout = 10000) {
  const { path, baudRate = 9600, dataBits = 8, stopBits = 1, parity = 'none' } = options;

  return new Promise((resolve) => {
    let settled = false;

    const done = (success, message) => {
      if (settled) return;
      settled = true;
      try { port.close(); } catch (_) { /* ignore */ }
      resolve({ success, message });
    };

    // 超时
    const timer = setTimeout(() => {
      done(false, `串口 ${path} 发送超时 (${timeout}ms)`);
    }, timeout);

    let port;
    try {
      port = new SerialPort({
        path,
        baudRate,
        dataBits,
        stopBits,
        parity,
        autoOpen: false,
      });
    } catch (err) {
      clearTimeout(timer);
      done(false, `无法创建串口连接 ${path}: ${err.message}`);
      return;
    }

    port.open((err) => {
      if (err) {
        clearTimeout(timer);
        done(false, `无法打开串口 ${path}: ${err.message}`);
        return;
      }

      // 发送 TSPL 指令 (GBK / ASCII 编码)
      const buffer = Buffer.from(tspCommands, 'ascii');

      port.write(buffer, (writeErr) => {
        if (writeErr) {
          clearTimeout(timer);
          done(false, `串口写入失败 ${path}: ${writeErr.message}`);
          return;
        }

        // 排空缓冲区后完成
        port.drain((drainErr) => {
          clearTimeout(timer);
          if (drainErr) {
            done(false, `串口排空失败 ${path}: ${drainErr.message}`);
          } else {
            done(true, `TSPL指令已通过串口 ${path} 发送`);
          }
        });
      });
    });
  });
}

/**
 * 检测串口是否可能是 TSC 打印机
 *
 * 通过检查 pnpId / manufacturer 是否包含 TSC 关键字判断。
 *
 * @param {object} portInfo - SerialPort.list() 返回的端口信息
 * @returns {boolean}
 */
function isLikelyTSCPrinter(portInfo) {
  const keywords = ['TSC', 'TSC PRINT', 'PRINTER', 'BARCODE'];
  const checkStr = [
    portInfo.manufacturer,
    portInfo.pnpId,
    portInfo.serialNumber,
  ]
    .join(' ')
    .toUpperCase();

  return keywords.some((kw) => checkStr.includes(kw));
}

module.exports = {
  listSerialPorts,
  sendToSerialPrinter,
  isLikelyTSCPrinter,
};
