# TalkScript UI / 布局规则（细则，从 MEMORY.md 拆出）

> 本文件是权威细则。MEMORY.md 只留索引。**改 UI 前先读本文件。**

## 〇·五、绑定函数一律用 `util.bindOnce`（P2-2）

```js
export const bindXxx = bindOnce(function bindXxx() { … });
```

- **五个绑定函数全都要包**：`bindShell` / `bindSettings` / `bindOverlays` /
  `bindSessionList` / `T.bindScrollPin`。新增绑定函数照此办理。
- **不要再写 `fn._bound` 或 `el._bound`** —— 修复前有这三种写法并存，
  正是「同一个口径抄了几份，下次只修一半」。
- **为什么需要**：绑定里混着 `addEventListener`（**会重复挂**，一次点击触发两次）
  与 `onclick =`（天然幂等）。危害集中在用 `addEventListener` 的那三个。
- **验证入口**：`window.__ts.rebind()` 把五个函数再跑一遍；
  `_verify/verify.js` 包了 `EventTarget.prototype.addEventListener` 计数，
  断言「rebind 前后一个不多」。**漏掉任何一处的 bindOnce 都会报红。**
- `bindOverlays` 只用 `onclick =`，重复挂无害 → 变异检验抓不到它（M3 0 红），
  它的包装是**统一规则 + 将来防呆**，不是当前可观测的缺陷。

## 〇、CSP 约束（P2-5，改渲染层前必读）

页面带生产 CSP（引擎中间件下发，源头是 `app/server.py` 的 `CSP` 常量），
**`style-src 'self'` 不含 `'unsafe-inline'`**。因此：

- **模板串里不许写 `style="..."`** —— 会被 CSP **静默忽略**（列宽失效但界面不报错）。
  列宽/等宽单元格一律用 class：`.col-time`(86) / `.col-item`(160) / `.col-size`(90) /
  `.col-role`(110) / `.cell-mono`，都定义在 `styles.css` 的「表格」段。
- **`el.style.setProperty(...)` 不受影响**（走 CSSOM）—— 动态样式照常用。
  实测：5 种注入向量全被拦、CSSOM 放行，`node _verify/csp-probe.js` 可复现。
- 同样不许：内联 `<script>`、内联事件处理器（`onclick=` 之类）、`eval` / `new Function`、
  动态 `createElement('script')`、`<iframe>` / Worker。
- 新增外部资源要同时改 `CSP` 常量（比如加字体要加 `font-src`）。
- `_verify/verify.js` 全程监听 `securitypolicyviolation`，**违规就报红**。
- **查模板串里的 HTML 属性要两种引号都查**（`style="` 和 `style='`）——
  踩过一次：只查双引号，漏了 `settings.js` 里两处单引号写的 `style='width:90px'`。

## 一、左栏（已定，别改回去）

- **顺序**：品牌 → 新建对话 → 分隔线 → 搜索会话 → 分组标签 → 列表。搜索过滤列表，两者必须相邻。
- **两条线不要合并**：窗口头部线（`.left-head`/`.right-head` 各 48px + border-bottom，合起来横贯全窗）；
  侧栏分隔线（`.left-sep`，用 `margin:0 calc(-1*var(--s3))` 抵消 `.left-top` 内边距，否则断线）。
- **两个通栏控件同高同圆角**：`--h-ctl`(32px) / `--r-ctl`(9px)。搜索框是输入控件，不要用 `--h-btn`/`--r-sm`。
- **整栏统一 16px（`--s4`），六段都是 16，别引入第二种节奏**。用户原话：「左侧最好统一16px的间距」。
  `.left-top{padding:16px 12px;gap:16px}`、`.left-foot{padding:16px 12px 10px}`、`.left-scroll{padding:0 12px 16px}`。
  - 搜索框 `margin:0`，上下间距全交给 gap/padding。搜索框→下方内容的 16px **只有一个来源**：
    `.left-top` 的 `padding-bottom`。所以第一个分组标签必须
    `.group-lbl:first-child{margin-top:0;padding-top:0}` —— 只归零 margin 不够，
    padding-top 会让非滚动态多 12px、滚动态只多 8px，同位置出现两个间距。
    **通用规律**：滚动容器上边界间距 + 首个子元素 padding 会叠成两个视觉间距；
    凡"滚动前后间距会变"都按这个拆。
  - **项间距一律用容器 `gap`，不用子项 `margin`**：margin 在容器边界也会插一脚。
    曾 `.nav-item{margin:1px 0}` 把「分隔线→设置」从 16 顶成 17（实测 18）。
  - **`.nav-item`（左栏底部设置）与 `.stg-nav-item`（设置页左导航）必须同步改** ——
    同一控件的两处实例，规则逐字复制。两处都有「⚠ 必须同步」注释 + 断言
    （只比计算值不比 height：设置页未打开时在 `display:none` 子树里）。
  - 量「线到内容」：线底 = border box 顶边 + `borderTopWidth`。`0.5px` 的 border
    在 Win/dpr=1 被 Chrome 向上取整成 1px，而 `height:0.5px` 的独立元素不会。
- **`.group-lbl` 必须 `align-items:flex-start` + `line-height:9px`**，
  `.group-lbl .count` 必须 `line-height:1`（一起改）。
  **LH 必须是 9px 不是 1**：11px 字号内容区 15px 高于行盒，负半行距使字墨下沉 1px。
  `(LH−15)/2+(fontAsc−inkAsc)=0` → LH=9。行盒矮但 overflow 可见，不动布局。
  实测：字墨顶/药丸顶/容器顶 = 16/16/16。**不能给标签负 margin**（药丸会被裁）。
  药丸 18px→12px，否则「59」基线比「本周」低 4px。
  **「本周」与「54」对齐看墨底不看基线**（中文与数字"基线→墨底"关系不同，实测基线差 +1 但墨底重合）。有断言守着。
- **量"看得见的间距"要换算到字墨**：`getBoundingClientRect` 给 em 盒，
  用 `canvas.measureText` 的 `fontBoundingBoxAscent − actualBoundingBoxAscent` 换算。
  **断言量盒子会全绿放过这类 bug**（本项第一次没修对的原因）。换算之外再用
  `node _verify/ink-measure.js` 扫像素兜底（实测两者差 0.3~0.5）。
- **`#session-list` 绝对不许贴底**（不许 `margin-top:auto`，不许 `.left-scroll{justify-content:flex-end}`），
  必须从 `.left-scroll` 顶部开始。auto 语义就是吃掉剩余空间=贴底：只剩 2 条会话时列表沉底、
  搜索框到首条空 400+px；`.sess-empty` 也是 `#session-list` 子节点会一起沉底。`flex:0 0 auto` 仍必需。
