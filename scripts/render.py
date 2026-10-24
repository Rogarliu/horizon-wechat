#!/usr/bin/env python3
"""ContentItem -> 微信公众号 HTML。

设计原则：不走 Markdown -> HTML 转换。
手上的数据已经是结构化的，直接拼 HTML 最可控——转换器会生成 class、
嵌套 div、h1，这些在微信里全都会被剥离或重写。

用法：
    python scripts/render.py --demo --out out/preview.html
    python scripts/render.py --items out/items.json --out out/preview.html
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date as Date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

# ── 设计令牌 ───────────────────────────────────────────────
# 全部写成 px：微信不保证根字号，rem/em 会算出意外尺寸。
ACCENT = "#2f6df6"
INK = "#1a1a1a"
BODY = "#3f3f3f"
MUTED = "#a0a0a0"
RULE = "#ececec"

S = {
    "lead": (
        "margin:0 0 28px;padding:16px 18px;background:#f7f9fc;border-radius:6px;"
        "font-size:14px;line-height:1.8;color:#5a6472;"
    ),
    "section_head": "margin:36px 0 18px;",
    "section_bar": (
        f"display:inline-block;width:3px;height:15px;background:{ACCENT};"
        "vertical-align:-2px;margin-right:9px;"
    ),
    "section_title": f"display:inline;font-size:18px;font-weight:600;color:{INK};letter-spacing:0.5px;",
    "item": "margin:0 0 30px;",
    "index": f"margin:0 0 4px;font-size:12px;font-weight:600;color:{ACCENT};letter-spacing:1.5px;",
    "title": f"margin:0 0 8px;font-size:17px;font-weight:600;color:{INK};line-height:1.55;",
    "meta": f"margin:0 0 10px;font-size:12px;color:{MUTED};line-height:1.6;",
    # 出处用主色加粗：meta 行里其余信息（标签、时间、评分）都是辅助性的，
    # 只有「哪家媒体说的」是读者判断可信度的依据，值得单独给一个视觉权重
    "source": f"color:{ACCENT};font-weight:600;letter-spacing:0.3px;",
    "body": f"margin:0 0 10px;font-size:15px;line-height:1.85;color:{BODY};letter-spacing:0.3px;",
    "quote": (
        "margin:0 0 12px;padding:10px 14px;border-left:2px solid #dfe3e8;"
        "font-size:14px;line-height:1.8;color:#5f6672;"
    ),
    "quote_label": f"font-weight:600;color:#8a919c;margin-right:4px;",
    "hr": f"margin:0 0 30px;border:none;border-top:1px solid {RULE};",
    "scan_row": f"margin:0 0 10px;font-size:14px;line-height:1.8;color:{BODY};",
    "scan_num": f"color:{MUTED};margin-right:8px;",
    "scan_src": f"color:{MUTED};font-size:12px;white-space:nowrap;",
    "footer": (
        "margin:40px 0 0;padding-top:20px;border-top:1px solid #ececec;"
        "font-size:13px;line-height:1.9;color:#9aa0a8;"
    ),
    "footer_p": "margin:0 0 8px;font-size:13px;line-height:1.9;color:#9aa0a8;",
    "brand": (
        f"margin:0 0 6px;font-size:14px;font-weight:600;line-height:1.8;"
        f"color:{INK};letter-spacing:0.5px;"
    ),
    "link_item": f"margin:0 0 6px;font-size:12px;line-height:1.7;color:#8a919c;word-break:break-all;",
    "code": (
        "padding:2px 5px;background:#f2f4f6;border-radius:3px;"
        "font-size:13px;color:#c0392b;font-family:Menlo,Consolas,monospace;"
    ),
}

BLOCK_LABELS = {
    "background": "背景",
    "impact": "影响",
    "community_discussion": "讨论",
    "analysis": "解读",
    "insight": "启示",
    "solution": "方案",
    "why_it_matters": "意义",
}


def esc(text: Any) -> str:
    """转义为文本节点。quote=False：我们只放在标签之间，不进属性。"""
    return html.escape(str(text or ""), quote=False)


_INLINE_RULES = [
    # 行内代码
    (re.compile(r"`([^`]+)`"), lambda m: f'<code style="{S["code"]}">{m.group(1)}</code>'),
    # 粗体
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    # 链接：微信正文 <a> 不可点，只留文字
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    # 斜体（避开已处理的粗体）
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"<em>\1</em>"),
]


def md_inline(text: Any) -> str:
    """先转义再做行内 Markdown 替换。

    LLM 的输出几乎一定带 `code`、**bold**、[text](url) 这类标记。
    不处理的话，这些原始符号会直接显示在文章里。
    顺序不能反：先 esc 再替换，否则 esc 会把我们生成的标签也转义掉。
    """
    out = esc(text)
    for pattern, repl in _INLINE_RULES:
        out = pattern.sub(repl, out)
    return out


def plain(text: Any) -> str:
    """去掉 Markdown 标记，用于 title / digest 这类微信只收纯文本的字段。"""
    out = str(text or "")
    out = re.sub(r"`([^`]+)`", r"\1", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"\1", out)
    out = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", out)
    return out.strip()


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cut_at_punct(text: str, limit: int) -> str | None:
    """只在中文标点处断开；断不出来就返回 None，交给调用方兜底。

    查找窗口比 limit 多放 2 个字符：标点常常正好落在边界上，
    只看到 limit 会白白错过一个自然断点。

    英文标题里的空格断点很容易切出「vLLM 发布」这种半截短语，
    所以不拿空格当断点。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[: limit + 2]
    for sep in ("，", "。", "：", "；", "、", "｜", "—"):
        pos = window.rfind(sep)
        if 8 <= pos <= limit:
            return window[:pos]
    return None


