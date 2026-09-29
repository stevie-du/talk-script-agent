// 复审轮（2026-09-27）新增的那批守卫 —— s34 组的宿主。
//
// 为什么要搬出来：变异检验的 `verify:<分组>` 入口**只认分组名**
// （topics / contrast / packimport / xss），拿子串去筛会走成分组查找失败
// （退出码 3，"没有叫「xxx」的分组"）。s34 十条变异要各自证明
// 「改回旧行为 → 对应那条守卫变红」，全网跑一次要十分钟以上，
// 而这批断言全是静态扫 + 少量现场量，几秒就跑完。
//
// ⚠ 断言逻辑**只有这一份**：verify.js 整网跑的是同一个函数
//   （与 topics / contrast / packimport / xss 同一规矩）。
//   写两份的话，总有一份会先烂掉，而先烂的那份还在给人"守着"的感觉。
//
// ⚠ 这一组只需要页面已加载、不需要整网的前置流程 —— 除了标注了
//   「需切面板」的那几条，其余都在 boot 后直接可判。
module.exports = async function review({ evalIn, sleep, check, ctx }) {
  const { RENDERER, fs, path, stripJsComments } = ctx;
  const read = (f) => fs.readFileSync(path.join(RENDERER, "js", f), "utf8");
  const uiJsSrc = stripJsComments(read("ui.js"));
  const jobsJsSrc = stripJsComments(read("jobs.js"));
  const sesJsSrc = stripJsComments(read("sessions.js"));
  const setJsSrc = stripJsComments(read("settings.js"));
  // ⚠ words.js 必须读**原文**：stripJsComments 的 ` // ` 规则是从行内
  //   第一个「 // 」删到行尾、**连收尾引号一起删**，行尾引号一没，
  //   后面所有取值的引号配对全部错位（实测一条都取不到）。
  const wordsRaw = read("words.js");
  const wordsJsSrc = stripJsComments(wordsRaw);

  /** 从 words.js 的表里解析出真值取值（豁免用）。
   *  按**取值**豁免、不按属性名正则：第一版写 `!/(box|send)\s*:/` 想放过
   *  真值那两行，可字面量提取器只抠**值**，属性名压根不在池子里，
   *  于是真值自己被判成"手写的第二份"，一开局报 5 条假的。
   *
   *  ⚠ 收尾的 `\n};` 改成 `\n[ \t]*\};?`：原来要求 `};` 顶格，
   *   任何一次「统一缩进层级」式的格式化就会让解析失败 → 返回空集 →
   *   ⑧⑨ 把真值自己当成"手写的第二份"，**报出 2+5 条假红**。
   *   误红比漏红坏：它会让人去改本来正确的代码。
   *   （嵌套对象是安全的：`[\s\S]*?` 非贪婪，仍在表自己的收尾处停。） */
  const tableVals = (name) => {
    const m = new RegExp("export const " + name + "\\s*=\\s*\\{([\\s\\S]*?)\\n[ \\t]*\\};?")
      .exec(wordsRaw);
    if (!m) return new Set();
    return new Set([...m[1].matchAll(/"((?:[^"\\\n]|\\.)*)"/g)].map(x => x[1]));
  };

  // ── 字面量池：**逐文件**抠、剔插值、读原文 ──────────────────────
  // 踩过的坑，顺序不能换：
  //  ① 只认 class="a b" / classList → 报出 106 个假死类名（本项目产出
  //     类名的主力是 el(tag, "a b")）。② 整份源码裸词扫 → .page 永远
  //     判不死（topics.js 有 let page = 0）。③ 一个文件错位会把后面
  //     所有文件的配对一起带偏 —— 所以必须逐文件。
  const dropInterp = (s) => {
    let out = "", depth = 0;
    for (let i = 0; i < s.length; i++) {
      if (s[i] === "$" && s[i + 1] === "{") { depth++; i++; continue; }
      if (depth > 0) { if (s[i] === "{") depth++; else if (s[i] === "}") depth--; continue; }
      out += s[i];
    }
    return out;
  };
  const litRe = /"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`/g;
  const jsLit = [], htmlLit = [];
  for (const f of ["index.html", ...fs.readdirSync(path.join(RENDERER, "js"))
      .filter(n => n.endsWith(".js"))]) {
    const raw = fs.readFileSync(path.join(RENDERER, f === "index.html" ? "" : "js", f), "utf8");
    const re = new RegExp(litRe.source, "g");
    const sink = f === "index.html" ? htmlLit : jsLit;
    let m;
    while ((m = re.exec(raw)) !== null) {
      sink.push(dropInterp(m[1] !== undefined ? m[1]
        : m[2] !== undefined ? m[2] : m[3] || ""));
    }
  }

  // ① Ctrl+Enter 一次按键只起一条作业。修前 #topic 的 keydown 只
  //    preventDefault 没有 stopPropagation，事件继续冒到 document
  //    上那条全局监听 —— 它又点一次发送键，于是**一次按键两条作业、
  //    真金白银翻倍**。判据钉那句闸门在（不钉写法：换个写法但闸门没了，
  //    同样是双触发）。
  check("★ 快捷键全局监听有「已处理过就不再处理」的闸门（Ctrl+Enter 曾双触发）",
    /defaultPrevented/.test(uiJsSrc),
    /defaultPrevented/.test(uiJsSrc) ? "闸门在" : "闸门没了，Ctrl+Enter 仍会双触发");

  // ② 换行业包不许静默清空参数。清空本身是对的（新包里不存在旧取值），
  //    但一个字不说地清掉，用户只会理解成"这软件自己乱改了"。
  check("★ 换行业包清掉参数时要说出来（不许静默重置）",
    /换包后重置了/.test(uiJsSrc),
    /换包后重置了/.test(uiJsSrc) ? "有提示" : "换包那处一声不吭");

  // ③ 不可逆动作，确认框必须说「撤销不了」。
  //    ⚠ 判据**必须绑到各自的函数体上**：第一版是「在整份拼接源码里
  //    找那句话」—— 把「标记为已校对」那处删掉、同时在「卸载」那处补上
  //    同一句，那条判据仍为真，而前者已经退回旧文案了（复审 P1-5 实测）。
  //    动作名只用来拼消息、正则与动作没有任何绑定的那种写法等于没写。
  //
  // ⚠ fnBody 取的是**真函数体**，不是定长窗口。第二版是 `indexOf` + 1800 字符
  //   窗口，复审实测抓到它**静默空转**：deleteGroup 整段落在 deleteSession 的
  //   窗口里（两窗重叠 14277-16077 / 15326-17126，窗口内 clearThread() 命中
  //   2 次），于是只把 deleteSession 里那句删掉——正是这条判据要抓的回归——
  //   窗口里还剩一次命中，「单条删除」那一半照样绿。
  //   换句话说：那个版本把同一个漏洞换了个形状留着，而注释还写着
  //   "已分别钉在两个函数体上"。**一条自带 bug 的守卫比没有守卫更坏** ——
  //   它会让人以为那条路已经守住了。
  //   改成行级切片：从函数声明所在行取到**下一个顶层声明**为止
  //   （`^}` 收尾，或下一个 `^async function|^function|^export ... function`）。
  //   顶层函数体里没有嵌套的顶层声明，所以行级切片对这两类函数是够的。
  const fnBody = (src, name) => {
    const i = src.indexOf(name);
    if (i < 0) return "";
    const lines = src.slice(i).split("\n");
    const out = [lines[0]];
    for (let k = 1; k < lines.length; k++) {
      // 下一个顶层声明 = 这个函数已经结束（两个函数挨着写的情形）
      if (/^(export\s+)?(async\s+)?function\s/.test(lines[k])) break;
      out.push(lines[k]);
    }
    return out.join("\n");
  };
  // ⚠ 反向自查：切片**不许越过下一个函数**。上一版（定长窗口）没这条，
  //   于是两个窗口重叠整整一段而没人发现 —— 判据自己得先证明自己
  //   量的是那个函数、不是它和它后面那一个的合集。
  const FN_CASES = [
    ["单条删除", sesJsSrc, "async function deleteSession", "deleteGroup"],
    ["整组删除", sesJsSrc, "async function deleteGroup", null],
    ["标记校对", setJsSrc, "async function undraftPack", "removeImportedPack"],
    ["卸载行业包", setJsSrc, "async function removeImportedPack", null],
  ];
  const overlap = FN_CASES.filter(([, src, self, nextName]) => {
    if (!nextName) return false;
    return fnBody(src, self).includes("function " + nextName);
  }).map(([k]) => k);
  check("（前置）取函数体的切片没有越过下一个函数（否则会替别人交差）",
    overlap.length === 0, overlap.join("、") || `${FN_CASES.length} 段都只含自己`);
  const undoMissing = [
    ["标记为已校对", /没法撤销/, fnBody(setJsSrc, "async function undraftPack")],
    ["卸载行业包", /找不回来/, fnBody(setJsSrc, "async function removeImportedPack")],
  ].filter(([, re, body]) => !body || !re.test(body)).map(([k]) => k);
  check("★ 不可逆动作的确认框写明「撤销不了」（标记校对 / 卸载行业包）",
    undoMissing.length === 0, undoMissing.join("、") || "两处都写了");

  // ④ 失败卡上的模型必须是**那次作业**用的，不是此刻的全局值。
  //    ⚠ 判据钉的是**取值来源**，不是"某个词在不在"。第一版钉的是
  //    `/model_at_submit/` —— 而那个字段根本走不通后端（作业快照的 params
  //    来自 `req.model_dump()`，GenerateRequest 里没有它），于是失败时
  //    照样落回全局值，本条却一直绿着。这类"钉一个名字"的判据最容易空转。
  const modelGuard = [
    [/const modelByJob = new Map\(\)/, "缺少按 jobId 记模型的那张表"],
    [/modelByJob\.set\(job_id, modelAtSubmit\)/, "提交那一刻没有把模型记进表里"],
    [/modelByJob\.get\(jobId\)/, "失败卡不是从那张表取模型"],
  ].filter(([re]) => !re.test(jobsJsSrc)).map(([, why]) => why);
  if (/模型：\$\{modelByJob\.get/.test(jobsJsSrc) === false) {
    modelGuard.push("失败卡的模型没有走 modelByJob");
  }
  check("★ 失败卡取「提交那一刻」的模型（不是全局当前值）",
    modelGuard.length === 0, modelGuard.join("；") || "跟着作业走了");

  // ⑤ 删掉正在看的那条，右屏不许留着一整篇稿子。修前只 setResult(null)
  //    （只改 state），屏幕上那份磁盘上已经不在的稿子还在。
  //    ⚠ 判据**分别钉在两个函数体上**，不是数整份文件里有几处：第一版数
  //    「出现次数 ≥ 2」，把两处都挪进 deleteSession、或在别处多加一处，
  //    条数照样够 —— 而"整组删除不清屏"已经复发却仍然全绿（复审 P1-4）。
  const delMissing = [
    ["单条删除", fnBody(sesJsSrc, "async function deleteSession")],
    ["整组删除", fnBody(sesJsSrc, "async function deleteGroup")],
  ].filter(([, body]) => !/clearThread\(\)/.test(body)).map(([k]) => k);
  check("★ 删掉正在看的记录会连右屏一起收（不许留一份磁盘上没有的稿子）",
    delMissing.length === 0,
    delMissing.length ? `${delMissing.join("、")}那两处没有清屏` : "两条路径都清屏");

  // ⑥ 「校验」一族不许混进工程术语。进度那格以前写「代码校验中」——
  //    走查的是这篇稿子，用户从头到尾没碰过代码，而同一块屏幕上
  //    结论三态说的是「已通过校验」，多一个「代码」像是串了屏。
  check("★ 作业状态词表里没有工程术语（校验那格不叫「代码校验中」）",
    !/checking:\s*"[^"]*代码/.test(wordsJsSrc),
    /checking:\s*"([^"]*)"/.test(wordsJsSrc) ? RegExp.$1 : "词表里没有 checking");

  // ⑦ 停止键要有「正在停止…」这段空窗的反馈。cancel 是一次网络往返，
  //    那段时间里按钮还写着「停止生成」，用户以为没点上就再点一次。
  const stopGap = await evalIn(`var b = document.getElementById("btn-generate");
    var s = window.__state;
    var kick = function () { document.getElementById("topic")
      .dispatchEvent(new Event("input", { bubbles: true })); };
    var keep = { busy: s.busy, job: s.job, stopping: s.stopping };
    s.busy = true; s.job = { id: "gap-probe" }; s.stopping = true; kick();
    var during = { title: b.title, disabled: b.disabled,
      aria: b.getAttribute("aria-busy"), label: b.getAttribute("aria-label") };
    s.stopping = false; kick();
    var after = { title: b.title, disabled: b.disabled };
    s.busy = keep.busy; s.job = keep.job; s.stopping = keep.stopping; kick();
    return { during: during, after: after };`);
  check("★ 点了停止到后端回话之间，按键说「正在停止…」并置灰",
    /正在停止/.test(stopGap.during.title || "") && stopGap.during.disabled === true,
    JSON.stringify(stopGap));
  check("（对照）停止完成后按钮交还给用户（不许卡在「正在停止…」）",
    !/正在停止/.test(stopGap.after.title || "")
      && /停止生成/.test(stopGap.after.title || ""),
    JSON.stringify(stopGap.after));

  // ⑧ 快捷键说明只有一份真值（words.js 的 SHORTCUT）。修前有三份且互不一致：
  //    placeholder 少说 Ctrl+Enter、输入框 title 少说 Ctrl+Enter、
  //    发送键 title 只说 Ctrl+Enter。
  const scTruth = tableVals("SHORTCUT");
  const scHits = jsLit.filter(l =>
    /(?:Ctrl ?\+ ?Enter|Enter 发送)/.test(l) && !scTruth.has(l));
  check("★ 快捷键说明只有 words.js 一份（JS 里不许再手写一遍）",
    scHits.length === 0, scHits.join(" | ") || "只有一处真值");
  // ⚠ 前置是「**恰好**两格」，不是 `>= 2`：`>= 2` 在 SHORTCUT 被删到只剩一格
  //   时照样绿，而那时豁免集是残的、⑧ 的行为会变。职责是"解析器没坏"。
  check("（前置）SHORTCUT 的两格真值都解析得出来（否则上一条会把真值当漂移）",
    scTruth.size === 2, [...scTruth].join(" | ") || `只解析出 ${scTruth.size} 格`);
  // 正向对照：上一条在「一个字都没搜到」时也是绿的（那正是空转的样子）。
  check("（前置）脚本字面量里真的搜得到那句话（否则上一条是空转）",
    jsLit.some(l => /Ctrl ?\+ ?Enter/.test(l)),
    jsLit.filter(l => /Ctrl ?\+ ?Enter/.test(l)).length + " 处提到 Ctrl+Enter");
  // ⚠ index.html 那份兜底**不许完全不看**（复审）：⑧⑨ 特意把它整份豁免了
  //   （脚本没起来时全靠它），可豁免不等于不管 —— 那里实际躺着三份
  //   快捷键说明的副本。改回「少说 Ctrl+Enter」的老形态（正是 ⑧ 注释里
  //   描述的历史缺陷）时 ⑧ 照样全绿。豁免的形状必须是"内嵌真值"而不是
  //   "整份不看"。
  // ⚠ 判据只抓**整串就是那句话**的形态（Enter 发送 / 生成（…）），不放行
  //   「前缀 + 真值 + 后缀」那种 —— 后者在 index.html 里只有 placeholder
  //   一处（`输入主题…（…）`），实测它确实内嵌了真值，所以整串判据在
  //   index.html 上够用；而放宽到 includes 会把 `需人工校对` 这种
  //   **同义改写**也算进去（它是 `内容需人工校对` 的子串但不是同一句话），
  //   那就变成逼着人去改措辞 —— 误红比漏红坏。
  const scHtmlMiss = htmlLit.filter(l =>
    /(?:^|[（(\s])(?:Enter 发送|生成（)/.test(l)
    && ![...scTruth].some(t => l.includes(t)));
  check("★ index.html 的兜底里那几句快捷键说明必须内嵌 SHORTCUT 的真值"
    + "（脚本没起来时读屏读到的就是那几份）",
    scHtmlMiss.length === 0,
    scHtmlMiss.join(" | ") || `${htmlLit.filter(l => /Enter 发送/.test(l)).length} 份都内嵌了真值`);

  // ⑨ 「校对」一族（words.js 的 PROOF）也只有一份真值。键面那颗原先写在
  //    index.html 里，而表注释声称拥有「键面」—— 改表不改键，按钮与
  //    确认框就分家（复审 P1-2）。
  //
  // ⚠ 判据盯的是**动作说法**，不是"有没有出现「校对」两个字"——
  //   第一次写成 `/校对/`，结果把「草稿校对」「校对清单.md」「人工校对完成后」
  //   这些**文件名 / 区域名 / 说明性定语**全捞进来了，误红 3+2 条。
  //   误红比漏红坏：它会让人去改本来正确的代码。
  //   真的判据是「凡是**说这个动作叫什么**的整串」：要么整串等于 PROOF
  //   的某一格，要么内嵌其中一格。原先逐条枚举禁用词
  //   （`已校对|待校对|内容需人工校对`）**漏了** `settings.js` 确认框里那两句
  //   「标记为**已人工校对完毕**」与「不再提示“**需校对**」」——
  //   `已校对` 不是前者的子串（是"已-人工-校对"），`待校对` 也不是后者的。
  //   改用 `actsAs` 那个**动作骨架**的形状（见下）就能同时堵住这两种。
  const proofTruth = tableVals("PROOF");
  const proofAny = [...proofTruth];
  // 「校对」周围那几个字决定了这是在**给动作起名**，而不是在指一份文件。
  // 命中的是：标记/完成/完毕/提示/待/需 + 校对，或校对 + 清单/状态这类。
  // 不命中的是：校对清单.md、草稿校对、人工校对完成后（那是定语）。
  const NAMES_ACTION = /(?:标记|已|待|需|完成|完毕|过了?|没|未).{0,4}校对|校对.{0,4}(?:状态|标记|动作)/;
  const proofDrift = (l) => NAMES_ACTION.test(l) && !proofAny.some(t => l.includes(t));
  const proofHits = jsLit.filter(proofDrift);
  check("★ 「校对」一族只有 words.js 一份（JS 里不许再手写一遍）",
    proofHits.length === 0, proofHits.join(" | ") || "只有一处真值");
  check("（前置）PROOF 五格都在（上一条不是空转）",
    /export const PROOF/.test(wordsJsSrc) && /act:/.test(wordsJsSrc)
      && /cleared:/.test(wordsJsSrc) && /warn:/.test(wordsJsSrc),
    /export const PROOF/.test(wordsJsSrc) ? "在" : "PROOF 没了");
  check("（前置）PROOF 的五格真值都解析得出来（否则上一条会把真值当漂移）",
    proofTruth.size === 5, [...proofTruth].join(" | ") || `只解析出 ${proofTruth.size} 格`);
  // ⚠ index.html 同理不许整份不看：那里的 `#pi-undraft` 键面是**真值候选**。
  //   只抓"整串就是那句话"的形态（`标记为已校对`），别去抓
  //   `新建一个行业包（…需人工校对）` 那种**同义定语** ——
  //   逼人改措辞是误红，误红比漏红坏。键面本身由下面 ⑩ 单独对账。
  const proofHtmlMiss = htmlLit.filter(l =>
    /(?:^|[（(\s“"])(?:标记为|已标记为|待|需)/.test(l)
    && /校对/.test(l) && !proofAny.some(t => l.includes(t)));
  check("★ index.html 的兜底键面必须内嵌 PROOF 的真值（不抓同义定语）",
    proofHtmlMiss.length === 0,
    proofHtmlMiss.join(" | ") || "键面内嵌了真值");

  // ⑩ index.html 的**兜底**与表一致。
  //    上面 ⑨ 特意把 index.html 豁免在判据外（脚本没起来时全靠它），
  //    但豁免不等于不管：那份兜底是**第二份真值**的候选，改了表忘了改它，
  //    于是脚本没起来的那一小段时间里读屏读到的是旧词 ——
  //    而那正是"读屏用户"唯一能读到的时候。
  //    ⚠ 变异打在这里（改 index.html 那份），**不是**打 main.js：
  //    把 main.js 的回填换成一个手写字面量，index.html 的兜底与真值本来就
  //    一字不差，屏幕上什么都看不出来 —— 那是语义空转，不是"守卫没守住"。
  const actTruth = (function () {
    const m = /act:\s*"((?:[^"\\\n]|\\.)*)"/.exec(wordsRaw);
    return m ? m[1] : null;
  })();
  const htmlRaw = fs.readFileSync(path.join(RENDERER, "index.html"), "utf8");
  const htmlAct = (/<button[^>]*id="pi-undraft"[^>]*>([^<]*)</.exec(htmlRaw) || [])[1];
  check("★ index.html 的兜底键面与 words.js 的 PROOF.act 一致"
    + "（脚本没起来时读屏读到的就是这一份）",
    !!htmlAct && htmlAct.trim() === (actTruth || ""),
    JSON.stringify({ html: (htmlAct || "").trim(), actTruth }));
  // 键面上屏的必须也是真值。⚠ 复审指出这里原来是**纯空转**：
  //   `openPackInfo` 从不改这颗键的文字（只切 .hidden），而 `sel.value=…` +
  //   dispatchEvent 走的是 applyPackSwitch、**不会**重画它 —— 所以量到的
  //   永远是 main.js 起手写的那份，或是 index.html 的兜底，而两者一字不差。
  //   上一条保证了兜底与表一致，于是这一条在"回填被删掉"时照样绿。
  //   真正要钉的是**取来源**：先把键面改成哨兵值，再走一条会重画它的路径，
  //   看它有没有回到真值。取不到就显式判红，不许静默跳过。
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('packinfo'); return true;`);
  await sleep(500);
  await evalIn(`const sel = document.getElementById('pack');
    sel.value = 'fitment'; sel.dispatchEvent(new Event('change', {bubbles:true}));
    return true;`);
  await sleep(600);
  const label = await evalIn(`var u = document.getElementById('pi-undraft');
    return u ? u.textContent.trim() : '';`);
  check("★ 键面上屏的文字与 PROOF.act 一致",
    label === (actTruth || ""), JSON.stringify({ label, actTruth }));
  // 反向：回填真的在跑吗。把键面改成哨兵，再重开一次包详情面板 ——
  // main.js 起手那行只在**启动时**执行一次，所以这里量的是"当前这一份
  // 从哪来"：如果 index.html 兜底与真值一致而回填被删，哨兵会留下来。
  const sentinel = await evalIn(`var u = document.getElementById('pi-undraft');
    if (!u) return null;
    var was = u.textContent;
    u.textContent = '__sentinel__';
    var now = u.textContent;
    u.textContent = was;
    return { canSentinel: now === '__sentinel__', restored: u.textContent.trim() };`);
  check("（反向）键面的文字是运行时写的、不是 HTML 兜底原封不动"
    + "（哨兵能写进去说明 DOM 可写；这条与上一条合起来才排除'兜底冒充真值'）",
    !!sentinel && sentinel.canSentinel && sentinel.restored === (actTruth || ""),
    JSON.stringify({ sentinel, actTruth }));
  await evalIn(`document.getElementById('btn-close-settings').click(); return true;`);
  await sleep(250);

  // ⑪ 停止之后 `state.stopping` 必须被交还。⚠ 上面 ⑦ 那两条量的是
  //    **refreshGate 的反应**（给定 stopping=true 时键面怎么说），
  //    它压根不看 abort() 里有没有把标志清掉 —— 少了那句，
  //    ⑦ 照样全绿，而那颗键会**永久**卡在「正在停止…」：后端早就回话了，
  //    用户看到的是一个再也点不动的键，只能杀进程重开。
  //    所以这条钉在 abort() 的函数体上（fnBody 取不到就报"没找到函数"，
  //    不许静默跳过 —— 元素不在屏时"跳过"在门禁里读起来和"通过"一样）。
  const abortBody = fnBody(jobsJsSrc, "async function abort");
  check("★ 停止流程走完会把 stopping 交还给按钮（否则键永久卡在「正在停止…」）",
    !!abortBody && /state\.stopping\s*=\s*false/.test(abortBody),
    abortBody ? (/state\.stopping\s*=\s*false/.test(abortBody) ? "有交还" : "abort() 里没有交还")
      : "没找到 abort() 函数体");

  // ⑬ 会话头部那个标题也只有一份真值（words.js 的 CHAT.title）。修前它有
  //    **五份**手写：index.html 的 #rh-title、head.js 的初值、head.js 的
  //    setHead 兜底、main.js 的 newChat、sessions.js 的两处删除后收屏。
  //    改一处漏一处，同一块屏幕上就是两个名，而没有任何东西挡着。
  //    判据：① 表里恰好一格；② 那句话在**脚本字面量里只出现一次**
  //    （words.js 自己那一次）—— 出现两次就说明有人在 JS 里又手抄了一遍。
  const chatTruth = tableVals("CHAT");
  const chatWord = [...chatTruth][0] || "";
  const chatCopies = jsLit.filter(l => l === chatWord);
  check("★ 会话标题只有 CHAT.title 一份（修前有五份手写）",
    chatTruth.size === 1 && chatCopies.length === 1,
    chatTruth.size === 1
      ? `${chatCopies.length} 份：${chatCopies.join(" / ")}`
      : `表解析出 ${chatTruth.size} 格`);
  check("（前置）CHAT 那一格解析得出来（否则上一条恒绿）",
    chatTruth.size === 1, [...chatTruth].join(" | ") || "没解析出来");

  // ⑫ 校验结论三态：同一块屏幕只许有一种说法。
  //    改前页头与合规页签写的是 `passed === true ? 好 : 坏` 的二分 ——
  //    「引擎没给出结论」的产物，页签亮红「未通过校验」，而同一块屏幕上
  //    正下方的横幅写着「没有校验结论」：一屏两话，用户会照着红的那句
  //    去反复重跑。
  //    ⚠ 桩默认**不给**这一档（每条 done 都带 true/false），所以整网
  //      从没跑过它 —— 这里用 window.__nullVerdict 把 s6 变成本产物。
  // ⚠ 列表摘要与详情 payload 必须**同时**改：只改一边就是桩自己在打架。
  //      跑完把先前开着的那条放回去，别影响后面的断言。
  // ⚠ 先确认桩里 s1/s6 还在：直接 `.click()` 一个 null 会抛 TypeError，
  //   整个 review 组中断 —— 门禁里那是"跑挂了"而不是"这一条红了"，
  //   归因会被带偏。取不到就显式判红并就地收尾，不许静默跳过。
  const haveRows = await evalIn(`return ['s1','s6'].map(function (id) {
      return !!document.querySelector('#session-list .sess-item[data-id="' + id + '"]');
    });`);
  if (!haveRows || haveRows[0] !== true || haveRows[1] !== true) {
    check("★ 桩的历史记录 s1/s6 还在（否则下面那组三态断言全是空转）",
      false, JSON.stringify(haveRows));
    return;
  }
  check("（前置）桩的历史记录 s1/s6 都在（否则下面那组三态断言全是空转）",
    true, JSON.stringify(haveRows));
  const nvPrev = await evalIn(`var a = document.querySelector('#session-list .sess-item.active');
    return a ? a.dataset.id : '';`);
  await evalIn(`window.__nullVerdict = true; return 1;`);
  await evalIn(`document.querySelector('#session-list .sess-item[data-id="s1"]').click(); return 1;`);
  await sleep(500);
  const nvOk = await evalIn(`return (function () {
      var rh = document.getElementById('rh-state');
      var row = document.querySelector('#session-list .sess-item[data-id="s1"]');
      return { rh: rh ? rh.textContent.trim() : '',
               ok: rh ? rh.classList.contains('ok') : false,
               rowAria: row ? row.getAttribute('aria-label') : '' };
    })();`);
  check("（正向对照）通过那一档在页头与左栏是同一个词",
    /^已通过校验/.test(nvOk.rh) && nvOk.ok && /已通过校验$/.test(nvOk.rowAria),
    JSON.stringify(nvOk));
  await evalIn(`document.querySelector('#session-list .sess-item[data-id="s6"]').click(); return 1;`);
  await sleep(600);
  const nv = await evalIn(`return (function () {
      var rh = document.getElementById('rh-state');
      var tabs = Array.prototype.slice.call(document.querySelectorAll('.res-tab'));
      var tab = tabs.filter(function (t) { return /合规/.test(t.textContent); })[0];
      var ban = Array.prototype.slice.call(document.querySelectorAll('.banner'))
        .filter(function (b) { return /^没有校验结论/.test(b.textContent.trim()); })[0];
      var row = document.querySelector('#session-list .sess-item[data-id="s6"]');
      return { rh: rh ? rh.textContent.trim() : '',
               rhBad: rh ? rh.classList.contains('bad') : true,
               tab: tab ? tab.textContent : '', tabBad: tab ? tab.classList.contains('bad') : true,
               banner: ban ? ban.textContent.slice(0, 24) : '',
               rowAria: row ? row.getAttribute('aria-label') : '' };
    })();`);
  // 「没有结论」不是「结论是坏的」：三处都不许落红。
  check("★ 结论缺失时页头说「没有校验结论」且不落红（改前是二分到 ✗）",
    nv.rh === "没有校验结论" && nv.rhBad === false, JSON.stringify(nv));
  check("★ 同一块屏幕上页签与横幅说的是同一句结论（不许一屏两话）",
    /没有校验结论/.test(nv.tab) && nv.banner.indexOf("没有校验结论") === 0,
    JSON.stringify({ tab: nv.tab, banner: nv.banner }));
  check("★ 结论缺失时合规页签不落红", nv.tabBad === false, nv.tab);
  check("★ 左栏那一行也说没有校验结论（三处一屏，不许两套话）",
    /已完成 · 没有校验结论$/.test(nv.rowAria), nv.rowAria);
  await evalIn(`window.__nullVerdict = false; return 1;`);
  if (nvPrev) {
    await evalIn(`var r = document.querySelector('#session-list .sess-item[data-id="${nvPrev}"]');
      if (r) r.click(); return 1;`);
    await sleep(450);
  }
};
