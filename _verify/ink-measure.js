// 量「肉眼看到的间距」—— 直接扫截图像素，不依赖任何 DOM 换算。
//
// 为什么需要它：DOM 侧的字墨顶是「range rect + canvas TextMetrics」换算出来的，
// 而用户肉眼看到的是**渲染后的像素**。两者在亚像素吸附下会差 0.3~0.5px；
// 更糟的是换算公式本身写错时（比如漏算行盒偏移、把 em 盒当墨迹）会差好几个 px，
// 那时断言照样全绿 —— 本项目「搜索框 → 本周」这一处就因此被用户连纠三轮。
// 这个工具给出「目标文字的墨迹顶边距参照元素底边」的**真实像素距离**，用来兜底。
//
// 跑法：
//   node _verify/ink-measure.js [目标选择器] [参照选择器] [亮度阈值]
//   node _verify/ink-measure.js                        # 默认 .group-lbl / .sess-search
//   node _verify/ink-measure.js ".group-lbl" ".sess-search" 235
//
// 阈值要取在「背景亮度」与「墨迹亮度」之间。本项目的实测值：
//   侧栏背景 249 · 搜索框内部(--fill) 240 · 搜索框边框 223 · 文字墨迹 163
// 所以 235 合适。脚本会先打印亮度剖面，阈值不对时看剖面就能调。
// （踩过：阈值 250 会把整片 249 的侧栏背景当成墨迹，读出「间距 0px」。）
//
// 前置：桌面应用带调试端口启动
//   cd desktop && ./node_modules/.bin/electron . --no-sandbox --remote-debugging-port=9333
//
// 产物 _verify/ink-zoom.png 是放大 8 倍的局部图，可肉眼复核。
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const zlib = require("zlib");
const { freePort, killTree, launchChrome, waitTarget, connect } = require("./lib/cdp");

const APP_CDP = 9333;
const TARGET = process.argv[2] || ".group-lbl";
const REF = process.argv[3] || ".sess-search";
const THRESHOLD = Number(process.argv[4] || 235);
const SCALE = 8;
const DSF = 2;                    // Emulation 的 deviceScaleFactor 也会乘进截图尺寸
const PX_PER_CSS = SCALE * DSF;   // 图 px / CSS px
const sleep = ms => new Promise(r => setTimeout(r, ms));

/** 最小 PNG 解码：只支持 8bit / 非隔行 / colorType 0|2|6（CDP 截图就是这个） */
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

const lum = (img, x, y) => {
  const i = (y * img.width + x) * img.ch;
  return (img.data[i] + img.data[i + 1] + img.data[i + 2]) / 3;
};

