#!/usr/bin/env python3
"""运行状态与已见内容索引。

两个职责，都不可省：
1. daily_run —— 幂等。automation 双触发（07:00 / 09:00）时，
   第二次看到当天已 drafted 就直接退出，不会重复建草稿。
2. seen_url —— 补上 Horizon 的短板。它的去重只在单次运行内生效，
   同一篇文章可能连续两天上榜。这里按天窗口做跨运行排重。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date as Date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_HOME = Path(__file__).resolve().parent.parent / "data"
HOME = Path(os.environ.get("HORIZON_WECHAT_HOME", DEFAULT_HOME))
DB_PATH = HOME / "state.db"

TRACKING_PREFIXES = ("utm_",)
TRACKING_EXACT = {
    "_ga", "dclid", "fbclid", "gclid", "igshid", "li_fat_id",
    "mc_cid", "mc_eid", "msclkid", "ttclid", "twclid", "vero_id",
    "ref", "ref_src", "source",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_url (
    url_key    TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_score REAL
);
CREATE TABLE IF NOT EXISTS daily_run (
    day        TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    media_id   TEXT,
    item_count INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_date ON seen_url(first_seen);
"""


def normalize_url(url: str) -> str:
    """去掉追踪参数、fragment、默认端口，作为去重 key。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()

    host = (parts.hostname or "").lower()
    port = parts.port
    if (parts.scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    netloc = host if port is None else f"{host}:{port}"

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_EXACT
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, urlencode(kept), ""))


def connect() -> sqlite3.Connection:
    HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ── 每日运行状态 ──────────────────────────────────────────
def get_run(day: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM daily_run WHERE day = ?", (day,)).fetchone()
    return dict(row) if row else None


def is_done(day: str) -> bool:
    """当天已经建过草稿（或正在建）就视为完成。"""
    run = get_run(day)
    return bool(run and run["status"] in {"drafting", "drafted"})


def mark_run(day: str, status: str, *, media_id: str | None = None, count: int = 0) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO daily_run (day, status, media_id, item_count, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(day) DO UPDATE SET
                status=excluded.status,
                media_id=COALESCE(excluded.media_id, daily_run.media_id),
                item_count=excluded.item_count,
                updated_at=excluded.updated_at
            """,
            (day, status, media_id, count),
        )


def reset_run(day: str) -> None:
    """--force 时清掉当天状态，允许重建草稿。

    必须连当天的「已见 URL」一起清：这些 URL 是今天那轮跑出来的，
    如果留着，重跑时它们全被判成「7 天内出现过」而滤掉，
    结果是草稿被删了却再也重建不出内容。
    """
    with connect() as conn:
        conn.execute("DELETE FROM daily_run WHERE day = ?", (day,))
        conn.commit()
    forget_seen(day)


# ── 已见索引 ─────────────────────────────────────────────
def filter_unseen(urls: list[str], *, window_days: int = 7) -> set[str]:
    """返回窗口期内没出现过的 URL 原始值集合。

    窗口而非永久去重：一条内容隔了三周再次成为热点，是应该重新上榜的。
    """
    if not urls:
        return set()

    cutoff = (Date.today() - timedelta(days=window_days)).isoformat()
    keys = {u: normalize_url(u) for u in urls}

    with connect() as conn:
        rows = conn.execute(
            "SELECT url_key FROM seen_url WHERE first_seen >= ?", (cutoff,)
        ).fetchall()
    seen = {r["url_key"] for r in rows}
    return {u for u, k in keys.items() if k not in seen}


def mark_seen(items: list[dict], day: str | None = None) -> None:
    day = day or Date.today().isoformat()
    rows = [
        (normalize_url(it.get("url", "")), day, it.get("score"))
        for it in items
        if it.get("url")
    ]
    if not rows:
        return
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO seen_url (url_key, first_seen, last_score) VALUES (?, ?, ?)
            ON CONFLICT(url_key) DO UPDATE SET
                last_score=excluded.last_score
            """,
            rows,
        )


def forget_seen(day: str) -> int:
    """删掉某天首次见到的 URL，返回删除条数。

    由 reset_run 调用，不单独对外——「重建当天日报」只有一个入口，
    多一个入口就多一种状态不一致的可能。
    """
    with connect() as conn:
        cur = conn.execute("DELETE FROM seen_url WHERE first_seen = ?", (day,))
        conn.commit()
    return cur.rowcount


def prune(days: int = 60) -> int:
    """清理过期记录，避免库无限增长。"""
    cutoff = (Date.today() - timedelta(days=days)).isoformat()
    with connect() as conn:
        cur = conn.execute("DELETE FROM seen_url WHERE first_seen < ?", (cutoff,))
        return cur.rowcount


# ── 杂项键值（记录上次成功的出口 IP）──────────────────────
def get_meta(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_run ORDER BY day DESC LIMIT 10"
            ).fetchall()
        for r in rows:
            print(f"{r['day']}  {r['status']:<10} {r['item_count']:>3} 条  {r['media_id'] or ''}")
    else:
        print(f"db = {DB_PATH}")
        with connect() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM seen_url").fetchone()["c"]
        print(f"已记录 {n} 条已见 URL")