def regroup(items: list[dict], cfg: dict) -> dict[str, list[dict]]:
    """按分数重排成公众号友好的三段式。

    Horizon 的日报是按 Profile 分组的（科技新闻/技术长文/金融），
    但公众号读者要的是按重要性分组。
    """
    d = cfg.get("digest", {})
    n_head = int(d.get("headline_count", 3))
    n_worth = int(d.get("worth_count", 7))
    n_scan = int(d.get("scan_count", 8))

    ordered = sorted(items, key=lambda x: x.get("score") or 0, reverse=True)
    return {
        "headline": ordered[:n_head],
        "worth": ordered[n_head : n_head + n_worth],
        "scan": ordered[n_head + n_worth : n_head + n_worth + n_scan],
    }


def _fmt_when(published_at: Any) -> str:
    """把发布时间格式化成「10-17 07:32」（本地时区）。

    用绝对时间而不是「9 小时前」：日报早上发出、读者晚上才看，
    相对时间到那时就变成误导。绝对时间不会随时间漂移，
    也让读者一眼看出这条是不是隔夜的。
    """
    if not published_at:
        return ""
    try:
        t = datetime.fromisoformat(str(published_at)).astimezone()
    except (ValueError, TypeError):
        return ""
    return f"{t.month}-{t.day:02d} {t.hour:02d}:{t.minute:02d}"


def _source_name_of(item: dict) -> str:
    """取出处：优先用 bridge 给的 `source_name`，没有就从 URL 域名推。

    为什么要域名兜底：10-18 之前生成的 items.json 里根本没有这个字段，
    直接读会渲染成空白。域名虽然不如「The Verge」好看，
    但至少能告诉读者这条来自哪个站。
    """
    name = str(item.get("source_name") or "").strip()
    if name:
        return name
    try:
        host = urlparse(str(item.get("url") or "")).netloc
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _meta_html(item: dict) -> str:
    """标题下那行小字：出处 ｜ 标签 ｜ 时间。

    出处排第一，因为读者最先想知道的是「谁说的」。老版本把标签放第一位，
    标签一多（「特斯拉 · FSD · 自动驾驶」）就把真正重要的信息挤没了。

    评分不上版面：加上出处之后一行放不下，实测「评分 9.0」会被折成两行
    （「评分」在行尾、「9.0」掉到下一行）。而且评分是内部排序口径，
    条目本来就是按它降序排的——顺序已经表达了重要性，读者不需要看分数。
    数据里仍然保留 `score`，`verify` 和排查都用得到。
    """
    parts = []
    src = _source_name_of(item)
    if src:
        parts.append(f'<span style="{S["source"]}">{esc(src)}</span>')

    tags = " · ".join((item.get("tags") or [])[:3])
    if tags:
        parts.append(esc(tags))

    when = _fmt_when(item.get("published_at"))
    if when:
        parts.append(when)

    return "　｜　".join(p for p in parts if p)


