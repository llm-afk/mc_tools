/**
 * 打印机自动发现模块
 *
 * 扫描所有可能的打印机连接：
 * 1. 串口 (USB连接的TSC打印机)
 * 2. 局域网 (TCP 9100端口)
 */

const { listSerialPorts, isLikelyTSCPrinter } = require('./serial-printer');
const { checkNetworkPrinter } = require('./network-printer');

/**
 * 发现所有可用的 TSC 打印机
 *
 * @param {object} config - 完整配置对象
 * @returns {Promise<{serial: Array, network: Array, all: Array}>}
 */
async function discoverPrinters(config) {
  const serial = [];
  const network = [];

  // 1. 扫描串口
  if (config.printerDiscovery?.serialScan !== false) {
    try {
      const ports = await listSerialPorts();
      for (const port of ports) {
        // 如果指定了串口路径则优先匹配，否则列出所有可能端口
        if (config.printer?.serial?.path && port.path === config.printer.serial.path) {
          serial.push({
            type: 'serial',
            path: port.path,
            manufacturer: port.manufacturer,
            configured: true,
            label: `${port.path} (已配置)`,
          });
        } else {
          // 列出所有串口供手动选择
          serial.push({
            type: 'serial',
            path: port.path,
            manufacturer: port.manufacturer,
            configured: false,
            label: `${port.path}${port.manufacturer ? ' - ' + port.manufacturer : ''}`,
            likelyTSC: isLikelyTSCPrinter(port),
          });
        }
      }
    } catch (err) {
      console.error('[发现] 串口扫描失败:', err.message);
    }
  }

  // 2. 扫描网络打印机
  if (config.printerDiscovery?.networkScan !== false) {
    // 先检查配置中指定的 IP
    if (config.printer?.network?.host) {
      try {
        const result = await checkNetworkPrinter(
          config.printer.network.host,
          config.printer.network.port || 9100,
          3000
        );
        network.push({
          type: 'network',
          host: config.printer.network.host,
          port: config.printer.network.port || 9100,
          configured: true,
          online: result.online,
          label: `${config.printer.network.host}:${config.printer.network.port || 9100}${result.online ? ' ✓' : ' ✗'}${config.printer.name ? ' (' + config.printer.name + ')' : ''}`,
        });
      } catch (err) {
        network.push({
          type: 'network',
          host: config.printer.network.host,
          port: config.printer.network.port || 9100,
          configured: true,
          online: false,
          label: `${config.printer.network.host}:${config.printer.network.port || 9100} ✗ (无法连接)`,
        });
      }
    }

    // 扫描局域网 (如果不为空且配置了扫描范围)
    if (config.printerDiscovery?.networkScanRange) {
      const range = config.printerDiscovery.networkScanRange;
      const match = range.match(/^(\d+\.\d+\.\d+)\.(\d+)-(\d+\.\d+\.\d+)\.(\d+)$/);
      if (match) {
        // 简化处理：仅扫描配置中指定的 C 段
        // 对于大型扫描，建议使用专门工具
        console.log('[发现] 局域网范围扫描耗时较长，仅检查已配置IP。' +
          '如需全量扫描请使用专用网络工具。');
      }
    }
  }

  const all = [...serial, ...network];

  return { serial, network, all };
}

module.exports = { discoverPrinters };