- **会话行副标题用包 `display_name`**（`sessions.js:packLabel`），不要吐 `it.pack`
  （slug 会显示成 `elevator`）。`matches()` 也要带显示名，否则搜不出来。
- **不要动的三处**：`.sess-del` 常驻 `opacity:.5`（改回 hover 会"删除要点好几次"）；
  `.sess-topic` 截断 187px（给删除按钮留 31px）；行高 49px。
- 断言在 `_verify/verify.js`（左栏 13 条），**回归总数 116**（2026-09-15）。

## 二、设置页「生成偏好」面板（已定）

- **字段一律单列整行**。`#param-front` 的 `grid-template-columns:1fr 1fr`
  （首个提交 `7b09a4b` 遗留）已删 —— 当时 `MORE_KEYS` 只剩 `cta`，唯一的
  「结尾引导」被按在左半格 328px、右半空着，而同卡其他字段整行 672px。
  **不要为"省高度"改回两列**（字段奇数个时最后一个必然半宽）。
  2026-09-16 参数分层后 `MORE_KEYS` 变成 3 个（style / persona / cta），
  这条结论只会更成立。
- **换行业包必须重渲染它带来的参数**：`bindShell()` 里
  `$("pack").addEventListener("change", () => renderPackParams())`。
  曾经这条路径没人接 → 不只是显示旧字段，`cta` 的值域来自包，换了包留着旧值
  → 生成时该值在新包不存在 → **静默降级**。
- **`renderPackParams()` 只清 `innerHTML` 就够**，别补菜单清理 ——
  `beautifySelects()` 末尾已有全局孤儿菜单清扫。**重复机制比缺机制更糟**。
- **`renderPackParams()` 不排除 `TOOLBAR_KEYS`**（2026-09-17 任务 2）。
  工具条胶囊是「常用参数的快速入口」，设置页「生成参数」卡片是**所有可定制项
  的总账** —— 两者共用同一份 `pack.params`，`select id` 都是 `p-<key>`，
  改哪一处都会同步（`updateStale` 也听得到）。**不要加回排除**：
  用户报过「生成参数里怎么没有细分领域/受众」。
- ⚠ **互斥切换的卡片不承担「堆叠分隔」职责**（2026-09-17，用户报
  「生成参数和进阶上面怎么有两个分割线？和其他设置页面不统一啊」）。

  `#gen-sec-detail` 里三张 `.page-card`（pack / param / adv）**同时只有一张可见**，
  而 `.page-card` 的 `border-top` 是为「多张卡片上下堆叠」设计的分隔线 ——
  互斥切换下这个语义不成立：可见的那张平白多一条线 + 20px 顶部内边距。

  | 卡片 | border-top | padding-top |
  |---|---|---|
  | pack（`div:first-of-type`） | 0 | 0 |
  | param / adv | **1px** | **20px** |

  ⚠ **`:first-of-type` 修不了** —— 它**认不出 `.hidden`**：隐藏的卡片仍占
  DOM 顺序，切到 param 时 pack 照样是 `div:first-of-type`。必须显式去掉：
  ```css
  #gen-sec-detail > .page-card { border-top: none; padding-top: 0; }
  ```
  （id 选择器 (1,0,1) 压得住 `.page-card:first-of-type` (0,2,0)，不用 `!important`。）

  判据见 verify.js「生成偏好右列三张卡片顶部都没有分割线（与 llm 面板的卡片
  一致）」—— 取「与同角色面板的卡片**相等**」而不是写死 0，贴用户那句「不统一」。
  已过变异检验（删掉那条 CSS → `param/adv={1,20}` vs `pack/llm={0,0}` 报红）。

  **通用规律：凡「多张卡堆叠 → 互斥切换」的改造，都要问一句
  「分割线的语义还成立吗」。** 同一容器里"可见的只有一张"时，
  它应当表现得像第一张（无线、无顶部内边距），而不是像"被隐藏卡片推下来的第二张"。

### 「模型接口」面板的**未配置状态**（2026-09-16 已定，2026-09-17 修订）

用户原话：「改一下没有配置的状态」。问题不是文案，是**未配置与已配置长得一样**：
六个框里填的是内置默认值（`glm-4.7` / `0.7` / `180` / `16000`），
每一个都像用户自己填的，只有最底下一行浅灰「未配置 API Key」。
根因与修法见 `MEMORY.md` 第一节第 ⑤ 种静默降级；UI 侧约定：

- ⚠ **2026-09-17 任务 5 修订：「默认模型」概念整个下线，模型行不再挂
  `.tag-default`**（用户原话「移除默认模型，改为需用户自行配置」）。
  - 不是 `display:none` 隐藏 —— **连 DOM 节点都不创建**。`display:none`
    是"假象"：DOM 仍有节点、未配置与已配置在底层还是两种状态。
  - picker 上也不再附「（默认）」后缀（`ui.js` 的 `tag()` 已删）。
  - 区分「未配置 vs 已配置」现在靠三件事：① 模型行的「未配置 Key」`tag-warn`
    （红）；② 编辑表单字段留空 + placeholder 给示例；③ Key 输入框两态
    placeholder；④ 未配 Key 时 picker 的 `_warnNote`（橙色）。
  - **高级配置的三项（retries / timeout / max_tokens）仍挂 `.tag-default`** ——
    它们是「兜底值」不是「默认模型」，告诉用户「这个值不是你存的」仍有用
    （点「保存高级配置」就能变成自己的）。`markDefault()` 仍**幂等且可撤销**。
- **中性灰（`--fill-strong` + `--text-2`），不要 `--warn` 橙**：
  没配过不是故障。染橙会让橙色贬值 —— 真正会失败的「未配置 API Key」就没人看了。
- **Key 输入框的 placeholder 分两态**：未配置「粘贴你的 API Key」／
  已配置「已配置时留空即保持不变」。原来只有后者，等于在暗示已经配过了。
  两句话**各只有一份**：已配置那句是 HTML 的 `placeholder`（首次运行时存进
  `data-ph-set`），未配置那句在 `data-ph-empty`。
- ⚠ **写「文本含某字符」的断言要用 `includes`，不要用正则**（2026-09-17）：
  判 picker 是否带「（默认）」后缀时最初写的是 `/-默认-/` —— 它要求"默认"
  **前后都是 `-`**，而实际形态是「（默认）」前后为括号 → **变异检验假绿**。
  正解 `!text.includes("默认")`。**凡正则里出现字面量锚点，先问一句
  「目标形态真的长这样吗」。**

### 模型接口的「恢复默认」按钮（2026-09-17 任务 6 已删）

`#md-reset-model` / `#md-reset-baseurl` 两个 `.linkbtn` + `fillDefault()`
函数已整段删除 —— 「默认模型」不存在了，"恢复到默认"就没有对象。
**高级配置的 `#st-reset-adv` 保留**（重试/超时/输出预算的"恢复默认"是
另一回事：那些是兜底值，用户改错了需要一键回默认）。

