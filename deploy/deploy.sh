#!/usr/bin/env bash
# 从这台 Mac 把整条流水线推到服务器，并在服务器上装好。
#
#   HOST=root@1.2.3.4 bash deploy/deploy.sh
#   HOST=root@1.2.3.4 PORT=3333 bash deploy/deploy.sh        # 非 22 端口
#   HOST=root@1.2.3.4 SKIP_BOOTSTRAP=1 bash deploy/deploy.sh   # 只推文件
#
# 只传「代码 + 配置 + 去重索引」，不传 147MB 的 .venv：
# 服务器的 Python 版本和本机不一样，venv 搬过去必坏，必须现场建。
set -euo pipefail

: "${HOST:?用法：HOST=user@服务器IP bash deploy/deploy.sh}"
PORT="${PORT:-22}"

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="${HORIZON_REPO:-$HOME/Horizon}"
SSH_OPTS=(-p "$PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -d "$REPO_DIR/src" ] || die "本机找不到 Horizon 仓库：$REPO_DIR"
[ -f "$SKILL_DIR/.env" ] || die "本机没有 .env，凭证传不过去（见 SKILL.md「凭证」）"

say "确认服务器可达：$HOST"
ssh "${SSH_OPTS[@]}" "$HOST" 'echo "  ✓ $(hostname) / $(uname -sr) / $(id -un)"' || die "连不上 $HOST"

say "推送 skill（代码 / 配置）"
# 不推 .env 和 data/state.db：这两个是「谁在用谁说了算」的运行时文件。
# 服务器跑起来之后，本机那份 state.db 就是旧的——覆盖上去等于把服务器
# 积累的去重历史冲掉，第二天可能把昨天发过的内容再发一遍。
# .env 同理：只在服务器上改过密钥的话，重部署会被本机旧值盖回去。
rsync -az --delete \
    -e "ssh ${SSH_OPTS[*]}" \
    --exclude '.venv' --exclude 'out/' --exclude 'logs/' \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' \
    --exclude '.env' --exclude 'data/state.db' \
    "$SKILL_DIR/" "$HOST:~/horizon-wechat/"
echo "  ✓ skill 已同步（.env / state.db 保留服务器现有版本）"

# 首次部署才需要种子文件：远端没有才推，有就绝不动。
say "补齐首次部署需要的种子文件"
rsync -az --ignore-existing \
    -e "ssh ${SSH_OPTS[*]}" \
    "$SKILL_DIR/.env" "$HOST:~/horizon-wechat/.env" 2>/dev/null \
    && echo "  ✓ .env 已就位（远端已有时不覆盖）" \
    || die "推 .env 失败，检查本机 $SKILL_DIR/.env"
rsync -az --ignore-existing \
    -e "ssh ${SSH_OPTS[*]}" \
    "$SKILL_DIR/data/state.db" "$HOST:~/horizon-wechat/data/state.db" 2>/dev/null \
    && echo "  ✓ state.db 已就位（远端已有时不覆盖）" \
    || echo "  · 本机没有 state.db，跳过（服务器会自己建）"

say "推送 Horizon 仓库（不含 .venv / .git）"
# 不加 --delete：服务器上的 data/ 里有运行时产生的缓存和数据库，
# 本地没有不代表可以删。
# data/config.json 必须排除：里面的源开关是本机按「本机 IP 实测」的结果，
# 搬到服务器上本来就不对（本机 502 的源在服务器能通，反之亦然）。
# 服务器的源清单由 configure-sources.py 在服务器上实测生成，那才是准的。
rsync -az \
    -e "ssh ${SSH_OPTS[*]}" \
    --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'data/config.json' \
    "$REPO_DIR/" "$HOST:~/Horizon/"
echo "  ✓ 仓库已同步（约 10MB，不含 venv / config.json）"

# 首次部署才需要：远端没有 config.json 就推一份过去当模板。
rsync -az --ignore-existing \
    -e "ssh ${SSH_OPTS[*]}" \
    "$REPO_DIR/data/config.json" "$HOST:~/Horizon/data/config.json" 2>/dev/null \
    && echo "  ✓ config.json 已就位（远端已有时不覆盖）" \
    || echo "  · 本机没有 config.json，跳过"

# 每次部署都修一遍本机绝对路径。放在这里而不是只放在 bootstrap 里，
# 是为了让 SKIP_BOOTSTRAP=1 的快速部署也能自愈——否则一旦 config.json
# 带着 /Users/... 上去，第二天 07:00 就会 ValueError: No profiles found。
say "修正配置里的本机绝对路径"
ssh "${SSH_OPTS[@]}" "$HOST" \
    "python3 ~/horizon-wechat/deploy/fix-paths.py --config ~/Horizon/data/config.json --skill-dir ~/horizon-wechat" \
    || die "路径修正失败，服务器配置现在不可用，别让它跑定时任务"

if [ "${SKIP_BOOTSTRAP:-0}" = "1" ]; then
    echo; echo "已跳过 bootstrap，服务器上手动执行：bash ~/horizon-wechat/deploy/bootstrap.sh"
    exit 0
fi

say "在服务器上安装"
ssh "${SSH_OPTS[@]}" -t "$HOST" 'bash ~/horizon-wechat/deploy/bootstrap.sh'
