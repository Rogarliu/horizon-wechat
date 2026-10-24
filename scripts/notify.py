#!/usr/bin/env python3
"""本地通知。失败静默——通知本身不该成为故障点。"""

from __future__ import annotations

import platform
import subprocess


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send(title: str, message: str) -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            script = (
                f'display notification "{_escape(message)}" '
                f'with title "{_escape(title)}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                check=False, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif system == "Linux":
            subprocess.run(
                ["notify-send", title, message],
                check=False, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            print(f"[{title}] {message}")
    except Exception:
        pass


def success(count: int, media_id: str = "") -> None:
    send("科技辉说AI日报已就绪", f"{count} 条入选，去草稿箱确认后群发")


def failure(reason: str) -> None:
    send("科技辉说AI日报生成失败", reason[:120])