⚠ 注意 `.linkbtn` 的 `height: auto` 那条规则仍要留着（`#st-reset-adv` 还在用）——
它被全局 `button { height: var(--h-btn) }`（30px）接管会把标签行盒顶到 30px，
同一组字段的节奏就断了（详见第六节）。

## 三、设置页左导航（已定）

- **不要再加搜索框**（2026-09-15 删，用户原话「设置中不要搜索了吧」）。
  理由是**它是唯一会隐藏导航项的入口**：过滤词残留会让用户看到一份缺项却无提示的导航。
  连带删了 `.stg-nav-item.hidden` / `.nav-sec.hidden`。有断言守着。

## 四、底部输入区（2026-09-16 已定，一体化卡片 + 白面靠描边）

结构：`#composer > #composer-body`（一个卡片）`> #topic` + `.composer-tools`，
工具条里左 `#quick-params`、右 `.composer-actions`（`#model-pick` + 发送键）。
**不要改回「参数条 / 输入框 / 提示行」三个并列块** —— 那是主次倒置
（参数占了输入框上方最贵的位置）且三块各有各的边界。

### 卡片的边界语言：白底 + 看得见的描边（这是本轮的核心结论）

> ⚠ **中途翻过一次车，记在这里免得再翻**：第一版我凭肉眼看参考图，判定
> 「卡片是浅灰面、无描边」，据此做成 `--fill` 灰面 + 聚焦才变白 + 加发丝线。
> 用户一句「没选中时的背景色是不是不对啊」逼我**逐像素量**了三张参考图 ——
> **三张全是白底 + 1px × 亮度 230（≈10% 黑）描边，没有一张用灰底。**
> 教训：参考图不能「看」，要**量**（复用 `_verify/ink-measure.js` 的最小 PNG 解码器）。

- **`#composer-body` 恒为白面**（`--surface`），未聚焦时就有
  `box-shadow: 0 0 0 1px var(--line-strong)`；`:focus-within` 是**同一圈描边 +
  多一层 `--shadow-composer`**（只加抬升，不换面、不加粗描边）。
- **判据是「有效墨量」= alpha × 宽度，不是其中任何一个。**
  `0.5px × 7%` → 0.035（读成「框」）；`1px × 12%` → 0.12（读成「卡」）。
  ⚠ 只查一个维度会漏掉「改了一半」：变异 `1px × 7%` 得 0.07，
  改宽度不改颜色照样红 —— 所以断言写成 `ringInk(ring) >= 0.09`。
- **两处通栏分割线都已删掉**：`#composer` 的 `border-top`、`.composer-tools` 的
  `border-top`。**不要加回去** —— 卡内画线等于把一个卡片切成两个「小卡片」，
  且通栏的线会切开圆角，得再拿 `overflow:hidden` 去裁（那条 `overflow:hidden`
  也已撤掉，因为不再有东西需要裁）。
- **`--shadow-composer` 不能调淡**：原来两层都是 5%，在纯白页面上等于没有。
  断言「聚焦比未聚焦多一层真实抬升」用**投影最大模糊 ≥ 16px**（实测 20 vs 1）。
- ⚠ **应用启动是自动聚焦的**（`main.js` 的 `$("topic").focus()`），所以用户开机
  看到的是**聚焦**那一态 —— 断言两种状态都要测，截图也要两张
  （`main-composer.png` / `main-composer-idle.png`）。
- **`--r-xl` 收口**：卡片圆角走令牌，别写死 `20px`。

### 工具条内的控件

- **一律幽灵态**（`.select-wrap.pill .select-btn`：`background: transparent` +
  `--text-2`）：靠箭头表示可点，不靠灰底胶囊。一排灰底胶囊会让工具条比输入区还重。
- ⚠ **hover / 展开态必须用 `--fill-strong`，不能用 `--fill`** ——
  卡面本身就是 `--fill`，同色叠同色等于没反馈。`.qp-more:hover` 同理。
- **模型选择器 `#model-pick .select-btn` 是这一排唯一的「深色大字」**
  （`--f-sm` + `--text-1`）：参数只影响这一条脚本，模型影响之后所有生成。
  id 选择器 (1,1,0) 压得住 `.select-wrap.pill .select-btn` (0,2,1)，不用 `!important`。
- ⚠ **`is-warn` 的特异性**：现显式写 `.select-wrap.pill .select-btn.is-warn`
  （0,3,0），**不能只靠源码顺序赢** —— 那是「改一次顺序就静默失效」的写法。
  表现方式是**橙色文字**（同参考图里橙色的「完全访问」），hover 才给 `--warn-soft` 底。
- **参数分层只改一处**：`ui.js` 的 `TOOLBAR_KEYS` / `MORE_KEYS`。
  移出工具条的参数会自动出现在设置页「生成偏好」（`renderPackParams` 的
  逻辑本来就是「MORE_KEYS + 未被工具条覆盖的」），不用另写代码。
  ⚠ 但 `verify.js` 有一份**规格副本** `TOOLBAR_KEYS` 要同步 —— 那是有意
  不复用实现常量（复用 = 同义反复），注释里已写明。

### 发送键 / 停止键

- **圆角方块，不是正圆**：`border-radius: var(--r-md)`（34px 见方）。
  三个参考图都是圆角方块。断言判据是「圆角明显小于半宽」而不是写死数值。
- **空输入不可点，但不能隐形**：用 `--fill-disabled`（新令牌，`rgba(0,0,0,.10)`），
  ⚠ **不要用 `--fill`** —— 它跟白卡面只差 10 等效灰度，等于没有。
  断言判据是「底色与**两种**卡面都拉得开（≥ 12 等效灰度）」；
  「有没有底色」这种问法测不出这个 bug（`--fill` 也有底色）。
- **停止键只换图标，不改底色**：生成中输入框被禁用会失焦 → 卡面退回浅灰，
  白底停止键与卡面只差 10，压不住。参考图里生成中的那个键就是
  「实心深色方块 + 白色停止图标」。断言看 `backgroundImage`（渐变落在 image 上，
  `backgroundColor` 恒为透明 —— 只查底色会把实心键判成「没有背景」）。
- **`#btn-generate.send .spinner` 是死规则**：没有任何代码往按钮里塞 `.spinner`
  （只有 `progress.js` 在消息区用 `.spinner`）。看到别以为按钮有 spinner 态。

### 其它

- **`placeholder` 用 `--text-2`**（`#composer #topic::placeholder`）：它是空白输入区里的
  **主指令**，不是装饰性提示，`--text-3` 在白面上读不出来（参考图实测字墨亮度约 143，
  比 `--text-3`(161) 更深、接近 `--text-2`(110)）。