(async function main() {
  const targets = await (await fetch(`http://127.0.0.1:${APP_CDP}/json/list`)).json();
  const app = targets.find(t => t.type === "page" && t.webSocketDebuggerUrl);
  if (!app) throw new Error("应用没有可用的 page target（带 --remote-debugging-port 启动了吗？）");
  const appCdp = await connect(app.webSocketDebuggerUrl);
  await appCdp.send("Runtime.enable");
  const url = (await appCdp.send("Runtime.evaluate", {
    expression: "location.href", returnByValue: true,
  })).result.value;
  appCdp.close();

  const cdpPort = await freePort(9466);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ts-ink-"));
  const chrome = launchChrome(cdpPort, path.join(tmp, "cprof"));
  const cdp = await connect(await waitTarget(cdpPort));
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 1320, height: 861, deviceScaleFactor: DSF, mobile: false,
  });
  await cdp.send("Page.navigate", { url });
  await sleep(4000);

  const evalIn = async (expr) => {
    const r = await cdp.send("Runtime.evaluate", {
      expression: "(async () => { " + expr + " })()", awaitPromise: true, returnByValue: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "eval 失败");
    return r.result.value;
  };
  // 刚导航完 loadSessions() 还没回来，量到的是空列表 —— 必须等
  for (let i = 0; i < 30; i++) {
    if (await evalIn("return document.querySelectorAll('#session-list .sess-item').length;") > 0) break;
    await sleep(500);
  }

  const geo = await evalIn(`const t = document.querySelector(${JSON.stringify(TARGET)});
    const r = document.querySelector(${JSON.stringify(REF)});
    if (!t) return { err: '找不到目标 ' + ${JSON.stringify(TARGET)} };
    if (!r) return { err: '找不到参照 ' + ${JSON.stringify(REF)} };
    const tn = [...t.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    if (!tn) return { err: '目标里没有文本节点（试试更内层的选择器）' };
    const rg = document.createRange(); rg.selectNodeContents(tn);
    const c = document.createElement('canvas').getContext('2d');
    const cs = getComputedStyle(t);
    c.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
    const m = c.measureText(tn.textContent.trim());
    const rr = rg.getBoundingClientRect();
    const rb = r.getBoundingClientRect();
    return { 文字: tn.textContent.trim(), 行高: cs.lineHeight, 字号: cs.fontSize,
             refBottom: rb.bottom, 文字左: rr.left, 文字右: rr.right,
             DOM字墨顶距参照底: +(rr.top + (m.fontBoundingBoxAscent - m.actualBoundingBoxAscent)
                                - rb.bottom).toFixed(2),
             DOM目标盒顶距参照底: +(t.getBoundingClientRect().top - rb.bottom).toFixed(2) };`);
  if (geo.err) throw new Error(geo.err);
  console.log("DOM 侧:", JSON.stringify(geo));

  const CLIP = { x: 0, y: Math.floor(geo.refBottom) - 8, width: 140, height: 36, scale: SCALE };
  const shot = await cdp.send("Page.captureScreenshot", { format: "png", clip: CLIP });
  fs.writeFileSync(path.join(__dirname, "ink-zoom.png"), Buffer.from(shot.data, "base64"));

  const img = decodePNG(Buffer.from(shot.data, "base64"));
  const x0 = Math.max(0, Math.round(geo.文字左 * PX_PER_CSS));
  const x1 = Math.min(img.width - 1, Math.round(geo.文字右 * PX_PER_CSS));
  console.log(`图 ${img.width}x${img.height} · 图y=0 对应 CSS y=${CLIP.y} · 1 CSS px = ${PX_PER_CSS} 图 px`);

  // 亮度剖面（每 1 CSS px 一行）：阈值不合适时看这里调
  const profile = [];
  for (let off = 0; off < 36; off++) {
    const y = Math.round(off * PX_PER_CSS);
    if (y >= img.height) break;
    let min = 255;
    for (let x = x0; x <= x1; x++) { const l = lum(img, x, y); if (l < min) min = l; }
    profile.push(`${(CLIP.y + off - geo.refBottom).toFixed(0)}:${Math.round(min)}`);
  }
  console.log("亮度剖面（距参照底 CSS px : 该行最暗亮度）:");
  console.log("  " + profile.join("  "));

  // 从参照元素底边 +1px 之后开始找墨迹（避开边框抗锯齿尾巴：1 CSS px = 16 图 px）
  const yInk = Math.max(0, Math.ceil((geo.refBottom + 1 - CLIP.y) * PX_PER_CSS));
  const inkRows = [];
  for (let y = yInk; y < img.height; y++) {
    let min = 255;
    for (let x = x0; x <= x1; x++) { const l = lum(img, x, y); if (l < min) min = l; }
    if (min < THRESHOLD) inkRows.push([y, min]);
  }
  if (!inkRows.length) {
    console.log(`\n★ 没扫到墨迹（阈值 ${THRESHOLD}）。看上面的剖面，把阈值调到「背景亮度」与「墨迹亮度」之间。`);
  } else {
    const first = inkRows[0][0], last = inkRows[inkRows.length - 1][0];
    const pxTop = CLIP.y + first / PX_PER_CSS - geo.refBottom;
    console.log(`\n★ 像素实测：「${geo.文字}」墨迹顶距参照底 ${pxTop.toFixed(3)} px` +
      `（起始行 imgY=${first}，亮度 ${inkRows[0][1].toFixed(0)}）`);
    console.log(`  墨迹底距参照底 ${(CLIP.y + (last + 1) / PX_PER_CSS - geo.refBottom).toFixed(3)} px` +
      ` · 墨迹高 ${((last - first + 1) / PX_PER_CSS).toFixed(2)} CSS px`);
    console.log(`  DOM 换算 ${geo.DOM字墨顶距参照底} vs 像素 ${pxTop.toFixed(2)}` +
      ` → 差 ${(pxTop - geo.DOM字墨顶距参照底).toFixed(2)}（亚像素吸附通常在 0.5 内）`);
    console.log(`  局部放大图: _verify/ink-zoom.png`);
  }

  cdp.close();
  killTree(chrome.pid);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) { }
  process.exit(0);
})().catch(e => { console.error("FAIL:", e.message); process.exit(1); });
