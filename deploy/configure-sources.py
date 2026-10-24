#!/usr/bin/env python3
"""按「这台机器实际能不能抓到」来开/关信息源。

为什么需要这个：源能不能用不是配置问题，是**网络位置问题**。
同一份 config.json 在本机（家用宽带）要关掉 The Verge / BBC / Guardian /
Rest of World / NYT——代理 502、直连超时；搬到美国服务器上，这些站
全部 200 且 60ms 返回。反过来，从某些机房访问国内站也可能变慢。

所以不要在配置文件里写死「谁能用」，而是每次部署时实测一遍：
能抓到的打开，抓不到的关掉，并把理由打出来。

用法：
    python deploy/configure-sources.py                 # 用 ~/Horizon/data/config.json
    python deploy/configure-sources.py --dry-run       # 只看结论，不改文件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("缺 httpx：用 skill 的 .venv 跑这个脚本")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 候选源：缺了就补进去，已有的按实测结果决定开关。
# 这份清单是「值得配的源」，不是「一定开着的源」——开不开由下面的探测决定。
CANDIDATES = [
    # 境外
    ("Techmeme", "https://www.techmeme.com/feed.xml", "tech"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "tech-news"),
    ("TechCrunch", "https://techcrunch.com/feed/", "tech-business"),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "tech"),
    ("BBC Tech", "https://feeds.bbci.co.uk/news/technology/rss.xml", "tech"),
    ("The Guardian Tech", "https://www.theguardian.com/uk/technology/rss", "tech"),
    ("NYT Tech", "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", "tech"),
    ("Rest of World", "https://restofworld.org/feed/latest/", "tech"),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/", "tech"),
    ("CNBC Tech", "https://www.cnbc.com/id/19854910/device/rss/rss.html", "tech-business"),
    ("VentureBeat", "https://venturebeat.com/feed/", "ai-news"),
    ("OpenAI News", "https://openai.com/news/rss.xml", "ai-news"),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", "ai-news"),
    ("Cloudflare Blog", "https://blog.cloudflare.com/rss/", "systems"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/", "ai-tools"),
    # 中文
    ("IT之家", "https://www.ithome.com/rss/", "consumer-tech"),
    ("Solidot", "https://www.solidot.org/index.rss", "tech-news"),
    ("InfoQ 中文", "https://www.infoq.cn/feed", "tech-news"),
    ("量子位", "https://www.qbitai.com/feed", "ai-news"),
    ("爱范儿", "https://www.ifanr.com/feed", "tech-news"),
    ("极客公园", "https://www.geekpark.net/rss", "tech-news"),
    ("少数派", "https://sspai.com/feed", "productivity"),
]

FEED_MARKS = ("<rss", "<feed", "<rdf:rdf", "<rdf:RDF")


def probe(url: str) -> tuple[bool, str]:
    """返回 (能不能用, 说明)。判据是「200 + 看起来像 feed」，不是只看状态码：

    有的站（The Register）对爬虫返回 200 的 PoW 验证页，
    状态码漂亮但内容不是 feed，照收会在打分阶段变成一条垃圾内容。
    """
    for attempt in range(2):
        try:
            with httpx.Client(
                timeout=15, follow_redirects=True, headers={"User-Agent": UA}
            ) as c:
                r = c.get(url)
            # 429 是限流，不代表源坏了。并发探测很容易撞上，等两秒重试一次。
            if r.status_code == 429 and attempt == 0:
                time.sleep(2)
                continue
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            body = r.text[:4000]
            if not any(m in body for m in FEED_MARKS):
                return False, f"200 但不是 feed（{len(r.content)}B，像是验证页）"
            if len(r.content) < 500:
                return False, f"内容太短（{len(r.content)}B）"
            return True, f"200 / {len(r.content) // 1024}KB"
        except Exception as exc:  # 网络异常一律算不可用
            if attempt == 0:
                continue
            name = type(exc).__name__
            return False, f"{name}: {str(exc)[:40]}"
    return False, "重试后仍失败"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=os.environ.get("HORIZON_CONFIG")
        or str(Path(os.path.expanduser("~/Horizon")) / "data" / "config.json"),
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印结论，不写文件")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"找不到配置：{cfg_path}", file=sys.stderr)
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    feeds = cfg.setdefault("sources", {}).setdefault("rss", [])

    by_url = {f.get("url"): f for f in feeds}
    for name, url, cat in CANDIDATES:
        if url not in by_url:
            feeds.append(
                {"name": name, "url": url, "enabled": False,
                 "category": cat, "profile": "tech-news"}
            )
            by_url[url] = feeds[-1]

    # 只管候选清单里的源。清单外的（vLLM Blog、阮一峰、Latent Space 这类
    # 个人博客）是编辑上故意关掉的，抓得到不代表想每天收——
    # 上一版没做这个限制，一跑就把六七个博客全打开了。
    managed = [f for f in feeds if f.get("url") in {u for _, u, _ in CANDIDATES}]
    skipped = [f for f in feeds if f not in managed]
    if skipped:
        print(f"（另有 {len(skipped)} 个非候选源未改动："
              f"{'、'.join(f.get('name', '?') for f in skipped)}）")

    print(f"实测 {len(managed)} 个 RSS 源…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda f: probe(f.get("url", "")), managed))

    changed = []
    for feed, (ok, why) in zip(managed, results):
        before = bool(feed.get("enabled"))
        feed["enabled"] = ok
        if ok != before:
            changed.append((feed.get("name"), "开" if ok else "关", why))
        print(f"  {'✅' if ok else '❌'} {feed.get('name', '?'):<22} {why}")

    on = [f for f in managed if f.get("enabled")]
    print(f"\n可用 {len(on)} / {len(managed)}")
    if changed:
        print("变更：")
        for name, act, why in changed:
            print(f"  {act} {name}（{why}）")
    else:
        print("没有需要改的")

    if args.dry_run:
        print("\n--dry-run：未写文件")
        return 0

    cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已写入 {cfg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