- **placeholder 只留最短的一组键位**：现在是
  `输入主题，生成口播脚本…（Enter 发送 · Esc 停止）`。砍掉的是**重复**信息 ——
  原来的「回车生成」与「Enter 发送」重复，「Esc 停止生成」在生成时状态行已经会说。
  完整键位（含 `Shift+Enter` 换行）挪进 `title`，别再加回 placeholder。
- **`shot()` 支持 `clip`**：布局类改动请留局部特写
  （`main-composer.png` / `main-composer-idle.png`），整屏图上工具条只有几十像素高。
- ⚠ **切状态后不能立刻读 computed style**：`#composer-body` 挂了
  `transition: background .18s, box-shadow .18s`，过渡进行中 `getComputedStyle`
  返回的是**插值中的当前值**。踩过：blur 后立刻读碰巧对（首次无过渡），
  focus 后立刻读拿到的是未聚焦的值。**读之前先 `sleep(260)`。**
- **「返回工作区」→ 导航列表 = 16px**，由 `.stg-back{margin-bottom:var(--s4)}`
  承担，`.stg-nav-scroll{padding-top:0}`。
  **间距要放在容器外**：**容器内边距在滚动时留不住**（内容能滚进 padding 区），
  滚到中间首项会贴住返回按钮，**"永久间距"只能由 margin 给**。
- **通用规律：删掉一个控件是布局改动，不只是删 DOM** —— 要显式问
  「这份间距现在归谁」。有断言量实测值（容差 0.6）。

## 五、空态主区（`#empty`，2026-09-16 已定）

用户贴了 `.setup-card` 的截图说「不要这个吧，太丑了」。

- **一块区域只放一个居中块。** 丑的根源不是那张卡本身，而是它和 hero
  **并排叠了两块居中内容、各带一个大图标** —— 没有主次，开机第一眼是两张卡
  在抢位置。所以引导**并进了 hero**，`.setup-card` / `.setup-ic` / `.setup-actions`
  一族 CSS 已全删。**别再往 `#empty` 里 insertBefore 新块**。
- **两种态共用一个 hero**，由 `[data-when="ready"|"setup"]` 切换：图标
  （柱状图 ↔ 链接）、标题、副标题、底注各两份，外加 `.empty-cta`（「去配置」按钮）。
- ⚠ **两态文案都写在 index.html 里，不要搬进 JS** —— 搬进去就是同一句话两个副本
  （HTML 初始值 + JS 覆盖值），改一处忘一处。class 切换让每个状态各只有一份。
- **示例卡在引导态必须保留**：先挑主题、配完 Key 直接生成，这条路不该被挡掉。
- ⚠ **`applyEmptyHero()` 要无条件调**（boot + `meta` 事件）。只在 `noKey` 时切一次
  的话，用户配好 Key 之后 hero 会一直写着「先配置模型接口」——
  **这是「状态转移」缺陷，不是初始化缺陷**，只在加载时比对测不出来。
- **`#btn-empty-setup` 是静态节点**，绑定放在 `bindShell()`（`bindOnce`）里，
  不随状态重绑。
- ⚠ 写 `verify.js` 探针时两个坑（都踩过，都是「断言恒真」级）：
  ① **`h3.textContent` 会把两态拼起来**（`"想聊点什么？先配置模型接口"`）→
     两个正则都能匹配。用 `window.__shown(root)` 只取**可见的那一个子元素**。
  ② **`.hidden` 挂在 `.empty-cta` 这个外层上**，按钮自己永远不带 →
     查按钮本身的 class 恒为「可见」。要查 `#empty .empty-cta`。

## 六、设置页「模型接口」（2026-09-16 已定，模型列表 + 弹窗）

用户贴了 1 张现状 + 4 张参考图（其他 Agent 工具的模型配置），要求**既改样式也改逻辑**。
结构：

```
page-head   标题 + 副标题                     [＋ 添加模型]（primary slim）
page-card
  ├ 模型        <table class="mdl-table">   模型 | 服务商 | 启用 | 操作
  │             tbody#st-model-rows  ← renderModelList() 每次整表重建
  ├ <details class="st-adv" id="st-adv">    高级配置（默认收起）
  │             st-temperature / st-retries / st-timeout / st-maxtokens
  ├ #st-cfg-err（config.yaml 读坏时的橙色警示）
  ├ #st-status（状态行）
  └ page-actions                             [保存]（只存高级配置）
```

- **「保存」只管高级配置**。连接信息住在模型条目里，由弹窗保存 ——
  这一页再带一遍 base_url/model，就等于同一件事有两处写入口。
- **「启用」是单选语义**（同时只有一个生效），虽然长得像开关。
  所以 title 里写清「同一时间只有一个」，行也要高亮（`.mdl-table tr.on`）——
  三行长得一样时，光靠开关的明暗去猜当前用的是哪条太费劲。
- **服务商名是从请求地址推的**（`providerOf()`），这是一次猜测：未命中已知表时
  **原样显示主机名**，不硬套一个名字；完整地址写在 `title` 上供核对。
- ⚠ **表头只用一条发丝线，不要铺底色**（2026-09-16 改，用户贴图报「显示有问题」）。
  原来写的是「表头用一层极淡的 `--fill` 底」，而「当前启用」那一行
  （`.mdl-table tbody tr.on`）**也是 `--fill`** —— 两者上下紧贴、颜色一模一样，
  在界面上读成**一整块灰**：表头「模型/服务商/启用/操作」和下面那条数据行连成
  一片，看不出哪一行才是数据行；行自带 `--r-sm` 圆角，还在表头下沿留了两个
  白缺口。**同一层灰铺在相邻的两块上，就等于没铺。**
  现在表头走全站通用表格那套（`th, td { border-bottom: 0.5px solid var(--line) }`），
  灰底归「当前启用」那一行独占，高亮才真的在高亮。
  断言：拿两处的**有效底色**（逐层把 rgba 叠到白底上）比亮度，差 ≥ 6 才算分开 ——
  比颜色字符串会把「透明 vs 灰」判成不同，而那不是要守的东西。
- **高级配置收起时，摘要上要留一个「N 项还是内置默认」**（`.st-adv-sub`，
  有值时才用 `--warn` 色）。收起来就什么都看不见 —— 而「没配过」正是最需要
  被看见的那个状态。

### 弹窗（`#model-dialog`，添加 / 编辑共用）

模型 ID（`md-model`）/ 展示名称（`md-name`）/ 请求地址（`md-baseurl`）/
API Key（`md-apikey`）+ [测试连接] [取消] [保存]。

- ⚠ **编辑一条「没配过」的模型时，`md-baseurl` / `md-model` 必须留空。**
  回填生效值（引擎兜出来的默认地址）等于程序写的值冒充用户输入：用户没动过手
  却看到一串地址，而且保存一次它就真的成了他的配置。留空 + placeholder 给默认值
  + 标签上挂「内置默认」小标，三件事一起做才说得清。