def render_item(item: dict, index: int, *, compress: bool = False) -> str:
    meta = _meta_html(item)

    # summary 单独渲染，其余区块走引用块样式
    parts = [f'<p style="{S["body"]}">{md_inline(item.get("summary"))}</p>']

    blocks = [b for b in (item.get("blocks") or []) if b.get("id") != "summary"]
    if compress:
        # 压缩模式：只留摘要，去掉所有区块。
        # 不截断区块——半句话（"币价格跌幅过跌了…"）比没有更伤阅读。
        blocks = []

    for block in blocks:
        label = BLOCK_LABELS.get(block.get("id"), block.get("title") or "")
        label_html = f'<span style="{S["quote_label"]}">{esc(label)}</span>　' if label else ""
        content = block.get("content") or ""
        if compress:
            content = _truncate(content, QUOTE_LIMIT)
        parts.append(
            f'<p style="{S["quote"]}">{label_html}{md_inline(content)}</p>'
        )

    return (
        f'<section style="{S["item"]}">'
        f'<p style="{S["index"]}">{index:02d}</p>'
        f'<p style="{S["title"]}">{md_inline(item.get("title"))}</p>'
        f'<p style="{S["meta"]}">{meta}</p>'
        + "".join(parts)
        + "</section>"
    )


def render_scan_row(item: dict, index: int) -> str:
    """速览行：标题 + 出处。速览里没有摘要，出处就是唯一能帮读者判断
    「要不要点进去」的线索，所以即使挤也要带上。

    用全角括号而不是 margin 分隔：微信对行内元素的 margin 支持不稳，
    实测会塌成零间距，标题和出处粘成「…超 GPT-6IT之家」。
    """
    src = _source_name_of(item)
    src_html = f'<span style="{S["scan_src"]}">（{esc(src)}）</span>' if src else ""
    # 序号后面补一个空格：margin 在部分客户端会塌掉，标题会粘成「11Kimi…」
    return (
        f'<p style="{S["scan_row"]}">'
        f'<span style="{S["scan_num"]}">{index:02d} </span>'
        f'{md_inline(item.get("title"))}{src_html}'
        f'</p>'
    )


def render_section(title: str) -> str:
    return (
        f'<section style="{S["section_head"]}">'
        f'<span style="{S["section_bar"]}"></span>'
        f'<span style="{S["section_title"]}">{esc(title)}</span>'
        f"</section>"
    )


def build_title(day: str, groups: dict, cfg: dict, total: int = 0) -> str:
    """标题必须 <= 32 字，否则 draft/add 直接拒绝。

    实际按 30 字收口，留 2 字余量：32 字整的标题在手机列表里会被截断，
    而且后续加后缀就会超限。头条切不出完整的半句时，退回「今日 N 条精选」。
    """
    cap = 30
    prefix = cfg.get("digest", {}).get("title_prefix", "科技辉说AI日报")
    try:
        d = Date.fromisoformat(day)
        short = f"{d.month}.{d.day}"
    except ValueError:
        short = day

    head = f"{prefix} · {short}"
    room = cap - len(head) - 1

    top = plain((groups["headline"] or [{}])[0].get("title", ""))
    mode = cfg.get("digest", {}).get("title_mode", "auto")

    # headline：无条件用头条，硬截；适合标题本身就是卖点的号
    if mode == "headline" and top:
        return f"{head}｜{top}"[:32]

    # auto：能断出完整短语就用头条，否则退回条数——半截标题比条数更伤点击
    if mode != "count" and top and room >= 8:
        cut = _cut_at_punct(top, room)
        if cut:
            return f"{head}｜{cut}"[:32]

    fallback = f"{head}｜今日 {total} 条精选" if total else head
    return fallback[:32]


