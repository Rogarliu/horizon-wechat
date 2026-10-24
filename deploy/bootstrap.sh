#!/usr/bin/env bash
# 服务器端安装脚本 —— 在服务器上跑，不是在本机跑。
#
#   bash ~/horizon-wechat/deploy/bootstrap.sh
#
# 前提：deploy.sh 已经把两样东西放到 $HOME 下
#   ~/horizon-wechat/   这个 skill（含 .env、data/state.db）
#   ~/Horizon/          Horizon 仓库（不含 .venv；config.json 只在首次部署时种下）
#
# 幂等：重复跑只补缺的东西，不会重建已有 venv，也不会覆盖 .env。
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${HORIZON_REPO:-$HOME/Horizon}"
# 23:00 UTC = 07:00 北京时间。用 UTC 写死而不是写本地时间：
# 服务器可能在任何时区，写 UTC 就永远是北京时间 7 点，也不用管夏令时
# （日历里的本地时间遇上美国服务器，一年会漂两次）。
RUN_HOUR_UTC="${RUN_HOUR_UTC:-23}"
PY_VER="${HORIZON_PY:-3.12}"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()  { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[1;33m!\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -d "$SKILL_DIR/scripts" ] || die "找不到 $SKILL_DIR/scripts，先跑 deploy.sh 把文件推上来"
# config.json 是「用户自己的配置」，不进 git（见 .gitignore）。新克隆的仓库
# 只有 config.example.json，这里补一份默认的，省得部署第一步就死。
if [ ! -f "$SKILL_DIR/config.json" ]; then
    cp "$SKILL_DIR/config.example.json" "$SKILL_DIR/config.json"
    ok "config.json 不存在，已从 config.example.json 复制一份默认配置"
fi
[ -d "$REPO/src" ] || die "找不到 Horizon 仓库：$REPO（用 HORIZON_REPO=... 指定）"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null && SUDO="sudo" || die "非 root 且没有 sudo，装不了系统依赖"
fi

# ── 1. 系统依赖 ──────────────────────────────────────────
say "检查系统依赖"
PM=""
for c in apt-get dnf yum apk; do command -v "$c" >/dev/null && { PM="$c"; break; }; done
ok "包管理器：${PM:-未识别}"

if [ "$PM" = "apt-get" ]; then
    $SUDO apt-get update -qq >/dev/null 2>&1 || warn "apt-get update 失败，继续尝试安装"
    $SUDO apt-get install -y -qq curl ca-certificates python3 >/dev/null 2>&1 || warn "基础包安装有报错"
elif [ "$PM" = "dnf" ] || [ "$PM" = "yum" ]; then
    $SUDO $PM install -y -q curl ca-certificates python3 >/dev/null 2>&1 || warn "基础包安装有报错"
elif [ "$PM" = "apk" ]; then
    $SUDO apk add --no-cache curl ca-certificates python3 >/dev/null 2>&1 || warn "基础包安装有报错"
fi

# 封面图要画中文。没有中文字体时 PIL 会退化成默认字体，
# 结果是一张「方块封面」——图能生成、也能上传，但没人看得懂，最难发现的那种坏。
say "检查中文字体"
FONT_OK=0
for f in \
    /usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc \
    /usr/share/fonts/truetype/wqy/wqy-zenhei.ttc \
    /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
do
    [ -f "$f" ] && { ok "已有字体：$f"; FONT_OK=1; break; }
done
if [ "$FONT_OK" -eq 0 ]; then
    warn "缺中文字体，尝试安装 fonts-noto-cjk"
    if [ "$PM" = "apt-get" ]; then
        $SUDO apt-get install -y -qq fonts-noto-cjk fonts-wqy-zenhei >/dev/null 2>&1 || true
    elif [ "$PM" = "dnf" ] || [ "$PM" = "yum" ]; then
        $SUDO $PM install -y -q google-noto-sans-cjk-fonts wqy-zenhei-fonts >/dev/null 2>&1 || true
    elif [ "$PM" = "apk" ]; then
        $SUDO apk add --no-cache font-noto-cjk >/dev/null 2>&1 || true
    fi
fi

# ── 2. uv ────────────────────────────────────────────────
say "检查 uv"
export PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
    ok "uv 已存在：$(command -v uv) $(uv --version 2>/dev/null | awk '{print $2}')"
else
    warn "没装 uv，开始安装"
    if curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; then
        export PATH="$HOME/.local/bin:$PATH"
        ok "uv 装好了：$(uv --version | awk '{print $2}')"
    else
        # astral.sh 不通时退回 PyPI。uv 有 wheel，pip 装得上。
        python3 -m pip install --user -q uv >/dev/null 2>&1 \
            || die "uv 装不上：astral.sh 和 PyPI 都不通，检查服务器网络"
        export PATH="$HOME/.local/bin:$PATH"
        ok "uv 从 PyPI 装好了"
    fi
fi

# ── 3. Horizon 环境 ──────────────────────────────────────
# 为什么不让它用系统 python：Horizon 要 >=3.11，服务器上常见的是 3.9/3.6。
# uv 能自己下载一个 3.12 并锁在项目里，比在系统里升 python 安全得多。
say "准备 Horizon 运行环境（$PY_VER）"
cd "$REPO"
if [ -f uv.lock ]; then
    uv sync --python "$PY_VER" >/dev/null 2>&1 || uv sync >/dev/null 2>&1 || {
        warn "uv sync 失败，改用 venv + pip install -e ."
        uv venv --python "$PY_VER" .venv >/dev/null 2>&1
        uv pip install --python .venv/bin/python -e . >/dev/null
    }
else
    uv venv --python "$PY_VER" .venv >/dev/null 2>&1
    uv pip install --python .venv/bin/python -e . >/dev/null
fi
[ -x "$REPO/.venv/bin/python" ] || die "Horizon 的 .venv 没建起来"
ok "Horizon venv：$("$REPO/.venv/bin/python" -V 2>&1)"
"$REPO/.venv/bin/python" -c "import src.orchestrator" >/dev/null 2>&1 \
    && ok "能 import src.orchestrator" \
    || die "import src.orchestrator 失败，依赖没装全"

# ── 4. 修正配置里的本机绝对路径 ──────────────────────────
# 实现放在 deploy/fix-paths.py，deploy.sh 也会调它——两边共用一份逻辑，
# 避免「快速部署跳过 bootstrap」时漏修。这里只负责调用 + 校验。
say "修正配置里的本机绝对路径"
python3 "$SKILL_DIR/deploy/fix-paths.py" \
    --config "$REPO/data/config.json" \
    --skill-dir "$SKILL_DIR" \
    || die "路径修正失败：config.json 里的 profiles_dir 不可用，定时任务会崩"

# ── 5. skill 自己的环境 ──────────────────────────────────
# 只用到 Pillow（画封面）和 httpx（调微信/翻译），不需要整个 Horizon 的依赖树。
say "准备 skill 环境"
if [ ! -x "$SKILL_DIR/.venv/bin/python" ]; then
    uv venv --python "$PY_VER" "$SKILL_DIR/.venv" >/dev/null 2>&1
fi
uv pip install --python "$SKILL_DIR/.venv/bin/python" -q --upgrade Pillow httpx >/dev/null
"$SKILL_DIR/.venv/bin/python" - <<'PY'
import PIL, httpx, sys
print(f"  \033[1;32m✓\033[0m Pillow {PIL.__version__} / httpx {httpx.__version__} / {sys.version.split()[0]}")
PY

# 真去加载一次字体，而不是只看文件在不在——路径对、文件坏，一样是方块
FONT_USED=$("$SKILL_DIR/.venv/bin/python" - <<PY
import sys
sys.path.insert(0, "$SKILL_DIR/scripts")
import cover
f = cover._load_font(28)
print(getattr(f, "path", "默认字体"))
PY
)
case "$FONT_USED" in
    *"默认字体"*|*DejaVu*|*Liberation*) warn "封面会退化成默认字体（中文可能是方块）：$FONT_USED" ;;
    *) ok "封面字体：$FONT_USED" ;;
