/**
 * 标签图片生成 — 用 @napi-rs/canvas 渲染真实矢量字体
 * 等宽无衬线工业风格 (Consolas), 支持 203 / 300 DPI，默认 40mm×15mm
 *
 * DPI 与纸宽高由 label 配置驱动（TE244=203 DPI，MA3400=300 DPI），
 * 画布尺寸与字号按 DPI 换算，毫米级布局保持一致。
 */
const { createCanvas, loadImage } = require('@napi-rs/canvas');
const bwipjs = require('bwip-js');

const DEFAULT_DPI = 203;          // TSC TE244 / T-4502 (8 dots/mm)
const DEFAULT_WIDTH_MM = 50;
const DEFAULT_HEIGHT_MM = 15;

// 字体配置（40×15 小标签适配）— 以毫米定义，按 DPI 换算为像素
const FONT_FAMILY = 'Consolas';
const FONT_SIZE_TEXT_MM = 2.0;    // 文字字号 (WiFi+SN 同行)
const FONT_SIZE_SN_MM = 2.4;      // 模组SN 单独字号

function canvasToBMP(canvas) {
  const w = canvas.width, h = canvas.height;
  const ctx = canvas.getContext('2d');
  const imgData = ctx.getImageData(0, 0, w, h);
  const pixels = imgData.data;

  const rowBytes = ((w + 31) >> 5) << 2;
  const dataSize = rowBytes * h;
  const fileSize = 14 + 40 + 8 + dataSize;
  const buf = Buffer.alloc(fileSize, 0xFF);

  let off = 0;
  buf[off++] = 0x42; buf[off++] = 0x4D;
  const w32 = v => { for(let i=0;i<4;i++) buf[off++]=(v>>>(i*8))&0xFF; };
  const w16 = v => { for(let i=0;i<2;i++) buf[off++]=(v>>>(i*8))&0xFF; };
  w32(fileSize); w16(0); w16(0); w32(14+40+8);
  w32(40); w32(w); w32(h); w16(1); w16(1);
  w32(0); w32(dataSize); w32(0); w32(0); w32(2); w32(0);
  buf[off++] = 0x00; buf[off++] = 0x00; buf[off++] = 0x00; buf[off++] = 0x00;
  buf[off++] = 0xFF; buf[off++] = 0xFF; buf[off++] = 0xFF; buf[off++] = 0x00;

  for (let cy = 0; cy < h; cy++) {
    const bmpRow = h - 1 - cy;
    const rowOff = off + bmpRow * rowBytes;
    for (let x = 0; x < w; x++) {
      const pxIdx = (cy * w + x) * 4;
      const r = pixels[pxIdx], g = pixels[pxIdx + 1], b = pixels[pxIdx + 2], a = pixels[pxIdx + 3];
      if (a > 128 && (r < 128 || g < 128 || b < 128)) {
        buf[rowOff + (x >> 3)] &= ~(0x80 >> (x & 7));
      }
    }
  }
  return buf;
}

/**
 * 生成单张标签打印数据（TSPL 文本头 + 1-bit BMP + PUTBMP/PRINT 尾）
 *
 * @param {string} wifiName WiFi 名称（可为空）
 * @param {string} sn       SN 编号
 * @param {object} [label]  标签配置（config.label），缺省 203 DPI 40×15mm
 * @param {number} [label.dpi]       打印分辨率 (203 / 300)
 * @param {number} [label.width]     标签宽度 mm
 * @param {number} [label.height]    标签高度 mm
 * @param {number} [label.gap]       标签间距 mm
 * @param {number} [label.density]   打印浓度 0-15
 * @param {number} [label.speed]     打印速度 ips
 * @param {number} [label.direction] 打印方向
 */
