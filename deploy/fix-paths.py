#!/usr/bin/env python3
"""把 Horizon 配置里的本机绝对路径改写成服务器上的路径。

Horizon 的 data/config.json 里存着 profiles_dir 之类的绝对路径。从 Mac 传过来
就是 /Users/xxx/.workbuddy-ai/skills/horizon-wechat/profiles，服务器上必然
找不到，报错是 "No profiles found under /Users/..."——看着像文件没传，其实是
路径没换。

这个脚本被 deploy.sh 和 bootstrap.sh 共用：
  - deploy.sh 每次同步完都跑一遍（包括 SKIP_BOOTSTRAP=1 的快速部署）
  - bootstrap.sh 在装完依赖后跑一遍
两边都跑不会出问题：已经是服务器路径时原样返回，输出「没有本机绝对路径」。

用法：
  python3 fix-paths.py --config ~/Horizon/data/config.json --skill-dir ~/horizon-wechat
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def walk(node, skill_dir: str, hits: list, path: str = ""):
    if isinstance(node, dict):
        return {k: walk(v, skill_dir, hits, f"{path}.{k}") for k, v in node.items()}
    if isinstance(node, list):
        return [walk(v, skill_dir, hits, path) for v in node]
    if isinstance(node, str) and node.startswith("/Users/"):
        parts = node.split("/horizon-wechat/", 1)
        if len(parts) == 2:
            new = os.path.join(skill_dir, parts[1])
            hits.append((path, node, new))
            return new
        hits.append((path, node, None))
    return node


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.expanduser("~/Horizon/data/config.json"))
    ap.add_argument("--skill-dir", default=os.path.expanduser("~/horizon-wechat"))
    args = ap.parse_args()

    config_path = os.path.expanduser(args.config)
    skill_dir = os.path.expanduser(args.skill_dir)

    if not os.path.exists(config_path):
        print(f"  ! 找不到 {config_path}，跳过")
        return 1

    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)

    hits: list = []
    cfg = walk(cfg, skill_dir, hits)

    if any(new for _, _, new in hits):
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    for path, old, new in hits:
        if new:
            print(f"  ✓ {path}: {old} → {new}")
        else:
            print(f"  ! {path} 仍是本机路径且无法映射：{old}")
    if not hits:
        print("  ✓ 没有本机绝对路径")

    # 校验：改完之后 profiles_dir 必须真的能读到
    profiles_dir = (cfg.get("processing") or {}).get("profiles_dir")
    if profiles_dir:
        if os.path.isdir(profiles_dir):
            print(f"  ✓ profiles_dir 可读：{profiles_dir}")
        else:
            print(f"  ✗ profiles_dir 不存在：{profiles_dir}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
