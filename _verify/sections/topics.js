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
      var chips = [...on.querySelectorAll('#src-chips .src-pill, #src-chips .m-chip')].map(c => c.textContent.trim());
      // 可筛的那几颗现在是**胶囊按钮**（.src-pill），只做解释的两类仍是 .m-chip。
      // 分界是"能不能点"，所以两族分开数：混成一族的话用户会去点一颗不会动的标签。
      var pillsOn = [...on.querySelectorAll('#src-chips .src-pill.on')].map(b => b.textContent.trim());
      // 胶囊一族同型：与首页起手胶囊（.sample-card）逐计算值比。
      // ⚠ 必须拿**未选中**那颗比 —— .src-pill.on 换的是底色与字色，那是选中态
      //   该有的差别（规范 §3「选中态允许平涂 --ink」），拿它比会误报成"两种画法"。
      var pillTwin = (function () {
        var p = on.querySelector('#src-chips .src-pill:not(.on)');
        var s = document.querySelector('#empty-samples .sample-card');
        if (!p || !s) return { missing: true, hasPill: !!p, hasSample: !!s };
        var a = getComputedStyle(p), b = getComputedStyle(s), diff = [];
        ['height','fontSize','fontWeight','color','backgroundColor','borderTopLeftRadius',
         'paddingTop','paddingRight','paddingBottom','paddingLeft','lineHeight',
         'boxShadow','whiteSpace','transitionProperty'].forEach(function (k) {
           if (a[k] !== b[k]) diff.push(k + ': ' + a[k] + ' vs ' + b[k]);
         });
        return { diff: diff, h: Math.round(p.getBoundingClientRect().height), hpx: a.height };
      })();
      // 三条左边界：页签文字 / 筛选胶囊 / 组标题。页签条与阅读面同档宽（--w-stream），
      // 且 .res-tab 自己的 padding-inline(16) 正好等于 .topics-inner 的 16 ——
      // 少一层 padding 就能对齐，多写一层就会在窄窗差出 8px。
      var edges = (function () {
        var left = function (n) { return n ? +n.getBoundingClientRect().left.toFixed(1) : null; };
        var bar = on.querySelector('.topics-tabs');
        var inner = on.querySelector('.topics-inner');
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
      var srcHead = srcRows[0] ? [...srcRows[0].children].map(t => t.textContent.trim()) : [];
      var accCol = srcRows.length > 1 ? [...srcRows[1].children].map(t => t.textContent.trim()) : [];
      var inner = on.querySelector('.topics-inner').getBoundingClientRect();
      // 源表**每一行**的「接入」列与「说明」列 —— 抓取失败的源必须在这里被认出来，
      // 而不是照 state 显示成"已接入"（后端 today() 给失败源的 state 就是 "ok"）。
      // 表体列数**比表头少**时（把「接入」与「上次抓取」合成一列那类回归），
      // 直接取 children[6] 会拿到 undefined → 整段探针抛 TypeError →
      // 变异检验把它判成「工具错」而不是一条真红（实测就是这样漏掉一次归因）。
      // 所以要**越界取空串**：让该红的断言红，而不是让探针崩。
      var cell = function (r, i) { var c = r.children[i]; return c ? c.textContent.trim() : ''; };
      var srcRowsBody = [...srcRows].slice(1);
      var srcStates = srcRowsBody.map(function (r) { return cell(r, 3); });
      var srcNotes = srcRowsBody.map(function (r) { return cell(r, 6); });
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
        pillTwin: pillTwin,
        selState: selState,
        edges: edges,
        head: head,
        packLabel: packLabel,
        // 界面**实际拿哪个包去要的数据**：不读它就看不出"请求带错包"这类缺陷。
        intelReqs: (window.__intelReqs || []).slice(),
        // 组与组之间必须有呼吸：#topics-root 挂的是现成的 .kb-groups（gap --s4）。
      // 不挂就是 0px —— 桩数据一屏只出一组，所以这条只有真数据才看得见。
      groupsGap: getComputedStyle(on.querySelector('#topics-root')).rowGap,
      // 两两一行的四格指标：**组间缝必须大于组内缝**，否则
      // 「0.30 供给 S」会被读成一个词组（三元组之间也是 8px）。
      sigGaps: (function () {
        var g = on.querySelector('.sig-grid');
        if (!g || g.children.length < 6) return null;
        var R = function (i) { return g.children[i].getBoundingClientRect(); };
        return { 组内: +(R(1).left - R(0).right).toFixed(1),
                 组间: +(R(3).left - R(2).right).toFixed(1) };
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
      oppCells: (function () {
        var out = [];
        document.querySelectorAll('.sig-grid').forEach(function (g) {
          for (var i = 0; i + 2 < g.children.length; i += 3) {
            if (g.children[i].textContent.trim() !== '机会分') continue;
            out.push({ text: g.children[i + 2].textContent.trim(),
                       w: g.children[i + 1].firstChild.getAttribute('data-w') });
          }
        });
        return out;
      })(),
      // 结论的唯一真通道是 flags 那排标记（app/intel.py 的 _item_flags）。
      // 条目级 it.warn 那条渲染支已删 —— 引擎从不发这个键，桩也不许再喂它。
      warnChips: document.querySelectorAll('.page-card .m-chip.warn').length,
      cardTailText: Array.prototype.slice.call(document.querySelectorAll('.page-card .topic-quote'))
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
        srcStates: srcStates, srcNotes: srcNotes, declCount: decl.length,
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
        sigValues: (function () {
          // ⚠ 要**汇总所有卡**：第一张卡的 D/S/E 恰好都有值，只看它测不到
          //   「算不出显示 —」这件事（桩里 E 为 null 的是第二张卡）。
          var out = [];
          on.querySelectorAll('.sig-grid').forEach(function (g) {
            for (var i = 2; i < g.children.length; i += 3) {
              out.push(g.children[i].textContent.trim());
            }
          });
          return out;
        })(),
        // 信号条拿到 --w 了没有：宽度是**数据驱动**的，必须走 CSSOM 写入。
        // ⚠ 这里量的是"写进去了"，不是"有没有 style 属性" —— CSSOM 的 setProperty
        //   本身就会生成 style 属性，那是规范允许的做法（§2.4 明写"数据驱动的宽度
        //   一律走 CSSOM"）。真正不许的是**源码里的 style= 字面量**，那条由本文件
        //   既有的 securitypolicyviolation 监听覆盖（CSP 无 'unsafe-inline' 会拦下它）。
        barsWithW: [...on.querySelectorAll('.sig-track > i')]
          .filter(i => i.style.getPropertyValue('--w') !== '').length,
        barCount: on.querySelectorAll('.sig-track > i').length,
        // 四格指标**两两一行**：量轨道顶边有几种取值（几种=几行），不写死像素。
        // 原来四格四行，一张卡光这一段就吃掉 ~120px；改两行是"卡内布局"这一项
        // 唯一可量的部分 —— 只改 CSS 不改 DOM，所以三元组序列那条判据仍然成立。
        sigRows: (function () {
          var g = on.querySelector('.sig-grid');
          if (!g) return null;
          var tops = {};
          for (var i = 1; i < g.children.length; i += 3) {
            tops[Math.round(g.children[i].getBoundingClientRect().top)] = 1;
          }
          return Object.keys(tops).length;
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
    check("筛选胶囊与首页起手胶囊逐项同型、且落在 --h-sm 控件档（一族只有一种画法）",
      !topicsGeo.pillTwin.missing && topicsGeo.pillTwin.diff.length === 0
        && topicsGeo.pillTwin.hpx === topicsGeo.hSm,
      JSON.stringify({ twin: topicsGeo.pillTwin, hSm: topicsGeo.hSm }));
    check("页签条与阅读面同档宽，页签文字 / 筛选胶囊 / 组标题三条左边界重合",
      Math.abs(topicsGeo.edges.条宽 - topicsGeo.edges.面宽) <= 1
        && Math.abs(topicsGeo.edges.条宽 - streamW) <= 1
        && Math.abs(topicsGeo.edges.页签 - topicsGeo.edges.胶囊) <= 0.6
        && Math.abs(topicsGeo.edges.胶囊 - topicsGeo.edges.组标题) <= 0.6,
      JSON.stringify({ e: topicsGeo.edges, stream: streamW }));
    check("左栏 nav 键帽计数 = 今日条目总数（不是「未读数」——那要多存一个状态）",
      topicsGeo.navCount === "8" && /共 8 条/.test(topicsGeo.metaText),
      JSON.stringify({ nav: topicsGeo.navCount, meta: topicsGeo.metaText }));
    check("算不出的 D/S/E 显示「—」而不是 0（0 与「没数据」结论相反）",
      Array.isArray(topicsGeo.sigValues) && topicsGeo.sigValues.length >= 4
        && topicsGeo.sigValues.some(v => v === "—")
        && !topicsGeo.sigValues.some(v => /^0(\.00)?$/.test(v)),
      JSON.stringify({ vals: topicsGeo.sigValues }));
    check("选题阅读面与正文同档宽（--w-stream，§2.8 的列宽统一口径）",
      Math.abs(topicsGeo.topicsWidth - streamW) <= 1,
      JSON.stringify({ topics: topicsGeo.topicsWidth, stream: streamW }));
    check("信号条宽度走 CSSOM 写入（源码内联 style 由既有的 CSP 违规监听守着）",
      topicsGeo.barCount > 0 && topicsGeo.barsWithW === topicsGeo.barCount,
      JSON.stringify({ bars: topicsGeo.barCount, withW: topicsGeo.barsWithW }));
    check("卡内四格指标两两一行（D+S / 机会分+E），压掉原来四行占掉的垂直空间",
      topicsGeo.sigRows === 2, JSON.stringify({ rows: topicsGeo.sigRows }));
    // 两两一行的代价必须被买掉：组间缝 > 组内缝，否则四格读成一串没有边界的流水。
    check("四格指标的组间缝大于组内缝（否则「0.30 供给 S」读成一个词组）",
      !!topicsGeo.sigGaps && topicsGeo.sigGaps.组间 > topicsGeo.sigGaps.组内,
      JSON.stringify(topicsGeo.sigGaps));
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
    check("机会分的条宽与数字同源（78 → 条 78%，不是先除以 100 再四舍五入）",
      topicsGeo.oppCells.every(function (c) {
        return c.text === '—' || parseInt(c.w, 10) === parseInt(c.text, 10); }),
      JSON.stringify(topicsGeo.oppCells));
    check("「算不出机会分」这类结论走 flags 那一排标记（引擎真发的通道）",
      topicsGeo.warnChips >= 1, JSON.stringify({ warnChips: topicsGeo.warnChips }));
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
};