def build_digest(groups: dict) -> str:
    """摘要 <= 120 字，且必须是纯文本——微信这里不接受 HTML。"""
    titles = [plain(i.get("title", "")) for i in groups["headline"][:3]]
    text = "；".join(t for t in titles if t)
    return _truncate(text, 120)


CONTENT_BUDGET = 18_000  # 微信硬上限 20000，留 2000 字符余量


def _default_lead(total: int, fetched: int = 0) -> str:
    """导语只陈述真实发生过的筛选。

    早期版本写的是「从 {total} 条候选里筛出 {headline+worth} 条」，
    但 total 是入选条数而不是候选条数，于是出现过
    「从 13 条候选里筛出 10 条」——读者以为砍掉了 3 条，其实 13 条全在文里。
    宁可少说一句，也不要给读者一个错误的数字。
    """
    if fetched > total:
        return f"本期从 {fetched} 条新内容里选出 {total} 条，按重要程度排列。"
    return f"本期共 {total} 条，按重要程度排列。"


def _assemble(
    items: list[dict], day: str, cfg: dict, *, keep_blocks: int | None = None, fetched: int = 0
) -> dict[str, Any]:
    """keep_blocks 控制「值得一读」保留多少条完整区块。

    None  = 全保留（正常情况）
    0     = 全压缩（只留摘要）
    k     = 前 k 条保留区块，其余压缩

    做成「从尾部逐条丢」而不是「一刀全砍」，是因为一刀切太浪费：
    实测 13 条超预算 1185 字符，只要砍掉最后两条的区块就够了，
    却把 10 条的区块全砍了——读者少看了 8 条的背景与影响。
    """
    groups = regroup(items, cfg)
    total = len(items)

    lead = cfg.get("digest", {}).get("lead") or _default_lead(total, fetched)

    body: list[str] = [f'<p style="{S["lead"]}">{md_inline(lead)}</p>']

    idx = 1
    if groups["headline"]:
        body.append(render_section("今日要闻"))
        for item in groups["headline"]:
            # 头条永远保留完整区块：这是整期最值钱的部分
            body.append(render_item(item, idx, compress=False))
            idx += 1

    if groups["worth"]:
        body.append(f'<hr style="{S["hr"]}"/>')
        body.append(render_section("值得一读"))
        for pos, item in enumerate(groups["worth"]):
            cut = keep_blocks is not None and pos >= keep_blocks
            body.append(render_item(item, idx, compress=cut))
            idx += 1

    if groups["scan"]:
        body.append(f'<hr style="{S["hr"]}"/>')
        body.append(render_section("快速浏览"))
        for item in groups["scan"]:
            body.append(render_scan_row(item, idx))
            idx += 1

    # 正文 <a> 在微信里不可点击，链接只能作为纯文本给读者复制
    if cfg.get("digest", {}).get("link_list", True) and groups["headline"]:
        body.append(f'<section style="{S["footer"]}">')
        body.append(f'<p style="{S["footer_p"]}">延伸阅读（长按复制）</p>')
        for i, item in enumerate(groups["headline"], 1):
            body.append(
                f'<p style="{S["link_item"]}">{i:02d} {esc(item.get("url"))}</p>'
            )
        body.append("</section>")

    brand = cfg.get("digest", {}).get("brand", "科技辉说")
    tagline = cfg.get("digest", {}).get("tagline", "AI 资讯精选")
    body.append(
        f'<section style="{S["footer"]}">'
        f'<p style="{S["brand"]}">{esc(brand)} · {esc(tagline)}</p>'
        f'<p style="{S["footer_p"]}">本期由 AI 辅助筛选整理，事实以原文为准。</p>'
        "</section>"
    )

    return {
        "title": build_title(day, groups, cfg, total),
        "digest": build_digest(groups),
        "content": "".join(body),
        "stats": {k: len(v) for k, v in groups.items()},
    }


