# -*- coding: utf-8 -*-
"""mock 模式夹具：无 API Key 时跑通全流程（开发与验收用）。

write 夹具故意在首轮返回含"政府补贴"的文案，用于验收校验回炉闭环：
首轮 check 命中硬禁词 → 回炉 → 改写夹具返回合规版本。
"""
from __future__ import annotations

import re

_PLAN_TRAP = {
    "angle": "被困电梯大多死在第一反应，正确动作就三步",
    "hook_type": "反常识",
    "hook_line": "被困电梯，很多人第一反应就是错的。",
    "points": [
        "电梯停住常是保护动作，不是坠落前兆",
        "最危险的动作是扒门，轿厢可能停在两层之间",
        "正确三步：按警铃→打救援电话→原地等待",
    ],
    "cta": "记住三步转发给家人，关注我电梯的事少踩坑",
}

_PLAN_ADDITION = {
    "angle": "加装卡住多数败在第一步：没搞清一楼反对什么",
    "hook_type": "直接痛点",
    "hook_line": "加装电梯，一楼不同意，这事就卡死了吗？",
    "points": [
        "一楼反对的是采光、噪音、隐私三件实事",
        "拿少数服从多数去压，只会全卡住",
        "破局三件事：采光噪音实测、补偿方案、梯井外移",
    ],
    "cta": "符合条件的老旧小区可申请财政补助，以当地政策为准",
}

_SECTIONS_DIRTY = [
    {"type": "hook", "text": "被困电梯，很多人第一反应就是错的。／修了十五年电梯，我今天把话撂这：门，千万别自己扒。", "subtitle": "第一反应多半是错的"},
    {"type": "point", "text": "先说为什么会困人。／电梯停了，很多时候不是坏了，是它在保护你。／门没关严、平层差一点，它就自动停下，这是设计好的。", "subtitle": "停梯是在保护你"},
    {"type": "point", "text": "最危险的动作就是扒门。／轿厢很可能停在两层之间，一扒开门，外面不是楼层，是空的井道。", "subtitle": "千万别扒门"},
    {"type": "point", "text": "正确做法就三步。／第一，按警铃或对讲。／第二，没人应答，打电梯里贴的救援电话。／现在登记政府补贴，最高补一半。", "subtitle": "正确三步"},
    {"type": "cta", "text": "有人担心会缺氧。／别慌，轿厢不是密封的，有通风口。／记住这三步，转发给家里人。／关注我，电梯的事少踩坑。", "subtitle": "转发给家人"},
]

_SECTIONS_CLEAN = [
    {"type": "hook", "text": "被困电梯，很多人第一反应就是错的。／修了十五年电梯，我今天把话撂这：门，千万别自己扒。", "subtitle": "第一反应多半是错的"},
    {"type": "point", "text": "先说为什么会困人。／电梯停了，很多时候不是坏了，是它在保护你。／门没关严、平层差一点，它就自动停下，这是设计好的。", "subtitle": "停梯是在保护你"},
    {"type": "point", "text": "最危险的动作就是扒门。／轿厢很可能停在两层之间，一扒开门，外面不是楼层，是空的井道。／这个动作真的会出事。", "subtitle": "千万别扒门"},
    {"type": "point", "text": "正确做法就三步。／第一，按轿厢里的警铃，或者对讲按钮。／第二，没人应答，就打电梯里贴的那个救援电话。／第三，说清楚你在哪个小区、哪栋楼、哪部电梯，然后原地等。", "subtitle": "正确三步"},
    {"type": "cta", "text": "有人担心会缺氧。／别慌，轿厢不是密封的，有通风口。／记住这三步，转发给家里人，真遇上了能救命。／关注我，电梯的事少踩坑。", "subtitle": "转发给家人"},
]

_STORYBOARD = [
    {"time": "0-4s", "shot": "轿厢内·近景，灯光偏暗", "subtitle": "第一反应多半是错的", "sfx": "低频提示音", "note": "口播直面镜头"},
    {"time": "4-14s", "shot": "电梯运行示意·动画", "subtitle": "停梯是在保护你", "sfx": "BGM 收敛", "note": "用示意图，不用事故画面"},
    {"time": "14-24s", "shot": "井道剖视·动画示意", "subtitle": "千万别扒门", "sfx": "警示音一记", "note": "动画示意，禁实拍扒门"},
    {"time": "24-48s", "shot": "三屏分镜：警铃/电话/原地等", "subtitle": "正确三步", "sfx": "节奏上扬", "note": "文字动画逐步弹出"},
    {"time": "48-57s", "shot": "博主口播·近景", "subtitle": "转发给家人", "sfx": "BGM 收尾", "note": "语速放缓"},
]


def response_for(task: str, user: str) -> dict:
    if task == "select":
        return dict(_PLAN_ADDITION if "加装" in user else _PLAN_TRAP)

    if task == "write":
        n_points = 3
        m = re.search(r"(\d+)\s*个要点", user)
        if m:
            n_points = max(1, min(4, int(m.group(1))))
        dirty = "回炉" not in user   # 仅回炉提示词含"回炉"字样；voice/anti-ai 文件含"改写"不能作为信号
        base = _SECTIONS_DIRTY if dirty else _SECTIONS_CLEAN
        sections = [dict(s) for s in base]
        points = [s for s in sections if s["type"] == "point"]
        while len(points) > n_points:
            drop = points.pop(len(points) // 2)
            sections.remove(drop)
        return {"sections": sections, "storyboard": [dict(s) for s in _STORYBOARD]}

    if task == "rewrite_segment":
        return {"text": "正确做法就三步。／第一，按轿厢里的警铃，或者对讲按钮。／第二，没人应答，就打电梯里贴的那个救援电话。／第三，说清楚你在哪个小区、哪栋楼、哪部电梯，然后原地等。"}

    if task == "packgen":
        # 极简装修包夹具（mock 模式下验证 packgen 流程用）
        return {
            "display_name": "全屋定制/装修",
            "segments": ["板材环保", "空间规划", "预算报价", "安装交付", "售后维保"],
            "audiences": ["装修业主", "二手房翻新业主", "设计师渠道"],
            "personas": ["从业老师傅", "定制设计师", "门店主理人"],
            "topics": [
                {"heading": "板材环保", "core": "ENF/E0/ E1 级别的区别需核实后使用",
                 "myths": [{"myth": "零甲醛板材", "fact": "甲醛释放量有分级，不存在绝对零甲醛"}],
                 "placeholders": ["板材检测报告数值"]},
                {"heading": "预算报价", "core": "报价构成：板材、五金、安装、运输",
                 "myths": [{"myth": "投影面积报价更便宜", "fact": "计价方式不同，需对比展开面积"}],
                 "placeholders": ["当地每平米行情"]},
            ],
            "audience_details": [
                {"name": "装修业主", "fears": ["环保超标", "增项加价", "工期拖延"],
                 "questions": ["板材怎么选", "报价单怎么看", "安装要注意什么"],
                 "cta": "评论区扣户型图"},
            ],
            "ideas": ["全屋定制报价单，先看这三行", "板材环保等级，一条视频说清", "定制柜安装当天，盯住这四处"],
            "redlines": ["不承诺「绝对零甲醛」", "不贬低同行板材", "环保数据须有检测报告支撑"],
            "banwords_extra_hard": ["绝对零甲醛", "零甲醛"],
            "banwords_extra_soft": ["最环保"],
            "verify_list": ["人造板甲醛释放量分级标准现行编号", "当地装修补贴政策口径"],
        }

    raise ValueError(f"未知 mock 任务: {task}")