- **「恢复默认」是把默认值填进框里**（`fillDefault`），由用户点保存落盘 ——
  不再直接改服务端（那会绕过「这条模型到底存了什么」，而且对非当前模型没法用）。
  默认值从 `/api/config` 的 `defaults` 拿，前端不另抄一份。
- **每行的「测试」要带 `model_id`**：编辑一条**非当前**模型时，不带 id 会错拿
  当前那条的 Key 去测 —— 测出来的结果与用户以为的不是一回事，而界面照常显示
  「连接正常」。
- Key 的 placeholder 两态：已配置 = HTML 里的（存进 `data-ph-set`），
  未配置 = `data-ph-empty`。**两个状态各只有一份文案，不在 JS 里再抄一遍。**
- ⚠ **高级配置里 `.linkbtn`（「恢复默认」）必须 `height: auto`**（2026-09-16）。
  它也是 `<button>`，被全局 `button { height: var(--h-btn) }`（30px）接管 ——
  而它写的是 `padding: 2px 6px`，意图是 20px 高。
  被撑到 30px 的后果不在它自己身上：`label.lbl` 是**块盒**，里面这个 inline-block
  会把**行盒**顶到 30px —— 同一组四个字段里只有「重试次数」的标签高 30px，
  那一个输入框跟着下沉，整组字段的节奏就断了（实测 **16 / 30 / 16 / 16**）。
  修法是 `height: auto` 拿回 20px 真实高度，再用 `-2px` 的上下外边距把
  **外边距盒**压回 16px（**行盒看的是外边距盒**），于是标签仍是 16px、
  可点区域仍是 20px。
  > 这是「按钮被当卡片/当行内控件用」的第二种形态：第一种是 `.kb-item`
  > （见第七节，内容漏出卡片），这一种是**自己没坏、把邻居顶坏**。
  > 两者都源自同一条全局规则，**新增任何 `<button>` 都要问一句它的高度是谁定的**。

### 断言口径（改这一页时必须同步）

- 「内置默认」小标带 **`data-field`**，断言按**字段集合**比，不按标签个数比 ——
  个数对不上可能只是布局变了，集合对不上才是「该标的没标 / 不该标的标了」。
  行内那个标把 `base_url,model` 合成一个（一行挂两个同样的标是噪音），按逗号拆开。
- ⚠ **「只增不减」要按机制分别验。** 列表行的标是整表 `innerHTML` 重建的，
  数值项的标走 `markDefault` 就地增删 —— **两套机制**。只验前者的话，
  一个「挂上就不摘」的 `markDefault` 会全绿漏过（实测漏过一次）。
  现在 12c 段走的是 `6 → 4 → 0`：弹窗存模型 → 再点保存存高级配置。
- ⚠ **表格里的「内置默认」小标必须 `display: inline-block`**（2026-09-16）。
  `.mdl-sub` 是 block，后面的 inline 元素虽然会自动换行，但 **inline 的
  `margin-top` 是被忽略的** —— 样式表里写着的 `margin-top: 4px` 从来没算进布局，
  小标就紧贴在「glm-4.7 · 智谱」下面（截图里看着像被行底边裁掉一截）。
  判据取**真实像素间距**（`tag.top - sub.bottom >= 3`），不查 CSS 文本：
  查文本的话，写着一句不生效的 `margin-top` 也会判绿。
  通用规律：**凡「写在 CSS 里但元素是 inline」的垂直间距/尺寸都要问一句它算不算数。**
- **「同一组字段的标签必须一样高」**（高级配置的四个 `.lbl`）。
  判据是**彼此相等**，不是某个绝对值 —— 把 30 改成 24 也一样是坏的。
  已过变异检验（改回 `margin-left: 8px` → 报红 `16/30/16/16`）。

## 七、设置页「知识库 / 技能」卡片列表（2026-09-16 已定）

- ⚠ **`.kb-item` 必须显式 `height: auto`** —— 这是全站唯一一处「按钮被当卡片用」
  的地方，而全局有 `button { height: var(--h-btn) }`（30px）。
  少了那一行：卡里是「36px 图标 + 一行文件名」需要 56px，内容从卡片底部漏出
  18px；**下一张卡的白底正好把漏出的那半行盖住**，所以看到的是「文件名下面漂着
  一行小字、还压在下一张卡上」，只有每组的最后一张整行露在组外。
  **通用规律：把 `display`/`padding` 改成卡片形态的按钮，一定要同时问
  「高度还是按钮的高度吗」。** 加新控件时同类检查。
- **卡内不再写角色小字（`.kb-desc` 已删）**：卡片已经按角色分在 `.kb-group` 里，
  组标题上写着同样的词，卡里再写一遍是**同一信息两份表示** ——
  而且它正是上面「漏出卡片外那行小字」的来源。
- **判据是「内容真的漏出盒子」**：拿每个**非绝对定位**子元素的底边与卡的内容盒
  底边比，溢出 ≥ 2px 就报红。**不要用 `scrollHeight > clientHeight`** ——
  那个口径有假阳性：`.segmented .radio` 里视觉隐藏的 `<input>` 是绝对定位的，
  照样把 scrollHeight 撑大 4px（实测），那是探针的问题、不是界面的问题。
- ⚠ **量之前先确认面板可见**：另一个面板此刻是 `display:none`，高度全是 0，
  差值算出来毫无意义。探针遇到 0 高度要**显式报 `HIDDEN` 并判失败**，
  不能默默跳过 —— 那就是断言空转（假绿）。
  （第一次写这条断言时就踩了：kb 面板已被切走，两条报红全是假信号。）

## 八、设置页「知识库 / 技能」面板的左右分栏（2026-09-16 已定）

`.kb-wrap` 必须是 **`flex-direction: row`**，让左侧文件列表与右侧内容查看器并排。

- 列表：固定 `340px` 宽 + `max-height: 70vh` + `overflow: auto`（13 个文件自然
  高度 839px，不滚就看不全）。
- 查看器：`flex: 1; min-width: 0`（占剩余宽度）。`.kb-body` 同步去掉
  `max-height: 50vh`，改用 `flex: 1` 让它填满查看器。

⚠ **历史是 `flex-direction: column`** —— 列表 h=839px 把查看器挤到屏外，
只剩 68px 头部。界面看起来「右侧空」，用户 2026-09-16 贴图报的就是这个。
**不要改回 column**，除非同时给列表加 `max-height` 把它撑矮。

判据见 `_verify/verify.js`：「知识面板是左右分栏」——
`view.left > list.right`（gap 在 [0, 50) 之间）+ 垂直区间有重叠 > 100px。
已过变异检验（改回 `column` → 报红 `gap:-672, vertOverlap:-20`）。

> 共用 `.kb-wrap`，所以这一改**同时修了知识与技能两个面板**，
> skills 那侧没单独改。

