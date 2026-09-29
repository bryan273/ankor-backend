"""Translations for the strings the agent does NOT write with a model.

Most of what the customer reads is composed per turn, so it already comes back in
their language: the reply, the planner's reasoning line, the step label the planner
writes for each tool call. What does not is the fixed furniture — the six stage names
and their descriptions in the progress trail, the static label a tool falls back to,
the suggestion chips the state produces when the composer forgets its SUGGESTIONS
line, and the two picker lines used when that prompt fails. Those are literals in the
source, so before this module every Chinese conversation carried an English progress
trail beside a Chinese answer, and a Chinese safety warning under English chips.

Keyed by the English string rather than by a symbol, so the English source stays the
single source of truth and a string with no translation degrades to English instead
of to a missing-key placeholder. `ui()` is therefore safe to wrap around anything.

English and Chinese are what this product supports. Any other language falls through
to English here while its composed text still arrives translated, because a model
writes that part and a table cannot.
"""
from __future__ import annotations

ZH = {
    # ── the six stages in the progress trail ─────────────────────────────────
    "Reading your photo": "正在看你的照片",
    "Understanding you": "正在理解你的问题",
    "Identifying the device": "正在确认你的设备",
    "Investigating": "正在排查",
    "Writing the answer": "正在整理答复",
    "Checking against the rules": "正在按规则复核",
    "Vision model extracts the error code, device type and any damage":
        "视觉模型从照片中提取错误代码、设备类型和损坏情况",
    "One call classifies emotion, urgency, intent and the entities mentioned":
        "一次调用判断情绪、紧急程度、意图，以及消息中提到的关键信息",
    "Alias table first, then purchase history, symptom wording, photo — "
    "asks only if those cannot decide":
        "先查别名表，再看购买记录、故障描述和照片；只有这些都无法判断时才会询问",
    "ReAct loop: think, call a tool, read the result, decide whether to continue":
        "ReAct 循环：思考、调用工具、读取结果，再决定是否继续",
    "Composes the reply under the emotion policy for this conversation":
        "按这次对话的情绪策略撰写答复",
    "Six guard rules run on the draft; a violation rewrites it before you see it":
        "六条护栏规则检查草稿，发现问题会在你看到之前重写",

    # ── stage labels the graph sets directly ─────────────────────────────────
    "Picking up where we left off": "接着刚才的进度继续",
    "This one needs immediate attention": "这件事需要立刻处理",
    "Opening an urgent ticket": "正在创建紧急工单",
    "Checking the warranty rules": "正在核对保修规则",

    # ── the label a tool falls back to when the planner writes none ──────────
    "Looking up the product": "正在查找产品",
    "Reading the product details": "正在读取产品详情",
    "Looking up that error code": "正在查询该错误代码",
    "Checking the manuals": "正在查阅说明书",
    "Pulling up the repair steps": "正在调取维修步骤",
    "Checking similar cases": "正在查询同类案例",
    "Checking your order": "正在查询你的订单",
    "Checking the dealer directory": "正在查询经销商名录",
    "Checking warranty coverage": "正在核对保修范围",
    "Looking at your photo again": "正在重新查看你的照片",
    "Opening a ticket for you": "正在为你创建工单",
    "Checking Anker's site": "正在查询 Anker 官网",

    # ── suggestion chips the state produces on its own ──────────────────────
    "Is it safe to leave it unplugged in the house?": "拔掉电源放在家里安全吗？",
    "How soon will someone contact me?": "多久会有人联系我？",
    "What exactly needs to be visible in the photo?": "照片里具体需要拍到什么？",
    "How long does verification usually take?": "审核一般需要多久？",
    "Can I still use it while the claim is open?": "理赔处理期间还能继续用吗？",
    "What if the dealer won't help?": "如果经销商不肯处理怎么办？",
    "Do I need the original packaging?": "需要原包装吗？",
    "How long should the repair take?": "维修大概要多久？",
    "What would a paid repair cost?": "自费维修大概多少钱？",
    "Is it worth repairing or replacing?": "是修划算还是换新划算？",
    "Do you have a trade-in option?": "有以旧换新吗？",
    "How do I check on this ticket later?": "之后怎么查这个工单的进度？",
    "Can I add a photo to the ticket?": "可以给工单补充照片吗？",
    "What if none of that fixes {product}?": "如果这些都没能解决{product}怎么办？",
    "How often should I be doing this?": "这个多久做一次比较好？",
    "Can I talk to a person instead?": "可以转人工吗？",
    "I'm not sure which one I have — how do I tell?": "我不确定自己是哪一款，怎么分辨？",
    "What else should I check?": "还有什么需要检查的？",
    "Where do I find the error code?": "错误代码在哪里看？",
    "What if there is no code, only a beep?": "如果没有代码，只有提示音呢？",

    # ── picker lines, used only when the picker prompt itself fails ──────────
    "I can see the type of device, but not which model. The model number is usually "
    "{where_to_look}. Is it one of these?":
        "我能看出是哪一类设备，但看不到具体型号。型号通常在{where_to_look}。是下面这几款中的一款吗？",
    "on a sticker on the device": "设备上的标签贴纸上",
    'Quick check: "{mention}" is used for more than one of our products. '
    "Which of these is yours?":
        "确认一下：“{mention}”对应我们不止一款产品，你的是下面哪一款？",
}


def ui(text: str, lang: str = "en", **kwargs: object) -> str:
    """The customer-facing form of a fixed string.

    `lang` accepts either a locale ("zh-CN") or a detected language code ("zh"); only
    the first two characters decide. An untranslated string comes back as it went in,
    so a new English literal is merely untranslated, never broken.
    """
    if (lang or "en").lower()[:2] == "zh":
        text = ZH.get(text, text)
    return text.format(**kwargs) if kwargs else text
