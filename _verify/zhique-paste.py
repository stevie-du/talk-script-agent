# -*- coding: utf-8 -*-
"""A-7 朱雀对照的送检页生成器：把人工流程的成本降到一次点击。

背景（需求方案 §2.10 A-7）：朱雀对照**必须人工**（红线：不逆向、不做自动化），
但"复制 10 篇文本 → 贴朱雀 → 手写 audit JSON"这套手工流程本身就是流失源：
复制漏段、JSON 写错格式、判定与样本对不上号，任何一环出错都得重来。

本工具把样本渲染成一个本地 HTML 页面，人工流程收敛为：
  1. 每篇卡片上点「复制文本」→ 贴进 matrix.tencent.com/ai-detect
  2. 在卡片上点「人类 / AI / 拿不准」——判定存在浏览器 localStorage，
     关掉页面再打开不丢
  3. 全部点完，底部「导出判定 JSON」→ 存成 data/zhique-audit.json
  4. 跑 `python _verify/zhique-sample.py --score` 出相关性报告

页面是纯本地文件（无网络请求、无外部依赖），判定先落 localStorage，
导出时才落盘 —— 中点错、中途关页面都不丢已做的部分。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "_verify" / "zhique-sample.json"
OUT = ROOT / "_verify" / "zhique-paste.html"


def main() -> None:
    if not SAMPLE.exists():
        print(f"还没看到 {SAMPLE.name} —— 先跑 python _verify/zhique-sample.py 生成样本")
        return
    samples = json.loads(SAMPLE.read_text(encoding="utf-8"))
    payload = json.dumps(
        [{"path": s["path"].replace("\\", "/"), "layer": s["layer"],
          "score": s["score"], "chars": s["chars"], "text": s["text"]}
         for s in samples],
        ensure_ascii=False)

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>朱雀对照送检页（A-7）</title>
<style>
  body { font-family: system-ui, "Microsoft YaHei", sans-serif; margin: 24px;
         background: #f6f7f9; color: #1f2329; }
  h1 { font-size: 20px; } .tip { color: #646a73; font-size: 13px; line-height: 1.7; }
  .card { background: #fff; border: 1px solid #dee0e3; border-radius: 8px;
          padding: 14px 16px; margin: 12px 0; }
  .meta { font-size: 12px; color: #646a73; margin-bottom: 8px; }
  .meta b { color: #1f2329; }
  .txt { white-space: pre-wrap; font-size: 14px; line-height: 1.8;
         background: #f2f3f5; border-radius: 6px; padding: 10px 12px;
         max-height: 180px; overflow: auto; }
  .row { margin-top: 10px; display: flex; gap: 8px; align-items: center; }
  button { border: 1px solid #dee0e3; background: #fff; border-radius: 6px;
           padding: 6px 14px; font-size: 13px; cursor: pointer; }
  button:hover { background: #f2f3f5; }
  .v { font-weight: 600; }
  .v.human { color: #1b7030; } .v.ai { color: #c00117; } .v.unsure { color: #995100; }
  .on { outline: 2px solid #3370ff; outline-offset: 1px; }
  #bar { position: sticky; top: 0; background: #f6f7f9ee; padding: 10px 0;
         border-bottom: 1px solid #dee0e3; z-index: 9; display: flex;
         gap: 12px; align-items: center; }
  #export { background: #3370ff; color: #fff; border-color: #3370ff;
            font-weight: 600; padding: 8px 18px; }
  #export:disabled { background: #bbb; border-color: #bbb; cursor: not-allowed; }
  pre#out { display: none; background: #1f2329; color: #d0d3d6; padding: 12px;
            border-radius: 6px; font-size: 12px; max-height: 260px; overflow: auto; }
</style>
</head>
<body>
<h1>朱雀对照送检页（A-7）</h1>
<p class="tip">
每篇：点「复制文本」→ 贴进 <b>matrix.tencent.com/ai-detect</b> → 按朱雀判定点
「人类 / AI / 拿不准」。判定存在本页面（localStorage），关掉重开不丢。<br>
全部点完后点「导出判定 JSON」，把内容存成项目里的
<b>data/zhique-audit.json</b>，再跑
<code>python _verify/zhique-sample.py --score</code> 出相关性报告。<br>
红线：判定必须人工给出 —— 本页不调用任何检测服务。
</p>
<div id="bar">
  <span id="prog">进度 0/0</span>
  <button id="export" disabled>导出判定 JSON</button>
  <button id="clear">清空判定</button>
</div>
<div id="list"></div>
<pre id="out"></pre>
<script>
const SAMPLES = __PAYLOAD__;
const KEY = "zhique-audit-v1";
const verdicts = JSON.parse(localStorage.getItem(KEY) || "{}");
const LABEL = {human: "人类", ai: "AI", unsure: "拿不准"};

function save() { localStorage.setItem(KEY, JSON.stringify(verdicts)); render(); }

function render() {
  const list = document.getElementById("list");
  list.innerHTML = "";
  let done = 0;
  SAMPLES.forEach((s, i) => {
    const v = verdicts[s.path] || "";
    if (v) done++;
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      '<div class="meta">#' + (i + 1) + ' · <b>L1=' + s.score + '</b> · ' +
      s.layer + ' · ' + s.chars + ' 字<br>' + s.path + '</div>' +
      '<div class="txt"></div>' +
      '<div class="row">' +
      '<button data-act="copy">复制文本</button>' +
      '<button data-v="human" class="v ' + (v === "human" ? "on" : "") + '">人类</button>' +
      '<button data-v="ai" class="v ' + (v === "ai" ? "on" : "") + '">AI</button>' +
      '<button data-v="unsure" class="v ' + (v === "unsure" ? "on" : "") + '">拿不准</button>' +
      '<span class="v ' + v + '">' + (LABEL[v] || "未判定") + '</span>' +
      '</div>';
    card.querySelector(".txt").textContent = s.text;
    card.querySelector('[data-act="copy"]').onclick = (e) => {
      navigator.clipboard.writeText(s.text).then(() => {
        e.target.textContent = "已复制 ✓";
        setTimeout(() => (e.target.textContent = "复制文本"), 1200);
      });
    };
    card.querySelectorAll("[data-v]").forEach((b) => {
      b.onclick = () => {
        if (verdicts[s.path] === b.dataset.v) delete verdicts[s.path];
        else verdicts[s.path] = b.dataset.v;
        save();
      };
    });
    list.appendChild(card);
  });
  document.getElementById("prog").textContent =
    "进度 " + done + "/" + SAMPLES.length +
    (done === SAMPLES.length ? " · 全部判完，可以导出了" : "");
  document.getElementById("export").disabled = done === 0;
}

document.getElementById("export").onclick = () => {
  const out = SAMPLES.filter((s) => verdicts[s.path])
    .map((s) => ({path: s.path, zhique: verdicts[s.path], note: ""}));
  const text = JSON.stringify(out, null, 2);
  document.getElementById("out").style.display = "block";
  document.getElementById("out").textContent = text;
  navigator.clipboard.writeText(text).then(() => {
    document.getElementById("export").textContent = "已复制到剪贴板 ✓";
    setTimeout(() => (document.getElementById("export").textContent = "导出判定 JSON"), 2000);
  });
};
document.getElementById("clear").onclick = () => {
  if (confirm("清空所有判定？")) { localStorage.removeItem(KEY); render(); }
};
render();
</script>
</body>
</html>
"""
    html = html.replace("__PAYLOAD__", payload)
    OUT.write_text(html, encoding="utf-8")
    print(f"送检页已生成 → {OUT.relative_to(ROOT)}（{len(samples)} 篇）")
    print("双击打开它，按页内提示操作即可。")


if __name__ == "__main__":
    main()