def render_digest(
    items: list[dict], day: str, cfg: dict, *, fetched: int = 0
) -> dict[str, Any]:
    """返回 {title, digest, content}，可直接喂给 draft/add。

    带内容预算。微信 content 硬上限 20000 字符，实测 13 条真实内容能到
    19185——只剩 4% 余量，某天区块稍长就会建草稿失败。所以做两级降级：
    先压缩每条区块，仍超再减少条目。

    fetched 是抓取阶段的原始条数，只用于导语措辞（「从 70 条里选出 13 条」），
    拿不到就退回「本期共 N 条」。
    """
    budget = int(cfg.get("digest", {}).get("content_budget", CONTENT_BUDGET))

    article = _assemble(items, day, cfg, fetched=fetched)
    if len(article["content"]) <= budget:
        return article

    # 一级降级：从「值得一读」尾部逐条丢区块，丢到刚好装进预算就停
    n_worth = article["stats"].get("worth", 0)
    for k in range(n_worth - 1, -1, -1):
        trial = _assemble(items, day, cfg, keep_blocks=k, fetched=fetched)
        if len(trial["content"]) <= budget:
            trial["compressed"] = True
            trial["blocks_kept"] = k
            return trial

    # 二级降级：区块全丢了还超，才减条目
    article = _assemble(items, day, cfg, keep_blocks=0, fetched=fetched)
    article["compressed"] = True
    n = len(items)
    while n > 3 and len(article["content"]) > budget:
        n -= 1
        article = _assemble(items[:n], day, cfg, keep_blocks=0, fetched=fetched)
        article["compressed"] = True
    article["trimmed"] = len(items) - n
    return article


def validate(article: dict) -> list[str]:
    """提前拦住微信会拒绝的字段，避免建草稿时才发现。"""
    problems = []
    if len(article["title"]) > 32:
        problems.append(f"title 超长：{len(article['title'])} > 32")
    if len(article["digest"]) > 120:
        problems.append(f"digest 超长：{len(article['digest'])} > 120")
    if len(article["content"]) >= 20_000:
        problems.append(f"content 超长：{len(article['content'])} >= 20000 字符")
    if not article["content"].strip():
        problems.append("content 为空")
    return problems


