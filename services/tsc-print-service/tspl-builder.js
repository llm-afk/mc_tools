/**
 * TSPL 文本指令构建 — /api/preview 预览用
 *
 * 标签尺寸与 DPI 由 label 配置驱动（TE244=203 DPI，MA3400=300 DPI）。
 * 注意：实际打印主链路走 label-image.js 的 DOWNLOAD+PUTBMP 图片方案，
 * 本模块仅生成可读的纯 TSPL 文本指令。
 *
 * 坐标单位 dots，随 DPI 换算 (203 DPI: 1mm ≈ 8 dots; 300 DPI: 1mm ≈ 11.8 dots)
 */

const DEFAULT_DPI = 203;
const DEFAULT_WIDTH_MM = 40;
const DEFAULT_HEIGHT_MM = 15;

/** label 配置 → { dpi, widthMm, heightMm, W, H } (dots) */
function dims(label) {
  const dpi = (label && label.dpi) || DEFAULT_DPI;
  const widthMm = (label && label.width) || DEFAULT_WIDTH_MM;
  const heightMm = (label && label.height) || DEFAULT_HEIGHT_MM;
  return {
    dpi,
    widthMm,
    heightMm,
    W: Math.round(widthMm * dpi / 25.4),
    H: Math.round(heightMm * dpi / 25.4),
  };
}

function mm(d, dpi) { return Math.round(d * dpi / 25.4); }

/**
 * 估算 CODE128 条码宽度 (dots)
 * 每字符 11 modules × narrow，+start/check/stop 3 字符，+4 margin
 * narrow=2 适用于 300 DPI；203 DPI 用 narrow=1 才能放进 40mm 标签
 */
function barcodeWidth(sn, narrow = 1) {
  return (sn.length + 3) * 11 * narrow + 4;
}

/**
 * 估算文字宽度 (dots)
 * 字体"2"≈20px/char, 字体"3"≈28px/char (300 DPI 基准)
 */
function textWidth(text, font) {
  return text.length * (font === '3' ? 28 : 20);
}

function centerX(totalW, elemW) {
  return Math.round((totalW - elemW) / 2);
}

/** DPI → CODE128 最窄条宽（narrow，dots）：保证条码不超出 40mm 标签 */
function narrowFor(dpi) {
  return dpi >= 300 ? 2 : 1;
}

/**
 * 单张标签
 */
function buildLabelTSPL({ sn, wifiName, label }) {
  const { dpi, widthMm, heightMm, W } = dims(label);
  const { gap = 3, density = 10, speed = 3, direction = 1, copies = 1 } = label || {};
  const narrow = narrowFor(dpi);

  const lines = [];
  lines.push(`SIZE ${widthMm}mm,${heightMm}mm`);
  lines.push(`GAP ${gap}mm,0`);
  lines.push(`SPEED ${speed}`);
  lines.push(`DENSITY ${density}`);
  lines.push(`DIRECTION ${direction}`);
  lines.push('REFERENCE 0,0');
  lines.push('CLS');

  // WiFi 名称（上，居中，font=2）
  if (wifiName) {
    const wx = centerX(W, textWidth(wifiName, '2'));
    lines.push(`TEXT ${wx},${mm(2.5, dpi)},"2",0,1,1,"${wifiName}"`);
  }

  // 条形码（中，居中，humanReadable=1）
  const bx = centerX(W, barcodeWidth(sn, narrow));
  const by = mm(7, dpi); // 距顶 7mm
  const bh = mm(6, dpi); // 条码高 6mm
  lines.push(`BARCODE ${bx},${by},"128",${bh},1,0,${narrow},${narrow},"${sn}"`);

  lines.push(`PRINT ${copies},1`);
  return lines.join('\r\n');
}

/**
 * 批量标签（全局设置一次，逐张 CLS→PRINT）
 */
function buildBatchTSPL(items, label) {
  if (!items || items.length === 0) return '';
  const { dpi, widthMm, heightMm, W } = dims(label);
  const { gap = 3, density = 10, speed = 3, direction = 1 } = label || {};
  const narrow = narrowFor(dpi);

  const bh = mm(6, dpi);
  const by = mm(7, dpi);
  const wy = mm(2.5, dpi);

  const lines = [];
  lines.push(`SIZE ${widthMm}mm,${heightMm}mm`);
  lines.push(`GAP ${gap}mm,0`);
  lines.push(`SPEED ${speed}`);
  lines.push(`DENSITY ${density}`);
  lines.push(`DIRECTION ${direction}`);
  lines.push('REFERENCE 0,0');

  for (const item of items) {
    lines.push('CLS');
    if (item.wifiName) {
      const wx = centerX(W, textWidth(item.wifiName, '2'));
      lines.push(`TEXT ${wx},${wy},"2",0,1,1,"${item.wifiName}"`);
    }
    const bx = centerX(W, barcodeWidth(item.sn, narrow));
    lines.push(`BARCODE ${bx},${by},"128",${bh},1,0,${narrow},${narrow},"${item.sn}"`);
    lines.push('PRINT 1,1');
  }

  return lines.join('\r\n');
}

module.exports = { buildLabelTSPL, buildBatchTSPL, barcodeWidth, textWidth, centerX };
