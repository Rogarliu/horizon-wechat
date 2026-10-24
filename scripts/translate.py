#!/usr/bin/env python3
"""把英文的标题与摘要翻成中文。

为什么需要这一步：
Horizon 的 `ai.languages = ["zh"]` 只管富化区块（背景/影响/讨论）的语言，
`item.title` 和 `analysis.summary` 始终是原文。所以不做这一步，
日报里就是中英混杂——正文中文、标题英文，读起来像半成品。

为什么不用 Horizon 自己的能力：
它的源码里没有任何翻译实现（grep translat 零命中），这是它的功能边界，
不是配置问题。

设计取舍：
- 批量翻（默认 8 条一请求），15 条只要 2 次调用，成本和延迟都可控
- 失败一律降级保留原文，绝不阻断出稿——英文总比没有强
- 专有名词保留原文，避免把 OpenAI 译成「开放人工智能」这类灾难
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import httpx

import env as _env  # noqa: F401  读 .env，import 时即完成

CJK = re.compile(r"[\u4e00-\u9fff]")

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
BATCH = 8
TIMEOUT = 90.0

SYSTEM = """你是中文科技媒体的编辑，把英文科技新闻的标题和摘要改写成简体中文。

规则：
1. 专有名词保留原文，不要音译或意译：OpenAI、Anthropic、GPT-5、CUDA、Rust、llama.cpp、Hacker News
2. 标题像新闻标题，简洁有力，不超过 40 个字，不要句末句号
3. 摘要说清「发生了什么 + 为什么值得关注」，不超过 90 个字
4. 不要添加原文没有的信息，不要评论，不要写「本文介绍了」这类废话
5. 原文是中文的条目原样返回

只输出 JSON，格式：{"items": [{"i": 0, "title": "...", "summary": "..."}]}
i 是输入的序号，必须原样带回。"""


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(CJK.findall(text)) / len(text)


def _cjk_count(text: str) -> int:
    return len(CJK.findall(text or ""))


def needs_translation(text: Any) -> bool:
    """判断一段文本是否「以英文为主」。

    阈值取 0.3 而不是 0.5：像「Meta 发布其开放权重模型 Muse Spark Open」
    这种中英混排的标题，读起来已经通了，不该再翻一遍。
    只有中文占比低于三成才算需要处理。
    """
    if not isinstance(text, str):
        return False
    text = text.strip()
    if len(text) < 4:
        return False
    return _cjk_ratio(text) < 0.3


def _post(url: str, payload: dict, key: str) -> dict:
    resp = httpx.post(
        f"{url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _parse_items(raw: str) -> list[dict]:
    """模型有时会包一层 ```json，也偶尔少写外层大括号。两种都兜住。"""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}|\[.*\]", raw, re.S)
        if not m:
            return []
        data = json.loads(m.group(0))

    if isinstance(data, dict):
        data = data.get("items") or data.get("data") or []
    return [x for x in data if isinstance(x, dict)]


def _translate_batch(
    batch: list[dict], cfg: dict, key: str, *, titles_only: bool = False
) -> dict[int, dict]:
    """翻一批，返回 {原序号: {title, summary}}。"""
    payload_in = [
        {
            "i": n,
            "title": b["title"],
            # 补翻标题时把摘要清空：摘要在第一遍已经变成中文，
            # 模型看到「中文摘要 + 英文标题」会认为这条不用处理，直接原样返回。
            "summary": "" if titles_only else b.get("summary", ""),
        }
        for n, b in enumerate(batch)
    ]
    body = {
        "model": cfg.get("model", DEFAULT_MODEL),
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload_in, ensure_ascii=False)},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    data = _post(cfg.get("base_url", DEFAULT_BASE), body, key)
    raw = data["choices"][0]["message"]["content"]
    out: dict[int, dict] = {}
    for row in _parse_items(raw):
        try:
            i = int(row["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < len(batch):
            out[i] = row
    return out


def _apply(batch: list[dict], mapping: dict[int, dict], *, titles_only: bool = False) -> int:
    """把模型返回写回原条目，返回成功条数。

    验收标准是「中文变多了」，不是「中文占比达标」。
    后者会误杀翻译正确的结果：像
    「Show HN: Gemini 4 Argon plays StarCraft」
    译成「Show HN：Gemini 4 Argon 玩转《星际争霸》」，
    专名保留得多，中文占比只有 18%，但显然已经翻好了。
    """
    done = 0
    for i, row in mapping.items():
        it = batch[i]

        new_title = (row.get("title") or "").strip()
        old_title = it.get("title") or ""
        if new_title and new_title != old_title and _cjk_count(new_title) > _cjk_count(old_title):
            # 留一份原文：聚合页、话题页的特征往往只在英文原题里
            # （"All the news about X"），翻成中文就看不出来了
            it.setdefault("title_original", old_title)
            it["title"] = new_title
            it["title_translated"] = True
            done += 1

        if titles_only:
            continue

        new_summary = (row.get("summary") or "").strip()
        old_summary = it.get("summary") or ""
        if (
            new_summary
            and new_summary != old_summary
            and _cjk_count(new_summary) > _cjk_count(old_summary)
        ):
            it["summary"] = new_summary
    return done


def _run_batches(targets: list[dict], cfg: dict, key: str, size: int, *, titles_only: bool) -> int:
    done = 0
    for start in range(0, len(targets), size):
        batch = targets[start : start + size]
        try:
            mapping = _translate_batch(batch, cfg, key, titles_only=titles_only)
        except Exception as exc:  # 翻译是增强项，失败不能拖垮整条流水线
            print(f"⚠️  第 {start // size + 1} 批翻译失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        done += _apply(batch, mapping, titles_only=titles_only)
    return done


def translate_items(
    items: list[dict],
    cfg: dict | None = None,
    *,
    verbose: bool = True,
) -> int:
    """就地翻译 items 里的 title / summary，返回实际翻译的条数。

    只动这两个字段：blocks 是 Horizon 按 languages 生成的，本来就是中文。
    """
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return 0

    key = os.environ.get(cfg.get("api_key_env", "DEEPSEEK_API_KEY"), "")
    if not key:
        if verbose:
            print("⚠️  未设置 DEEPSEEK_API_KEY，跳过翻译（标题会是原文）", file=sys.stderr)
        return 0

    targets = [
        it
        for it in items
        if needs_translation(it.get("title")) or needs_translation(it.get("summary"))
    ]
    if not targets:
        return 0

    size = int(cfg.get("batch_size", BATCH))
    done = _run_batches(targets, cfg, key, size, titles_only=False)

    # 第二遍：一批 8 条时，模型偶尔会把个别标题原样吐回来。
    # 补翻时逐条发（size=1）——混在批里它会继续偷懒，单独发基本都能翻出来。
    # 剩下的通常只有 0–3 条，多几次调用可以忽略。
    left = [it for it in targets if not it.get("title_translated")]
    if left:
        done += _run_batches(left, cfg, key, 1, titles_only=True)

    if verbose and done:
        print(f"✓ 翻译 {done} 条英文内容为中文")
    return done


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "out/items.json"
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    n = translate_items(data)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"已处理 {n} 条，写回 {path}")