## 九、知识 / 技能条目视觉与 .pl-item 同族（2026-09-17 已定）

`.kb-item` 默认**无背景 + 无描边**，hover 时浮起一格 `--fill`，`.on` 时
`--sel-bg + --line-strong + --shadow-selected` —— **与 .pl-item 默认态完全
一致**。这是「幽灵态条目」的家族设计：

- 调色板：`bgAlpha = 0`（默认），`.on` 走 `--sel-bg`
- 图标：`30×30`，与 `.pl-ic` 同尺寸
- 网格：`30px 1fr auto`，与 `.pl-item` 一致（容器宽 1fr 解出来不同，
  所以断言只比**图标列宽**，不比整段 grid-template-columns 字符串）
- padding：`9px 10px`，与 `.pl-item` 一致
- 圆角：`var(--r-md)`，与 `.pl-item` 一致
- 文件名：sans、不等宽（去掉等宽字）；数字（kb-size）保留 mono 让一列对齐

**为什么从「白卡」改回「幽灵态」**：13 个文件原本是「白卡堆叠 + 描边」，每张卡
40×自高的方块，14 个堆下来视觉重量太大；分组标题（`.kb-group-t` 「知识库 5」）
本来就在做组层分隔，去掉卡面后由它承担这层视觉落点。

**保留的信息**：`.kb-icon` 按 `data-role` 上色（知识=ok 绿，技能=ring，禁用词=bad，
合规=warn，其他=neutral）—— 这是「哪个角色」这层语义信息，没有它 kb-item 就退回
成「纯粹的 pl-item」，丢了「按角色筛选」的视觉提示。

**判据**见 `_verify/verify.js`：「知识 / 技能列表条目与 .pl-item 视觉同族
（透明默认 + 一致几何）」—— 取一条非 `.on`/`.sel` 的 pl-item 当对照，把
padding / borderRadius / fontSize / 图标列宽 / 默认 background 的 alpha 全对上。
已过变异检验（kb-item 改回 `background: var(--surface)` → 188/189 报红）。

> ⚠ **不要改回白卡**（`.kb-item { background: var(--surface); border: 0.5px solid var(--line) }`）。
> 用户 2026-09-17 贴图报「和其它面板没有统一」，改回去这个反馈会再来。
> 视觉同族**不允许任何面板的列表条目长得像白卡** —— 要么都幽灵态、要么 pl-item
> 也改白卡。两个方向都行，但**必须是一族**。

## 十、知识 / 技能面板与 gen / llm / packinfo 结构同族（2026-09-17 已定）

四个「三列面板」必须有一样的**外层容器形状**：

```
<section class="stg-pane hidden has-cols" id="pane-x">
  <header class="page-head">…</header>       ← 标题 + 右上操作
  [可选手控区，例如 `.kb-filter`]               ← 不需要就别加
  <div class="stg-cols">…</div>               ← 三列骨架直接挂节下
</section>
```

这是把 `has-cols` / `.stg-cols` 直接挂节 / 跳过 `.page-card` 包装这两条特性**对齐成 1 套**。

`#pane-gen` / `#pane-packinfo` / `#pane-llm` 早就走这个形态。`#pane-kb` /
`#pane-skills` 的旧实现是：

- **缺 `has-cols`**：`max-width: 720px`（其它三个是 1160px），面板窄一截
- **`.stg-cols` 嵌在 `.page-card` 里**：多一条 border-top 横线 + 上下 padding

修法：
1. 加 `has-cols` 类
2. 移除 `.page-card` 包装 → 行业包选择器提取为独立的 `.kb-filter`，
   自己负责 spacing（`margin-bottom: var(--s5)`）

**判据**见 `_verify/verify.js`：「知识 / 技能 与其他设置面板同族」——
五个面板的 `maxW` 必须统一（去重后 size=1）+ `.stg-cols` 与 `.kb-filter`
都必须直接挂在节下（不在 `.page-card` 里）。过两道变异检验：
- `#pane-kb` 拿掉 `has-cols` → `maxW=720px` vs 其它 1160px → 红
- `.kb-filter` 套回 `.page-card` → `filterDirectParent: page-card` → 红

> ⚠ 写断言的时候要查**所有挂在节下的直接子节点**（不只是 `.stg-cols`）——
> 任何中间多套一层 `.page-card` / `.fg` 都会破坏同族判据。**漏一条机制
> 就是变异溜过去的口子**，跟「同一个标记被几套机制挂上只验证一套」同形。


## 十一、设置页的「五项统一」（2026-09-17 已定）

用户先报「行业包包内容高度要占满」，追问后勾选：右列高度行为不一致 /
右列卡片边距间距不一 / 模型面板有两个「保存」/ headbar 按钮不齐 /
按钮大小与文字大小。**改这一页之前先读本节。**

### 1. 右列统一「撑满剩余高度」

```css
.stg-pane.has-cols:not(.hidden) { display: flex; flex-direction: column; }
.stg-pane.has-cols .stg-cols { flex: 1; min-height: 0; align-items: stretch; }
.stg-pane.has-cols .pane-detail { display: flex; flex-direction: column; min-height: 0; }
.stg-pane.has-cols .pane-detail > .page-card { flex: 1; min-height: 0; }
.stg-pane.has-cols .pane-detail > .pi-cols { flex: 1; min-height: 320px; }
.stg-pane.has-cols .pane-detail > .page-actions { flex: 0 0 auto; }
```

⚠ **`:not(.hidden)` 不能省**：`.stg-pane.hidden { display: none }` 与
`.stg-pane.has-cols` 同为 (0,2,0)，只靠源码顺序太脆；写成 id 选择器则会
**盖掉 `display:none`** —— 面板永远可见、切不走（本项目踩过的
「id 压过 class 状态」的坑）。

⚠ **量「有没有撑满」要量 `.pane-detail` 的底边，不要量首块** ——
llm 右列在首块下面还有折叠区 + 状态行，量首块会把那两行算成「没撑满」。
判据：`.stg-main` 底边 − `.pane-detail` 底边 ∈ [0, 48]（= padding-bottom 32 + 余量）。

### 2. 一屏一个保存（模型接口）

删掉了独立的「保存高级配置」。高级配置（重试 / 超时）由模型表单的
「保存」一并存掉（`saveAdvancedConfig()`）。**不要加回第二个保存按钮** ——
第一次是用折叠区把两个错开，但同屏两个确认按钮依然是更差的设计。

⚠ `saveAdvancedConfig()` 越界时**抛异常**让调用方统一 toast：
前端不许比后端更严，也不许更松（越界当场拒掉、请求不发）。

### 3. 按钮规格统一（28px + 13px）

```css
.page-head-actions button,
.page-actions button,
.pg-empty-actions button { height: var(--h-btn-sm); font-size: var(--f-sm); }
```