esac

# ── 6. 按实测结果决定源的开关 ────────────────────────────
# 源能不能用取决于这台机器在哪。本机（深圳家宽）必须关掉 The Verge / BBC /
# Guardian / Rest of World / NYT（代理 502、直连超时）；同一份配置搬到
# 美国服务器上，这几个站全部 200 且 60ms 返回。所以不写死，每次部署实测。
say "按实测结果调整信息源"
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/deploy/configure-sources.py" \
    || warn "源探测失败，沿用 config.json 里现有的开关"

# ── 7. 权限 ──────────────────────────────────────────────
say "收紧文件权限"
chmod 600 "$SKILL_DIR/.env" 2>/dev/null && ok ".env → 600" || warn "没有 .env，第一次跑会失败（见 SKILL.md「凭证」）"
chmod 700 "$SKILL_DIR/data" 2>/dev/null || true
mkdir -p "$SKILL_DIR/logs" "$SKILL_DIR/out"

# rsync -a 会把 Mac 的 uid/gid（501:staff）原样带过来，服务器上没有这个用户，
# ls 里就是一串数字。root 跑服务照样能写，但看着像「文件不归我」，而且万一
# 以后有人建了 uid 501 的账号，这些文件就归别人了。顺手收回 root。
chown root:root "$SKILL_DIR/.env" "$SKILL_DIR/data/state.db" 2>/dev/null || true
chown root:root "$REPO/data/config.json" 2>/dev/null || true
chown -R root:root "$SKILL_DIR/data" "$SKILL_DIR/logs" "$SKILL_DIR/out" 2>/dev/null || true