def render_preview(article: dict, day: str) -> str:
    """生成可浏览器打开的预览页。

    外壳用 <style> 模拟手机宽度；正文本身仍是纯内联样式，
    和微信里的最终效果一致。
    """
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(article['title'])}</title>
<style>
  body{{margin:0;padding:32px 16px;background:#eef0f3;
       font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}}
  .phone{{max-width:420px;margin:0 auto;background:#fff;border-radius:12px;
         padding:22px 20px 40px;box-shadow:0 1px 3px rgba(0,0,0,.08);}}
  .banner{{max-width:420px;margin:0 auto 12px;font-size:12px;color:#8a919c;text-align:center;}}
  .card{{max-width:420px;margin:0 auto 20px;background:#fff;border-radius:12px;
        padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,.08);}}
  .card h1{{margin:0 0 10px;font-size:19px;line-height:1.5;color:#1a1a1a;}}
  .card p{{margin:0;font-size:13px;line-height:1.7;color:#7a808a;}}
</style></head>
<body>
<div class="banner">手机端预览 · {esc(day)}</div>
<div class="card">
  <h1>{esc(article['title'])}</h1>
  <p>{esc(article['digest'])}</p>
</div>
<div class="phone">
{article['content']}
</div>
</body></html>"""


# ── demo 数据：用于在没有 Horizon 的情况下验证排版 ────────────
DEMO_ITEMS = [
    {
        "title": "vLLM 发布 0.9，PagedAttention 换成可插拔调度器",
        "url": "https://blog.vllm.ai/2026/10/16/vllm-0-9.html",
        "source": "rss", "score": 9.4, "profile": "tech-news",
        "tags": ["推理引擎", "开源", "调度"],
        "summary": "vLLM 0.9 把 PagedAttention 的块管理从核心循环里抽出来，做成可替换的调度策略接口，同时把连续批处理的最大并发从固定窗口改成动态水位。",
        "blocks": [
            {"id": "background", "title": "背景",
             "content": "过去两个版本里，长上下文请求和短请求混跑时显存碎片率会涨到 30% 以上，团队收到的最多反馈就是「吞吐上不去」。这次重构的直接动因是这个。"},
            {"id": "impact", "title": "影响",
             "content": "对自建推理服务的团队来说，升级后长上下文场景的吞吐提升明显，但自定义过调度逻辑的 fork 需要重写适配层。"},
        ],
    },
    {
        "title": "Anthropic 公开 Claude 的工具调用失败率内部基准",
        "url": "https://www.anthropic.com/research/tool-use-bench",
        "source": "hackernews", "score": 8.9, "profile": "tech-news",
        "tags": ["Agent", "评测", "工具调用"],
        "summary": "一份覆盖 12 类工具、3800 个任务的基准，显示多步工具调用在第 4 步之后失败率陡增，主要原因是参数累积误差而非模型能力不足。",
        "blocks": [
            {"id": "background", "title": "背景",
             "content": "此前社区普遍把 Agent 失败归因于「模型不够聪明」，这份基准把失败拆成了规划错误、参数错误、状态错误三类，占比分别是 21%、54%、25%。"},
            {"id": "community_discussion", "title": "讨论",
             "content": "HN 上的高赞评论认为 54% 的参数错误说明问题在 schema 设计而不在模型，把工具参数从自由文本改成强约束枚举能直接砍掉一半失败。"},
        ],
    },
    {
        "title": "Linux 6.15 合并调度器改进，桌面响应延迟下降 40%",
        "url": "https://lwn.net/Articles/1042113/",
        "source": "rss", "score": 8.6, "profile": "tech-news",
        "tags": ["Linux", "内核", "调度"],
        "summary": "EEVDF 调度器新增了交互式任务的优先级衰减修正，在混合负载下桌面卡顿明显减少，Phoronix 的实测显示 P99 延迟从 18ms 降到 11ms。",
        "blocks": [
            {"id": "background", "title": "背景",
             "content": "EEVDF 自 6.6 进入主线后一直被诟病「编译时鼠标发飘」，这次补丁来自 Valve 的 Steam Deck 团队。"},
        ],
    },
    {
        "title": "SQLite 作者谈为什么不用 WAL2",
        "url": "https://www.sqlite.org/wal2.html",
        "source": "hackernews", "score": 8.2, "profile": "tech-blog",
        "tags": ["SQLite", "存储", "设计取舍"],
        "summary": "Richard Hipp 解释了 WAL2 分支在并发写入上的优势，以及为什么最终没有合并进主线：复杂度换来的收益只在极端写密集场景成立。",
        "blocks": [
            {"id": "insight", "title": "启示",
             "content": "值得借鉴的是他的评估框架：先问「这个场景有多少用户真的会遇到」，再问「维护成本由谁承担」。多数基础设施决策失败在第二步。"},
        ],
    },
    {
        "title": "Rust 1.85 稳定 async closure",
        "url": "https://blog.rust-lang.org/2026/10/15/Rust-1.85.0.html",
        "source": "github", "score": 7.9, "profile": "tech-news",
        "tags": ["Rust", "语言特性"],
        "summary": "async closure 正式稳定，配合 async fn in trait 补齐了异步生态的最后一块拼图，此前需要 async-trait crate 的样板代码可以删掉了。",
        "blocks": [],
    },
    {
        "title": "一个 15 年历史的 CSS 特性终于被 Safari 修好了",
        "url": "https://webkit.org/blog/16234/aspect-ratio-fix/",
        "source": "rss", "score": 7.5, "profile": "tech-blog",
        "tags": ["CSS", "浏览器"],
        "summary": "`aspect-ratio` 与 `min-height` 的冲突处理在 Safari 26 里改成和 Chrome 一致，大量依赖图片容器的响应式布局可以去掉 hack 了。",
        "blocks": [
            {"id": "impact", "title": "影响",
             "content": "如果你维护的组件库里有 `padding-top: 56.25%` 这类比例占位技巧，现在可以逐步替换。"},
        ],
    },
    {
        "title": "PostgreSQL 18 的增量备份实测",
        "url": "https://www.crunchydata.com/blog/pg18-incremental",
        "source": "rss", "score": 7.2, "profile": "tech-blog",
        "tags": ["PostgreSQL", "备份", "运维"],
        "summary": "pg_basebackup 的增量模式在 1TB 库上把每日备份窗口从 4 小时压到 12 分钟，但首次全量仍是瓶颈。",
        "blocks": [],
    },
    {
        "title": "为什么我们的 CI 从 40 分钟降到 6 分钟",
        "url": "https://blog.example.dev/ci-speedup",
        "source": "rss", "score": 6.8, "profile": "tech-blog",
        "tags": ["CI", "工程效率"],
        "summary": "三个改动：把测试分片从按文件改成按历史耗时、缓存 node_modules 的 content hash、把 lint 从 CI 挪到 pre-commit。",
        "blocks": [],
    },
    {"title": "OpenAI 调整 API 定价，长上下文档位降价 30%", "url": "https://openai.com/pricing",
     "source": "hackernews", "score": 6.5, "tags": ["API", "定价"], "summary": "", "blocks": []},
    {"title": "GitHub 推出仓库级别的依赖健康分", "url": "https://github.blog/dependency-score",
     "source": "github", "score": 6.1, "tags": ["GitHub", "供应链"], "summary": "", "blocks": []},
    {"title": "一篇讲清楚 Bloom filter 变体的长文", "url": "https://example.com/bloom",
     "source": "rss", "score": 5.8, "tags": ["算法"], "summary": "", "blocks": []},
    {"title": "某云厂商对象存储再次出现跨区延迟抖动", "url": "https://status.example.com",
     "source": "telegram", "score": 5.2, "tags": ["云服务"], "summary": "", "blocks": []},
]

DEMO_CFG = {
    "digest": {
        "title_prefix": "科技辉说AI日报",
        "headline_count": 3,
        "worth_count": 4,
        "scan_count": 5,
        "link_list": True,
    }
}


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染公众号 HTML")
    ap.add_argument("--items", help="bridge.py 输出的 JSON 文件")
    ap.add_argument("--day", default=Date.today().isoformat())
    ap.add_argument("--out", default="out/preview.html")
    ap.add_argument("--demo", action="store_true", help="用内置样例数据渲染")
    args = ap.parse_args()

    if args.demo or not args.items:
        items, cfg = DEMO_ITEMS, DEMO_CFG
        print("（demo 模式：使用内置样例数据）")
    else:
        items = json.loads(Path(args.items).read_text(encoding="utf-8"))
        cfg_path = Path(args.items).with_name("config.json")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else DEMO_CFG

    article = render_digest(items, args.day, cfg)

    problems = validate(article)
    if problems:
        print("⚠️  字段校验未通过：")
        for p in problems:
            print("   -", p)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_preview(article, args.day), encoding="utf-8")

    print(f"标题  {article['title']}  ({len(article['title'])}/32 字)")
    print(f"摘要  {len(article['digest'])}/120 字")
    print(f"正文  {len(article['content'])} 字符")
    print(f"分布  {article['stats']}")
    print(f"预览  {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
