#!/usr/bin/env python3
"""生成公众号封面图（900x383，2.35:1）。

为什么不用 AI 生图：封面只需要「日期 + 一行标题」，模板化生成
零成本、零延迟、零幻觉，而且每天视觉一致，反而更像一个稳定的栏目。
"""

from __future__ import annotations

import sys
from datetime import date as Date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 900, 383
BG = "#12161c"
ACCENT = "#2f6df6"
INK = "#ffffff"
SUB = "#7c8492"

# (路径, ttc 内索引)。PingFang.ttc 在新版 macOS 上已无法被 PIL 读取。
FONT_CANDIDATES = [
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),   # macOS，W6 粗
    ("/System/Library/Fonts/STHeiti Medium.ttc", 1),     # macOS，黑体
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("C:/Windows/Fonts/msyhbd.ttc", 0),
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path, index in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    print("⚠️  未找到中文字体，封面文字将退化为默认字体", file=sys.stderr)
    return ImageFont.load_default(size)


def _wrap(font: ImageFont.FreeTypeFont, text: str, max_w: int, max_lines: int) -> list[str]:
    """按像素宽度换行；英文单词尽量不从中间断开。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        if font.getlength(trial) <= max_w:
            cur = trial
            continue
        if ch.isascii() and ch.isalnum() and " " in cur:
            cut = cur.rfind(" ")
            lines.append(cur[:cut])
            cur = cur[cut + 1 :] + ch
        else:
            lines.append(cur)
            cur = ch
        if len(lines) >= max_lines:
            return lines
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def _fit_headline(
    font: ImageFont.FreeTypeFont, text: str, max_w: int, max_lines: int = 2
) -> list[str]:
    """把头条压进 max_lines 行，必要时在标点处收口并加省略号。

    直接 _wrap 到 max_lines 会把句子从中间斩断（"…Anthropic 团队首次"），
    读者以为标题就是这样的半句话。宁可短一点，也要是个完整的短语。
    """
    if len(_wrap(font, text, max_w, max_lines + 1)) <= max_lines:
        return _wrap(font, text, max_w, max_lines)

    # 二分找「能装进 max_lines 的最长前缀」
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_wrap(font, text[:mid], max_w, max_lines + 1)) <= max_lines:
            lo = mid
        else:
            hi = mid - 1

    cut = text[:lo]
    for sep in ("，", "。", "：", "；", "、", "｜"):
        pos = cut.rfind(sep)
        if pos >= 8:
            cut = cut[: pos + 1]
            break

    lines = _wrap(font, cut, max_w, max_lines)
    if lines and len(cut) < len(text):
        # 收口处的逗号/顿号单独留着很怪（"…悬案落地，"），去掉再补省略号
        last = lines[-1].rstrip("，、；：,;:")
        for tail in ("…", ""):
            if font.getlength(last + tail) <= max_w:
                lines[-1] = last + tail
                break
    return lines


def make_cover(
    day: str,
    headline: str,
    out_path: str | Path,
    *,
    brand: str = "科技辉说AI日报",
    accent: str = ACCENT,
) -> Path:
    """品牌 + 日期占主视觉，头条退到第三层。

    公众号信息流里封面经常被缩到 200px 宽，那时只有最大的那行还看得清。
    所以「科技辉说AI日报」和日期必须是大字，头条标题只是锦上添花——
    读者点进来才知道今天有什么，而不是靠封面读完标题。
    """
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 左侧竖条做视觉锚点，也方便在信息流里被一眼认出
    d.rectangle([0, 0, 8, H], fill=accent)

    f_brand = _load_font(52)
    f_date = _load_font(30)
    f_title = _load_font(28)

    d.text((64, 52), brand, font=f_brand, fill=INK)

    try:
        dt = Date.fromisoformat(day)
        date_text = f"{dt.year}.{dt.month:02d}.{dt.day:02d}"
    except ValueError:
        date_text = day
    d.text((66, 128), date_text, font=f_date, fill=accent)

    # 细分割线，把品牌区和内容区分开
    d.rectangle([64, 192, W - 64, 193], fill="#2a3038")

    if headline:
        lines = _fit_headline(f_title, headline, W - 128, 2)
        y = 216
        for line in lines:
            d.text((64, y), line, font=f_title, fill="#9aa3b2")
            y += 40

    # 底部一行小字，把版面重量压住；头条只有一行时右下角不会显得空
    d.text((66, 338), "AI 资讯精选 · 每日更新", font=_load_font(18), fill="#4b525c")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".png":
        img.save(out_path, "PNG", optimize=True)
    else:
        img.save(out_path, "JPEG", quality=88, optimize=True)
    return out_path


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else Date.today().isoformat()
    head = sys.argv[2] if len(sys.argv) > 2 else "AI 编程助手进入免费时代，三家厂商同日降价"
    p = make_cover(day, head, Path(__file__).resolve().parent.parent / "out" / "cover.jpg")
    print(p)
