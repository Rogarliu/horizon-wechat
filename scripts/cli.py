#!/usr/bin/env python3
"""统一入口。

    python scripts/cli.py doctor              验证公众号接口权限
    python scripts/cli.py preview --demo      用样例数据看排版
    python scripts/cli.py run --dry-run       只产出预览，不碰微信
    python scripts/cli.py draft               用已生成的 items.json 建草稿（不重新采集）
    python scripts/cli.py run                 采集 + 渲染 + 建草稿
    python scripts/cli.py draft               用已生成的 items.json 建草稿（不重新采集）
    python scripts/cli.py verify              反读草稿核对内容
    python scripts/cli.py sources             配置了哪些源 + 上一期实际出处分布
    python scripts/cli.py status              查看最近运行记录
    python scripts/cli.py clean --days 30     清理过期草稿与索引
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date as Date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
OUT = ROOT / "out"
sys.path.insert(0, str(SCRIPTS))

import cover  # noqa: E402
import notify  # noqa: E402
import render  # noqa: E402
import state  # noqa: E402
import translate  # noqa: E402
import wx  # noqa: E402

# 中文源白名单（按 config 里的 name 匹配）。不用 TLD 判断：Solidot 是 .org
# 但内容全是中文，靠域名分类会把「版面被中文源占满」这个真问题判成已解决。
CHINESE_FEEDS = {
    "IT之家", "Solidot", "InfoQ 中文", "量子位", "少数派",
    "爱范儿", "极客公园", "阮一峰的网络日志",
}

DEFAULT_CFG = {
    "wechat": {
        "appid_env": "WX_APPID",
        "secret_env": "WX_SECRET",
        "author": "科技辉说",
        "content_source_url": "",
        "need_open_comment": 1,
    },
    "horizon": {"repo_path": "~/Horizon", "data_dir": "data", "runner": "uv"},
    "digest": {
        "title_prefix": "科技辉说AI日报",
        "brand": "科技辉说",
        "tagline": "AI 资讯精选",
        "headline_count": 3,
        "worth_count": 7,
        "scan_count": 8,
        "title_mode": "auto",
        "link_list": True,
        "lead": "",
        "max_per_event": 0,
        "max_per_source": 3,
    },
    "style": {"accent": "#2f6df6"},
    "filter": {
        "block_patterns": [
            "all the news about", "everything we know", "everything apple announced",
            "everything announced", "live blog", "live updates", "live-blog",
            "news-announcements", "story-stream", "recap", "roundup",
            "早报", "晚报", "午报", "盘点", "汇总", "一周回顾", "速览", "全知道",
        ],
        "block_keywords": [
            "儿童性虐待", "性虐待", "性侵", "强奸", "猥亵", "色情",
            "裸照", "血腥", "屠杀", "自杀", "割腕", "恐怖袭击",
        ],
        # 广告和聚合页必须在打分之前剔掉：它们标题党、命中率高，会挤掉真新闻。
        # 这份默认值不能省——config.json 缺失时（新克隆）仍然要过滤，
        # 否则「广告当资讯」和「整版都是一篇汇总」两个问题会一起复发。
        "ad_patterns": [
            "percent off", "black friday", "prime day", "cyber monday",
            "promo code", "best deals", "deal of the day", "gift guide",
            "sponsored content", "advertorial", "affiliate",
            "优惠券", "领券", "限时折扣", "到手价", "软文", "促销活动",
        ],
    },
    "translate": {
        "enabled": True,
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "batch_size": 8,
    },
    "state": {"seen_window_days": 7, "keep_drafts_days": 30},
}


def load_config() -> dict:
    path = ROOT / "config.json"
    if not path.exists():
        print(f"⚠️  未找到 {path}，使用默认配置")
        print(f"   建议：cp config.example.json config.json 后按需修改")
        return DEFAULT_CFG
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # 浅合并，缺字段用默认值补齐
    merged = json.loads(json.dumps(DEFAULT_CFG))
    for section, values in cfg.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def _env(cfg: dict, key: str) -> str:
    name = cfg["wechat"].get(key, "")
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(
            f"缺少环境变量 {name}（由 config.json 的 wechat.{key} 指定）\n"
            f"  export {name}=..."
        )
    return value


def _find_uv() -> str | None:
    """uv 常被 pip 装进 venv 而不在 PATH 上，所以多探几个位置。"""
    found = shutil.which("uv")
    if found:
        return found
    for cand in (
        Path.home() / ".workbuddy-ai/binaries/python/envs/default/bin/uv",
        Path.home() / ".local/bin/uv",
        Path.home() / ".cargo/bin/uv",
    ):
        if cand.exists():
            return str(cand)
    return None


def _fetched_of(day: str) -> int:
    """读回采集阶段的抓取条数；读不到就返回 0，导语自动退回「本期共 N 条」。"""
    path = OUT / f"stats-{day}.json"
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("fetched", 0))
    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return 0


# ── 采集 ─────────────────────────────────────────────────
def collect(
    cfg: dict, hours: int, *, no_enrich: bool = False, stats_out: Path | None = None
) -> list[dict]:
    repo = Path(os.path.expanduser(cfg["horizon"]["repo_path"]))
    if not (repo / "src" / "orchestrator.py").exists():
        raise SystemExit(
            f"Horizon 仓库不存在或未初始化：{repo}\n"
            f"  git clone https://github.com/Thysrael/Horizon.git {repo}"
        )

    bridge = str(SCRIPTS / "bridge.py")
    common = [
        "--repo", str(repo),
        "--data-dir", cfg["horizon"].get("data_dir", "data"),
        "--hours", str(hours),
        "--lang", "zh",
    ]
    if no_enrich:
        common.append("--no-enrich")
    if stats_out:
        common += ["--stats-out", str(stats_out)]

    # 聚合页和广告都在打分前剔除：聚合页会被话题去重当成「正主」，
    # 把同一事件的真新闻判为重复丢掉；广告则纯粹是浪费打分和富化的钱。
    flt = cfg.get("filter", {}) or {}
    patterns = list(flt.get("block_patterns") or []) + list(flt.get("ad_patterns") or [])
    if patterns:
        common += ["--drop-patterns", json.dumps(patterns, ensure_ascii=False)]

    # 出处限额：打分只看质量不看产量，IT之家一天 60 条、The Verge 只有 10 条，
    # 不设限额的话版面自然被高产媒体吃掉（实测 15 条里 9 条来自国内源）。
    per_src = int((cfg.get("digest") or {}).get("max_per_source") or 0)
    if per_src > 0:
        common += ["--max-per-source", str(per_src)]

    runner = cfg["horizon"].get("runner", "uv")
    uv_bin = _find_uv() if runner == "uv" else None
    if uv_bin:
        cmd = [uv_bin, "run", "python", bridge, *common]
    else:
        if runner == "uv":
            print("⚠️  未找到 uv，回退为直接调用 Python")
        venv_py = repo / ".venv" / "bin" / "python"
        py = str(venv_py) if venv_py.exists() else sys.executable
        cmd = [py, bridge, *common]

    print(f"▶ 调用 Horizon：{' '.join(cmd)}", flush=True)

    # stdout 落到临时文件而不是管道：整份 JSON 可能有上百 KB，
    # 管道缓冲区（64KB）写满后子进程会阻塞，而我们此时正逐行读 stderr，
    # 谁也不让谁，直接死锁。文件没有这个上限。
    # 顺带解决另一个问题：capture_output 要等进程结束才吐日志，
    # 跑 20 分钟全程静默，出问题根本没法定位。
    with tempfile.TemporaryFile("w+", encoding="utf-8") as out_fh:
        proc = subprocess.Popen(
            cmd, cwd=str(repo), stdout=out_fh, stderr=subprocess.PIPE, text=True
        )
        assert proc.stderr is not None
        for line in proc.stderr:
            print(f"  {line.rstrip()}", flush=True)
        code = proc.wait()
        out_fh.seek(0)
        stdout = out_fh.read()

    if code != 0:
        raise SystemExit(f"Horizon 流水线失败（退出码 {code}）")

    try:
        return json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"bridge 输出不是合法 JSON：{exc}\n前 300 字符：{stdout[:300]}")


# ── 命令 ─────────────────────────────────────────────────
def _drop_by_patterns(items: list[dict], cfg: dict) -> list[dict]:
    """丢弃聚合页 / 话题页 / 早报 / 广告导购。

    为什么要单独一道：
    The Verge 的「All the news about iPhone Duo」这类话题页，RSS 时间戳
    会随每次更新刷新，所以在 Horizon 看来是「9 小时前的新内容」，
    实际讲的是 9 月的发布会。只按 published_at 过滤永远拦不住它。

    广告是另一类：Wired 的 feed 里 18% 是「Best Dyson Week Deals」
    「20 Percent Off Sitewide」这种返利导购，混进日报会直接毁掉可信度。
    实测就是因为这个把 Wired 整条源排除了——单靠关键词只能拦掉一部分，
    剩下「What Type of Mattress Is Right for You?」这种不带促销字样的软文拦不住。

    特征在英文原题和 URL 里（`-news-announcements`、`all the news about`），
    所以这一道必须在翻译**之前**跑，否则标题已经变成中文，特征就丢了。
    """
    flt = cfg.get("filter", {}) or {}
    roundups = [p.lower() for p in (flt.get("block_patterns") or []) if p]
    ads = [p.lower() for p in (flt.get("ad_patterns") or []) if p]
    if not roundups and not ads:
        return items

    kept, hits = [], []
    for it in items:
        hay = " ".join(
            str(it.get(k) or "") for k in ("title", "title_original", "url")
        ).lower()
        word = next((p for p in roundups if p in hay), None)
        reason = "聚合页"
        if not word:
            word = next((p for p in ads if p in hay), None)
            reason = "广告导购"
        # 结构特征兜底：一条标题里三个以上分号，多半是早报合集，
        # 这类标题不含「早报」字样，关键词匹配不到
        if not word:
            t = str(it.get("title") or "")
            if t.count("；") >= 2 or t.count("; ") >= 2:
                word, reason = "多事件合集", "聚合页"
        if word:
            hits.append((it, reason, word))
        else:
            kept.append(it)

    for it, reason, word in hits:
        print(f"  ⛔ {reason}过滤（命中「{word}」）：{it.get('title', '')[:44]}")
    if hits:
        print(f"✓ 丢弃 {len(hits)} 条聚合/广告类内容")
    return kept


def _apply_blocklist(items: list[dict], cfg: dict) -> list[dict]:
    """按关键词丢弃合规风险内容。

    为什么模型打分之外还要这一层：
    analysis.md 第 7 条要求敏感题材压到 3 分以下，实测确实生效过一次
    （「副检察长用 AI 制作儿童性虐待材料」被过滤）。但那是概率性的，
    同一批里给 7 分、下一批给 2 分都可能。合规不能赌概率，
    所以关键词命中就直接丢，不问分数。

    代价是可能误杀——比如「未成年人模式上线」这种正常产品新闻。
    对日报来说，少一条远好过被删文，所以宁可从严。
    """
    words = [w for w in (cfg.get("filter", {}).get("block_keywords") or []) if w]
    if not words:
        return items

    kept, hits = [], []
    for it in items:
        hay = f"{it.get('title', '')} {it.get('summary', '')}"
        word = next((w for w in words if w in hay), None)
        if word:
            hits.append((it, word))
        else:
            kept.append(it)

    for it, word in hits:
        print(f"  ⛔ 合规过滤（命中「{word}」）：{it.get('title', '')[:40]}")
    if hits:
        print(f"✓ 合规过滤掉 {len(hits)} 条")
    return kept


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", (text or "").lower())
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) > 1 else set()


def _proper_tokens(text: str) -> set[str]:
    """抓英文/数字专名：iPhone Duo、GPT-6、K3.1。

    中文专名（华为、小米）抓不到，所以这条只当辅助信号，不能单独用来判重。
    """
    return {m.lower() for m in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{2,}", text or "")}


def _same_event(a: dict, b: dict) -> bool:
    """两条是不是同一件事。

    两个信号叠加：标题字符二元组的 Jaccard 重合度，以及共有的英文专名。
    阈值是在 15 条真实数据上标定的——同事件的 iPhone Duo 三条互相 0.22–0.34，
    其余任意两条都低于 0.15，中间有足够间隔。

    只看专名不行：「OpenAI」能同时出现在三条完全不同的新闻里。
    只看重合度也不行：「苹果发布 X」和「苹果回应 X」字面很近，其实是两件事。
    """
    ja, jb = _bigrams(a.get("title", "")), _bigrams(b.get("title", ""))
    j = len(ja & jb) / max(1, len(ja | jb))
    if j >= 0.30:
        return True
    shared = _proper_tokens(a.get("title", "")) & _proper_tokens(b.get("title", ""))
    return bool(shared) and j >= 0.18


def _merge_same_event(items: list[dict], cfg: dict) -> list[dict]:
    """同一事件最多保留 N 条（分数高的优先）。

    默认关闭（`digest.max_per_event = 0`）。这是编辑决策不是技术决策：
    苹果开售那天，「拆机说明书 / 屏幕问题 / 首日越狱」确实是三条不同的新闻，
    砍掉哪条都可能可惜；但它们也可能一口气占掉三成版面。
    要开就把 `max_per_event` 设成 2。
    """
    cap = int((cfg.get("digest") or {}).get("max_per_event") or 0)
    if cap <= 0 or len(items) < 2:
        return items

    ordered = sorted(items, key=lambda x: x.get("score") or 0, reverse=True)
    groups: list[list[dict]] = []
    dropped: set[int] = set()

    for it in ordered:
        for group in groups:
            if _same_event(group[0], it):
                if len(group) < cap:
                    group.append(it)
                else:
                    dropped.add(id(it))
                    print(
                        f"  ⏭ 同事件合并（同「{group[0].get('title', '')[:16]}…」）："
                        f"{it.get('title', '')[:34]}"
                    )
                break
        else:
            groups.append([it])

    if dropped:
        print(f"✓ 同事件合并掉 {len(dropped)} 条，剩 {len(items) - len(dropped)} 条")
    return [it for it in items if id(it) not in dropped]


def cmd_doctor(args) -> int:
    import doctor
    # 传空 argv：否则 cli 的子命令名 "doctor" 会被 doctor 的 argparse 当成未知参数
    return doctor.main([])


def cmd_preview(args) -> int:
    cfg = load_config()
    day = args.day or Date.today().isoformat()

    if args.demo:
        items = render.DEMO_ITEMS
        # demo 用样例的分段条数，否则真实配置的 worth_count 会把样例切得很难看
        cfg = {**cfg, "digest": {**cfg.get("digest", {}), **render.DEMO_CFG["digest"]}}
    else:
        src = OUT / "items.json"
        if not src.exists():
            print(f"未找到 {src}，先跑一次 `cli.py run --dry-run`")
            return 1
        items = json.loads(src.read_text(encoding="utf-8"))

    if getattr(args, "translate", False):
        translate.translate_items(items, cfg.get("translate", {}))

    article = render.render_digest(items, day, cfg, fetched=_fetched_of(day))
    problems = render.validate(article)
    if problems:
        for p in problems:
            print("⚠️ ", p)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    # demo 单独一个文件名：都用 preview-<day>.html 的话，
    # 看一眼样例排版就会把当天的真实预览覆盖掉（实测踩过一次）
    name = "preview-demo.html" if getattr(args, "demo", False) else f"preview-{day}.html"
    out = OUT / name
    out.write_text(render.render_preview(article, day), encoding="utf-8")
    head = (items[0]["title"] if items else "")
    cover_name = "cover-demo.jpg" if getattr(args, "demo", False) else f"cover-{day}.jpg"
    cover.make_cover(day, head, OUT / cover_name,
                     accent=cfg["style"]["accent"])
    print(f"标题  {article['title']}  ({len(article['title'])}/32)")
    print(f"正文  {len(article['content'])} 字符")
    print(f"预览  {out}")
    return 0


def cmd_run(args) -> int:
    cfg = load_config()
    day = args.day or Date.today().isoformat()
    OUT.mkdir(parents=True, exist_ok=True)

    if state.is_done(day) and not args.force:
        print(f"⏭  {day} 已生成过草稿，跳过（--force 强制重跑）")
        return 0
    if args.force:
        state.reset_run(day)

    # 先验 token 再采集。白名单没配好时，采集 + 打分 + 富化要花三分钟和一整轮
    # API 费用，最后才卡在「上传封面」那一步——前面全白做。
    # 这一步只花一秒，失败就立刻停，并把当前 IP 打出来。
    if not args.dry_run:
        try:
            wx.get_token(_env(cfg, "appid_env"), _env(cfg, "secret_env"))
            clear_blocked_ip()
        except wx.WxError as exc:
            state.mark_run(day, "failed")
            if exc.errcode == 40164:
                ip = wx.public_ip()
                note_blocked_ip(ip)
                notify.failure(f"IP {ip} 不在白名单")
                copied = _copy_to_clipboard(ip)
                print(f"❌ 出口 IP {ip} 不在白名单" + ("（已复制到剪贴板）" if copied else ""))
                print("   mp.weixin.qq.com → 设置与开发 → 基本配置 → 开发密钥 → API IP白名单 → 设置名单")
                cands = _cidr_candidates(ip)
                if cands:
                    print(f"   官方支持 CIDR，填 {cands[0]} 可免去同网段内换 IP 的重复操作")
                print("   白名单为空会被一律拒绝。加好后跑 `cli.py draft` 用已生成的内容建草稿。")
            else:
                print(f"❌ 微信接口 {exc.errcode}：{exc.errmsg}")
            return 1
        except Exception as exc:
            state.mark_run(day, "failed")
            print(f"❌ {type(exc).__name__}: {exc}")
            return 1

    state.mark_run(day, "collecting")

    stats_path = OUT / f"stats-{day}.json"
    try:
        items = collect(cfg, args.hours, no_enrich=args.no_enrich, stats_out=stats_path)
    except SystemExit as exc:
        state.mark_run(day, "failed")
        notify.failure(str(exc))
        print(f"❌ {exc}")
        return 1

    fetched = _fetched_of(day)
    print(f"✓ 采集 {len(items)} 条")
    if not items:
        state.mark_run(day, "empty")
        notify.failure("今日无内容")
        print("今天没有值得发的内容，未创建草稿（这是正常情况）")
        return 0

    # 跨运行排重
    window = int(cfg["state"].get("seen_window_days", 7))
    fresh = state.filter_unseen([i["url"] for i in items], window_days=window)
    dropped = len(items) - len(fresh)
    if dropped:
        items = [i for i in items if i["url"] in fresh]
        print(f"✓ 排除 {dropped} 条 {window} 天内出现过的内容，剩 {len(items)} 条")

    if not items:
        state.mark_run(day, "empty")
        notify.failure("内容全部重复")
        print("内容全部与近期重复，未创建草稿")
        return 0

    # 聚合页过滤。必须在翻译前跑：The Verge 的「All the news about X」
    # 这类话题页时间戳会随更新刷新，但讲的是几个月前的事，
    # 特征只在英文原题和 URL 里，翻成中文就认不出来了。
    items = _drop_by_patterns(items, cfg)
    if not items:
        state.mark_run(day, "empty")
        notify.failure("内容全是聚合页")
        print("今天的内容全是聚合/盘点类，未创建草稿")
        return 0

    # 英文标题与摘要翻成中文。放在渲染之前、写 items.json 之前，
    # 这样预览文件、封面头条、摘要用的都是中文，不会出现半英半中。
    translate.translate_items(items, cfg.get("translate", {}))

    # 合规硬闸放在翻译之后：关键词是中文的，英文条目翻完才可能命中。
    # 评分口径里已经要求敏感题材压到 3 分以下，但模型打分是概率性的——
    # 同一条换个批次可能就给 7 分。合规不能赌概率。
    items = _apply_blocklist(items, cfg)
    if not items:
        state.mark_run(day, "empty")
        notify.failure("内容全部被合规过滤")
        print("今天的内容全部被合规过滤，未创建草稿")
        return 0

    # 同事件合并放在翻译之后：中文标题才好算字符重合度。
    # 默认关闭，见 _merge_same_event 的说明。
    items = _merge_same_event(items, cfg)

    (OUT / "items.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return _render_and_publish(items, day, cfg, fetched=fetched, dry_run=args.dry_run)


def _render_and_publish(
    items: list[dict], day: str, cfg: dict, *, fetched: int = 0, dry_run: bool = False
) -> int:
    """渲染 → 校验 → 预览 → 封面 → （非 dry-run 时）建草稿。

    `run` 和 `draft` 共用这一段：两条路走同一套排版和校验，
    不会出现「审的是一份、发的是另一份」。
    """
    article = render.render_digest(items, day, cfg, fetched=fetched)
    problems = render.validate(article)
    if problems:
        state.mark_run(day, "failed")
        for p in problems:
            print(f"❌ {p}")
        return 1

    preview = OUT / f"preview-{day}.html"
    preview.write_text(render.render_preview(article, day), encoding="utf-8")

    cover_path = cover.make_cover(
        day, items[0]["title"], OUT / f"cover-{day}.jpg", accent=cfg["style"]["accent"]
    )
    print(f"✓ 渲染完成  标题「{article['title']}」 正文 {len(article['content'])} 字符")
    print(f"  预览 {preview}")

    if dry_run:
        state.mark_run(day, "previewed", count=len(items))
        print("\n--dry-run：未调用微信接口。白名单就绪后跑 `cli.py draft` 用这份内容建草稿")
        return 0

    return _publish(article, cover_path, items, day, cfg)


def _publish(article: dict, cover_path: Path, items: list[dict], day: str, cfg: dict) -> int:
    """上传封面 + 建草稿。`run` 和 `draft` 共用这一段。"""
    appid = _env(cfg, "appid_env")
    secret = _env(cfg, "secret_env")

    state.mark_run(day, "drafting", count=len(items))
    try:
        token = wx.get_token(appid, secret)
        thumb_id = wx.add_permanent_image(token, cover_path)
        media_id = wx.add_draft(
            token,
            {
                "title": article["title"],
                "author": cfg["wechat"].get("author", "科技辉说"),
                "digest": article["digest"],
                "content": article["content"],
                "content_source_url": cfg["wechat"].get("content_source_url", ""),
                "thumb_media_id": thumb_id,
                "need_open_comment": int(cfg["wechat"].get("need_open_comment", 1)),
                "only_fans_can_comment": 0,
            },
        )
    except wx.WxError as exc:
        state.mark_run(day, "failed")
        if exc.errcode == 40164:
            ip = wx.public_ip()
            note_blocked_ip(ip)
            copied = _copy_to_clipboard(ip)
            print(f"\n当前出口 IP：{ip}" + ("（已复制到剪贴板）" if copied else ""))
            print("  mp.weixin.qq.com → 设置与开发 → 基本配置 → 开发密钥 → API IP白名单 → 设置名单")
            cands = _cidr_candidates(ip)
            if cands:
                print(f"  官方支持 CIDR，填 {cands[0]} 可免去同网段内换 IP 的重复操作")
            print("  白名单为空会被一律拒绝，必须显式加入这个 IP")
            notify.failure(f"IP {ip} 不在白名单")
        else:
            notify.failure(f"微信接口 {exc.errcode}")
        print(f"❌ {exc}")
        return 1
    except Exception as exc:
        state.mark_run(day, "failed")
        notify.failure(str(exc))
        print(f"❌ {type(exc).__name__}: {exc}")
        return 1

    state.mark_run(day, "drafted", media_id=media_id, count=len(items))
    state.mark_seen(items, day)
    clear_blocked_ip()
    notify.success(len(items), media_id)

    print(f"\n✅ 草稿已创建（media_id={media_id}）")
    print("   下一步：打开「订阅号助手」App → 草稿箱 → 确认后群发")
    return 0


def cmd_draft(args) -> int:
    """用 out/items.json 里已经审阅过的内容建草稿，不重新采集。

    为什么要这个命令：`--dry-run` 只渲染不建草稿。等白名单 / 权限修好再跑一遍
    `run`，会重新采集、重新打分，选出来的条目很可能和刚审过的不是同一批——
    「审的是这份、发的是那份」是最不该出现的事。这个命令保证所见即所发。
    """
    cfg = load_config()
    day = args.day or Date.today().isoformat()
    src = OUT / "items.json"
    if not src.exists():
        print(f"没有 {src}，先跑一次 `cli.py run --dry-run`")
        return 1

    items = json.loads(src.read_text(encoding="utf-8"))
    if not items:
        print("items.json 是空的")
        return 1

    # 文件可能是昨天留下的。日期写错会让标题和内容对不上，宁可拦下来。
    age_h = (time.time() - src.stat().st_mtime) / 3600
    if age_h > 12 and not args.force:
        print(f"⚠️  items.json 是 {age_h:.1f} 小时前生成的，可能不是今天的")
        print("   确认要用它建草稿就加 --force，或重新跑 `cli.py run --dry-run`")
        return 1

    print(f"用 {src} 的 {len(items)} 条内容建草稿（不重新采集）")
    fetched = _fetched_of(day)
    return _render_and_publish(items, day, cfg, fetched=fetched)


def _copy_to_clipboard(text: str) -> bool:
    """把 IP 塞进剪贴板，省掉「看屏幕→手敲 13 位数字」这一步。"""
    cmds = (
        ["pbcopy"],
        ["xclip", "-selection", "clipboard"],
        ["clip"],
    )
    for cmd in cmds:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text, text=True, check=True, timeout=5)
            return True
        except (subprocess.SubprocessError, OSError):
            continue
    return False


def _cidr_candidates(ip: str) -> list[str]:
    """给出覆盖该 IP 的网段候选，供白名单填写。

    微信官方文档明确支持 CIDR（`172.0.0.1/24`，不支持 `172.0.0.*`）：
    填 /24 能扛住同一网段内的漂移（光猫重拨常见），/16 能扛住换子网。
    代价是范围越大，白名单的防护越弱——真正的凭据还是 AppSecret，
    所以别一上来就填 /8。
    """
    parts = ip.split(".")
    if len(parts) != 4:
        return []
    try:
        a, b, c = (int(p) for p in parts[:3])
    except ValueError:
        return []
    return [f"{a}.{b}.{c}.0/24", f"{a}.{b}.0.0/16"]


BLOCKED_IP_FILE = OUT / "whitelist-needed.txt"


def note_blocked_ip(ip: str) -> None:
    """把「白名单缺哪个 IP」落到磁盘，而不是只发一条通知。

    为什么：macOS 通知一闪而过。实测 10-19 到 10-24 连续六天 40164，
    每期都发了通知，但没人注意到——内容全停在 `previewed`，草稿一条没建成。
    磁盘上的文件不会消失，`cli.py status` 每次都会把它念一遍。
    """
    cands = _cidr_candidates(ip)
    lines = [
        f"{Date.today().isoformat()} 出口 IP {ip} 不在白名单",
        f"可填网段：{'、'.join(cands) if cands else '（无法推算）'}",
        "mp.weixin.qq.com → 设置与开发 → 基本配置 → 开发密钥 → API IP白名单 → 设置名单",
        "加好后跑 `python scripts/cli.py draft`，用已生成的内容建草稿（不会重新采集）",
    ]
    try:
        BLOCKED_IP_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def clear_blocked_ip() -> None:
    """白名单通了就把提示删掉，免得 status 里一直挂着一个过期的警告。"""
    try:
        BLOCKED_IP_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def cmd_ip(args) -> int:
    """查出口 IP，并和上次成功的 IP 比对——这个号所在网络会漂移。"""
    ip = wx.public_ip()
    last = state.get_meta("last_ok_ip")

    print(ip)
    if last:
        print(f"上次成功：{last}", file=sys.stderr)
        if last != ip:
            print("⚠️  IP 已变化，白名单需要更新", file=sys.stderr)

    if getattr(args, "ranges", False):
        cands = _cidr_candidates(ip)
        if cands:
            print("可填的白名单网段（支持 CIDR）：", file=sys.stderr)
            for c in cands:
                print(f"  {c}", file=sys.stderr)

    if getattr(args, "copy", False) and _copy_to_clipboard(ip):
        print("（已复制到剪贴板）", file=sys.stderr)
    return 0


def cmd_sources(args) -> int:
    """列出信息源，以及最近一期入选的条目实际来自哪些媒体。

    为什么要看「实际来源」而不只是配置：配了 18 个源不代表版面就均衡，
    IT之家 一天能出 60 条、The Verge 只有 10 条，最后谁占版面是数量和
    打分共同决定的。这个命令把两边摆在一起，来源单一一眼就能看出来。
    """
    cfg = load_config()
    hcfg = Path(os.path.expanduser(cfg["horizon"]["repo_path"])) / "data" / "config.json"
    if not hcfg.exists():
        print(f"没有 {hcfg}")
        return 1

    c = json.loads(hcfg.read_text(encoding="utf-8"))
    rss = c.get("sources", {}).get("rss", []) or []
    on = [f for f in rss if f.get("enabled", True)]
    off = [f for f in rss if not f.get("enabled", True)]

    print(f"配置中的 RSS：{len(on)} 启用 / {len(off)} 关闭")
    # 按名字标语言，不按 TLD：Solidot 是 .org 但内容是中文，
    # 用域名判断会把「中文源占版面」这个真问题判成已经解决了。
    zh = [f for f in on if f.get("name", "") in CHINESE_FEEDS]
    print(f"  其中中文源 {len(zh)} 个、英文源 {len(on) - len(zh)} 个")
    for f in on:
        mark = "中" if f.get("name", "") in CHINESE_FEEDS else "英"
        print(f"  ✓ [{mark}] {f.get('name', '(无名)'):<22} {f.get('url')}")
    if off:
        print("  已关闭：" + "、".join(f.get("name", "?") for f in off))

    others = []
    for key, label in (("hackernews", "Hacker News"), ("reddit", "Reddit"),
                       ("twitter", "X (Twitter)"), ("telegram", "Telegram"),
                       ("google_news", "Google News"), ("gdelt", "GDELT")):
        v = c.get("sources", {}).get(key) or {}
        if isinstance(v, dict):
            others.append(f"  {'✓' if v.get('enabled') else '✗'} {label}")
    if c.get("sources", {}).get("github"):
        others.append(f"  ✓ GitHub ×{len(c['sources']['github'])}")
    print("\n其他来源：")
    print("\n".join(others))

    items_path = OUT / "items.json"
    if items_path.exists():
        items = json.loads(items_path.read_text(encoding="utf-8"))
        counts = Counter(str(i.get("source_name") or "未知") for i in items)
        cap = int((cfg.get("digest") or {}).get("max_per_source") or 0)
        hint = f"（限额每家 ≤{cap} 条）" if cap else "（未设限额）"
        print(f"\n最近一期入选 {len(items)} 条的出处分布{hint}：")
        for name, n in counts.most_common():
            flag = "  ⚠️ 超限" if cap and n > cap else ""
            print(f"  {name:<26} ×{n}{flag}")
        zh_n = sum(
            n for name, n in counts.items() if name.split(" · ")[0] in CHINESE_FEEDS
        )
        print(f"  中文源 {zh_n} 条 / 英文源 {len(items) - zh_n} 条")
    return 0


def cmd_verify(args) -> int:
    """反读微信侧的草稿，核对它到底存了什么。

    draft/add 返回 media_id 只说明请求被接受，不说明内容存对了。
    这一步把微信返回的 content 拿回来做一遍合规审计，
    是「接口说成功」和「真的成功了」之间唯一的证据。
    """
    cfg = load_config()
    day = args.day or Date.today().isoformat()

    media_id = args.media_id
    if not media_id:
        run = state.get_run(day)
        if not run or not run.get("media_id"):
            print(f"{day} 没有记录 media_id，用 --media-id 指定")
            return 1
        media_id = run["media_id"]

    token = wx.get_token(_env(cfg, "appid_env"), _env(cfg, "secret_env"))
    data = wx.get_draft(token, media_id)
    news = data.get("news_item") or []
    if not news:
        print(f"草稿里没有图文：{data}")
        return 1

    a = news[0]
    content = a.get("content", "")
    print(f"media_id  {media_id[:32]}…")
    print(f"标题      {a['title']}  ({len(a['title'])}/32)")
    print(f"作者      {a.get('author', '')}  ({len(a.get('author', ''))}/16)")
    print(f"摘要      {a.get('digest', '')}  ({len(a.get('digest', ''))}/120)")
    print(f"正文      {len(content)} 字符  (上限 20000)")
    print(f"封面      {a.get('thumb_media_id', '')[:28]}…")
    print(f"原文链接  {a.get('content_source_url') or '（空）'}")

    checks = [
        ("无 <style> 标签", not re.search(r"<style", content, re.I)),
        ("无 class 属性", not re.search(r"\sclass=", content, re.I)),
        ("无 h1–h6", not re.search(r"<h[1-6][\s>]", content, re.I)),
        ("无外链图片", not re.search(r'<img[^>]+src="(?!https://mmbiz)', content, re.I)),
        ("无 rem/em 单位", not re.search(r":\s*[\d.]+(?:rem|em)\b", content)),
        ("正文非空", bool(content.strip())),
        ("未超 2 万字符", len(content) < 20_000),
    ]
    print()
    bad = 0
    for name, ok in checks:
        print(f"{'✓' if ok else '✗'} {name}")
        bad += 0 if ok else 1

    if bad:
        print(f"\n❌ {bad} 项不合规，建议删草稿重跑")
        return 1
    print("\n✅ 微信侧内容与预期一致，可以去后台群发了")
    return 0


def cmd_ready(args) -> int:
    """一次性列出所有前置条件，缺什么一眼看到。"""
    cfg = load_config()
    ok = True

    def line(flag: bool, name: str, detail: str = "") -> None:
        nonlocal ok
        if not flag:
            ok = False
        print(f"{'✓' if flag else '✗'} {name:<24} {detail}")

    repo = Path(os.path.expanduser(cfg["horizon"]["repo_path"]))
    line(repo.exists(), "Horizon 仓库", str(repo))

    uv = _find_uv()
    line(bool(uv), "uv", uv or "未找到（回退到 .venv/bin/python）")

    hcfg_path = repo / "data" / "config.json"
    key_env = ""
    if hcfg_path.exists():
        try:
            hcfg = json.loads(hcfg_path.read_text(encoding="utf-8"))
            ai = hcfg.get("ai", {})
            key_env = ai.get("api_key_env", "")
            sources = hcfg.get("sources", {})
            rss = sources.get("rss", []) or []
            n_rss = sum(1 for s in rss if s.get("enabled", True))
            n_off = len(rss) - n_rss
            line(
                True,
                "Horizon 配置",
                f"{ai.get('provider')} / {ai.get('model')} / "
                f"{ai.get('languages')} / 上限 {hcfg.get('digest', {}).get('max_items')}",
            )
            # n_rss 是「已启用」的数量，n_off 是关掉的。写成
            # 「13 个 RSS（6 个已关）」会被读成「13 个里关了 6 个」，
            # 所以把「启用」两个字写出来。
            active = [f"{n_rss} 个 RSS 启用" + (f"（另有 {n_off} 个已关）" if n_off else "")]
            if (sources.get("hackernews") or {}).get("enabled"):
                active.append("HN")
            if (sources.get("reddit") or {}).get("enabled"):
                active.append("Reddit")
            n_gh = len(sources.get("github") or [])
            if n_gh:
                active.append(f"GitHub×{n_gh}")
            line(n_rss > 0, "信息源", " + ".join(active))
        except (ValueError, OSError) as exc:
            line(False, "Horizon 配置", f"读取失败：{exc}")
    else:
        line(False, "Horizon 配置", "data/config.json 不存在")

    if key_env:
        has_key = bool(os.environ.get(key_env))
        line(has_key, f"LLM Key ({key_env})", "已设置" if has_key else "未设置")

    appid = os.environ.get(cfg["wechat"]["appid_env"], "")
    secret = os.environ.get(cfg["wechat"]["secret_env"], "")
    line(bool(appid and secret), "微信凭证", "已设置" if appid and secret else "未设置")

    if appid and secret:
        try:
            wx.get_token(appid, secret)
            line(True, "微信 token", "可用")
        except wx.WxError as exc:
            line(False, "微信 token", f"[{exc.errcode}] {exc.errmsg[:48]}")
        except Exception as exc:
            line(False, "微信 token", f"{type(exc).__name__}: {exc}")

    print()
    if ok:
        print("全部就绪，可以跑：cli.py run --dry-run")
    else:
        print("有未就绪项，按上面提示处理后再跑 cli.py ready")
    return 0 if ok else 1


def cmd_status(args) -> int:
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_run ORDER BY day DESC LIMIT 10"
        ).fetchall()
        n = conn.execute("SELECT COUNT(*) c FROM seen_url").fetchone()["c"]
    if not rows:
        print("还没有运行记录")
    for r in rows:
        print(f"{r['day']}  {r['status']:<10} {r['item_count']:>3} 条  {r['media_id'] or ''}")
    print(f"\n已见索引 {n} 条")

    # 白名单没配好时，这里必须吵一次：通知会消失，这个文件不会。
    if BLOCKED_IP_FILE.exists():
        print("\n⚠️  白名单待处理：")
        for line in BLOCKED_IP_FILE.read_text(encoding="utf-8").splitlines():
            print(f"   {line}")
    return 0


def cmd_clean(args) -> int:
    cfg = load_config()
    days = args.days or int(cfg["state"].get("keep_drafts_days", 30))
    cutoff = (Date.today() - timedelta(days=days)).isoformat()

    appid = _env(cfg, "appid_env")
    secret = _env(cfg, "secret_env")
    token = wx.get_token(appid, secret)

    with state.connect() as conn:
        rows = conn.execute(
            "SELECT day, media_id FROM daily_run WHERE day < ? AND media_id IS NOT NULL",
            (cutoff,),
        ).fetchall()

    removed = 0
    for r in rows:
        try:
            wx.delete_draft(token, r["media_id"])
            removed += 1
        except Exception:
            pass  # 已群发的草稿会被自动移除，删除报错属正常

    pruned = state.prune(days=60)
    print(f"清理草稿 {removed}/{len(rows)} 条，索引 {pruned} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Horizon 每日资讯 → 公众号草稿")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="验证公众号接口权限")

    p_prev = sub.add_parser("preview", help="渲染预览，不碰微信")
    p_prev.add_argument("--demo", action="store_true")
    p_prev.add_argument("--day")
    p_prev.add_argument("--translate", action="store_true", help="顺带把英文翻成中文")

    p_run = sub.add_parser("run", help="完整流程")
    p_run.add_argument("--hours", type=int, default=24)
    p_run.add_argument("--day")
    p_run.add_argument("--dry-run", action="store_true", help="只渲染，不建草稿")
    p_run.add_argument("--force", action="store_true", help="忽略当天已完成状态")
    p_run.add_argument("--no-enrich", action="store_true", help="跳过富化，省成本")

    sub.add_parser("status", help="查看运行记录")
    p_ip = sub.add_parser("ip", help="查当前出口 IP（更新白名单用）")
    p_ip.add_argument("--copy", action="store_true", help="同时复制到剪贴板")
    p_ip.add_argument("--ranges", action="store_true", help="同时给出可填的 CIDR 网段")

    p_verify = sub.add_parser("verify", help="反读草稿核对内容")
    p_verify.add_argument("--media-id", default="", help="不填则取当天记录的")
    p_verify.add_argument("--day", default="", help="默认今天")
    sub.add_parser("ready", help="检查所有前置条件")
    sub.add_parser("sources", help="列出信息源与最近一期的出处分布")

    p_draft = sub.add_parser("draft", help="用 out/items.json 建草稿，不重新采集")
    p_draft.add_argument("--day", default="", help="默认今天")
    p_draft.add_argument("--force", action="store_true", help="items.json 超过 12 小时也照用")

    p_clean = sub.add_parser("clean", help="清理过期草稿与索引")
    p_clean.add_argument("--days", type=int)

    args = ap.parse_args()
    handler = {
        "doctor": cmd_doctor,
        "preview": cmd_preview,
        "run": cmd_run,
        "status": cmd_status,
        "ip": cmd_ip,
        "verify": cmd_verify,
        "ready": cmd_ready,
        "sources": cmd_sources,
        "draft": cmd_draft,
        "clean": cmd_clean,
    }[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
