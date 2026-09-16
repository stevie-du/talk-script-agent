// PNG 最小解码 / 编码（零依赖）。
//
// 为什么要抽出来：`ink-measure.js`、`png-tools.js` 都要「把截图的像素读进来」，
// 同一个解码器抄两份的话，改一处忘一处 —— 本项目已经因为「同一个口径抄几份」
// 栽过好几次（见 `.workbuddy-ai/memory/LESSONS.md`）。所以只留这一份。
//
// 只支持 CDP 截图与 Windows 剪贴板那两种常见形态：
// 8bit、非隔行、colorType 0（灰度）/ 2（RGB）/ 6（RGBA）。遇到别的直接抛，
// **不静默降级** —— 解不出来却返回一张空图，会让上层量出「间距 0px」这种假结论。
"use strict";
const zlib = require("zlib");

/** @returns {{width:number,height:number,ch:number,data:Buffer}} ch = 每像素通道数 */
function decodePNG(buf) {
  if (buf.readUInt32BE(0) !== 0x89504e47) throw new Error("不是 PNG");
  let pos = 8, width = 0, height = 0, bitDepth = 0, colorType = 0;
  const idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos);
    const type = buf.toString("ascii", pos + 4, pos + 8);
    if (type === "IHDR") {
      width = buf.readUInt32BE(pos + 8);
      height = buf.readUInt32BE(pos + 12);
      bitDepth = buf[pos + 16];
      colorType = buf[pos + 17];
    } else if (type === "IDAT") {
      idat.push(buf.slice(pos + 8, pos + 8 + len));
    } else if (type === "IEND") break;
    pos += 12 + len;
  }
  if (bitDepth !== 8) throw new Error("只支持 8bit，实际 " + bitDepth);
  const ch = colorType === 6 ? 4 : colorType === 2 ? 3 : colorType === 0 ? 1 : 0;
  if (!ch) throw new Error("不支持的 colorType " + colorType);
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * ch;
  const out = Buffer.alloc(height * stride);
  let rp = 0;
  for (let y = 0; y < height; y++) {
    const f = raw[rp++];
    const line = raw.subarray(rp, rp + stride); rp += stride;
    // 必须用 subarray（视图），slice 是拷贝、写不回去
    const cur = out.subarray(y * stride, (y + 1) * stride);
    const prev = y > 0 ? out.subarray((y - 1) * stride, y * stride) : null;
    for (let x = 0; x < stride; x++) {
      const a = x >= ch ? cur[x - ch] : 0;
      const b = prev ? prev[x] : 0;
      const c = (prev && x >= ch) ? prev[x - ch] : 0;
      let v = line[x];
      if (f === 1) v += a;
      else if (f === 2) v += b;
      else if (f === 3) v += (a + b) >> 1;
      else if (f === 4) {
        const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
        v += (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c);
      }
      cur[x] = v & 255;
    }
  }
  return { width, height, ch, data: out };
}

/** 亮度（0~255）。三通道等权即可 —— 本项目只在「背景 vs 墨迹」这种粗判上用。 */
function lumAt(img, x, y) {
  const i = (y * img.width + x) * img.ch;
  if (img.ch === 1) return img.data[i];
  return (img.data[i] + img.data[i + 1] + img.data[i + 2]) / 3;
}

/** 写一张 RGB PNG（只用于把量出来的局部图存下来给人看） */
function encodePNG(w, h, rgb) {
  const crcTable = (() => {
    const t = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c;
    }
    return t;
  })();
  const crc = b => {
    let c = -1;
    for (const x of b) c = crcTable[(c ^ x) & 255] ^ (c >>> 8);
    return (c ^ -1) >>> 0;
  };
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const t = Buffer.from(type, "ascii");
    const cr = Buffer.alloc(4); cr.writeUInt32BE(crc(Buffer.concat([t, data])));
    return Buffer.concat([len, t, data, cr]);
  };
  const stride = w * 3;
  const raw = Buffer.alloc(h * (stride + 1));
  for (let y = 0; y < h; y++) {
    raw[y * (stride + 1)] = 0;                    // filter: none
    rgb.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 2;                       // 8bit RGB
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}

module.exports = { decodePNG, encodePNG, lumAt };
