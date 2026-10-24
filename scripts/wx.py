"""微信公众号 API 封装。

只封装本方案用到的五个接口：
  stable_token / media.uploadimg / material.add_material / draft.add / draft.delete

设计约束：
1. 所有含中文的请求体必须 ensure_ascii=False 后手动 utf-8 encode。
   微信文档明确要求"不要使用 Unicode 转义格式"，用 httpx 的 json= 参数
   在某些版本下会转义中文。
2. token 走 stable_token 而非 access_token。前者与后者隔离，不会互相踢下线。
3. 错误统一抛 WxError，附带面向人的修复提示。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

import env as _env  # noqa: F401  读 .env，import 时即完成

BASE = "https://api.weixin.qq.com/cgi-bin"
# 状态目录放在 skill 内部，保持自包含。
# 不用 ~/.horizon-wechat：某些沙箱环境会拦掉那里的 SQLite 文件操作，
# 报 "disk I/O error"，排查起来很浪费时间。
DEFAULT_HOME = Path(__file__).resolve().parent.parent / "data"
TIMEOUT = 60.0

HOME = Path(os.environ.get("HORIZON_WECHAT_HOME", DEFAULT_HOME))
TOKEN_FILE = HOME / "token.json"

# 错误码 -> 人话
HINTS = {
    40001: "token 失效，通常是被其它进程刷新过；脚本会自动 force_refresh 重试一次",
    40013: "AppID 不合法，检查是否有空格或大小写错误",
    40125: "AppSecret 无效，去后台重置后更新 .env",
    40164: "出口 IP 不在白名单。去后台把当前 IP 加进 API IP白名单（空名单=全部拒绝，不能靠清空放行）",
    45009: "接口调用超过天级限额",
    45011: "调用太频繁，等一分钟再试",
    48001: "该账号没有此接口权限（可能未认证或账号类型不支持）",
    53404: "账号被限制带货能力",
}


class WxError(RuntimeError):
    """微信接口返回了非 0 的 errcode。"""

    def __init__(self, payload: dict[str, Any]):
        self.errcode = payload.get("errcode")
        self.errmsg = payload.get("errmsg", "")
        self.hint = HINTS.get(self.errcode, "")
        msg = f"微信接口错误 [{self.errcode}] {self.errmsg}"
        if self.hint:
            msg += f"\n  → {self.hint}"
        super().__init__(msg)


def _json_body(payload: dict[str, Any]) -> bytes:
    """中文安全的 JSON 编码。"""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _parse(resp: httpx.Response, *, context: str) -> dict[str, Any]:
    """把响应解析成 dict；非 JSON（通常是网关错误页）单独报错。"""
    if resp.status_code != 200:
        raise RuntimeError(
            f"{context} HTTP {resp.status_code}: {resp.text[:300]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"{context} 返回了非 JSON 内容（可能是网关错误页）：{resp.text[:300]}"
        ) from exc
    if data.get("errcode"):
        raise WxError(data)
    return data


def public_ip() -> str:
    """查当前出口 IP，用于 40164 排查。失败返回 unknown。"""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            r = httpx.get(url, timeout=8)
            if r.status_code == 200 and r.text.strip():
                return r.text.strip()
        except Exception:
            continue
    return "unknown"


def get_token(appid: str, secret: str, *, force: bool = False) -> str:
    """获取并缓存 access_token。

    缓存留 120 秒提前量，避免卡在过期边界上偶发 40001。
    """
    if not force and TOKEN_FILE.exists():
        try:
            cached = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if cached.get("appid") == appid and cached.get("expire_at", 0) > time.time() + 120:
                return cached["token"]
        except (ValueError, KeyError):
            pass  # 缓存损坏就当没有

    resp = httpx.post(
        f"{BASE}/stable_token",
        content=_json_body(
            {
                "grant_type": "client_credential",
                "appid": appid,
                "secret": secret,
                "force_refresh": force,
            }
        ),
        headers={"Content-Type": "application/json"},
        timeout=20.0,
    )
    data = _parse(resp, context="stable_token")

    token = data["access_token"]
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "appid": appid,
                "token": token,
                "expire_at": time.time() + int(data.get("expires_in", 7200)),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(TOKEN_FILE)
    TOKEN_FILE.chmod(0o600)
    return token


def upload_body_image(token: str, path: str | Path) -> str:
    """上传正文图片，返回 mmbiz.qpic.cn 域名 URL。

    限制：仅 jpg/png，单张 <= 1MB。外部图片 URL 会被微信过滤，必须先过这里。
    """
    path = Path(path)
    if path.stat().st_size > 1_048_576:
        raise ValueError(f"正文图片超过 1MB：{path} ({path.stat().st_size} bytes)")
    with path.open("rb") as fh:
        resp = httpx.post(
            f"{BASE}/media/uploadimg",
            params={"access_token": token},
            files={"media": (path.name, fh, _mime(path))},
            timeout=TIMEOUT,
        )
    data = _parse(resp, context="media/uploadimg")
    if "url" not in data:
        raise RuntimeError(f"media/uploadimg 未返回 url：{data}")
    return data["url"]


def add_permanent_image(token: str, path: str | Path) -> str:
    """上传封面图，返回永久 media_id。

    draft/add 的 thumb_media_id 必须是永久素材，不能用 media/upload 的临时素材。
    """
    path = Path(path)
    with path.open("rb") as fh:
        resp = httpx.post(
            f"{BASE}/material/add_material",
            params={"access_token": token, "type": "image"},
            files={"media": (path.name, fh, _mime(path))},
            timeout=TIMEOUT,
        )
    data = _parse(resp, context="material/add_material")
    if "media_id" not in data:
        raise RuntimeError(f"material/add_material 未返回 media_id：{data}")
    return data["media_id"]


def add_draft(token: str, article: dict[str, Any]) -> str:
    """新增一条图文草稿，返回 media_id。不消耗群发额度。"""
    resp = httpx.post(
        f"{BASE}/draft/add",
        params={"access_token": token},
        content=_json_body({"articles": [article]}),
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    data = _parse(resp, context="draft/add")
    if "media_id" not in data:
        raise RuntimeError(f"draft/add 未返回 media_id：{data}")
    return data["media_id"]


def get_draft(token: str, media_id: str) -> dict[str, Any]:
    """反读草稿。用于验证「接口说成功」之后微信到底存了什么。"""
    resp = httpx.post(
        f"{BASE}/draft/get",
        params={"access_token": token},
        content=_json_body({"media_id": media_id}),
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    return _parse(resp, context="draft/get")


def delete_draft(token: str, media_id: str) -> None:
    """删除草稿。用于清理探测草稿和过期日报。"""
    resp = httpx.post(
        f"{BASE}/draft/delete",
        params={"access_token": token},
        content=_json_body({"media_id": media_id}),
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    _parse(resp, context="draft/delete")


def _mime(path: Path) -> str:
    return "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