原来 `.ghost` 走全局 `button { height: var(--h-btn) }`（30px）且 `font: inherit`
继承到 15px，而 `.primary.slim` 是 28px / 13px —— 并排时**底边差 2px、
字差 2px**。这两处都是紧凑操作区，统一走 slim 那一档。
判据取「彼此相等」而不是某个绝对值（改成 30px 也一样是统一）。

### 4. headbar 同构

两个「列表型」面板（行业包 / 模型接口）的 `.page-head-actions` 都是
**ghost bordered 次级（刷新）+ primary slim 主操作（新建 / 添加模型）**。
生成偏好是纯表单、没有列表，不加按钮。

### 5. 右列首块统一用 `.page-card`

`.page-head` 是**面板顶部**的 headbar 结构（含 `.page-head-actions`），
套在右列里是错位复用，且间距节奏（`margin-bottom: 20px`）与
`.page-card` 的 `gap: 16px` 不同。packinfo 右列已从 `.page-head` 改成 `.page-card`。

### 6. `.btn-row`：两个纯按钮不要套 `.row`

```css
.row { display: flex; gap: var(--s2); align-items: center; }
.row > :first-child { flex: 1; }          /* ← 给「主控件 + 若干按钮」用的 */
.btn-row { display: flex; gap: var(--s2); align-items: center; }   /* 纯按钮行 */
```

`.row > :first-child { flex: 1 }` 的语义是「一个主控件 + 若干按钮」
（原来的 select + 详情 + 新建）。**两个纯按钮时套上去会把第一个拉满整行**
（实测「详情」横跨 618px，见截图）。新增纯按钮行一律用 `.btn-row`。

## 十二、模型接口的「空列表」状态（2026-09-17 已定）

用户原话：「没有模型的时候不应该显示添加啊，还有高级配置」。

**一条模型都没有时，右列只显示空态引导**（`#llm-empty`，复用 packgen 的
`.pg-empty` 同族）：图标 + 「还没有配置模型」+ 说明 + 「添加模型」按钮。

`renderLlmEmpty(c)` 负责切换：
```js
const empty = !((c || {}).models || []).length;
const showEmpty = empty && !addingNew;
$("llm-empty").classList.toggle("hidden", !showEmpty);
$("md-form-card").classList.toggle("hidden", showEmpty);
$("st-adv").classList.toggle("hidden", empty);   // 空列表 → 永远隐藏
```

- ⚠ **不要摆一张空的「添加模型」表单**：会让人以为"已经在配了"。
- ⚠ **高级配置（重试 / 超时）在空列表时始终隐藏**：它是给**已配好的模型**
  调优用的，没有模型时没有对象。
- ⚠ **状态行也要留空**：原来会写「当前启用的模型还没配 API Key」——
  根本没有启用的模型，这话是错的。
- `addingNew` 标志：空列表下用户主动点「添加模型」（headbar 或空态里那颗）
  → 临时显示表单。由保存成功 / 取消 / 重新进面板归零。
  **少了它那颗按钮点了没反应。**

### 开关**可以关掉**（同一天的另一个修正）

「启用」是单选语义，但「都不启用」是合法状态（想先停用、改完配置再启用）。
三处必须一起改，少一处就「关了又跳回来」或「点了没反应」：

| 层 | 改法 |
|---|---|
| 前端 `activateModel` | 点已启用的那条 → `target = ""`（**不要** `return`） |
| 后端 `activate_model` | 空 id 合法（空 = 都不启用），只在非空时查存在性 |
| 后端 `_parse_models` | 「键**不存在**」→ 用第一条；「键存在但空串」→ 原样保留 |
| 输入区 picker | `models.find(...) \|\| null`（**不要** `\|\| models[0]`）+ 「未启用模型」占位 |

⚠ picker 的 `|| models[0]` 兜底会让「都没启用」时仍显示第一条的名字 ——
**显示与状态不一致**：用户以为还有模型在用，点生成才发现被拒。

### 三种「不能生成」必须分开说

`_require_model` 的三条出口（同一句话会把用户指向错的地方）：
1. 一条模型都没加 → 「还没有配置模型 —— 点右上角「添加模型」」
2. 加了但都没启用 → 「当前没有启用任何模型 —— 打开一个模型的开关」
3. 启用了但没填 Key → 「当前模型还没配 API Key」

picker 的 `_warnNote` 同样按这三档给橙色提示。

### 空态里**不放按钮**（2026-09-17 二改）

用户问：「没有配置的时候有两个添加模型入口，你觉得需要优化吗？」

原来空列表时**三处**都在说「添加模型」：headbar 按钮 + 中列提示 +
右列空态（**空态文案写「点右上角「添加模型」」而自己还有一颗按钮**）。

⚠ 问题不是"两个入口"，而是**空态文案指错方向** —— 用户会去右上角、
忽略眼前那颗按钮。用户拍板「去掉中间的按钮，改文案」：

- 空态 = **只负责说明**（图标 + 标题 + 「点右上角「添加模型」，填上…」）
- 入口 = **全页只有一个**（headbar 那颗常驻主操作）
- `addingNew` 逻辑保留 —— headbar 那颗在空列表下要能把表单换出来

判据见 verify.js：「空态里不再有第二个「添加模型」入口」
（`#llm-empty button` 数为 0）+「空列表下点 headbar「添加模型」→ 换成表单」。
已过变异检验（空态加回按钮 → `emptyBtnCount=1` 报红）。

### 分组命名：**别用「进阶」这种泛词**（2026-09-17）

「进阶」→「生成方式与输出」（中列条目 + 右列 `.card-title` 同步，
副标题「流程、产出形式、补充资料」）。

**为什么改**：用户原话「进阶有用吗？」—— 名字听起来像"高级 / 不常用"，
而里面「输出内容」「补充资料」其实常用。**名字要能预告内容**。

⚠ **改名前先 grep 每个字段的消费点**（用户问"有用吗"时尤其要）：
```bash
grep -rn '"voice"\|"format"\|"mode"' app/pipeline.py
grep -rn 'facts' app/knowledge.py
```
本轮 4 个字段**全部**有真实消费点（`mode` 分步确认分支 / `voice` 白名单 /
`format` 仅口播分支 / `facts` 私有资料）—— 凭感觉答"没用"就会误删功能。

## 十三、左栏会话列表（2026-09-18 已定）

### 1. 副标题只留「行业 · 时间 · 平台」

用户原话：「不用展示时间和字数还有合格，只需要展示行业、时间还有平台
（抖音/小红书）」。此前是 `行业 · 时刻 · 时长 · 字数 · 合格` 五段。

**为什么砍后三项**：时长与字数在同一个包 + 同一档时长下基本恒定、
「合格」几乎每条都是 —— 一列同值词只有噪声；而真正区分记录的**平台**
（同选题的抖音稿 / 小红书稿是两个产物）压根没显示。
列表这一层的用途只是「认哪条是哪条」。