async function generateLabelBMP(wifiName, sn, label) {
  const dpi = (label && label.dpi) || DEFAULT_DPI;
  const widthMm = (label && label.width) || DEFAULT_WIDTH_MM;
  const heightMm = (label && label.height) || DEFAULT_HEIGHT_MM;
  const gapMm = (label && label.gap) != null ? label.gap : 3;
  const density = (label && label.density) != null ? label.density : 10;
  const speed = (label && label.speed) != null ? label.speed : 3;
  const direction = (label && label.direction) != null ? label.direction : 1;

  const LABEL_W = Math.round(widthMm * dpi / 25.4);   // 203 DPI: 320
  const LABEL_H = Math.round(heightMm * dpi / 25.4);  // 203 DPI: 120
  const MM = d => Math.round(d * dpi / 25.4);

  // 字号毫米 → 像素 (203 DPI: 16 / 19 px; 300 DPI: 24 / 28 px)
  const fontSizeText = Math.round(FONT_SIZE_TEXT_MM * dpi / 25.4);
  const fontSizeSn = Math.round(FONT_SIZE_SN_MM * dpi / 25.4);
  const shrinkStep = Math.max(2, Math.round(4 * dpi / 300));

  // 1. 生成条码 PNG — scale 越大 PNG 越宽，下采样比例≈0.57 时条宽最均匀
  //    (300 DPI/scale 3 为实测可扫描基准，203 DPI 画布更小故用 scale 2)
  const bwipScale = dpi >= 300 ? 3 : 2;
  const bcHeight = 6; // mm
  const bcBuf = await new Promise((res, rej) => {
    bwipjs.toBuffer({ bcid: 'code128', text: sn, scale: bwipScale, height: bcHeight,
      includetext: false, background: 'ffffff' },
      (e, p) => e ? rej(e) : res(p));
  });
  const bcImg = await loadImage(bcBuf);

  // 2. 创建标签画布
  const cv = createCanvas(LABEL_W, LABEL_H);
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#FFFFFF';
  ctx.fillRect(0, 0, LABEL_W, LABEL_H);
  ctx.fillStyle = '#000000';

  // 3. 布局计算
  const targetBcW = Math.round(LABEL_W * 0.92); // 条码宽度占标签 92%
  const targetBcH = MM(bcHeight);
  const bcY = MM(1);                             // 条码距顶 1mm

  // 条码略微偏左 1.5mm（补偿右侧 margin 差异）
  const bcX = Math.round((LABEL_W - targetBcW) / 2) - MM(1.5);
  ctx.imageSmoothingEnabled = false;  // 关平滑防条缺失
  ctx.drawImage(bcImg, bcX, bcY, targetBcW, targetBcH);
  ctx.imageSmoothingEnabled = true;

  // 4. 文字（条码下方）
  const hasWifi = wifiName && wifiName.length > 0;
  const text = hasWifi ? `${wifiName} ${sn}` : sn;

  // 自适应字体：先试 SN 字号，放不下降级到同行字号再缩小
  let textSize = hasWifi ? fontSizeText : fontSizeSn;
  ctx.font = `${textSize}px "${FONT_FAMILY}"`;
  let textW = ctx.measureText(text).width;
  if (textW > LABEL_W - MM(2)) {
    textSize -= shrinkStep; // 继续缩小
    ctx.font = `${textSize}px "${FONT_FAMILY}"`;
    textW = ctx.measureText(text).width;
  }

  const textY = bcY + targetBcH + MM(1.2) + textSize;
  ctx.fillText(text, Math.round((LABEL_W - textW) / 2) - MM(1.5), textY);

  // 5. BMP + TSPL
  const bmp = canvasToBMP(cv);
  const hdr = Buffer.from(
    `SIZE ${widthMm}mm,${heightMm}mm\r\nGAP ${gapMm}mm,0\r\nSPEED ${speed}\r\nDENSITY ${density}\r\nDIRECTION ${direction}\r\nCLS\r\nDOWNLOAD "L.BMP",${bmp.length},`, 'ascii');
  const ftr = Buffer.from('\r\nPUTBMP 0,0,"L.BMP"\r\nPRINT 1,1\r\n', 'ascii');
  return Buffer.concat([hdr, bmp, ftr]);
}

module.exports = { generateLabelBMP };
