#!/usr/bin/env python3
"""在 Horizon 的虚拟环境里跑流水线，把 ContentItem 序列化成 JSON 输出到 stdout。

为什么单独一个脚本：
Horizon 有自己的 uv 环境和一堆可选依赖（openbb / playwright），
WorkBuddy 的 Python 装不下。让这个脚本在 Horizon 环境里跑、只吐 JSON，
两边环境干净隔离。

为什么直接 import 而不解析它的 Markdown 日报：
Markdown 是给人看的产物，格式一改解析器就崩；ContentItem 是 Pydantic 模型，
字段有契约。Horizon 的 orchestrator 也明确把这些阶段标成了
"stable stage entry point for integrations such as MCP"。

用法（在 Horizon 仓库里）：
    uv run python /path/to/bridge.py --repo . --hours 24 --lang zh

注意：artifacts 是 Dict[language, ContentArtifact]，不是列表。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Horizon 抓取客户端写死 timeout=30.0（orchestrator.py:437）。
# 从美国服务器抓 IT之家（272KB）正常只要 1.5 秒，但它会瞬时限速：
# 实测被压到 3.8KB/s 时，同一份 feed 要 73 秒——30 秒必超时。
# 更坑的是 httpx 的 ReadTimeout 转字符串是空串，日志里只剩
# 「Error fetching RSS feed IT之家: 」，看着像配置错误，其实是网慢。
FETCH_TIMEOUT = 90.0


def patch_default_ua() -> None:
    """给 httpx 的默认请求头补浏览器 UA，并把抓取超时放宽。

    UA 部分：量子位这类站点按 UA 拦截，默认的 python-httpx/x.y 直接 403，
    换成浏览器 UA 立刻 200。Horizon 建客户端时没传 headers
    （orchestrator.py: httpx.AsyncClient(timeout=30.0)），
    改它的源码会在以后 git pull 时冲突，所以在自己这边拦一道。

    超时部分：只动「显式传了 ≤30 秒数字」的客户端——那正是 Horizon 的抓取
    客户端。LLM SDK 传的是 httpx.Timeout 对象，类型不匹配，不会被误改。

    用 setdefault 而不是覆盖 UA：OpenAI SDK 这类会自带 User-Agent 的客户端不受影响。
    """
    try:
        import httpx

        orig = httpx.AsyncClient.__init__
        if getattr(orig, "_ua_patched", False):
            return

        def patched(self, *args, **kwargs):
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("User-Agent", UA)
            kwargs["headers"] = headers
            timeout = kwargs.get("timeout")
            if isinstance(timeout, (int, float)) and timeout <= 30:
                kwargs["timeout"] = FETCH_TIMEOUT
            orig(self, *args, **kwargs)

        patched._ua_patched = True  # type: ignore[attr-defined]
        httpx.AsyncClient.__init__ = patched  # type: ignore[method-assign]
    except Exception as exc:  # 补丁失效不该让整条流水线挂掉
        print(f"⚠️  UA/超时补丁未生效：{exc}", file=sys.stderr)


SEARCH_TIMEOUT = 12.0


def patch_web_search() -> None:
    """给 web_search 加超时和进程内缓存。

    ddgs 实测一次要 18 秒：它在背后依次轮询 google / bing / brave / mojeek
    等多个后端，前面几个不通就一直等到各自超时。而富化是逐条串行等工具的，
    15 条内容 × 每条两个带搜索的区块，光搜索就能吃掉十几分钟
    （实测整轮从 6 分钟涨到 25 分钟）。

    超时的结果本来就是空列表——等下去不会变出结果，所以 12 秒就放弃。
    顺便按 query 缓存：同一条新闻的 background 和 impact 两个区块问的问题
    高度相似，重复搜索纯属浪费。
    """
    try:
        from src.processing.tools import WebSearchTool

        orig = WebSearchTool.execute
        if getattr(orig, "_timeout_patched", False):
            return

        cache: dict[str, list] = {}

        async def patched(self, arguments):
            query = str((arguments or {}).get("query") or "").strip()
            key = query.lower()
            if key in cache:
                return cache[key]
            try:
                results = await asyncio.wait_for(orig(self, arguments), timeout=SEARCH_TIMEOUT)
            except asyncio.TimeoutError:
                # to_thread 里的线程停不下来，但它 18 秒后自己会结束，不影响主流程
                print(f"  ⏱ web_search 超时，跳过：{query[:36]}", file=sys.stderr)
                results = []
            except Exception as exc:
                print(f"  ⚠️  web_search 失败：{exc}", file=sys.stderr)
                results = []
            cache[key] = results
            return results

        patched._timeout_patched = True  # type: ignore[attr-defined]
        WebSearchTool.execute = patched  # type: ignore[method-assign]
    except Exception as exc:  # 补丁失效不该让整条流水线挂掉
        print(f"⚠️  web_search 补丁未生效：{exc}", file=sys.stderr)


AGGREGATOR_FEEDS = {"techmeme"}

# 聚合站自己的域名。链接落在这上面时，补域名只会得到「Techmeme · techmeme.com」
# 这种把二传手写两遍的废话。
AGGREGATOR_DOMAINS = {"techmeme.com", "news.ycombinator.com"}

# Techmeme 的标题自带出处：「… (Liv McMahon/BBC)」，也有只写媒体的
# 「… (Bloomberg)」。取最后一个括号里的媒体名。这个括号在翻译之后就没了
# （「AISI 称 Mythos 5 …」），所以必须在这里、翻译之前取。
_TECHMEME_OUTLET = re.compile(r"\(([^()]{2,60})\)\s*$")

# 括号里可能是「(Video)」「(Updated)」这类修饰，不是媒体名
_NOT_OUTLET = {"video", "podcast", "audio", "updated", "live", "analysis", "opinion"}

PLATFORM_NAMES = {
    "hackernews": "Hacker News",
    "github": "GitHub",
    "reddit": "Reddit",
    "telegram": "Telegram",
    "twitter": "X",
    "openbb": "OpenBB",
    "ossinsight": "OSS Insight",
    "gdelt": "GDELT",
    "google_news": "Google News",
}


def _domain(url: Any) -> str:
    try:
        host = urlparse(str(url)).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _techmeme_outlet(title: str) -> str:
    m = _TECHMEME_OUTLET.search(title or "")
    if not m:
        return ""
    # 「Liv McMahon/BBC」取后半段；「Bloomberg」直接用
    name = m.group(1).split("/")[-1].strip()
    if not name or name.isdigit() or name.lower() in _NOT_OUTLET:
        return ""
    return name


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _same_outlet(label: str, domain: str) -> bool:
    """出处名里已经含域名主干时，别再补一遍域名。

    实测跨源合并会把 HN 的条目和 The Verge 的 RSS 条目并成一条：
    `source` 是 hackernews（于是走聚合站分支），但 `metadata.feed_name`
    被合并成 "The Verge"，拼出来就是「The Verge · theverge.com」——
    同一家媒体写了两遍，比不写还难看。
    """
    base = _norm(domain.split(".")[0])
    lab = _norm(label)
    if not base or not lab:
        return False
    return lab.startswith(base) or base.startswith(lab)


def source_name(item: Any) -> str:
    """给读者看的出处。

    `source_type` 是 rss / hackernews 这类内部枚举，直接印在日报上毫无意义。
    优先取 RSS 的 `feed_name`（The Verge / IT之家 / 量子位 这些真名），
    没有就退回平台名。GitHub 带上仓库名，读者才知道是哪条流水线又发了版本。

    聚合站要特殊处理：Hacker News 和 Techmeme 只是转发，真正的媒体是
    它链接过去的那个域名。只写「Techmeme」等于把二传手当成了信源，
    读者没法判断消息的可靠程度，所以补上原发媒体。
    """
    meta = getattr(item, "metadata", None) or {}
    feed = str(meta.get("feed_name") or "").strip()
    kind = item.source_type.value

    if kind == "hackernews" or feed.lower() in AGGREGATOR_FEEDS:
        label = feed or PLATFORM_NAMES.get(kind, kind)
        outlet = _techmeme_outlet(str(getattr(item, "title", "")))
        if outlet:
            return f"{label} · {outlet}"
        dom = _domain(getattr(item, "url", ""))
        if dom and dom not in AGGREGATOR_DOMAINS and not _same_outlet(label, dom):
            return f"{label} · {dom}"
        return label

    if feed:
        return feed

    name = PLATFORM_NAMES.get(kind, kind)
    repo = str(meta.get("repo") or "").strip()
    if kind == "github" and repo:
        return f"{name} · {repo}"
    sub = str(meta.get("subreddit") or "").strip()
    if kind == "reddit" and sub:
        return f"{name} · r/{sub}"
    return name


def _origin_key(item: Any) -> str:
    """判「同一家媒体」用的键。

    聚合站的出处是「Hacker News · theverge.com」这种复合名，按整串分组会让
    HN 转发的每条新闻各自成一组，等于没有上限；所以取「·」右边——原发媒体。
    """
    name = source_name(item)
    if " · " in name:
        return name.split(" · ", 1)[1].strip().lower()
    return name.strip().lower()


def cap_per_source(items: list, limit: int, total: int) -> list:
    """限制单家媒体在一期里的条数，让版面不被高产媒体吃掉。

    为什么需要：IT之家一天能出 60 条、Solidot 18 条，The Verge 只有 10 条。
    打分只看内容质量，不看产量，于是高产媒体自然垄断版面——实测 15 条里
    Solidot 占 5 条、IT之家 3 条，境外媒体只剩零星几条。这是「均衡」问题，
    不是「质量」问题，只能在选完之后按出处再切一刀。

    上限是软约束：切完不够 total 条时，按原始顺序把落选的补回来。
    宁可有两条同源，也不能交一期只有 9 条的日报。

    调用方要先把候选池放宽（见 run()），否则池子里只有 total 条，
    切掉的就再也补不回来了。
    """
    if limit <= 0 or len(items) <= total:
        return items

    used: dict[str, int] = {}
    keep: set[int] = set()
    overflow = []
    for it in items:
        key = _origin_key(it)
        if used.get(key, 0) < limit:
            used[key] = used.get(key, 0) + 1
            keep.add(id(it))
        else:
            overflow.append(it)

    dropped = len(overflow)
    if len(keep) < total:
        need = total - len(keep)
        for it in overflow[:need]:
            keep.add(id(it))
        if need:
            print(f"  出处限额后不足 {total} 条，补回 {min(need, dropped)} 条", file=sys.stderr)

    # 按原始顺序（即分数从高到低）输出，补回来的条目也回到它该在的位置
    out = [it for it in items if id(it) in keep][:total]
    if dropped:
        print(f"  出处限额（每家 ≤{limit} 条）：{len(items)} → {len(out)}", file=sys.stderr)
    return out


def serialize(item: Any, lang: str) -> dict:
    """把 ContentItem 拍平成渲染层需要的字段。"""
    processing = getattr(item, "processing", None)
    analysis = getattr(processing, "analysis", None) if processing else None
    classification = getattr(processing, "classification", None) if processing else None

    artifacts = getattr(processing, "artifacts", None) or {}
    artifact = artifacts.get(lang) if isinstance(artifacts, dict) else None

    blocks = []
    sources = []
    if artifact is not None:
        blocks = [
            {
                "id": b.id,
                "title": b.title,
                "content": b.content,
                "primary": getattr(b, "primary", False),
            }
            for b in artifact.blocks
        ]
        sources = [
            {"id": s.id, "title": s.title, "url": s.url}
            for s in getattr(artifact, "sources", []) or []
        ]

    return {
        "id": item.id,
        "title": item.title,
        "url": str(item.url),
        "source": item.source_type.value,
        "source_name": source_name(item),
        "author": getattr(item, "author", None),
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "score": getattr(analysis, "score", None) if analysis else None,
        "reason": getattr(analysis, "reason", "") if analysis else "",
        "summary": getattr(analysis, "summary", "") if analysis else "",
        "tags": list(getattr(analysis, "tags", []) or []) if analysis else [],
        "profile": getattr(classification, "profile", "") if classification else "",
        "blocks": blocks,
        "sources": sources,
    }


def looks_like_bundle(title: str) -> bool:
    """一条标题里塞了多个不相干的事件，多半是早报/快讯合集。

    实测抓到过：「iPhone Duo 一秒售罄，发货排到明年；马斯克：考虑自建电商平台；
    谷歌创始人：AI 正开始自己造自己」——三个事件挤在一个标题里，
    放进日报读者会以为是一条新闻，点进去才发现是合集。
    这类标题不含「早报」字样，只能靠结构特征认：分号数量。
    """
    return title.count("；") >= 2 or title.count("; ") >= 2


def drop_roundups(items: list, patterns: list[str]) -> list:
    """在打分之前剔掉聚合页 / 话题页 / 早报 / 广告导购。

    为什么必须在这里而不是渲染前：
    The Verge 的「All the news about iPhone Duo」这类话题页会刷新时间戳，
    分数通常还很高，Horizon 的话题去重会「保留它、丢掉真正的新消息」
    （实测：留了聚合页，把「iPhone Duo 一秒售罄」这条真新闻判为重复丢掉）。
    等选完再删，那条真新闻已经找不回来了。

    在这里删还能省掉这些页面和广告的打分成本。
    """
    pats = [p.lower() for p in patterns if p]
    if not pats:
        return items

    kept, hits = [], []
    for it in items:
        title = str(getattr(it, "title", ""))
        hay = f"{title} {getattr(it, 'url', '')}".lower()
        word = next((p for p in pats if p in hay), None)
        if not word and looks_like_bundle(title):
            word = "多事件合集"
        if word:
            hits.append((it, word))
        else:
            kept.append(it)

    for it, word in hits:
        print(f"  ⏭ 内容剔除（命中「{word}」）：{getattr(it, 'title', '')[:48]}", file=sys.stderr)
    if hits:
        print(f"  内容剔除 {len(hits)} 条，剩 {len(kept)} 条参与打分", file=sys.stderr)
    return kept


def write_stats(path: str, stats: dict[str, int]) -> None:
    """把漏斗各阶段条数写到磁盘，供渲染层写导语。

    为什么不塞进 stdout：stdout 只有一个用途——给 cli.py 解析的 JSON 数组。
    混入别的字段就得改解析逻辑，多一处会坏的地方。
    """
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, ensure_ascii=False)
    except OSError as exc:
        print(f"⚠️  统计文件写入失败：{exc}", file=sys.stderr)


async def run(args: argparse.Namespace) -> int:
    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(repo):
        print(f"Horizon 仓库不存在：{repo}", file=sys.stderr)
        return 2

    # Horizon 内部有 docs/_posts、profiles 等相对路径，先切到仓库根目录
    os.chdir(repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)

    # 必须在任何 Horizon 组件建 httpx 客户端之前打补丁
    patch_default_ua()

    from src.orchestrator import HorizonOrchestrator  # noqa: E402
    from src.storage.manager import StorageManager  # noqa: E402

    patch_web_search()

    storage = StorageManager(args.data_dir)
    config = storage.load_config()

    if args.lang not in (config.ai.languages or []):
        print(
            f"⚠️  config.json 的 ai.languages 不含 {args.lang!r}，"
            f"富化不会产出该语言的区块。当前：{config.ai.languages}",
            file=sys.stderr,
        )

    orch = HorizonOrchestrator(config, storage)

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    print(f"抓取窗口：{since:%Y-%m-%d %H:%M} UTC 起 {args.hours}h", file=sys.stderr)

    items = await orch.fetch_all_sources(since)
    report = orch.last_fetch_report
    if report and report.all_failed:
        print(f"全部信息源失败：{report.failure_message()}", file=sys.stderr)
        return 3
    print(f"抓取 {len(items)} 条", file=sys.stderr)

    stats = {"fetched": len(items), "merged": 0, "selected": 0, "returned": 0}

    if not items:
        write_stats(args.stats_out, stats)
        print("[]")
        return 0

    merged = orch.merge_cross_source_duplicates(items)
    stats["merged"] = len(merged)
    print(f"跨源合并后 {len(merged)} 条", file=sys.stderr)

    if args.drop_patterns:
        try:
            patterns = json.loads(args.drop_patterns)
        except json.JSONDecodeError:
            patterns = [p for p in args.drop_patterns.split(",") if p]
        merged = drop_roundups(merged, patterns)

    analyzed = await orch.analyze_items(merged)
    print("分析完成", file=sys.stderr)

    target = config.digest.max_items
    if target and args.max_per_source > 0:
        # 候选池放宽到两倍：出处限额切掉的部分要从池子里补回来，
        # 池子里只有 target 条时，切一条就少一条。
        config.digest.max_items = target * 2
        print(
            f"  候选池放宽到 {config.digest.max_items} 条（目标 {target} 条）",
            file=sys.stderr,
        )

    result = await orch.select_digest_items(analyzed)
    selected = result.items
    stats["selected"] = len(selected)
    print(f"筛选后入选 {len(selected)} 条", file=sys.stderr)

    if target and args.max_per_source > 0:
        selected = cap_per_source(selected, args.max_per_source, target)
        stats["selected"] = len(selected)

    if not selected:
        write_stats(args.stats_out, stats)
        print("[]")
        return 0

    if not args.no_enrich:
        await orch.enrich_items(selected)
        print("富化完成", file=sys.stderr)

    payload = [serialize(item, args.lang) for item in selected]
    stats["returned"] = len(payload)
    write_stats(args.stats_out, stats)
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Horizon 流水线桥接，输出 JSON")
    ap.add_argument("--repo", default=".", help="Horizon 仓库根目录")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--no-enrich", action="store_true", help="跳过富化，省成本调试用")
    ap.add_argument("--stats-out", default="", help="漏斗统计 JSON 的落盘路径")
    ap.add_argument("--drop-patterns", default="", help="聚合页标题/URL 特征（JSON 数组）")
    ap.add_argument(
        "--max-per-source",
        type=int,
        default=0,
        help="单家媒体在一期里最多几条（0=不限）；启用时会把候选池放宽到两倍",
    )
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
