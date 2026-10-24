#!/usr/bin/env python3
"""极简 .env 加载，三个模块共用。

单独拆出来是因为 wx.py / translate.py 都需要读 .env，
各自复制一份十行的函数不如放一处。
不引入 python-dotenv：省一个依赖，这段逻辑也不值得为它装包。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: Path | None = None) -> None:
    """只处理 KEY=VALUE 和 # 注释，不覆盖已存在的环境变量。"""
    path = path or ENV_PATH
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_dotenv()
