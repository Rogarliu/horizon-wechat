#!/usr/bin/env python3
"""第 0 步：验证公众号接口权限，10 分钟出结论。

用法：
    export WX_APPID=wx...  WX_SECRET=...
    python scripts/doctor.py

    # 或直接传参
    python scripts/doctor.py --appid wx... --secret ...

为什么要单独做这一步：官方文档对"认证订阅号能否调用 draft/add"
的适用范围表述含糊。先用最小代价验证，比写完 800 行代码再发现没权限划算。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wx  # noqa: E402
import state  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}✅{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{RED}❌{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}⚠️ {RESET} {msg}")


def step(n: int, title: str) -> None:
    print(f"\n{DIM}{n}. {title}{RESET}")


def probe_cover() -> Path:
    """生成一张探测用封面（900x383）。

    不用 1x1 像素图：微信对封面有尺寸要求，小图可能被拒，
    那样会把"尺寸不符"误判成"权限不足"。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (900, 383), "#12161c")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 10, 383], fill="#2f6df6")
    d.text((56, 170), "Horizon permission probe", fill="#ffffff")
    out = Path(tempfile.gettempdir()) / "horizon_probe_cover.png"
    img.save(out, "PNG")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="微信公众号接口权限自检")
    ap.add_argument("--appid", default=os.environ.get("WX_APPID", ""))
    ap.add_argument("--secret", default=os.environ.get("WX_SECRET", ""))
    ap.add_argument("--keep", action="store_true", help="保留探测草稿，方便去后台肉眼确认")
    args = ap.parse_args(argv)

    print("\nHorizon × 公众号 权限自检")
    print("─" * 46)

    if not args.appid or not args.secret:
        fail("缺少 WX_APPID / WX_SECRET")
        print("   export WX_APPID=wx...  WX_SECRET=...")
        return 2

    step(1, "出口 IP")
    ip = wx.public_ip()
    print(f"   当前出口 IP: {ip}")
    print(f"   {DIM}若后续报 40164，就是这个 IP 不在白名单{RESET}")

    step(2, "获取 stable_token")
    try:
        token = wx.get_token(args.appid, args.secret)
    except wx.WxError as exc:
        fail(str(exc))
        if exc.errcode == 40164:
            print(f"\n   当前出口 IP：{ip}")
            last = state.get_meta("last_ok_ip")
            if last and last != ip:
                print(f"   {YELLOW}注意：上次成功时的 IP 是 {last}，现在变成了 {ip}。")
                print(f"   说明宽带重拨过，白名单需要更新。{RESET}")
            print("   去这里把它加进白名单：")
            print("     mp.weixin.qq.com → 设置与开发 → 基本配置 → 开发密钥 → API IP白名单 → 设置名单")
            print(f"   {DIM}注意：新版后台的提示是「设置 IP 白名单后，可调用 access_token」，")
            print(f"   也就是白名单为空时一律拒绝，必须显式加入 IP，不能靠清空放行。{RESET}")
        return 1
    except Exception as exc:  # 网络问题
        fail(f"{type(exc).__name__}: {exc}")
        return 1
    ok(f"token 已获取（{args.appid[:8]}…）")
    state.set_meta("last_ok_ip", ip)

    step(3, "上传封面到永久素材库")
    media_id = None
    try:
        cover = probe_cover()
        media_id = wx.add_permanent_image(token, cover)
        ok(f"media_id = {media_id[:24]}…")
    except Exception as exc:
        fail(str(exc))
        print("   封面都传不了，后面的 draft/add 也没意义，先解决这一步。")
        return 1

    step(4, "新增草稿")
    draft_id = None
    try:
        draft_id = wx.add_draft(
            token,
            {
                "title": "权限探测（可删除）",
                "author": "Horizon",
                "digest": "这是一条用于验证 draft/add 权限的探测草稿。",
                "content": (
                    '<section style="padding:16px;">'
                    '<p style="font-size:16px;line-height:1.8;color:#3f3f3f;">'
                    "如果你在草稿箱看到这段文字，说明接口权限完整。"
                    "</p></section>"
                ),
                "thumb_media_id": media_id,
                "need_open_comment": 0,
                "only_fans_can_comment": 0,
            },
        )
        ok(f"草稿 media_id = {draft_id[:24]}…")
    except wx.WxError as exc:
        fail(str(exc))
        if exc.errcode in (48001, 48004):
            print(f"\n{YELLOW}结论：该账号没有草稿箱接口权限。{RESET}")
            print("  降级路径：用 `python scripts/cli.py preview` 生成 HTML，")
            print("  浏览器打开后全选复制，粘贴进公众号编辑器（内联样式会保留）。")
        return 1
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return 1

    step(5, "清理探测草稿")
    if args.keep:
        warn("按 --keep 保留，请到「草稿箱」手动删除")
    else:
        try:
            wx.delete_draft(token, draft_id)
            ok("已删除")
        except Exception as exc:
            warn(f"删除失败（不影响结论）：{exc}")

    print("\n" + "─" * 46)
    print(f"{GREEN}全链路权限通过，可以进入 Day 1。{RESET}")
    print(f"{DIM}下一步：在 config.json 里配好 horizon.repo_path，跑 `cli.py run --dry-run`{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
