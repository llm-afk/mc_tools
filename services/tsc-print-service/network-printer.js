/**
 * 网络打印机通信模块
 *
 * 通过 TCP Socket 连接到网络 TSC 打印机（默认端口 9100），
 * 直接发送 TSPL 原始指令。
 */

const net = require('net');

/**
 * 向网络打印机发送 TSPL 指令
 *
 * @param {string} host - 打印机 IP 地址
 * @param {number} port - 打印机端口（默认 9100）
 * @param {string} tspCommands - TSPL 指令文本
 * @param {number} timeout - 连接超时 ms
 * @returns {Promise<{success: boolean, message: string}>}
 */
function sendToNetworkPrinter(host, port, tspCommands, timeout = 5000) {
  return new Promise((resolve) => {
    const client = new net.Socket();
    let settled = false;

    const done = (success, message) => {
      if (settled) return;
      settled = true;
      client.destroy();
      resolve({ success, message });
    };

    client.setTimeout(timeout);

    client.on('connect', () => {
      client.write(tspCommands, (err) => {
        if (err) {
          done(false, `发送TSPL指令失败: ${err.message}`);
        } else {
          done(true, 'TSPL指令已发送到网络打印机');
        }
      });
    });

    client.on('error', (err) => {
      done(false, `无法连接到打印机 ${host}:${port} — ${err.message}`);
    });

    client.on('timeout', () => {
      done(false, `连接打印机 ${host}:${port} 超时 (${timeout}ms)`);
    });

    client.connect(port, host);
  });
}

/**
 * 检测网络打印机是否在线（尝试 TCP 连接）
 *
 * @param {string} host - IP 地址
 * @param {number} port - 端口
 * @param {number} timeout - 超时
 * @returns {Promise<{online: boolean, error?: string}>}
 */
function checkNetworkPrinter(host, port, timeout = 3000) {
  return new Promise((resolve) => {
    const socket = new net.Socket();
    let settled = false;

    const done = (online, error) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve({ online, error });
    };

    socket.setTimeout(timeout);

    socket.on('connect', () => {
      done(true);
    });

    socket.on('error', (err) => {
      done(false, err.message);
    });

    socket.on('timeout', () => {
      done(false, '连接超时');
    });

    socket.connect(port, host);
  });
}

/**
 * 扫描指定 IP 范围，发现网络 TSC 打印机
 *
 * @param {string} baseIP - IP 段前三位，如 "192.168.1"
 * @param {number} port - 扫描端口 (9100)
 * @param {number} timeout - 每台超时 ms
 * @returns {Promise<Array<{host: string, port: number}>>}
 */
async function scanNetworkPrinters(baseIP, port = 9100, timeout = 500) {
  const found = [];
  const promises = [];

  for (let i = 1; i <= 254; i++) {
    const host = `${baseIP}.${i}`;
    promises.push(
      checkNetworkPrinter(host, port, timeout).then((result) => {
        if (result.online) {
          found.push({ host, port });
        }
      })
    );
  }

  await Promise.allSettled(promises);
  return found;
}

module.exports = {
  sendToNetworkPrinter,
  checkNetworkPrinter,
  scanNetworkPrinters,
};