**未完成记录例外**：状态文字插在行业之后（`行业 · 文案撰写中 · 时刻 · 平台`）
—— 状态不能只靠状态点颜色表达（色弱与读屏用户拿不到颜色）。

**挪走的信息不丢**：时长 / 字数 / 校验结果并进 `row.title` 悬浮提示。
判据：verify.js「悬浮提示补回了被挪走的时长/字数/校验结果」。

⚠ **断言要盯住「挪走了什么」，不能只盯「平台出现了」**：
只测 `/小红书/` 的话，把 duration/chars/passed 再塞回去照样全绿 ——
而用户告的就是那几个字段。断言同时查 `segs.length === 3`、
时刻是 `MM-DD HH:MM` 格式、以及全文不含 `数字+s` / `字` / `合格`。

### 2. 删除按钮悬停：**只改图标颜色，不加任何底色**

⚠⚠ **用户明确否决过一版实现**。原话：
 ①「删除按钮悬浮时的样式改一下，现在会显得距离边距不统一」
 ②「**不是让你改成红色的删除按钮，不要背景色，就改删除图标的颜色就行了啊**」

我先按 ① 做了「实色红底 + 白图标」，被 ② 否掉。

**最终规格**（`.sess-del`，桌面 styles.css）：
```css
.sess-del:hover  { background: transparent; color: var(--bad); opacity: 1; }
.sess-del:active { background: transparent; color: var(--bad-hover); transform: scale(.9); }
.sess-del:focus-visible { outline: 2px solid var(--bad); outline-offset: 1px; }
```
（`background: transparent` 要**显式写出来**，包括 `:active`；
`transition` 只留 `opacity` 与 `color`，不要 `background`。）

**「边距不统一」的真正根因是几何，不是颜色**：
- 按钮 `position: absolute; right: 3px; width/height: 28px`
- 行的 padding 是 `7px 9px`
- 副标题 / 顶行的 `padding-right: 32px`（= 28 + 3）
- 垂直居中必须用 `top: 0; bottom: 0; margin-block: auto`
  （**不能**用 `top:50% + translateY(-50%)` —— 全局
  `button:active{transform:scale(.96)}` 会在按下瞬间覆盖掉 translateY）

**贪心结论**：遇到「这个小控件看起来没对齐」时，
**加底色是把问题放大，不是解决** —— 凭空多出的面积让「歪」更显眼。
先问：用户抱怨的是「看不见」还是「位置歪」。

**断言口径**（改这一处必须同步）：
- `bg` 必须钉成**透明**（只测 `fg` 挡不住「随便补个底色」；
  透明在 computedStyle 里写作 `rgba(0, 0, 0, 0)`，用 `/rgba?\([^)]*,\s*0\)$/` 匹配）
- `fg === "rgb(215, 0, 21)"`（`--bad`）
- `:hover` 与 `:active` **各测一条**（单测 hover 挡不住
  「hover 透明但 active 给底色」这种半吊子改法；`:active` 的 fg 是
  `rgb(194, 0, 20)`，即 `--bad-hover`）
- 几何：`size === [28,28]`、`|up − down| <= 1`、`right <= padR`
- **用真悬停**（`Input.dispatchMouseEvent`）而不是加 class ——
  样式写错时假 hover 照样绿

## 十四、今日选题视图与情报源面板（2026-09-23，B 线）

**入口**：左栏底部 `.left-foot > .nav-sec` 的 `.nav-item`，**在「设置」上面**，
计数走 `.nav-item kbd` 键帽灰底（橙色 `.badge` 是状态色，留给真状态）。
点它 = 只换 `#right` 的内容区（`gotoView("topics")`），**左栏与会话列表常驻**。

**切视图的唯一入口是 `ui.js` 的 `gotoView(name)`**，它顺带做两件事：
1. 给 `#right` 写 `data-view` —— 头部那两颗情报动作（重抓 / 情报源）由
   `#right[data-view="topics"] .rh-intel` 控显隐。**别改成给按钮加 `hidden` 类**：
   切视图的路径有好几条（导航 / 去生成 / 新建对话 / 打开历史），逐条开关按钮必漏。
2. 同步左栏 `.nav-item` 的高亮。⚠ **不能在 `topics.js` 里做这件事** ——
   它已经 `import` 了 `ui.js`，反向 import 会绕成循环。

**列宽（§2.8 口径）**：阅读面 `.topics-inner` 与正文同档 `--w-stream`（780）居中；
**唯一例外是情报源那张表** `.topics-src-inner`，走设置页三列同档 `--w-stg-3col`（1160）。
窄窗媒体查询（≤1120）里 `.topics-inner` / `.topics-src-inner` 的内边距
**必须跟着 `#chat-stream` 一起降** —— 实测过"新视图没跟媒体查询"导致窄窗差 8px。

**结构与类**：分区用 `.kb-group` / `.kb-group-t` / `.kb-cards`（知识库页现成范式）；
卡头用 `.res-top` + `.card-title`（⚠ **不能用 `.kb-group-t`**：它是 nowrap 的组标题行，
长标题 + 三个 chip 挤一行会把标题压成一字一行）；小标记用 `.m-chip`；
表格用全局 `table/th/td` + `td.ok` / `td.bad` / **`td.warn`**（本轮补的第三档 ——
只有两档会把"我们决定不做"与"配错了"说成同一件事）。

**数据驱动的宽度一律走 CSSOM**（`el.style.setProperty("--w", …)`）：
内联 `style=` 属性会被 CSP 静默忽略（`style-src 'self'` 且无 `'unsafe-inline'`）。
⚠ 由此产生的一条**判据要写对**：`[style]` 属性计数**不能**用来断言"没有内联样式" ——
CSSOM 的 `setProperty` 自己就会生成 style 属性，那是规定做法。
要守的是"源码里没有 `style=` 字面量"，那条由既有的 `securitypolicyviolation` 监听覆盖。

**三种"没有"必须长得不一样**（B 线最容易做错的一处）：
`已接入但今天 0 条`（留在筛选行里、压淡）≠ `未接入`（带一句为什么、不进可筛态）≠
`盘上有条目但声明里没有这个源`（照常露出来，否则分组计数之和与总数对不上）。

**结果页三样（§2.7）**：节奏条 `.tempo`（`--w` 走 CSSOM）、每段语速 `.rate`
（超目标 10% 用现成的 `.over` 红）、人味标记 `.tell-mark`
（**只加下划线、不改文字色** —— 拿 `--warn` 当正文色就撞上 §2.6 那笔对比度欠账）。
`.tempo` 是 `insertAdjacentHTML` 进来的，它的 `--w` 必须在**插入之后再写一次**，
否则条是全 0 宽而界面不报错。
