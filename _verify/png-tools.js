// 扫截图 / 裁截图 —— 用来「量」用户贴来的那张图，而不是「看」它。
//
// 为什么需要它
// ------------
// 用户报界面问题时会贴一张截图（Windows 剪贴板 PNG，落在
// `%USERPROFILE%\.workbuddy-ai\clipboard-images\`）。截图里含着两个关键事实：
//   ① 他的窗口有多宽、缩放倍率是多少（**决定我该在什么尺寸下复现**）；
//   ② 元素的实际像素尺寸（边框在哪一行、节距多少）。
// 这两件事靠肉眼看图都得不到 —— 本项目为此栽过（「参考图不能看，要量」）。
//
// 用法
// ----
//   node _verify/png-tools.js scan <in.png> <x> [y0] [y1] [阈值]
//       沿第 x 列扫，打印「暗于阈值的行」与「暗带区间」。用于找边框 / 灰底 / 圆角缺口。
//       阈值取在「背景亮度」与「墨迹亮度」之间：本项目白页 249、发丝线 237、墨迹 163，
//       所以 250 能同时抓到灰底，252 只抓边框。
//   node _verify/png-tools.js crop <in.png> <out.png> <x> <y> <w> <h> [放大倍数]
//       裁一块并放大存成 PNG，用来放大看清细节（默认 1 倍）。
//
// 例：从用户截图反推缩放倍率
//   node _verify/png-tools.js scan shot.png 1200 260 700 253
//   → 卡片节距 42.6 源 px；已知 CSS 里是 34px（30 卡 + 4 间距）
//   → 倍率 = 42.6 / 34 ≈ 1.25（即 Windows 显示缩放 125%）
//   → 窗口逻辑宽度 = 图宽 / 1.25
"use strict";
const fs = require("fs");
const { decodePNG, encodePNG, lumAt } = require("./lib/png");

const [mode, ...rest] = process.argv.slice(2);

function scan([inp, X, Y0, Y1, TH]) {
  const img = decodePNG(fs.readFileSync(inp));
  const x = Number(X), y0 = Number(Y0 || 0), y1 = Number(Y1 || img.height);
  const th = Number(TH || 250);
  if (!Number.isFinite(x) || x < 0 || x >= img.width) {
    throw new Error(`列 x=${X} 超出图片宽度 ${img.width}`);
  }
  console.log(`图 ${img.width}x${img.height} ch=${img.ch} · 扫描列 x=${x} · 阈值 ${th}`);
  const marks = [];
  for (let y = y0; y < y1; y++) { const L = lumAt(img, x, y); if (L < th) marks.push(`${y}:${Math.round(L)}`); }
  console.log("暗于阈值的行:", marks.length ? marks.join(" ") : "（无）");
  let start = -1; const runs = [];
  for (let y = y0; y <= y1; y++) {
    const dark = y < y1 && lumAt(img, x, y) < th;
    if (dark && start < 0) start = y;
    if (!dark && start >= 0) { runs.push([start, y - 1]); start = -1; }
  }
  console.log("暗带区间:", runs.length
    ? runs.map(([a, b]) => `${a}~${b}(${b - a + 1}px,L${Math.round(lumAt(img, x, a))})`).join("  ")
    : "（无）");
  // 顺手给出「亮带」节距：连续亮带的中点两两相减，等于行/卡的节距
  const mids = runs.map(([a, b]) => (a + b) / 2);
  if (mids.length > 1) {
    const gaps = mids.slice(1).map((m, i) => Math.round((m - mids[i]) * 10) / 10);
    console.log("暗带中点间距（节距）:", gaps.join(" "));
  }
}

function crop([inp, outp, X, Y, W, H, S]) {
  const scale = Number(S || 1);
  const img = decodePNG(fs.readFileSync(inp));
  const x0 = Math.max(0, Number(X)), y0 = Math.max(0, Number(Y));
  const w = Math.min(Number(W), img.width - x0), h = Math.min(Number(H), img.height - y0);
  if (w <= 0 || h <= 0) throw new Error("裁剪区域为空，检查 x/y/w/h 是否落在图内");
  const ow = Math.round(w * scale), oh = Math.round(h * scale);
  const out = Buffer.alloc(ow * oh * 3);
  for (let y = 0; y < oh; y++) {
    for (let x = 0; x < ow; x++) {
      const si = ((y0 + Math.floor(y / scale)) * img.width + x0 + Math.floor(x / scale)) * img.ch;
      const di = (y * ow + x) * 3;
      out[di] = img.data[si];
      out[di + 1] = img.ch === 1 ? img.data[si] : img.data[si + 1];
      out[di + 2] = img.ch === 1 ? img.data[si] : img.data[si + 2];
    }
  }
  fs.writeFileSync(outp, encodePNG(ow, oh, out));
  console.log(`${img.width}x${img.height} ch=${img.ch} → ${outp} ${ow}x${oh}（放大 ${scale} 倍）`);
}

try {
  if (mode === "scan") scan(rest);
  else if (mode === "crop") crop(rest);
  else { console.error("用法：\n  node _verify/png-tools.js scan <in.png> <x> [y0] [y1] [阈值]\n"
    + "  node _verify/png-tools.js crop <in.png> <out.png> <x> <y> <w> <h> [放大倍数]"); process.exit(2); }
} catch (e) { console.error("失败：" + e.message); process.exit(1); }
