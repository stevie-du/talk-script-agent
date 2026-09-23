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
    await evalIn(`window.__refreshCalls = 0; window.__ignoredKeys = [];
      document.getElementById('btn-topics').click(); return true;`);
    // ⚠ 原来是固定 sleep(600)：整网里跑到这一组时页面早就"热"了（模块已加载、
    //   情报已取过），600ms 绰绰有余；而**单独跑这一组**时是首次进入选题视图，
    //   要等动态 import + intel 请求，600ms 不够 —— chip 里会整整少一组
    //   （「未接入 1」），那条断言恒红。恒红的断言在变异检验里 = 假绿。
    //   改成"等到真的渲染出来"，两边都成立，整网侧还省掉多余的等待。
    let prev = "";
    let stable = 0;
    for (let i = 0; i < 40 && stable < 3; i++) {
      const snap = await evalIn(`var on = document.getElementById('view-topics');
        return on && !on.classList.contains('hidden')
          && on.querySelectorAll('#topics-root .page-card').length > 0
          ? [...document.querySelectorAll('#src-chips .m-chip')]
              .map(function (c) { return c.textContent.trim(); }).join('|')
          : "";`);
      stable = (snap && snap === prev) ? stable + 1 : 0;
      prev = snap;
      if (stable < 3) await sleep(100);
    }
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
      var chips = [...on.querySelectorAll('#src-chips .m-chip')].map(c => c.textContent.trim());
      var srcRows = on.querySelectorAll('#sources-table tr');
      var srcHead = srcRows[0] ? [...srcRows[0].children].map(t => t.textContent.trim()) : [];
      var accCol = srcRows.length > 1 ? [...srcRows[1].children].map(t => t.textContent.trim()) : [];
      var inner = on.querySelector('.topics-inner').getBoundingClientRect();
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
        navCount: document.getElementById('topics-count').textContent,
        metaText: document.getElementById('topics-meta').textContent,
        srcHead: srcHead, accCol: accCol, srcRowCount: srcRows.length - 1,
        // ⚠ 表体列数必须等于表头列数：少一列时**表头看着完全正常**，
        //   只有数据行整体左移（「今日命中」跑到「上次抓取」下面）。
        //   实测就是这样抓到的：表体里那个 last 变量写了却没进模板。
        srcBodyCols: srcRows.length > 1 ? [...srcRows[1].children].length : 0,
        topicsWidth: +inner.width.toFixed(1),
        dashForUncomputable: /—/.test(on.textContent),
        // 信号条拿到 --w 了没有：宽度是**数据驱动**的，必须走 CSSOM 写入。
        // ⚠ 这里量的是"写进去了"，不是"有没有 style 属性" —— CSSOM 的 setProperty
        //   本身就会生成 style 属性，那是规范允许的做法（§2.4 明写"数据驱动的宽度
        //   一律走 CSSOM"）。真正不许的是**源码里的 style= 字面量**，那条由本文件
        //   既有的 securitypolicyviolation 监听覆盖（CSP 无 'unsafe-inline' 会拦下它）。
        barsWithW: [...on.querySelectorAll('.sig-track > i')]
          .filter(i => i.style.getPropertyValue('--w') !== '').length,
        barCount: on.querySelectorAll('.sig-track > i').length,
      }; })()`);
    check("点左栏「今日选题」切到选题视图（左栏与会话列表常驻，不跳页、不开二级窗）",
      topicsGeo.topicsVisible && topicsGeo.chatHidden && topicsGeo.navOn
        && topicsGeo.rightView === "topics",
      JSON.stringify({ v: topicsGeo.topicsVisible, c: topicsGeo.chatHidden,
                       n: topicsGeo.navOn, r: topicsGeo.rightView }));
    check("头部那两颗情报动作只在选题视图出现（用 #right[data-view] 控显隐）",
      topicsGeo.refreshBtnVisible, String(topicsGeo.refreshBtnVisible));
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
    check("筛选行 chip 含「全部 N」+ 各源计数 + 「未接入 N」（只列有货的源）",
      topicsGeo.chips.some(c => c.indexOf("全部 8") === 0)
        && topicsGeo.chips.some(c => c.indexOf("下拉词 4") === 0)
        && topicsGeo.chips.some(c => c.indexOf("未接入 1") === 0),
      JSON.stringify(topicsGeo.chips));
    check("左栏 nav 键帽计数 = 今日条目总数（不是「未读数」——那要多存一个状态）",
      topicsGeo.navCount === "8" && /共 8 条/.test(topicsGeo.metaText),
      JSON.stringify({ nav: topicsGeo.navCount, meta: topicsGeo.metaText }));
    check("算不出的 D/S/E 显示「—」而不是 0（0 与「没数据」结论相反）",
      topicsGeo.dashForUncomputable, String(topicsGeo.dashForUncomputable));
    check("选题阅读面与正文同档宽（--w-stream，§2.8 的列宽统一口径）",
      Math.abs(topicsGeo.topicsWidth - streamW) <= 1,
      JSON.stringify({ topics: topicsGeo.topicsWidth, stream: streamW }));
    check("信号条宽度走 CSSOM 写入（源码内联 style 由既有的 CSP 违规监听守着）",
      topicsGeo.barCount > 0 && topicsGeo.barsWithW === topicsGeo.barCount,
      JSON.stringify({ bars: topicsGeo.barCount, withW: topicsGeo.barsWithW }));
    check("情报源表：`接入` 与 `上次抓取` 是两根正交列，且表体列数与表头一致",
      topicsGeo.srcHead.indexOf("接入") >= 0 && topicsGeo.srcHead.indexOf("上次抓取") >= 0
        && topicsGeo.srcRowCount === 4
        && topicsGeo.srcBodyCols === topicsGeo.srcHead.length,
      JSON.stringify({ head: topicsGeo.srcHead, rows: topicsGeo.srcRowCount,
                       bodyCols: topicsGeo.srcBodyCols }));

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
      // ⚠ 判据是"那张卡真的从 DOM 里没了"，不是只看 data.items.length ——
      //   只过滤 data.items 而不过滤 g.items 时，计数变了、卡还在，
      //   后者才是用户看到的东西（实测这条弱断言漏过了一次）。
      gone: ![...document.querySelectorAll('#topics-root .page-card .card-title')]
              .some(t => t.textContent === window.__firstTitle),
      navCount: document.getElementById('topics-count').textContent };`);
    check("「忽略」落一条记录并本地摘掉该卡（只影响今天，明天同题还会回来）",
      ignored.keys.length === 1 && ignored.gone && ignored.navCount === "7",
      JSON.stringify(ignored));

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
      genCalls: window.__genCalls || 0 };`);
    check("「去生成」切回会话视图并填好主题，但**不自动发送**（留改参数的机会）",
      went.view === "chat" && went.chatVisible && went.topic.length > 0
        && !went.navOn && !went.refetchVisible && went.genCalls === 0,
      JSON.stringify(Object.assign({}, went, clicked)));
};
