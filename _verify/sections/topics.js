// 今日选题 / 情报源这一组断言（B 线 §2.2 / §2.4 / §2.5）。
//
// 从 verify.js 整段搬过来，原因只有一个：**让它能被单独跑**。
// 变异检验里 7 条 UI 变异守的断言全在这一组，而它们在 verify.js 里排在
// 6700 行开外 —— 为了跑这 7 条要把前面 350 条断言的前置流程全走一遍，
// 整网跑一趟是两分钟级（具体数字会漂，不写死在这里 —— 写死就一定会过期），
// 乘以那几条变异就是十几分钟。现在 `node _verify/verify.js topics`
// 只做「起桩服务 → 打开页面 → 切到选题视图」就跑这几条，十秒级。
//
// ⚠ 断言逻辑**只有这一份**：verify.js 整网跑的是同一个函数，不是复制品。
//   一旦出现两份，改了一处另一处就会静默过期 —— 那正是本项目最怕的失效形态。
//
// 入参由 verify.js 注入（evalIn / sleep / check），这样本文件不依赖
// verify.js 的内部变量，单独跑与整网跑走的是同一条代码路径。
module.exports = async function topics({ evalIn, sleep, check }) {
    // ── 今日选题 / 情报源（B 线，§2.2 与 §2.4/§2.5）────────────────
    // 这一组守的核心是「**一处声明、四处一致**」：分区、chip、计数、情报源表
    // 四处都由 META 里那份 intel_sources（= pack.yaml 的声明）渲染。
    // 桩里特意留了一个 enabled:false 的源，好让「未接入 N」这条有活样本 ——
    // 没有它，那条断言会在"全部接入"的桩上永远绿（空转）。
    // ⚠ 阅读面宽度必须在**切到选题视图之前**量：`#chat-stream` 在选题视图里是
    // hidden，量出来是 0，那条"同档宽"的断言会变成拿 780 比 0（恒不成立）。
    // ⚠ 比的是**阅读面那一档 token**（--w-stream），不是某个容器的实际宽度：
    //   `#chat-stream` 自己是通栏（内层 `.msg` 才限宽），量它会得到 1026 这种
    //   "看起来不一样"的假结论。§2.8 定的口径就是"同一档宽度 + 居中"，
    //   所以判据落在 token 上，两边都读同一个数。
    const streamW = await evalIn(`return parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue('--w-stream')) || 0;`);
    // 页签条「跟面走」的那一档：源表面是 1160（--w-stg-3col），不是新发明的档。
    const wideW = await evalIn(`return parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue('--w-stg-3col')) || 0;`);
    await evalIn(`window.__refreshCalls = 0; window.__ignoredKeys = [];
      window.__intelReqs = [];
      // 前置条件写进测试，而不是靠"前面碰巧没换包"：整网排在选题组前面的
      // 用例会把 #pack 换成 fitment 且**没人还原**，于是这一组曾在
      // "头部写 B 包、下面是 A 包数据"的状态下全绿。
      // 走真实路径（派发 change）把钉钉死，顺带证明请求真的带上了这个包。
      var ps = document.getElementById('pack');
      ps.value = 'elevator'; ps.dispatchEvent(new Event('change', {bubbles:true}));
      document.getElementById('btn-topics').click(); return true;`);
    // ⚠ 这里**曾经**改成过"轮询到 chips 渲染稳定"，想解决"单独跑这一组时首次
    //   进入选题视图、600ms 不够"的怀疑。**没能证明**：把改动退回固定 sleep(600)
    //   后连跑 5 次仍是 12/12，当初那两次 11/12 与并发会话改 topics.js 的时间
    //   完全重合，是环境噪声不是等待时长。
    //   所以保留原样（固定 600ms，与整网同一条口径）。真出现抖动也不怕 ——
    //   mutate.py 的基线守卫会把它变成"判定无意义"的明确拒绝，而不是假绿。
    await sleep(600);
    // **默认落在排行那一面**（§3.5）。这一条必须在切面之前量：切过去之后
    // "默认是哪一面"就没有现场了（界面上只剩最后点过的那颗页签）。
    const firstFace = await evalIn(`return (function(){
      var on = document.getElementById('view-topics');
      var tabs = [...on.querySelectorAll('#topics-tabs .res-tab')];
      var shown = ['topics-rank', 'topics-scroll', 'topics-src'].filter(function (id) {
        var n = document.getElementById(id);
        return n && !n.classList.contains('hidden'); });
      return { labels: tabs.map(function (b) { return b.textContent.trim(); }),
               on: (tabs.find(function (b) { return b.classList.contains('on'); }) || {}).id,
               shown: shown,
               rows: on.querySelectorAll('#rank-root .rank-row').length };
    })();`);
    // 默认面是**排行**（§3.5：排行 / 按来源 / 情报源）。下面这一整组断言量的是
    // 「按来源」那一面的卡片，所以先真切过去再量 —— 在 hidden 的面上量几何
    // 会得到 0（`getBoundingClientRect` 对 display:none 全是零），断言就变成
    // 拿 780 比 0：这是本仓记过的最典型的假红形态，别再用"看起来没红"糊过去。
    await evalIn(`document.getElementById('ttab-topics').click(); return true;`);
    await sleep(300);
    // ⚠ evalIn 的包装是 `(() => { <expr> })()`（**块体**），所以这里必须显式 return ——
    //   写成裸 IIFE 的话返回值被丢掉，拿到 undefined，后面每条断言都读属性报 TypeError。
    const topicsGeo = await evalIn(`return (function(){
      var on = document.getElementById('view-topics');
      var chat = document.getElementById('view-chat');
      var cards = on.querySelectorAll('#topics-root .page-card');
      var groups = on.querySelectorAll('#topics-root .kb-group');
      // ⚠ 这一段在**模板字符串内部**，所以注释与代码里都不许出现反斜杠转义
      //   （Node 会先把它转成真换行/真制表符，浏览器拿到的就是未闭合字符串，
      //    整段 evaluate 直接 SyntaxError: Invalid or unexpected token）。
      //   换行符一律用 String.fromCharCode(10) 现算。
      var NL = String.fromCharCode(10);
      var heads = [...groups].map(g => g.querySelector('.kb-group-t').textContent.trim().split(NL)[0]);
      var decl = (window.__tsMeta.packs.filter(function(p){ return p.name === 'elevator'; })[0] || {}).intel_sources || [];
      var declaredHead = function (h) {
        return decl.some(function (sc) { return h.indexOf(sc.label + ' \u00b7 ' + sc.role) === 0; }); };
      var chips = [...on.querySelectorAll('#src-chips .src-pill, #src-chips .src-note')].map(c => c.textContent.trim());
      // 可筛的那几颗是 .src-pill，只做解释的三类是 .src-note（改前是 .m-chip，
      // 与全站"有底色小标记"一族撞了手法）。分界是"能不能点"，所以两族分开数：
      // 混成一族的话用户会去点一颗不会动的标签。
      var pillsOn = [...on.querySelectorAll('#src-chips .src-pill.on')].map(b => b.textContent.trim());
      // 选题筛选那一族**不再与首页起手胶囊同型**（2026-09-28 重排版）。
      // 改前的契约是"两族逐项相等"；而这一行里"可筛的"与"只做解释的"都用
      // 有底 + 深色粗字时，用户读不出哪颗能点 —— 所以这一族降成文字 tab。
      // 判据换三件事，比原来更硬（不是放宽）：
      //   ① 高度仍在 --h-sm 那一档（点击目标不许缩水）；
      //   ② 未选中那颗**没有描边环、没有底色** —— 那正是"降成文字"的定义；
      //   ③ 正对照：首页起手胶囊**仍然带**描边环与底色。没有这一句，
      //      "把选题那一族分出去"和"把两族一起洗掉"在断言里长得一模一样。
      var pillShape = (function () {
        var p = on.querySelector('#src-chips .src-pill:not(.on)');
        var s = document.querySelector('#empty-samples .sample-card');
        if (!p || !s) return { missing: true, hasPill: !!p, hasSample: !!s };
        var a = getComputedStyle(p), b = getComputedStyle(s);
        return { hpx: a.height, h: Math.round(p.getBoundingClientRect().height),
                 pillShadow: a.boxShadow, pillBg: a.backgroundColor,
                 sampleShadow: b.boxShadow, sampleBg: b.backgroundColor };
      })();
      // 三条左边界：页签文字 / 筛选胶囊 / 组标题。页签条与阅读面同档宽（--w-stream），
      // 且 .res-tab 自己的 padding-inline(16) 正好等于 .topics-inner 的 16 ——
      // 少一层 padding 就能对齐，多写一层就会在窄窗差出 8px。
      var edges = (function () {
        var left = function (n) { return n ? +n.getBoundingClientRect().left.toFixed(1) : null; };
        var bar = on.querySelector('.topics-tabs');
        var inner = on.querySelector('#topics-scroll .topics-inner');
        return { 页签: left(on.querySelector('.topics-tabs .rt-t')),
                 胶囊: left(on.querySelector('#src-chips .src-pill')),
                 组标题: left(on.querySelector('.kb-group-t')),
                 条宽: bar ? +bar.getBoundingClientRect().width.toFixed(1) : null,
                 面宽: inner ? +inner.getBoundingClientRect().width.toFixed(1) : null };
      })();
      // 头部按视图渲染（问题 1 的判据）：切到选题视图之后，顶部那三格
      // 必须说"今日选题 + 哪个包"，而不是留着上一条会话的「新对话」。
      var head = { title: document.getElementById('rh-title').textContent,
                   sub: document.getElementById('rh-sub').textContent,
                   state: document.getElementById('rh-state').textContent };
      // 副标题比的是**当前所选包**的显示名，不写死"电梯"：
      // 整网跑到这一步时前面的用例可能已经换过包，写死就等于把判据换成
      // "恰好那个包"，换个顺序跑就红 —— 要守的是"头部跟着所选包走"。
      var packNow = (window.__tsMeta.packs.filter(function (p) {
        return p.name === document.getElementById('pack').value; })[0] || {});
      var packLabel = packNow.display_name || packNow.name || '';
      // 控件高度档**从 CSS 变量现取**，不在测试里重抄 28（哪天调档这里跟着走）。
      var hSm = getComputedStyle(document.documentElement).getPropertyValue('--h-sm').trim();
      // 选中态**看得出来**吗：拿选中那颗与未选中那颗比计算值。
      // 改前两者只差一层 5% 灰底（.m-chip.acc-badge.info），亮暗两主题下都读不出；
      // 现在底色与字色两档都要变（规范 §3「选中态允许平涂 --ink + --text-inv」）。
      // 不比具体色值：色值随主题走，比的是"两态必须可分辨"这个维度。
      var selState = (function () {
        var a = on.querySelector('#src-chips .src-pill.on');
        var b = on.querySelector('#src-chips .src-pill:not(.on)');
        if (!a || !b) return { missing: true };
        var ca = getComputedStyle(a), cb = getComputedStyle(b);
        return { bgOn: ca.backgroundColor, bgOff: cb.backgroundColor,
                 fgOn: ca.color, fgOff: cb.color };
      })();
      var srcRows = on.querySelectorAll('#sources-table tr');
      // 「两行头部」的纵向节奏（审查 P2 第 6 条点名的位置）：
      // topics-head 被拆成两个兄弟容器（筛选行 / 动作行），而它们的
      // **行间距**来自 topics-inner 的 gap，与其它兄弟块同档。
      // 这里把"两行之间"与"任意两个相邻兄弟之间"各量一次 ——
      // 如果前者是另一个数，界面上就会出现"同层级两种行距"。
      // ⚠ 这几行注释不许出现反引号：整段是一个模板字符串（踩过三次）。
      var headGaps = (function () {
        var inner = on.querySelector('#topics-scroll .topics-inner');
        var kids = [...inner.children].filter(function (n) {
          return n.getBoundingClientRect().height > 0; });
        var gaps = [];
        for (var i = 1; i < kids.length; i++) {
          gaps.push(Math.round(kids[i].getBoundingClientRect().top
            - kids[i - 1].getBoundingClientRect().bottom));
        }
        var heads = [...inner.querySelectorAll(':scope > .topics-head')];
        var betweenHeads = heads.length >= 2
          ? Math.round(heads[1].getBoundingClientRect().top
              - heads[0].getBoundingClientRect().bottom) : null;
        var rowGap = getComputedStyle(inner).rowGap;
        return { gaps: gaps, betweenHeads: betweenHeads, rowGap: rowGap,
                 headCount: heads.length };
      })();
      var srcHead = srcRows[0] ? [...srcRows[0].children].map(t => t.textContent.trim()) : [];
      var accCol = srcRows.length > 1 ? [...srcRows[1].children].map(t => t.textContent.trim()) : [];
      var inner = on.querySelector('#topics-scroll .topics-inner').getBoundingClientRect();
      // 源表**每一行**的「接入」列与「说明」列 —— 抓取失败的源必须在这里被认出来，
      // 而不是照 state 显示成"已接入"（后端 today() 给失败源的 state 就是 "ok"）。
      // 表体列数**比表头少**时（把「接入」与「上次抓取」合成一列那类回归），
      // 直接取 children[6] 会拿到 undefined → 整段探针抛 TypeError →
      // 变异检验把它判成「工具错」而不是一条真红（实测就是这样漏掉一次归因）。
      // 所以要**越界取空串**：让该红的断言红，而不是让探针崩。
      var cell = function (r, i) { var c = r.children[i]; return c ? c.textContent.trim() : ''; };
      var srcRowsBody = [...srcRows].slice(1);
      // 「接入 / 近 7 天 / 说明」三列**按表头文字取**，不按位置。
      // 加一列（本轮的健康列）就把所有 cell(r, 6) 挪一位，那种写法在两处
      // 都会静默拿到相邻列的内容 —— 上一轮就是靠越界取空串才没让探针崩，
      // 但拿到隔壁列的文本比崩掉更糟：它会让断言"看起来通过"。
      var colOf = function (name) {
        var h = srcRows[0] ? [...srcRows[0].children].map(t => t.textContent.trim()) : [];
        return h.indexOf(name); };
      var iAcc = colOf('接入'), iHealth = colOf('近 7 天'), iNote = colOf('说明');
      var srcStates = srcRowsBody.map(function (r) { return cell(r, iAcc); });
      var srcHealth = srcRowsBody.map(function (r) { return cell(r, iHealth); });
      var srcNotes = srcRowsBody.map(function (r) { return cell(r, iNote); });
      return {
        topicsVisible: !on.classList.contains('hidden'),
        chatHidden: chat.classList.contains('hidden'),
        navOn: document.getElementById('btn-topics').classList.contains('on'),
        rightView: document.getElementById('right').dataset.view,
        refreshBtnVisible: getComputedStyle(document.getElementById('btn-refetch')).display !== 'none',
        groupCount: groups.length, heads: heads, cardCount: cards.length,
        allHeadsDeclared: heads.every(declaredHead),
        declLabels: decl.map(function (sc) { return sc.label + ' \u00b7 ' + sc.role; }),
        chips: chips,
        pillsOn: pillsOn,
        pillShape: pillShape,
        selState: selState,
        edges: edges,
        headGaps: headGaps,
        head: head,
        packLabel: packLabel,
        // 界面**实际拿哪个包去要的数据**：不读它就看不出"请求带错包"这类缺陷。
        intelReqs: (window.__intelReqs || []).slice(),
        // 组与组之间必须有呼吸：#topics-root 挂的是现成的 .kb-groups（gap --s4）。
      // 不挂就是 0px —— 桩数据一屏只出一组，所以这条只有真数据才看得见。
      groupsGap: getComputedStyle(on.querySelector('#topics-root')).rowGap,
      // 「组间缝 > 组内缝」那条判据随四格信号条一起删掉了 —— 主语（两根轨道）
      // 不在界面上，判据留着就是空转。它守的"数据驱动宽度必须走 CSSOM"这一半
      // 由 verify.js:7845 在节奏条 .tempo 的 i 上继续量着，没有失守的口子。
      // 卡内结构改为量：块数 + 卡高（见 cardShape）。
      cardShape: (function () {
        var c = on.querySelector('.topic-card');
        if (!c) return null;
        var b = c.getBoundingClientRect();
        return { blocks: c.children.length, h: Math.round(b.height),
                 cls: Array.prototype.map.call(c.children, function (x) {
                   return x.className.replace('topic-card', '').trim(); }).join('+') };
      })(),
      // 抓取失败的源不许在筛选行自称「已接入，今天没有命中」：
      // 拿源表里标着「抓取失败」的那些 label 去反查胶囊行的 title，交集必须为空。
      // （后端 today() 给失败源的 state 就是 "ok" + count 0 —— 只看 state 就会说错话，
      //   而两句话的下一步动作正好相反：等平台 vs 去配 Key。）
      // ⚠ 本段在 evalIn 的模板串内部：反斜杠会**先被 Node 吃掉一层**，
      //   浏览器拿到的就是错的正则 / 未闭合字符串。所以换行用
      //   String.fromCharCode(10) 现算、数字用 [0-9] 而不是 \d、空格用字面量。
      failedSays: (function () {
        var bad = {};
        [...document.querySelectorAll('#sources-table tr')].slice(1).forEach(function (r) {
          var c = r.children;
          if (c && c[3] && c[3].textContent.trim() === '抓取失败')
            // 首列是「label 主行 + id/平台次行」，用 <br> 分开。
            // ⚠ 必须取 childNodes[0]：textContent 会把两行**连成一个词**
            //   （"policy_library国务院"），拿它当 key 去比胶囊 label
            //   永远不相等 —— 那条断言就变成空转（实测这样假绿过一次）。
            bad[(c[0].childNodes[0].textContent || '').trim()] = 1;
        });
        var mislabel = [];
        document.querySelectorAll('#src-chips [title]').forEach(function (n) {
          if (n.getAttribute('title') !== '已接入，今天没有命中') return;
          var lab = n.textContent.trim().replace(/ +[0-9]+$/, '');
          if (bad[lab]) mislabel.push(lab);
        });
        return { badLabels: Object.keys(bad), mislabel: mislabel };
      })(),
      // 节奏列不许印机器码（文案线 P1-6）：daily / on_demand 是给适配器看的词，
      // 用户那侧应该读到「每天 / 按需」。表里没有对应中文时代码原样上屏，所以钉两端：
      //   ① 至少有一格不是破折号（整列空掉时判据不许空转）
      //   ② 一格纯小写字母/下划线的机器码都不许有
      // ⚠ 同样在 evalIn 模板串内部：正则里不写反斜杠（[a-z_] 够用）。
      // 机会分印的必须是引擎那一档 0–100 的整数：以前卡片把它除以 100 印成 0.72，
      // 而**同一屏**的图例写「≥ 60」—— 一个数两套刻度，用户没法判断手里这条够不够线
      // （文案线 P1-3）。值格是 sig-grid 里每三格一组的第 3 格。
      // 改前值格是 sig-grid 里每三格一组的第 3 格；信号条撤掉后机会分单独印在
      // 卡底那一行的 .t-opp 里 —— 主语换了，判据（必须 0–100 整数、算不出印「—」）不变。
      oppCells: (function () {
        return [...document.querySelectorAll('.topic-card .t-opp b')]
          .map(function (n) { return { text: n.textContent.trim() }; });
      })(),
      // 结论的唯一真通道是 flags 那排标记（app/intel.py 的 _item_flags）。
      // 条目级 it.warn 那条渲染支已删 —— 引擎从不发这个键，桩也不许再喂它。
      warnChips: document.querySelectorAll('.page-card .m-chip.warn').length,
      // warn 这一档**真的落警示色**吗：只数 class 量不到（styles.css 里以前根本没有
      // .m-chip.warn 这条规则，class 挂着、屏幕上还是中性灰 —— 与同屏图例的橙点对不上）。
      // 拿一个现写 var(--warn)/var(--warn-soft) 的探针比计算值，亮暗两主题都成立。
      warnStyle: (function () {
        var n = document.querySelector('.page-card .m-chip.warn');
        if (!n) return null;
        var probe = document.createElement('span');
        probe.style.color = 'var(--warn)';
        probe.style.background = 'var(--warn-soft)';
        document.body.appendChild(probe);
        var pc = getComputedStyle(probe), nc = getComputedStyle(n);
        var out = { color: nc.color, bg: nc.backgroundColor,
                    refColor: pc.color, refBg: pc.backgroundColor };
        probe.remove();
        return out;
      })(),
      cardTailText: Array.prototype.slice.call(document.querySelectorAll('.topic-card .t-desc'))
        .map(function (n) { return n.textContent; }).join(' | '),
      cadCells: (function () {
        var cells = [...document.querySelectorAll('#sources-table tr')].slice(1)
          .map(function (r) { var c = r.children;
            return c && c[2] ? c[2].textContent.trim() : ''; });
        return { cells: cells,
                 raw: cells.filter(function (t) { return /^[a-z_]+$/.test(t); }),
                 real: cells.filter(function (t) { return t !== '—' && t !== ''; }).length };
      })(),
      hSm: hSm,
        navCount: document.getElementById('topics-count').textContent,
        metaText: document.getElementById('topics-meta').textContent,
        srcHead: srcHead, accCol: accCol, srcRowCount: srcRows.length - 1,
        srcStates: srcStates, srcNotes: srcNotes, srcHealth: srcHealth, declCount: decl.length,
        // ⚠ 表体列数必须等于表头列数：少一列时**表头看着完全正常**，
        //   只有数据行整体左移（「今日命中」跑到「上次抓取」下面）。
        //   实测就是这样抓到的：表体里那个 last 变量写了却没进模板。
        srcBodyCols: srcRows.length > 1 ? [...srcRows[1].children].length : 0,
        topicsWidth: +inner.width.toFixed(1),
        // ⚠ 判据要精确到**信号格的值格**：原来只查「整片文本里出现过一次破折号」，
        //   那可能来自任何地方（某条 desc 里就有一个），而这条断言守的是
        //   「算不出的那一格显示 —」。信号格每行是三个兄弟
        //   （标签 / 轨道 / 值），所以取索引 2、5、8… 就是四个值格。
        // ⚠ 本段在**模板串内部**：注释里不能出现反引号（会把模板串截断，
        //   而截断后**语法可能仍然合法** —— node --check 抓不到，要跑到这一行才炸）。
        // 卡片上的平台真实热度（§阶段一 #6）：排行行有分项条，卡片没有语境，
        // 所以"没有 hot"必须**不发这一颗**，而不是印一句「热度 —」。
        cardHot: [...on.querySelectorAll('#topics-root .topic-card .res-metric-list .rank-src')]
          .map(function (n) { return n.textContent.trim(); }),
        // 两个视图读的是同一份池子：排行行详情里的成员条目键必须逐个能在卡片里找到。
        // （隐藏的面照样有 DOM，这里比的是键不是几何 —— 别拿它当"看得见"的证据。）
        crossCheck: (function () {
          var pool = {};
          (window.__intelPool || []).forEach(function (k) { pool[k] = true; });
          var items = [...on.querySelectorAll('#rank-root .rank-item')];
          var seen = {};
          var missing = items.filter(function (n) {
            seen[n.dataset.key] = true;
            return !pool[n.dataset.key]; }).length;
          // 反向也要成立：池子里的每条都必须出现在某个事件行里（漏一条=排行少东西）
          var uncovered = (window.__intelPool || []).filter(function (k) {
            return !seen[k]; }).length;
          return { total: items.length, missing: missing, uncovered: uncovered,
                   pool: (window.__intelPool || []).length };
        })(),
        sigValues: (function () {
          // ⚠ 要**汇总所有卡**：第一张卡的 D/S/E 恰好都有值，只看它测不到
          //   「算不出显示 —」这件事（桩里 E 为 null 的是第二张卡）。
          // 三格现在挤在一句小字里（「需求 D 0.14 · 供给 S 0.00 · 事件 E —」），
          // 所以按「·」拆开、每对取"标签 + 值"，值与标签一起交出去 ——
          // 判据能指名道姓地说"哪一格该是 —"，而不是只看整卡文本里出现过破折号。
          var out = [];
          on.querySelectorAll('.topic-card .t-minor').forEach(function (n) {
            // ⚠ 本段在 evalIn 的模板串内部：反斜杠会被 Node 先吃掉一层，
            //   所以这里不写正则，用最后一次空格把"标签 / 值"切开。
            n.textContent.split('·').forEach(function (part) {
              var t = part.trim(), k = t.lastIndexOf(' ');
              if (k > 0) out.push({ label: t.slice(0, k), val: t.slice(k + 1) });
            });
          });
          return out;
        })(),
        // 分类行：可筛的与只做解释的必须**两种手法**，且整行只占一行。
        chipsRow: (function () {
          var box = document.getElementById('src-chips');
          if (!box) return null;
          // 行数是量**容器高**而不是数子项的 top：这一行是垂直居中的，
          // 28 的胶囊与 17 的说明在同一条视觉行里 top 也差 5px，按 top 分桶
          // 会把一行错数成三行（第一版就是这么误报的）。
          var boxH = Math.round(box.getBoundingClientRect().height);
          var on = box.querySelector('.src-pill.on');
          var off = box.querySelector('.src-pill:not(.on)');
          var note = box.querySelector('.src-note');
          var badge = document.querySelector('.page-card .m-chip');
          var g = function (n) { return n ? getComputedStyle(n) : null; };
          // 拿现写的探针比解析值，不写死 rgb —— 亮暗两主题都要成立（规范 §7.1）。
          var probe = document.createElement('span');
          probe.style.background = 'var(--fill)';
          document.body.appendChild(probe);
          var fillBg = getComputedStyle(probe).backgroundColor;
          probe.remove();
          return { boxH: boxH, fillBg: fillBg,
            onBg: g(on) && g(on).backgroundColor, offBg: g(off) && g(off).backgroundColor,
            onWeight: g(on) && g(on).fontWeight, offWeight: g(off) && g(off).fontWeight,
            noteBg: g(note) && g(note).backgroundColor,
            noteSize: g(note) && g(note).fontSize,
            badgeBg: g(badge) && g(badge).backgroundColor };
        })(),
        // 视图切换条：字号升一档 + 选中靠字重 + 与顶栏留出呼吸 + 默认不画那条线。
        tabStrip: (function () {
          var t = document.querySelector('#topics-tabs');
          if (!t) return null;
          var cs = getComputedStyle(t);
          var a = t.querySelector('.res-tab.on'), b = t.querySelector('.res-tab:not(.on)');
          var inner = document.querySelector('#topics-scroll .topics-inner');
          return { line: cs.borderBottomColor, gap: cs.gap, padTop: cs.paddingTop,
            top: cs.top, onFont: getComputedStyle(a).fontSize,
            onWeight: getComputedStyle(a).fontWeight,
            offWeight: b ? getComputedStyle(b).fontWeight : null,
            // 对齐量的是**条里第一颗**与胶囊行第一颗 —— 三面里选中的是哪一颗
            // 不该改变这条左边界（改前只有两颗页签且默认选中第一颗，用 .on 恰好
            // 也是第一颗，于是这个区别一直没暴露；加排行之后它就变成第二条了）。
            tabTextLeft: Math.round(t.querySelector('.res-tab').getBoundingClientRect().left
              + parseFloat(getComputedStyle(t.querySelector('.res-tab')).paddingLeft)),
            chipLeft: Math.round((document.querySelector('#src-chips .src-pill')
              || { getBoundingClientRect: function(){return {};} }).getBoundingClientRect().left || -1) };
        })(),
      }; })()`);
    check("点左栏「今日选题」切到选题视图（左栏与会话列表常驻，不跳页、不开二级窗）",
      topicsGeo.topicsVisible && topicsGeo.chatHidden && topicsGeo.navOn
        && topicsGeo.rightView === "topics",
      JSON.stringify({ v: topicsGeo.topicsVisible, c: topicsGeo.chatHidden,
                       n: topicsGeo.navOn, r: topicsGeo.rightView }));
    check("头部那颗「重抓」只在选题视图出现（用 #right[data-view] 控显隐）",
      topicsGeo.refreshBtnVisible, String(topicsGeo.refreshBtnVisible));
    // 「四处一致」这条命题的**前提**是界面按当前所选包去要数据。
    // 以前它被执行零次：桩不认 ?pack=、全网也没人读过 intel 请求的 query。
    check("选题数据是按**当前所选包**去要的（请求带 pack=elevator）",
      topicsGeo.intelReqs.length >= 1
        && topicsGeo.intelReqs.every(q => /pack=elevator/.test(q)),
      JSON.stringify(topicsGeo.intelReqs));
    // 问题 1 的判据：头部以前只由对话链路的 setHead 写，gotoView 从不碰它，
    // 于是切到选题视图顶部还留着「新对话」。现在头部按视图渲染，
    // 而且 `#rh-state`（生成中 / 合格 / 失败那格）不属于别的视图。
    check("头部按视图渲染：选题视图写「今日选题」+ 当前所选包，不留「新对话」、不串状态",
      topicsGeo.head.title === "今日选题" && topicsGeo.head.sub === topicsGeo.packLabel
        && topicsGeo.packLabel !== "" && topicsGeo.head.state === "",
      JSON.stringify({ head: topicsGeo.head, pack: topicsGeo.packLabel }));
    // ⚠ 不能断言"组数 == 今天有货的源数"：一屏只放 5 条，**只有本页涉及的源**
    //   才会出现分区。要守的是另外两件事：每个组标题都来自声明（渲染层真的在读
    //   配置，而不是自己写死平台名）；以及组数 **严格小于本页卡片数** ——
    //   后者正是"桶按 x.label 找、而桶里存的是 x.g"那个 bug 的判据
    //   （每张卡各成一组，组标题重复，实测就是这样被这条抓出来的）。
    check("分区按来源渲染：组标题逐条来自声明的 label · role，且不是每张卡各成一组",
      topicsGeo.groupCount >= 1 && topicsGeo.allHeadsDeclared
        && topicsGeo.groupCount < topicsGeo.cardCount,
      JSON.stringify({ n: topicsGeo.groupCount, cards: topicsGeo.cardCount,
                       heads: topicsGeo.heads, decl: topicsGeo.declLabels }));
    // 采集口径跟着控件形态走：可筛的胶囊（.src-pill）与只做解释的标记（.m-chip）
    // 一起读，**文本形状没变**（「全部 8」「下拉词 4」「未接入 1」），
    // 所以这条守的还是同一件事 —— 四处一致里的那一处。
    check("筛选行含「全部 N」+ 各源计数 + 「未接入 N」（只列有货的源）",
      topicsGeo.chips.some(c => c.indexOf("全部 8") === 0)
        && topicsGeo.chips.some(c => c.indexOf("下拉词 4") === 0)
        && topicsGeo.chips.some(c => c.indexOf("未接入 1") === 0),
      JSON.stringify(topicsGeo.chips));
    // 唯一性 + 默认值：多颗同时亮 = 选中态由 CSS 蒙的，不是状态机管的。
    check("筛选行恰好一颗选中，且默认是「全部」",
      topicsGeo.pillsOn.length === 1 && topicsGeo.pillsOn[0].indexOf("全部 8") === 0,
      JSON.stringify(topicsGeo.pillsOn));
    // 这一条是改胶囊按钮的**理由本身**，也是它唯一可量的形态：
    // 底色与字色两档都要变，且选中那颗不能是透明底（透明=根本没上样式）。
    check("选中那颗与未选中的底色、字色都不同（两态必须可分辨，不比具体色值）",
      !topicsGeo.selState.missing
        && topicsGeo.selState.bgOn !== topicsGeo.selState.bgOff
        && topicsGeo.selState.fgOn !== topicsGeo.selState.fgOff
        && topicsGeo.selState.bgOn !== "rgba(0, 0, 0, 0)",
      JSON.stringify(topicsGeo.selState));
    check("选题筛选降成文字 tab：无描边环 + 无底色，高度仍在 --h-sm 档",
      !topicsGeo.pillShape.missing && topicsGeo.pillShape.hpx === topicsGeo.hSm
        && topicsGeo.pillShape.pillShadow === "none"
        && topicsGeo.pillShape.pillBg === "rgba(0, 0, 0, 0)",
      JSON.stringify(topicsGeo.pillShape));
    check("正对照：首页起手胶囊仍带描边环与底色（分出去的是选题那一族，不是两族一起洗）",
      !topicsGeo.pillShape.missing && topicsGeo.pillShape.sampleShadow !== "none"
        && topicsGeo.pillShape.sampleBg !== "rgba(0, 0, 0, 0)",
      JSON.stringify({ sampleShadow: topicsGeo.pillShape.sampleShadow,
                       sampleBg: topicsGeo.pillShape.sampleBg }));
    check("页签条与阅读面同档宽，页签文字 / 筛选胶囊 / 组标题三条左边界重合",
      Math.abs(topicsGeo.edges.条宽 - topicsGeo.edges.面宽) <= 1
        && Math.abs(topicsGeo.edges.条宽 - streamW) <= 1
        && Math.abs(topicsGeo.edges.页签 - topicsGeo.edges.胶囊) <= 0.6
        && Math.abs(topicsGeo.edges.胶囊 - topicsGeo.edges.组标题) <= 0.6,
      JSON.stringify({ e: topicsGeo.edges, stream: streamW }));
    check("左栏 nav 键帽计数 = 今日条目总数（不是「未读数」——那要多存一个状态）",
      topicsGeo.navCount === "8" && /共 8 条/.test(topicsGeo.metaText),
      JSON.stringify({ nav: topicsGeo.navCount, meta: topicsGeo.metaText }));
    // 头部拆成两行之后，**行距不许分裂成两档**（审查 P2 第 6 条点名这里）。
    // 判据是"两行头部之间的间距 = 其它相邻兄弟之间的间距"，
    // 而不是某个写死的 px —— 那一档本来就该由 `.topics-inner` 的 gap 说了算。
    // ⚠ 这条量的是**屏幕上真实的间距**，不是类名：两行都用 `.topics-head`，
    //   只看 DOM 会以为"它们同族所以一样"，而真正会分叉的是 CSS 覆盖。
    check("头部两行之间与其它兄弟块之间是同一档行距（同层级不许两种行距）",
      topicsGeo.headGaps.headCount === 2
        && topicsGeo.headGaps.betweenHeads === Number(topicsGeo.headGaps.rowGap.replace("px", ""))
        && topicsGeo.headGaps.gaps.every(g => g === Number(topicsGeo.headGaps.rowGap.replace("px", ""))),
      JSON.stringify(topicsGeo.headGaps));
    check("算不出的 D/S/E 显示「—」（逐格带标签，能指名说哪一格该是破折号）",
      Array.isArray(topicsGeo.sigValues) && topicsGeo.sigValues.length >= 12
        && topicsGeo.sigValues.every(v => v.label && v.val)
        && topicsGeo.sigValues.some(v => v.val === "—")
        // 「—」只许落在这三格上：别处冒出来就是渲染串了位。
        && topicsGeo.sigValues.filter(v => v.val === "—")
             .every(v => /需求 D|供给 S|事件 E/.test(v.label))
        // 空值 / undefined / NaN 是"把没数据抹平"的三种形状。
        && !topicsGeo.sigValues.some(v => /^(undefined|NaN|--*)$/.test(v.val)),
      JSON.stringify(topicsGeo.sigValues.slice(0, 6)));
    // ⚠ 改前这条还带一句「任何一格都不许是 0 / 0.00」。那句**随四格条一起撤了**，
    //   不是放宽：真实数据里「供给 S」就是 0.00（下拉词那批每张都是），
    //   把它禁掉是在禁一条合法读数。null 与 0 的区分由上面那句「算不出必须印 —」
    //   逐格守着 —— 它比"整行里不许出现 0"更精确，也更难被绕开。
    check("选题阅读面与正文同档宽（--w-stream，§2.8 的列宽统一口径）",
      Math.abs(topicsGeo.topicsWidth - streamW) <= 1,
      JSON.stringify({ topics: topicsGeo.topicsWidth, stream: streamW }));
    // 上面三条（轨道走 CSSOM / 四格两两一行 / 组间缝大于组内缝）的主语是
    // .sig-grid 与两根 237px 轨道，随信号条一起撤掉 —— 主语不在界面上，
    // 判据留着就是空转。它们守的三件事换到新结构上量：
    //   ① 卡内只剩三块（退回一卡五段立刻红）；② 卡高 ≤180（改前实测 285）；
    //   ③「数据驱动宽度走 CSSOM」没有失守 —— 节奏条 `.tempo > i` 仍由
    //      verify.js 里 barWidths 那条量着（7845 行）。
    check("卡片也带平台真实热度（§阶段一 #6：hot 不能只上排行行）",
      topicsGeo.cardHot.length >= 1
        && topicsGeo.cardHot.some(h => /热度 [0-9.]+ [万亿]/.test(h))
        // 没热度的条目**不许**印「热度 —」：卡片没有轨道当语境，那会被读成"量过是零"
        && !topicsGeo.cardHot.some(h => /热度 —/.test(h)),
      JSON.stringify(topicsGeo.cardHot));
    // 事件详情里的条目要带可点出处（按来源那面的卡片没有这一层，只有排行行有）。
    // 三视图的数据一致性：排行行展开的条目键必须能在按来源那面的卡里找到同一条。
    // 排行与按来源读的是同一份池子：详情里每个条目键都必须是后端真发过的条目。
    // ⚠ **反向**（池子里每条都出现在某个事件里）这里量不到 —— 桩只给 4 个事件、
    //   覆盖了 8 条里的一部分，拿它当全集会假红。那一半由
    //   `tests/test_intel_ranking.py::test_events_partition_the_same_pool_the_source_view_shows`
    //   在真代码上守着（那里事件是对同一份 items 做的完整划分）。
    check("排行与按来源是同一份池子（事件成员键都是后端发过的条目）",
      topicsGeo.crossCheck.missing === 0 && topicsGeo.crossCheck.total >= 2
        && topicsGeo.crossCheck.pool >= topicsGeo.crossCheck.total,
      JSON.stringify(topicsGeo.crossCheck));
    check("选题卡压成三块（标题 / 证据 / 底行），不再是一卡五段",
      !!topicsGeo.cardShape && topicsGeo.cardShape.blocks === 3
        && topicsGeo.cardShape.cls === "res-top+t-desc+topic-foot",
      JSON.stringify(topicsGeo.cardShape));
    check("选题卡高 ≤180（改前实测 285：1000 高的窗口一屏只放得下 3 张）",
      !!topicsGeo.cardShape && topicsGeo.cardShape.h > 0
        && topicsGeo.cardShape.h <= 180,
      JSON.stringify({ h: topicsGeo.cardShape && topicsGeo.cardShape.h }));
    // 分类行：可筛的与只做解释的必须**两种手法**。改前两者都是"有底 + 深色粗字"，
    // 一行里读不出哪颗能点；而选中那颗用平涂 --ink，比它下面 24px 处那排 15px
    // 的视图页签还抢眼。
    // ⚠ 最后一句是**正对照**：状态标记去底靠的是新类 .src-note，
    //   不是把 .m-chip 一族改成没底 —— 后者会连带改掉全站八个有底标记。
    check("筛选行只占一行：可筛=文字 tab（选中给 --fill），说明=无底纯字",
      !!topicsGeo.chipsRow && topicsGeo.chipsRow.boxH === 28
        && topicsGeo.chipsRow.offBg === "rgba(0, 0, 0, 0)"
        && topicsGeo.chipsRow.noteBg === "rgba(0, 0, 0, 0)"
        && topicsGeo.chipsRow.onBg === topicsGeo.chipsRow.fillBg
        && topicsGeo.chipsRow.onWeight !== topicsGeo.chipsRow.offWeight
        && topicsGeo.chipsRow.badgeBg !== "rgba(0, 0, 0, 0)",
      JSON.stringify(topicsGeo.chipsRow));
    check("视图切换条：15px 字阶 + 选中靠字重 + 与顶栏留 24 呼吸 + 默认不画线",
      !!topicsGeo.tabStrip && topicsGeo.tabStrip.onFont === "15px"
        && topicsGeo.tabStrip.onWeight === "600"
        && topicsGeo.tabStrip.offWeight === "500"
        && topicsGeo.tabStrip.padTop === "24px"
        // 选题视图的滚动盒没有 padding-top 可抵消，跟着写 -24 会让条子滚起来
        // 钻到顶栏底下（实测 y 48 → 24），所以这一族的 top 必须归 0。
        && topicsGeo.tabStrip.top === "0px"
        && topicsGeo.tabStrip.line === "rgba(0, 0, 0, 0)"
        // 页签文字左沿 == 分类行左沿（§2.8 那笔"三条左边界重合"的账）
        && topicsGeo.tabStrip.tabTextLeft === topicsGeo.tabStrip.chipLeft,
      JSON.stringify(topicsGeo.tabStrip));
    // 吸顶那一态：不滚时那条线不画（两颗页签占 133px、条宽 780，画满就是
    // 一条 647px 的空线），滚起来才需要它把"条"与"从底下穿过去的正文"分开。
    // ⚠ 两头都要量：只验"滚下去有线"会放过"挂上就摘不掉"那种实现。
    const stuck = await evalIn(`return (async function(){
      var v = document.getElementById('view-topics');
      var t = document.getElementById('topics-tabs');
      var read = function () { return { on: v.classList.contains('stuck'),
        line: getComputedStyle(t).borderBottomColor }; };
      v.scrollTop = 400; v.dispatchEvent(new Event('scroll'));
      await new Promise(function (r) { setTimeout(r, 150); });
      var down = read(); down.got = v.scrollTop;
      v.scrollTop = 0; v.dispatchEvent(new Event('scroll'));
      await new Promise(function (r) { setTimeout(r, 150); });
      return { down: down, up: read() };
    })()`);
    check("滚下去页签条吸顶、发丝线出现；回到顶部线又收回去",
      stuck.down.got > 0 && stuck.down.on
        && stuck.down.line !== "rgba(0, 0, 0, 0)"
        && !stuck.up.on && stuck.up.line === "rgba(0, 0, 0, 0)",
      JSON.stringify(stuck));
    check("分组之间有呼吸：#topics-root 吃现成的 .kb-groups（gap = --s4 那一档）",
      topicsGeo.groupsGap === "16px", JSON.stringify({ gap: topicsGeo.groupsGap }));
    check("抓取失败的源不在筛选行自称「已接入，今天没有命中」（两句话的下一步动作相反）",
      topicsGeo.failedSays.badLabels.length >= 1 && topicsGeo.failedSays.mislabel.length === 0,
      JSON.stringify(topicsGeo.failedSays));
    check("节奏列说中文：daily / on_demand 这类机器码不许原样上屏",
      topicsGeo.cadCells.real >= 1 && topicsGeo.cadCells.raw.length === 0,
      JSON.stringify(topicsGeo.cadCells));
    check("机会分印整数（引擎那一档 0–100），不与同屏图例的「≥ 60」分成两套刻度",
      topicsGeo.oppCells.length >= 3
        && topicsGeo.oppCells.filter(function (c) { return /^[0-9]+$/.test(c.text); }).length >= 2
        && topicsGeo.oppCells.some(function (c) { return c.text === '—'; }),
      JSON.stringify(topicsGeo.oppCells));
    // ⚠ 这里原来有一句注释："不许先除以 100 再四舍五入"由上一条（必须印 0–100 的整数）
    //   继续守着。**它守不住** —— 78/100 四舍五入是 1，照样是 0–100 的整数。
    //   s14-1 那条变异整轮漏检就是这么来的（锚点腐烂之后判据空转，没人发现）。
    //   刻度类判据必须**与引擎发的那个数逐条对**，不是比形状。
    //   现取 `/api/intel/today`（同一个桩、同一个 fetch），按卡片上的 data-key 对。
    const oppScale = await evalIn(`return (async () => {
      var sel = document.getElementById('pack');
      var u = '/api/intel/today?pack=' + encodeURIComponent(sel ? sel.value : '');
      var d = await (await fetch(u)).json();
      var sent = {};
      (d.items || []).forEach(function (it) { sent[it.key] = (it.score || {}).opportunity; });
      var rows = [...document.querySelectorAll('.topic-card')].map(function (c) {
        var b = c.querySelector('.t-opp b');
        var has = Object.prototype.hasOwnProperty.call(sent, c.dataset.key);
        return { key: c.dataset.key, printed: b ? b.textContent.trim() : '(无)',
                 sent: has ? sent[c.dataset.key] : '(不在池子里)' };
      });
      var usable = function (v) { return v !== '(不在池子里)' && v !== null && v !== undefined; };
      return { n: rows.length, bad: rows.filter(function (x) {
        if (!usable(x.sent)) return x.printed !== '—';
        return x.printed !== String(Math.round(x.sent));
      }).slice(0, 3) };
    })();`);
    check("卡片印的机会分逐条等于引擎发的那个数（对不上就是同一屏两套刻度）",
      oppScale.n >= 3 && oppScale.bad.length === 0, JSON.stringify(oppScale));
    check("「算不出机会分」这类结论走 flags 那一排标记（引擎真发的通道）",
      topicsGeo.warnChips >= 1, JSON.stringify({ warnChips: topicsGeo.warnChips }));
    check("warn 那一档标记真的落警示色（CSS 里以前没有这一档，只数 class 量不到）",
      !!topicsGeo.warnStyle && topicsGeo.warnStyle.color === topicsGeo.warnStyle.refColor
      && topicsGeo.warnStyle.bg === topicsGeo.warnStyle.refBg,
      JSON.stringify(topicsGeo.warnStyle));
    check("卡片不演引擎发不出的能力（桩里那条假 banwords 警告不许回来）",
      topicsGeo.cardTailText.indexOf('包赔') < 0, topicsGeo.cardTailText.slice(0, 90));
    check("情报源表：`接入` 与 `上次抓取` 是两根正交列，且表体列数与表头一致",
      topicsGeo.srcHead.indexOf("接入") >= 0 && topicsGeo.srcHead.indexOf("上次抓取") >= 0
        && topicsGeo.srcRowCount === topicsGeo.declCount
        && topicsGeo.srcBodyCols === topicsGeo.srcHead.length,
      JSON.stringify({ head: topicsGeo.srcHead, rows: topicsGeo.srcRowCount,
                       decl: topicsGeo.declCount, bodyCols: topicsGeo.srcBodyCols }));
    // ⚠ 行数判据从写死的 4 改成**声明里的源数**（declCount）：声明加一个源就要改一次
    //   断言，属"实现细节抄成断言"，抄的那份迟早与声明漂移 —— 本次加 policy_library
    //   活样本时，正是这条先红。语义没变：声明了几个源，表里就该有几行。
    check("抓取失败的源在表里标成「抓取失败」并给出原因（不冒充「已接入」）",
      // 桩里 policy_library 的 `errors` 有值、而 `state` 是 "ok"（后端 today() 就是
      // 这么给的）—— 照 state 渲染就会显示"已接入"。这条守的正是"我们没抓到"
      // 不能长得像"平台今天没货"：两者下一步动作相反（重抓 vs 换题）。
      topicsGeo.srcStates.filter(s => s === "抓取失败").length === 1
        && topicsGeo.srcStates.filter(s => s === "已接入").length === 3
        && topicsGeo.srcNotes.some(n => n.indexOf("连接超时") >= 0),
      JSON.stringify({ states: topicsGeo.srcStates, notes: topicsGeo.srcNotes }));
    check("抓取失败的源不计入「N 源已接」（计数行不能把失败说成已接入）",
      /3 源已接/.test(topicsGeo.metaText) && /1 源抓取失败/.test(topicsGeo.metaText),
      JSON.stringify({ meta: topicsGeo.metaText }));

    // ── 视图内的「选题 / 情报源」页签（问题 2：情报源以前藏在头部图标按钮后面）──
    // ⚠ 可见性一律取**计算样式**，不取 classList.contains('hidden') ——
    //   verify.js 自己记录过这类盲区（节点被 CSS 藏起来却算通过）。
    await evalIn(`document.getElementById('ttab-sources').click(); return true;`);
    await sleep(150);
    const srcTab = await evalIn(`return (function(){
      var pane = document.getElementById('topics-src');
      var list = document.getElementById('topics-scroll');
      var row = document.querySelector('#sources-table tr:nth-child(2)');
      return { paneShown: getComputedStyle(pane).display !== 'none',
               listHidden: getComputedStyle(list).display === 'none',
               // 页签条必须跟着面走档：吸顶条是不透明的，只盖住 780 的话
               // 1160 的表格会从条的两侧穿出来（读起来像页签条坏了）。
               barW: Math.round(document.querySelector('.topics-tabs').getBoundingClientRect().width),
               srcW: Math.round(document.querySelector('.topics-src-inner').getBoundingClientRect().width),
               meta: document.getElementById('sources-meta').textContent,
               tabOn: document.getElementById('ttab-sources').classList.contains('on'),
               aria: document.getElementById('ttab-sources').getAttribute('aria-selected'),
               rowH: row ? Math.round(row.getBoundingClientRect().height) : 0,
               calls: window.__refreshCalls || 0 }; })()`);
    check("点「情报源」页签：那一面真的可见、选题面收起、表行有高度，且不发重抓请求",
      srcTab.paneShown && srcTab.listHidden && srcTab.tabOn && srcTab.aria === "true"
        && srcTab.rowH > 0 && srcTab.calls === 0
        // 抓取时间的短读法只有一份（`09-23 10:12`）：源表面那行不许退回
        // 原样 ISO —— 三处 slice 各写一份，迟早有一处忘了改。
        && /^最近一次：\d{2}-\d{2} \d{2}:\d{2}$/.test(srcTab.meta)
        // 页签条跟到当前面那一档：判据是**条宽 == 该面内容宽**（两者都被容器夹过，
        // 1320 窗下 1160 那档放不下会一起夹到同一个数），并确实比阅读面那一档宽
        // —— 否则"跟面走"这条就等于没量。
        && srcTab.barW === srcTab.srcW && srcTab.barW > streamW,
      JSON.stringify(Object.assign({}, srcTab, { wideW: wideW, streamW: streamW })));
    // ←/→ 在页签间移动是这一族控件既有的约定（与结果卡产物分区共用 util.wireTablist，
    // 两份实现迟早只修一半，所以这里量的是**同一族**的行为在选题页也成立）。
    const kbTab = await evalIn(`return (function(){
      var b = document.getElementById('ttab-sources'); b.focus();
      b.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
      var list = document.getElementById('topics-scroll');
      return { on: document.getElementById('ttab-topics').classList.contains('on'),
               focus: document.activeElement.id,
               listShown: getComputedStyle(list).display !== 'none' }; })()`);
    check("键盘左箭头从「情报源」回到「选题」：焦点与可见面一起跟上",
      kbTab.on && kbTab.focus === "ttab-topics" && kbTab.listShown,
      JSON.stringify(kbTab));

    // 「换一批」是在池子里翻页，**不是重新抓** —— 判据是刷新请求数没涨。
    await evalIn(`document.getElementById('btn-more').click(); return true;`);
    await sleep(120);
    const paged = await evalIn(`return { label: document.getElementById('btn-more').textContent,
      calls: window.__refreshCalls || 0,
      firstCard: document.querySelector('#topics-root .page-card .card-title').textContent };`);
    check("「换一批」纯前端翻页：不触发重抓请求（要新数据点顶栏那颗重抓）",
      paged.calls === 0 && /第 2\//.test(paged.label),
      JSON.stringify(paged));

    // 「忽略」只影响今天：本地摘掉 + 落一条忽略记录（服务端按日期判"连续几天沉底"）。
    await evalIn(`document.getElementById('btn-more').click();
      window.__firstTitle = document.querySelector('#topics-root .page-card .card-title').textContent;
      document.querySelector('#topics-root .page-card [data-act="ignore"]').click(); return true;`);
    await sleep(300);
    const ignored = await evalIn(`return { keys: window.__ignoredKeys || [],
      // 「已忽略 N」配的句是「只影响今天」，所以这一行只能数**今天**的。
      // 桩里故意让累计值（17）与今天的（1→2）不同：拿错那一个就会红。
      ignBtn: document.getElementById('btn-ignored').textContent,
      // ⚠ 判据是"那张卡真的从 DOM 里没了"，不是只看 data.items.length ——
      //   只过滤 data.items 而不过滤 g.items 时，计数变了、卡还在，
      //   后者才是用户看到的东西（实测这条弱断言漏过了一次）。
      gone: ![...document.querySelectorAll('#topics-root .page-card .card-title')]
              .some(t => t.textContent === window.__firstTitle),
      navCount: document.getElementById('topics-count').textContent };`);
    check("「忽略」落一条记录并本地摘掉该卡（只影响今天，明天同题还会回来）",
      ignored.keys.length === 1 && ignored.gone && ignored.navCount === "7"
        // 那行读的是**今天**的条数（桩里累计比今天多 17，拿错就是「已忽略 18」）
        && ignored.ignBtn === "已忽略 2",
      JSON.stringify(ignored));
    // 忽略键必须**来自后端下发的 `item.key`**，不是前端自己拼的。
    // 桩里 g-new 的 key 是 `srv:g-new`（前端按 guid 拼会得到 `g-new`）——
    // 两边规则一致的话这条永远绿（空转），所以桩里特意让它俩不同。
    // 守的是一个真实缺陷：前端拼 `guid||url||title`、后端忽略过滤只 `guid||title`，
    // 于是**只有 url 的条目**（政策库那类）点「忽略」之后原样回来。
    check("忽略用的是后端下发的 item.key（前端不再自己拼一份）",
      ignored.keys.length === 1 && ignored.keys[0] === "srv:g-new",
      JSON.stringify(ignored));

    // 筛到只剩一张卡 → 忽略掉它 → 被筛的那个源归零、pill 从可筛行里消失。
    // 这时筛选**不能悬空**：悬空的可见形态是"一颗选中的都没有 + 卡区说池子是空的"，
    // 而「全部」里其实还有货 —— 用户唯一的自救是猜要点「全部」。
    await evalIn(`document.querySelector('#src-chips .src-pill[data-src="抖音热榜"]').click(); return true;`);
    await sleep(250);
    const oneSrc = await evalIn(`return { cards: document.querySelectorAll('#topics-root .page-card').length,
      on: document.querySelectorAll('#src-chips .src-pill.on').length };`);
    await evalIn(`document.querySelector('#topics-root .page-card [data-act="ignore"]').click(); return true;`);
    await sleep(450);
    const fellBack = await evalIn(`return { on: [...document.querySelectorAll('#src-chips .src-pill.on')]
        .map(function (b) { return b.dataset.src; }),
      cards: document.querySelectorAll('#topics-root .page-card').length,
      pressed: [...document.querySelectorAll('#src-chips .src-pill[aria-pressed="true"]')]
        .map(function (b) { return b.dataset.src; }) };`);
    check("被筛源的条目清零后筛选自动回落「全部」，且 aria-pressed 跟着走（不许悬空）",
      oneSrc.cards === 1 && oneSrc.on === 1 && fellBack.on.length === 1
        && fellBack.on[0] === "all" && fellBack.cards > 0
        && fellBack.pressed.length === 1 && fellBack.pressed[0] === "all",
      JSON.stringify({ before: oneSrc, after: fellBack }));

    // 「去生成」= 切回会话 + 写主题 + 填参数，**不自动发送**（一次生成几十秒真金白银）。
    // ⚠ 在同一次 evaluate 里读一次主题：分开两次读的话，"谁把主题清掉的"
    //   会变成一个说不清的问题（实测就是这样 —— 分开读只能看到结果为空）。
    const clicked = await evalIn(`var b = document.querySelector('#topics-root .page-card [data-act="gen"]');
      var t = b ? b.closest('.page-card').querySelector('.card-title').textContent : '';
      window.__genCalls = 0;
      if (b) b.click();
      return { had: !!b, cardTitle: t,
               topicRightAfter: document.getElementById('topic').value,
               viewRightAfter: document.getElementById('right').dataset.view };`);
    await sleep(300);
    const went = await evalIn(`return { view: document.getElementById('right').dataset.view,
      chatVisible: !document.getElementById('view-chat').classList.contains('hidden'),
      topic: document.getElementById('topic').value,
      navOn: document.getElementById('btn-topics').classList.contains('on'),
      refetchVisible: getComputedStyle(document.getElementById('btn-refetch')).display !== 'none',
      headTitle: document.getElementById('rh-title').textContent,
      // 「去生成」说"已填好主题与细分领域"，那参数条上**看得见的那颗**就得真的是新值。
      // 只写 state.genParams 不重画自绘胶囊的话，发出去的是新值、看到的是旧值 ——
      // 而参数条正是用户确认参数的地方。比的是"界面显示的 == 将要发送的"。
      segPill: (function () {
        var s = document.querySelector('#quick-params #p-segment');
        var w = s && s.closest('.select-wrap');
        return w ? (w.querySelector('.sel-text') || {}).textContent : null;
      })(),
      segSent: (window.__ts.collectParams() || {}).segment,
      genCalls: window.__genCalls || 0 };`);
    check("「去生成」切回会话视图并填好主题，但**不自动发送**（留改参数的机会）",
      went.view === "chat" && went.chatVisible && went.topic.length > 0
        && !went.navOn && !went.refetchVisible && went.genCalls === 0
        // 头部也要跟着回会话那一份：停在「今日选题」就是问题 1 的镜像症状
        && went.headTitle !== "今日选题"
        // 参数条上显示的细分领域 == 将要发出去的那一份
        && went.segSent && went.segPill === went.segSent,
      JSON.stringify(Object.assign({}, went, clicked)));

    // 人在选题视图里点左栏一条会话 —— 必须**切回会话视图**。
    // 头部改成按视图渲染之后，这一条从"体验问题"变成"没有任何反馈"：
    // 不切视图的话，内容还是选题那一面、头部也还写着「今日选题」，
    // 用户只会以为左栏点不动（修复前点得动的是标题，不是内容）。
    await evalIn(`document.getElementById('btn-topics').click(); return true;`);
    await sleep(600);
    const rowClick = await evalIn(`return (function(){
      // 挑一条**已完成**的记录：桩里第一行是 writing，点它走 attach() 分支
      // （读内存里的作业），那不是"回看一条落盘的会话"这条路径。
      var row = document.querySelector('#session-list .sess-item[data-id="s1"]');
      var topic = row ? (row.querySelector('.sess-topic') || {}).textContent : '';
      if (row) row.click();
      return { had: !!row, topic: (topic || '').trim() }; })()`);
    await sleep(700);
    const back = await evalIn(`return { view: document.getElementById('right').dataset.view,
      title: document.getElementById('rh-title').textContent,
      chatShown: !document.getElementById('view-chat').classList.contains('hidden'),
      navOn: document.getElementById('btn-topics').classList.contains('on') };`);
    check("在选题视图里点左栏会话：切回会话视图、头部换成那条会话，左栏高亮也让出去",
      rowClick.had && back.view === "chat" && back.chatShown
        && back.title === rowClick.topic && back.title !== "今日选题" && !back.navOn,
      JSON.stringify({ row: rowClick, back: back }));

    // ── 换包收口（不派发 change 的那条路径）────────────────────────
    // 换包有三条路径：参数条下拉 change、行业包面板点选、fillPackSelect 程序化指包。
    // 后两条**只调 applyPackSwitch、不派发 change** —— 选题视图若只挂 change 监听，
    // 就会停在旧包的卡片上，而「忽略」把旧包条目的 key 写进新包的 ignored.json、
    // 「重抓」替新包发一轮真实抓取（看着 A 做 B）。
    await evalIn(`document.getElementById('btn-topics').click(); return true;`);
    await sleep(700);
    const beforeSwitch = await evalIn(`return (window.__intelReqs || []).length;`);
    await evalIn(`var s = document.getElementById('pack');
      s.value = 'fitment'; window.__ts.applyPackSwitch(); return 1;`);
    await sleep(900);
    const afterSwitch = await evalIn(`return { reqs: (window.__intelReqs || []).length,
      last: ((window.__intelReqs || []).slice(-1)[0] || ''),
      title: document.getElementById('rh-title').textContent,
      sub: document.getElementById('rh-sub').textContent };`);
    check("在选题视图里换包（不派发 change 的那条路径）：重载且拉的是新包，头部跟着走",
      afterSwitch.reqs > beforeSwitch && /pack=fitment/.test(afterSwitch.last)
        && afterSwitch.title === "今日选题" && /全屋定制/.test(afterSwitch.sub),
      JSON.stringify({ before: beforeSwitch, after: afterSwitch }));
    // 还原：这一组跑完整网就结束了，但留下 B 包状态会让下次单独跑的人摸不着头脑。
    await evalIn(`var s = document.getElementById('pack');
      s.value = 'elevator'; window.__ts.applyPackSwitch(); return 1;`);
    await sleep(400);
    // ── 按下「重抓」之后，那颗键自己得报 in-flight（流程线 P1-2）─────
    // 状态行 #topics-note 会被页签/视图藏起来，抓取又没有进度条与步骤树 ——
    // 于是"点了没反应"，用户连点就堆出第二个作业。桩用 __intelPending 把这次
    // 抓取停在 fetching，否则这一支在整网永远量不到。
    await evalIn(`window.__intelPending = true; window.__refreshCalls = 0; return 1;`);
    await evalIn(`document.getElementById('btn-refetch').click(); return 1;`);
    await sleep(450);
    const inflight = await evalIn(`var b = document.getElementById('btn-refetch');
      b.click();                       // 再点一次：禁用了就不该多出第二个作业
      return { disabled: b.disabled, busy: b.classList.contains('busy'),
               aria: b.getAttribute('aria-busy') || '', title: b.title,
               svg: b.querySelectorAll('svg').length,
               calls: window.__refreshCalls || 0 };`);
    await sleep(250);
    await evalIn(`window.__intelPending = false; return 1;`);
    await sleep(1600);
    const restored = await evalIn(`var b = document.getElementById('btn-refetch');
      return { disabled: b.disabled, busy: b.classList.contains('busy'),
               aria: b.getAttribute('aria-busy') || '', title: b.title,
               svg: b.querySelectorAll('svg').length,
               calls: window.__refreshCalls || 0 };`);
    check("抓取进行中：键被禁用 + aria-busy + 呼吸 class，title 说「正在重抓」",
      inflight.disabled === true && inflight.busy === true && inflight.aria === "true"
        && /正在重抓/.test(inflight.title), JSON.stringify(inflight));
    check("图标按钮的画形不许被文字顶掉（进行中/落地后 SVG 都还在）",
      inflight.svg === 1 && restored.svg === 1,
      JSON.stringify({ 抓取中: inflight.svg, 落地后: restored.svg }));
    check("抓取中再点一次不多堆一个作业（二启被禁掉）",
      inflight.calls <= 1, JSON.stringify({ 抓取中: inflight.calls, 落地后: restored.calls }));
    check("落地后把键还给用户：解禁、去 aria-busy、title 回到原文案",
      restored.disabled === false && restored.busy === false && restored.aria === ""
        && !/正在重抓/.test(restored.title) && /立即重抓/.test(restored.title),
      JSON.stringify(restored));
    // ── 抓取「落盘失败」必须说得出那句话（第 27 轮契约线 P1）──────────
    // `_save` 只存在于**作业结果**里：它的意思恰恰是"那份 latest.json 没写成"，
    // 而选题页读的 `/api/intel/today` 就是那份文件。修前两处 `await awaitJob(...)`
    // 把返回值整个丢弃 → 说明行那句「抓取结果没能存下来」从来没有渲染的可能（死分支）。
    await evalIn(`window.__saveFail = true; return 1;`);
    await evalIn(`document.getElementById('btn-refetch').click(); return 1;`);
    await sleep(1800);
    const saveFail = await evalIn(`var n = document.getElementById('topics-note');
      return { note: n ? n.textContent : '', cls: n ? n.className : '' };`);
    check("抓取落盘失败在选题页说得出那句话（作业结果不再被丢弃）",
      /抓取结果没能存下来/.test(saveFail.note) && /cfg-err/.test(saveFail.cls),
      JSON.stringify(saveFail));
    // 正对照：下一次存成功了，这句话必须跟着消失 —— 它不是钉死的警告条。
    await evalIn(`window.__saveFail = false; return 1;`);
    await evalIn(`document.getElementById('btn-refetch').click(); return 1;`);
    await sleep(1800);
    const savedAgain = await evalIn(`var n = document.getElementById('topics-note');
      return n ? n.textContent : '';`);
    check("存成功之后那句落盘警告要消失（不是一条永远挂着的红字）",
      !/没能存下来/.test(savedAgain), savedAgain);
    const cntKbd = await evalIn(`var k = document.getElementById('topics-count');
      return { hidden: k.hidden, text: (k.textContent || '').trim() };`);
    check("真拿到数据之后读数才亮出来（冷启动那个 0 是假陈述）",
      cntKbd.hidden === false && /^[0-9]+$/.test(cntKbd.text) && Number(cntKbd.text) > 0,
      JSON.stringify(cntKbd));
    // ── 按下胶囊之后，pressed 状态必须跟着走（无障碍线 P1）──────────
    // 修前 `aria-pressed` 与 `.on` 烘焙在 renderChips() 的字符串里，而点击只调
    // render()：卡区换了来源、胶囊行还停在上一颗 —— 读屏念的是假状态。
    // 前置条件写进测试：这一组量的是**按来源那一面的来源胶囊**，而默认面是排行。
    // 不先切面的话，探针在 hidden 的面上 focus 一颗按钮 → focus 根本落不上去，
    // 那条「切换筛选不抢走键盘焦点」的断言就变成在量"隐藏元素能不能拿焦点"。
    await evalIn(`var t = document.getElementById('ttab-topics');
      if (t) t.click(); return 1;`);
    await sleep(250);
    const pressed = await evalIn(`return (async () => {
      var pills = [...document.querySelectorAll('#src-chips [data-src]')];
      var target = pills.find(function (p) { return p.dataset.src !== 'all'; });
      if (!target) return { noPill: true };
      target.focus();
      target.click();
      await new Promise(function (r) { setTimeout(r, 250); });
      var now = [...document.querySelectorAll('#src-chips [data-src]')];
      var all = now.find(function (p) { return p.dataset.src === 'all'; });
      return { name: target.dataset.src,
               selfPressed: target.getAttribute('aria-pressed'),
               selfOn: target.classList.contains('on'),
               allPressed: all ? all.getAttribute('aria-pressed') : null,
               focusStillHere: document.activeElement === target,
               otherOn: now.filter(function (p) { return p !== target
                 && p.classList.contains('on'); }).length };
    })();`);
    check("点（或键盘按下）一颗胶囊后，aria-pressed 与高亮跟着换到这颗",
      !pressed.noPill && pressed.selfPressed === "true" && pressed.selfOn === true
        && pressed.allPressed === "false" && pressed.otherOn === 0,
      JSON.stringify(pressed));
    // 原地改属性而不是整行重建：重建会把焦点从刚按下的那颗摘走（键盘用户每次
    // 筛选都要从头 Tab 一遍），这条判据把"焦点还在"一起钉住。
    check("切换筛选不抢走键盘焦点（不整行重建胶囊）",
      pressed.noPill !== true && pressed.focusStillHere === true,
      JSON.stringify(pressed));
    await evalIn(`var all = document.querySelector('#src-chips [data-src="all"]');
      if (all) all.click(); return 1;`);
    await sleep(250);

    // ── 排行视图（需求方案 §3.2 / §3.4 / §3.5）────────────────
    // 这一组守的是"热点排行 + 选题建议"这次落地的全部可观察面。
    // ⚠ 整段 evalIn 里不许出现反引号与反斜杠（模板字符串会把它吃掉一层）。
    await evalIn(`document.getElementById('ttab-rank').click(); return 1;`);
    await sleep(300);
    check("三视图齐了，且默认面是排行（§3.5：排行 / 按来源 / 情报源）",
      firstFace.labels.join("|") === "排行|按来源|情报源"
        && firstFace.on === "ttab-rank"
        && firstFace.shown.length === 1 && firstFace.shown[0] === "topics-rank"
        && firstFace.rows >= 1,
      JSON.stringify(firstFace));
    const rank = await evalIn(`return (function(){
      var on = document.getElementById('view-topics');
      var rows = [...on.querySelectorAll('#rank-root .rank-row')];
      var one = rows[0];
      var sigs = one ? [...one.querySelectorAll('.sig')] : [];
      return {
        rowCount: rows.length,
        titles: rows.map(function (r) { return r.querySelector('.card-title').textContent.trim(); }),
        scores: rows.map(function (r) { return r.querySelector('.rank-score').textContent.trim(); }),
        // 排序轴与"信号不足排最后"是同一条断言的两半：只看数字会漏掉 null 那一档。
        scoreNums: rows.map(function (r) {
          var t = r.querySelector('.rank-score b').textContent.trim();
          return t === '—' ? null : Number(t); }),
        pills: rows.map(function (r) {
          var p = r.querySelector('.rank-pill'); return p ? p.textContent.trim() : ''; }),
        weak: rows.map(function (r) {
          var m = r.querySelector('.t-minor');
          return m ? /信号不足/.test(m.textContent) : false; }),
        // 四根信号条：名次 / 增速 / 持续 / 来源。
        sigLabels: sigs.map(function (s) { return s.querySelector('.sig-lbl').textContent.trim(); }),
        // 四根条要**读得出是四根**：组间缝（列与列）必须比组内缝（标签/轨道/数值）宽，
        // 缝一 equal 就粘成「0.92 名次」这种一个词组（s11-3 的当代形态）。
        // 比**计算值**、不写死 px —— 刻度随规范走，写死数就会在换档那天假红。
        sigGaps: (function () {
          var g = one ? one.querySelector('.rank-signals') : null;
          var s = g ? g.querySelector('.sig') : null;
          if (!g || !s) return { missing: true };
          return { between: Math.round(parseFloat(getComputedStyle(g).columnGap) || 0),
                   inside: Math.round(parseFloat(getComputedStyle(s).gap) || 0) };
        })(),
        sigVals: sigs.map(function (s) { return s.querySelector('.sig-val').textContent.trim(); }),
        // null 分项的轨道必须是**透明**的：留一条灰轨会被读成"量过、是零"。
        sigTracks: sigs.map(function (s) {
          return getComputedStyle(s.querySelector('.sig-track')).backgroundColor; }),
        // 平台真实热度（§1.2 #1「hot 抓了没人接」）
        minor: rows.map(function (r) { return r.querySelector('.t-minor').textContent.trim(); }),
        srcBadges: rows.map(function (r) {
          var b = r.querySelector('.rank-src');
          return { tag: b ? b.tagName : '', txt: b ? b.textContent.trim() : '' }; }),
        chips: [...on.querySelectorAll('#seg-chips .src-pill')].map(function (b) {
          return b.textContent.trim(); }),
        chipNotes: [...on.querySelectorAll('#seg-chips .src-note')].map(function (b) {
          return b.textContent.trim(); }),
        // 三面共用 topics-inner 这一族（同宽同 padding 才叫"统一"），但**只许
        // 有一面可见**：两个都可见时 querySelector('.topics-inner') 拿到第一个，
        // 后面所有拿它当锚点的判据就在量一个隐藏的面（本项目记过的假红/假绿双坑）。
        innerVisible: [...document.querySelectorAll('.topics-inner')].filter(function (n) {
          return n.getBoundingClientRect().width > 0; }).length,
        // 轨道**计算宽度**与 data-w 的比值：这条专治"内联 style 被 CSP 吃掉"
        // —— 属性写对了、像素没长，只有量计算值才分得出来。
        sigWidths: (function () {
          var r0 = on.querySelector('#rank-root .rank-row');
          if (!r0) return [];
          return [...r0.querySelectorAll('.sig')].map(function (s) {
            var f = s.querySelector('.sig-fill').getBoundingClientRect().width;
            var t = s.querySelector('.sig-track').getBoundingClientRect().width;
            return { w: Number(s.dataset.w), ratio: t > 0 ? +(f / t).toFixed(2) : null,
                     track: Math.round(t) }; });
        })(),
        // 四象限标签只有"高热高配"那一档上屏（§3.3 第 6 条的置顶推荐）；
        // 其余三档是分类内部状态，画出来只会让一屏全是标签。
        quadChips: rows.map(function (r) {
          var hit = [...r.querySelectorAll('.res-metric-list .m-chip')]
            .filter(function (c) { return /高热高配/.test(c.textContent); });
          return hit.length ? "高热高配" : ""; }),
        orderLabel: document.getElementById('btn-rank-order').textContent.trim(),
        orderPressed: document.getElementById('btn-rank-order').getAttribute('aria-pressed'),
        // 「今天已有选题卡」落在哪几行：只有后端随这一屏带回 brief_card 的那一行。
        briefChips: rows.map(function (r) {
          return [...r.querySelectorAll('.res-metric-list .m-chip')]
            .some(function (c) { return /今天已有选题卡/.test(c.textContent); }); }),
        // 缓存里那张卡的角度块在**展开之前**就已经在 DOM 里 = 水合在 load() 里做，
        // 不是等用户再点一次「生成选题卡」。
        cachedAngles: rows.map(function (r) {
          return r.querySelectorAll('.rank-brief .brief-angle').length; }),
        meta: document.getElementById('rank-meta').textContent.trim(),
        detailHidden: one ? one.querySelector('.rank-detail').classList.contains('hidden') : null,
        // 筛选轴不混（§3.5）：来源那一排只许出现在按来源面
        srcChipsInRank: on.querySelectorAll('#topics-rank #src-chips .src-pill').length,
        coolingNotes: [...on.querySelectorAll('#src-chips .src-note')].map(function (b) {
          return b.textContent.trim(); })
      };
    })();`);
    check("三个面里只有一面可见（共用 .topics-inner 时，多面同显会让探针量到隐藏面）",
      rank.innerVisible === 1, JSON.stringify({ n: rank.innerVisible }));
    check("排行行数 = 下发的事件数，且默认按热点分降序、信号不足的那行压到最后",
      rank.rowCount === 4 && rank.scoreNums[0] === 82 && rank.scoreNums[1] === 71
        && rank.scoreNums[2] === 12 && rank.scoreNums[3] === null
        && rank.weak[3] === true && !rank.weak.slice(0, 3).some(Boolean),
      JSON.stringify({ n: rank.rowCount, s: rank.scoreNums, w: rank.weak }));
    // 轨道**真的长了宽度**：值 92 的那根要到 ~0.92，null 的那根必须是 0。
    // 这条是防一种很安静的失效 —— 把宽度写成内联 style 时，本页 CSP
    // (style-src 'self') 会整条丢掉它：属性在、值在、屏幕上却是一根等长的空条。
    check("信号条的计算宽度跟着值走（CSP 下内联 style 会被丢，只有量像素抓得到）",
      rank.sigWidths.length === 4
        && rank.sigWidths.every(s => s.track > 0)
        && Math.abs(rank.sigWidths[0].ratio - rank.sigWidths[0].w / 100) <= 0.04
        && Math.abs(rank.sigWidths[3].ratio - rank.sigWidths[3].w / 100) <= 0.04
        && rank.sigWidths[1].w === 0 && rank.sigWidths[1].ratio === 0,
      JSON.stringify(rank.sigWidths));
    check("每行四根信号条：名次 / 增速 / 持续 / 来源（§3.5 的格子）",
      rank.sigLabels.join("|") === "名次|增速|持续|来源",
      JSON.stringify(rank.sigLabels));
    check("四根条之间比条内部更宽（缝 equal 就粘成一个词组，读不出是四根）",
      !rank.sigGaps.missing && rank.sigGaps.between > rank.sigGaps.inside
        && rank.sigGaps.inside > 0,
      JSON.stringify(rank.sigGaps));
    check("算不出的分项印「—」且轨道透明（留灰空轨会被读成量过是零）",
      rank.sigVals.filter(v => v === "—").length >= 1
        && rank.sigVals.some(v => /^[0-9]+$/.test(v))
        && rank.sigTracks.some(t => t === "rgba(0, 0, 0, 0)")
        && rank.sigTracks.some(t => t !== "rgba(0, 0, 0, 0)"),
      JSON.stringify({ vals: rank.sigVals, tracks: rank.sigTracks }));
    check("平台真实热度上了行（§1.2 #1：hot 不再抓了没人接）",
      rank.minor.some(m => /热度 247\.6 万/.test(m))
        && rank.minor.some(m => /热度 9904 万/.test(m))
        && rank.minor.some(m => /热度 —/.test(m)),
      JSON.stringify(rank.minor));
    check("状态药丸按事件给出：爆发中 / 持续在榜 / 衰退中各一，信号不足那行没有药丸",
      rank.pills.join("|") === "持续在榜|爆发中|衰退中|",
      JSON.stringify(rank.pills));
    check("四象限分档上了行：只有「高热高配」那一档进推荐位标签，低档不冒充",
      rank.quadChips[0] === "高热高配"
        && rank.quadChips.filter(c => c === "高热高配").length === 2
        && rank.quadChips[3] === "",
      JSON.stringify(rank.quadChips));
    check("来源数是**徽章不是筛选项**（筛选轴不混，§3.5）",
      rank.srcBadges.every(b => b.tag === "SPAN")
        && rank.srcBadges[0].txt === "2 源",
      JSON.stringify(rank.srcBadges));
    check("排行视图的筛选轴只有赛道；未归属走一句说明而不是一个可点的 chip",
      rank.chips.length >= 3 && rank.chips[0].indexOf("全部赛道") === 0
        && rank.chips.some(c => c.indexOf("维保") === 0)
        && rank.chipNotes.some(n => /未归属 1/.test(n))
        && rank.srcChipsInRank === 0,
      JSON.stringify({ chips: rank.chips, notes: rank.chipNotes,
                       srcInRank: rank.srcChipsInRank }));
    // 「今日跳过」不许说成"已接入，今天没有命中"：前者该去看为什么连着失败，
    // 后者该等平台出数据 —— 两句话的下一步动作相反（这是本仓反复抓过的那类错）。
    // 量的是按来源那一面（筛选行与计数行都在那里），前面 topicsGeo 已经取过。
    check("跳过 / 失败 / 没货 三态在筛选行与计数行各说各话，不合并",
      topicsGeo.chips.some(c => /微博热搜 今日跳过/.test(c))
        && !topicsGeo.chips.some(c => /^微博热搜 0/.test(c))
        && /1 源今日跳过/.test(topicsGeo.metaText)
        && /1 源抓取失败/.test(topicsGeo.metaText),
      JSON.stringify({ chips: topicsGeo.chips, meta: topicsGeo.metaText }));
    // 点一颗赛道 chip：只剩那一赛道的事件，且不可点的说明还在。
    const filtered = await evalIn(`return (async () => {
      var b = [...document.querySelectorAll('#seg-chips [data-seg]')]
        .find(function (x) { return x.dataset.seg && x.dataset.seg !== ''; });
      b.click();
      await new Promise(function (r) { setTimeout(r, 250); });
      var rows = [...document.querySelectorAll('#rank-root .rank-row')];
      return { seg: b.dataset.seg, pressed: b.getAttribute('aria-pressed'),
               n: rows.length,
               segs: rows.map(function (r) { return r.querySelector('.res-metric-list').textContent.trim(); }),
               allPressed: [...document.querySelectorAll('#seg-chips [data-seg]')]
                 .filter(function (x) { return x.getAttribute('aria-pressed') === 'true'; }).length,
               meta: document.getElementById('rank-meta').textContent.trim() };
    })();`);
    check("点赛道 chip 真的筛出该赛道的事件，且选中态只有一颗",
      filtered.pressed === "true" && filtered.allPressed === 1
        && filtered.n >= 1 && filtered.n < 4,
      JSON.stringify(filtered));
    await evalIn(`var a = document.querySelector('#seg-chips [data-seg=""]');
      if (a) a.click(); return 1;`);
    await sleep(250);
    // 排序轴切换（§5 #1：两轴都保留为可切换）。判据是**行序真的变了**，
    // 不是"按钮文字变了" —— 文字换了对调不上任何用户可见的事实。
    const reflow = await evalIn(`return (async () => {
      var before = [...document.querySelectorAll('#rank-root .card-title')].map(function (n) {
        return n.textContent.trim(); });
      var btn = document.getElementById('btn-rank-order');
      var labelBefore = btn.textContent.trim();
      btn.click();
      await new Promise(function (r) { setTimeout(r, 250); });
      var after = [...document.querySelectorAll('#rank-root .card-title')].map(function (n) {
        return n.textContent.trim(); });
      var scores = [...document.querySelectorAll('#rank-root .rank-score b')].map(function (n) {
        return n.textContent.trim(); });
      return { labelBefore: labelBefore, labelAfter: btn.textContent.trim(),
               pressed: btn.getAttribute('aria-pressed'),
               moved: JSON.stringify(before) !== JSON.stringify(after),
               before: before, after: after, scores: scores };
    })();`);
    check("排序轴从热点分切到机会分：行序真的换了，按钮文字与 pressed 跟着走",
      reflow.labelBefore === "按热点分" && reflow.labelAfter === "按机会分"
        && reflow.pressed === "false" && reflow.moved === true,
      JSON.stringify(reflow));
    await evalIn(`document.getElementById('btn-rank-order').click(); return 1;`);
    await sleep(250);
    // 事件详情：各源原始条目 + 出处（§3.5）。这一段必须在**没有选题卡**时也在。
    const detail = await evalIn(`return (async () => {
      var row = document.querySelector('#rank-root .rank-row');
      var btn = row.querySelector('[data-act="detail"]');
      btn.click();
      await new Promise(function (r) { setTimeout(r, 200); });
      var d = row.querySelector('.rank-detail');
      var items = [...d.querySelectorAll('.rank-item')];
      return { hidden: d.classList.contains('hidden'),
               label: btn.textContent.trim(),
               n: items.length,
               srcs: items.map(function (i) { return i.querySelector('.ri-src').textContent.trim(); }),
               links: items.map(function (i) {
                 var a = i.querySelector('.ri-title'); return a ? a.getAttribute('href') : ''; }),
               // 没有出处的条目不许画成"看起来能点"的样子：href="#" 那类回退
               // 会把一次点击变成一次页面内导航（或一颗永远没反应的假链接）。
               kinds: items.map(function (i) {
                 var a = i.querySelector('.ri-title'); return a ? a.tagName : ''; }),
               briefHidden: row.querySelector('.rank-brief').classList.contains('hidden') };
    })();`);
    check("点「看条目」展开各源原始条目（来源名 + 可点出处）",
      detail.hidden === false && detail.label === "收起" && detail.n === 2
        && detail.srcs.join("|") === "下拉词|B站同类"
        && detail.links.some(h => /^https/.test(h)),
      JSON.stringify(detail));
    check("没有出处的条目是纯文本，不是 `href=\"#\"` 那种点了什么都不发生的假链接",
      detail.kinds.filter(k => k === "A").length >= 1
        && detail.kinds.filter(k => k === "SPAN").length >= 1
        && !detail.links.some(h => h === "#" || h === ""),
      JSON.stringify({ kinds: detail.kinds, links: detail.links }));
    // 出处链接的**协议白名单**：桩里第 2 事件的那条条目带的是 javascript:alert(1)
    // （见 verify.js 里那行注释）。详情块是整屏一次性渲染的（展开只是去掉 hidden），
    // 所以这里不用点任何按钮就能扫到全部行 —— 不写这条，"只认 http(s)"就只是一句注释。
    const hrefs = await evalIn(`return (function(){
      var items = [...document.querySelectorAll('#rank-root .rank-item')];
      return { links: [...document.querySelectorAll('#rank-root .rank-item a')]
                 .map(function (a) { return a.getAttribute('href'); }),
               spans: items.filter(function (i) {
                 var t = i.querySelector('.ri-title'); return t && t.tagName === 'SPAN'; }).length,
               items: items.length };
    })();`);
    check("抓来的 url 只认 http(s)：javascript: 这类形状不做成可点链接",
      hrefs.links.length >= 1 && hrefs.links.every(h => /^https?:\/\//.test(h))
        && hrefs.spans >= 2,
      JSON.stringify(hrefs));
    // 选题卡：生成 → 卡片出现（含禁用词摘标与幻觉口径固定一行）。
    const brief = await evalIn(`return (async () => {
      var row = document.querySelector('#rank-root .rank-row');
      var btn = row.querySelector('[data-act="brief"]');
      btn.click();
      await new Promise(function (r) { setTimeout(r, 1200); });
      var box = row.querySelector('.rank-brief');
      var angles = [...box.querySelectorAll('.brief-angle')];
      return { calls: (window.__briefCalls || []).length,
               hidden: box.classList.contains('hidden'),
               angles: angles.length,
               kinds: angles.map(function (a) { return a.querySelector('.ba-head b').textContent.trim(); }),
               hooks: angles.map(function (a) { return a.querySelector('.ba-hook').textContent.trim(); }),
               risks: angles.map(function (a) { return a.querySelector('.ba-meta').textContent.trim(); }),
               titleCount: box.querySelectorAll('.ba-titles p').length,
               useBtns: box.querySelectorAll('[data-act="use"]').length,
               caveat: (box.querySelector('.brief-caveat') || {}).textContent || '',
               btnText: btn.textContent.trim() };
    })();`);
    check("「生成选题卡」发一次请求并渲染角度卡（严格 schema 的形状原样上屏）",
      brief.calls === 1 && brief.hidden === false && brief.angles === 2
        && brief.kinds.join("|") === "解读|反方" && brief.titleCount >= 4
        && brief.useBtns === 2,
      JSON.stringify(brief));
    check("禁用词摘标后写 {{待补}}，风险随卡片说出来（不是把那条角度抹掉）",
      brief.hooks.some(h => /待补/.test(h)) && brief.risks.some(r => /命中禁用词/.test(r)),
      JSON.stringify({ hooks: brief.hooks, risks: brief.risks }));
    check("幻觉口径那一行固定在卡片底部（§3.4）",
      /事实以你核实为准/.test(brief.caveat), JSON.stringify(brief.caveat));
    // 当日重复点击：直接回缓存，**不再起作业**（§3.4 硬约束的可观察面）。
    const cached = await evalIn(`return (async () => {
      window.__briefCached = true;
      var before = (window.__briefCalls || []).length;
      var row = document.querySelector('#rank-root .rank-row');
      var box = row.querySelector('.rank-brief');
      box.classList.add('hidden');
      row.querySelector('[data-act="brief"]').click();
      await new Promise(function (r) { setTimeout(r, 700); });
      return { before: before, after: (window.__briefCalls || []).length,
               shown: !row.querySelector('.rank-brief').classList.contains('hidden'),
               angles: row.querySelectorAll('.brief-angle').length };
    })();`);
    check("当日重复点击走缓存：再点一次发的是请求但不扣第二次模型调用（不起作业）",
      cached.after === cached.before + 1 && cached.shown === true
        && cached.angles === 2,
      JSON.stringify(cached));
    await evalIn(`window.__briefCached = false; return 1;`);
    // 「用这个角度」：只回填文本，参数面板一行都不动（§5 #5 的口径）。
    const useAngle = await evalIn(`return (async () => {
      var segBefore = (document.querySelector('[data-key="segment"] .select-btn .sel-text') || {}).textContent || '';
      var topicBefore = document.getElementById('topic').value;
      var genBefore = window.__genCalls || 0;
      var row = document.querySelector('#rank-root .rank-row');
      row.querySelector('[data-act="use"]').click();
      await new Promise(function (r) { setTimeout(r, 250); });
      return { view: document.getElementById('right').dataset.view,
               topic: document.getElementById('topic').value,
               topicChanged: document.getElementById('topic').value !== topicBefore,
               segAfter: (document.querySelector('[data-key="segment"] .select-btn .sel-text') || {}).textContent || '',
               segSame: segBefore === ((document.querySelector('[data-key="segment"] .select-btn .sel-text') || {}).textContent || ''),
               genCalls: (window.__genCalls || 0) - genBefore };
    })();`);
    check("「用这个角度」回填标题与钩子句、切回会话面，但不自动发送",
      useAngle.view === "chat" && useAngle.topicChanged === true
        && useAngle.topic.indexOf("电梯应急更换程序指引：这条到底在说什么") === 0
        && /第一反应多半是错的/.test(useAngle.topic)
        && useAngle.genCalls === 0,
      JSON.stringify(useAngle));
    // 忽略清单（§3.5）：点开是列表、能恢复。
    const igList = await evalIn(`return (async () => {
      var box = document.getElementById('topics-ignored');
      document.getElementById('btn-ignored').click();
      await new Promise(function (r) { setTimeout(r, 200); });
      var rows = [...box.querySelectorAll('.ig-row')];
      return { hidden: box.classList.contains('hidden'),
               expanded: document.getElementById('btn-ignored').getAttribute('aria-expanded'),
               n: rows.length,
               titles: rows.map(function (r) { return r.querySelector('.ig-title').textContent.trim(); }),
               buried: rows.map(function (r) { return /已沉底/.test(r.textContent); }),
               hasRestore: rows.every(function (r) { return !!r.querySelector('[data-unignore]'); }) };
    })();`);
    check("「已忽略 N」点开是可恢复的清单，沉底的那条标出来",
      igList.hidden === false && igList.expanded === "true" && igList.n === 2
        && igList.buried.filter(Boolean).length === 1 && igList.hasRestore === true,
      JSON.stringify(igList));
    const unignored = await evalIn(`return (async () => {
      var before = document.querySelectorAll('#topics-ignored .ig-row').length;
      document.querySelector('#topics-ignored [data-unignore]').click();
      await new Promise(function (r) { setTimeout(r, 900); });
      return { before: before, keys: (window.__unignoredKeys || []).length,
               after: document.querySelectorAll('#topics-ignored .ig-row').length };
    })();`);
    check("点「恢复」真的发 unignore 请求（清单不是只读的摆设）",
      unignored.keys >= 1, JSON.stringify(unignored));
    // 源表的「近 7 天」列与「今日跳过」那一行。
    const srcHealth = await evalIn(`return (function(){
      var rows = [...document.querySelectorAll('#sources-table tr')].slice(1);
      var head = [...document.querySelectorAll('#sources-table tr')[0].children].map(function (t) {
        return t.textContent.trim(); });
      var i = head.indexOf('近 7 天');
      var st = head.indexOf('接入');
      var note = head.indexOf('说明');
      var cell = function (r, k) { var c = r.children[k]; return c ? c.textContent.trim() : ''; };
      return { head: head.join("|"),
               health: rows.map(function (r) { return cell(r, i); }),
               states: rows.map(function (r) { return cell(r, st); }),
               notes: rows.map(function (r) { return cell(r, note); }) };
    })();`);
    // 「近 7 天」这一列的三态：试过的行给 `成/总`，**一次都没试过**的行给「—」。
    // 0/0 不是"没试过"而是"试了七次全空手"，两句话的下一步动作相反 ——
    // 本仓的口径一直是 `null` 画「—」、`0` 才是结论（新装用户第一屏全是 0/0
    // 那张脸，正是这条口径被写歪的样子）。
    const neverRow = srcHealth.states.findIndex(s => /未启用|未接入/.test(s));
    check("情报源表有「近 7 天」这一列：试过的行给 成/总，没试过的行给「—」（不许 0/0）",
      /近 7 天/.test(srcHealth.head)
        && srcHealth.health.length >= 5
        && srcHealth.health.every(h => h === '—' || /^\d+\/\d+$/.test(h))
        && !srcHealth.health.some(h => h === '0/0')
        && neverRow >= 0 && srcHealth.health[neverRow] === '—',
      JSON.stringify(Object.assign({ neverRow: neverRow }, srcHealth)));
    check("连续失败到闸值的源标成「今日跳过」并带原因（不冒充「已接入」）",
      srcHealth.states.some(s => /今日跳过/.test(s))
        && srcHealth.notes.some(n => /连续 3 次抓取失败，今天先跳过/.test(n)),
      JSON.stringify(srcHealth));
    // 排行那一面点「忽略」= 忽略**这件事**：事件在几个源上同时在榜，
    // 只记一条成员键的话，另外几条明天照样回来（用户看到的是"忽略没用"）。
    const ignoreEvent = await evalIn(`return (async () => {
      document.getElementById('ttab-rank').click();
      await new Promise(function (r) { setTimeout(r, 250); });
      var before = (window.__ignoredKeys || []).length;
      var row = document.querySelector('#rank-root .rank-row');
      var keys = [...row.querySelectorAll('.rank-item')].map(function (n) { return n.dataset.key; });
      row.querySelector('[data-act="ignore"]').click();
      await new Promise(function (r) { setTimeout(r, 800); });
      var after = (window.__ignoredKeys || []);
      return { before: before, added: after.length - before,
               keys: keys, sent: after.slice(before),
               stillThere: !!document.querySelector(
                 '#rank-root .rank-row[data-key="' + row.dataset.key + '"]') };
    })();`);
    check("排行行点「忽略」把这件事的**全部来源条目**一起记掉（不是只记第一条）",
      ignoreEvent.added === ignoreEvent.keys.length && ignoreEvent.added >= 2
        && JSON.stringify(ignoreEvent.sent.sort()) === JSON.stringify(ignoreEvent.keys.slice().sort())
        && ignoreEvent.stillThere === false,
      JSON.stringify(ignoreEvent));
    await evalIn(`window.__ignoredKeys = []; return 1;`);
    // 盘上已有的选题卡随 `/api/intel/today` 水合：桩里第四颗事件带 `brief_card`，
    // 它一进视图就该有卡（展开看得到角度、行上有「今天已有选题卡」），
    // 而不是退回「还没有这张卡的选题角度」—— 那句话与后端 `state: cached` 直接矛盾。
    check("盘上今天已有的选题卡随这一屏水合：行上有标记，卡片内容已经在 DOM 里",
      rank.briefChips.join(",") === "false,false,false,true"
        && rank.cachedAngles[3] === 2 && rank.cachedAngles[0] === 0,
      JSON.stringify({ chips: rank.briefChips, angles: rank.cachedAngles }));
    // 沉底（连续忽略满 3 天）在**排行这一面**也要沉到底：后端 `today()` 把 buried
    // 排到最后、注释写着"顺序只让后端排一次"，而这一面自己按热点分重排 ——
    // 不接住 buried，同一件事在按来源那面沉下去了，在默认面仍稳在榜首。
    const buriedLast = await evalIn(`return (async () => {
      window.__buryTop = true;
      document.getElementById('btn-topics').click();
      await new Promise(function (r) { setTimeout(r, 700); });
      var rows = [...document.querySelectorAll('#rank-root .rank-row')];
      var scores = rows.map(function (r) {
        var t = r.querySelector('.rank-score b').textContent.trim();
        return t === '—' ? null : Number(t); });
      // 上面「点「看条目」」那一步展开过的那一行，在这次**整屏重渲**之后：
      // 展开状态记在模块变量里（该保持），按钮的字也必须跟着走（该是「收起」）。
      var opened = rows.find(function (r) { return r.dataset.key === 'ev:0a1b2c3d4e5f'; });
      var probe = { label: '', detailHidden: null };
      if (opened) {
        probe.label = opened.querySelector('[data-act="detail"]').textContent.trim();
        probe.detailHidden = opened.querySelector('.rank-detail').classList.contains('hidden');
      }
      window.__buryTop = false;
      return { scores: scores, opened: probe };
    })();`);
    check("沉底的事件在排行面也压到最后（压过热点分，也压过「算不出」那一档）",
      buriedLast.scores[buriedLast.scores.length - 1] === 82
        && buriedLast.scores[0] === 71,
      JSON.stringify(buriedLast));
    check("整屏重渲之后，展开着的那一行按钮字跟着改成「收起」（详情也仍然开着）",
      buriedLast.opened.label === "收起" && buriedLast.opened.detailHidden === false,
      JSON.stringify(buriedLast.opened));
    // 「已忽略 N」的禁用条件过去认 N（今天点了几次），不是清单里有没有东西 ——
    // 于是昨天点下的那几条，其**唯一恢复入口**在今天恰好打不开。
    const ignoredBtn = await evalIn(`return (async () => {
      window.__noIgnoredToday = true;
      document.getElementById('btn-topics').click();
      await new Promise(function (r) { setTimeout(r, 700); });
      var btn = document.getElementById('btn-ignored');
      var out = { text: btn.textContent.trim(), disabled: btn.disabled };
      btn.click();
      await new Promise(function (r) { setTimeout(r, 250); });
      out.rows = document.querySelectorAll('#topics-ignored .ig-row').length;
      out.head = (document.querySelector('#topics-ignored .ig-head') || {}).textContent || '';
      btn.click();
      window.__noIgnoredToday = false;
      return out;
    })();`);
    check("今天没点过忽略、清单里还有历史条目：按钮仍可点，头部不把话说成「今天」",
      ignoredBtn.disabled === false && /已忽略 0/.test(ignoredBtn.text)
        && ignoredBtn.rows >= 1 && /之前点下的/.test(ignoredBtn.head),
      JSON.stringify(ignoredBtn));
    // 旧产物降级：桩把 events 抹掉，排行必须按条目池出行为 §3.1（分项全「—」）。
    const degraded = await evalIn(`return (async () => {
      window.__stripEvents = true;
      document.getElementById('btn-topics').click();
      await new Promise(function (r) { setTimeout(r, 700); });
      var on = document.getElementById('view-topics');
      var rows = [...on.querySelectorAll('#rank-root .rank-row')];
      var vals = rows.length ? [...rows[0].querySelectorAll('.sig-val')].map(function (n) {
        return n.textContent.trim(); }) : [];
      // 诊断用：把"池子里有几条 / 排行有几行 / 选中赛道几颗"一起端出来 ——
      // 0 行的红有两种成因（桩没回 items、渲染层没接住空 events），不带上这些
      // 就分不出来，而分不出来就会去改判据而不是改 bug。
      var shape = { pillsOn: document.querySelectorAll('#seg-chips .src-pill.on').length };
      shape.meta = document.getElementById('rank-meta').textContent.trim();
      shape.rootLen = (document.getElementById('rank-root').innerHTML || '').length;
      window.__stripEvents = false;
      return { n: rows.length,
               scores: rows.slice(0, 2).map(function (r) {
                 return r.querySelector('.rank-score b').textContent.trim(); }),
               vals: vals,
               empty: !!on.querySelector('.topics-empty'),
               shape: shape,
               note: (document.getElementById('rank-note') || {}).textContent || '' };
    })();`);
    check("旧 latest.json（没有 events 键）→ 排行退化成条目行，分项一律「—」不编 0",
      degraded.n >= 4 && degraded.scores.every(s => s === "—")
        && degraded.vals.join("|") === "—|—|—|—",
      JSON.stringify(degraded));
    // 排行视图的翻页同样是纯前端（不触发重抓）。
    const rankPaging = await evalIn(`return (async () => {
      var before = (window.__refreshCalls || 0);
      var btn = document.getElementById('btn-rank-more');
      var disabled = btn.disabled;
      btn.click();
      await new Promise(function (r) { setTimeout(r, 200); });
      return { disabled: disabled, text: btn.textContent.trim(),
               calls: (window.__refreshCalls || 0) - before };
    })();`);
    check("排行的「换一批」是池内翻页：不发重抓请求（桩里 4 条事件一屏放得下，故为禁用态）",
      rankPaging.calls === 0
        && (rankPaging.disabled === true || /^第 \d+\/\d+ 批$/.test(rankPaging.text)),
      JSON.stringify(rankPaging));
};