# ── 8. 定时任务 ──────────────────────────────────────────
say "配置定时任务（每天 07:00 北京时间）"
if command -v systemctl >/dev/null 2>&1; then
    RUN_USER="$(id -un)"
    $SUDO tee /etc/systemd/system/horizon-wechat.service >/dev/null <<EOF
[Unit]
Description=科技辉说AI日报：采集 + 渲染 + 建草稿
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$SKILL_DIR
# 服务器时区可能是 UTC，这里强制成北京时间，否则 Date.today() 会算成前一天，
# 标题里的日期、stats 文件名、state 的 day 全部错位。
Environment=TZ=Asia/Shanghai
# systemd 给的 PATH 很精简，uv 装在 ~/.local/bin（cli 有硬编码兜底，
# 但显式写出来省得以后换安装位置又踩一次）。
# HOME：expanduser 在 Linux 上会退回 pwd 查询，不设也能解析出 $HOME；
# 但 uv 的缓存目录、git 会直接读 \$HOME，显式设上更稳。
Environment=HOME=$HOME
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# 输出重定向到文件时 Python 是块缓冲的，不设这个 tail -f 看不到实时进度
Environment=PYTHONUNBUFFERED=1
ExecStart=$SKILL_DIR/.venv/bin/python $SKILL_DIR/scripts/cli.py run
StandardOutput=append:$SKILL_DIR/logs/run.log
StandardError=append:$SKILL_DIR/logs/run.log
TimeoutStartSec=1800
EOF

    $SUDO tee /etc/systemd/system/horizon-wechat.timer >/dev/null <<EOF
[Unit]
Description=每天 07:00 (Asia/Shanghai) 生成日报草稿

[Timer]
OnCalendar=*-*-* $RUN_HOUR_UTC:00:00 UTC
# 服务器在计划时间点处于关机/重启状态时，开机后补跑一次。
# 这一条是换到服务器的主要收益之一：本机睡眠就彻底漏掉了。
Persistent=true

[Install]
WantedBy=timers.target
EOF

    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now horizon-wechat.timer >/dev/null 2>&1
    ok "systemd 定时器已启用"
    systemctl list-timers horizon-wechat.timer --no-pager 2>/dev/null | head -3 || true
else
    warn "没有 systemd，退回 cron"
    ( crontab -l 2>/dev/null | grep -v "horizon-wechat" ; \
      echo "CRON_TZ=Asia/Shanghai" ; \
      echo "0 7 * * * cd $SKILL_DIR && ./.venv/bin/python scripts/cli.py run >> logs/run.log 2>&1" \
    ) | crontab -
    ok "crontab 已写入（cron 需支持 CRON_TZ，Debian/RHEL 新版都支持）"
fi

# ── 9. 收尾 ──────────────────────────────────────────────
say "下一步"
cat <<EOF
  1) 拿服务器出口 IP，加到公众号白名单（一次性，之后不会再漂）：
       cd $SKILL_DIR && ./.venv/bin/python scripts/cli.py ip

  2) 跑一次完整流程，确认能建出草稿：
       cd $SKILL_DIR && ./.venv/bin/python scripts/cli.py run

  3) 反读草稿核对：
       ./.venv/bin/python scripts/cli.py verify

  看日志： tail -f $SKILL_DIR/logs/run.log
  手动触发定时任务： $SUDO systemctl start horizon-wechat.service
EOF
