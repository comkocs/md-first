#!/usr/bin/env bash
set -euo pipefail
# 上服包打包器。
#
# ★为什么要有这个脚本,而不是让人手敲 git archive:
#   第一次真上服时撞到——`git archive` 打出来的包**没有 .git 目录**,
#   于是 update.sh 里那句 `git rev-parse HEAD` 永远拿不到部署头,
#   「后置闸通过之后自动建上服记录」这条路**每次都走兜底、从不生效**。
#   兜底那行提示写得再清楚,也只是把人手补单变成了常态。
#   根治办法只有一个:**打包那一刻就把提交号放进包里**。
#
# 用法:
#   bash deploy/pack.sh [输出路径] [提交号]
#   默认输出 /tmp/desk.tar,默认打当前 HEAD。
#
# 打完会把 sha256 打在最后一行——两端核指纹用它,别再另跑一次 sha256sum
# (另跑一次的人容易核错文件:本机 /tmp/desk.tar 与服务器 /tmp/desk-x.tar 同名不同路径)。

OUT="${1:-/tmp/desk.tar}"
REF="${2:-HEAD}"

# ★不靠「你在哪个目录跑」,按脚本自己的位置定位 ticket-desk/。
#   否则 `bash ticket-desk/deploy/pack.sh`(在仓根跑)会因为找不到 ticket_desk 而失败,
#   而那是个很自然的敲法。$OUT 先转成绝对路径,免得 cd 之后写错地方。
[[ "$OUT" = /* ]] || OUT="$PWD/$OUT"
cd "$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"

command -v git >/dev/null 2>&1 || { echo "拦下:找不到 git。" >&2; exit 2; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo "拦下:当前目录不在 git 仓里。" >&2; exit 2; }

HEAD_SHORT="$(git rev-parse --short=9 "$REF")"
# ★工作树脏就拦:打包打的是 $REF 那个**提交**,工作区里未提交的改动一个字都不会进包。
#   不拦的话,人改完没提交就打包上服,线上跑的是旧代码而他以为是新的——
#   这种「上服了但没生效」最难查,因为哪一步都没报错。
if [[ -n "$(git status --porcelain -- ticket_desk web)" ]]; then
  echo "拦下:ticket_desk 或 web 有未提交的改动。" >&2
  echo "  打包打的是提交 $HEAD_SHORT,工作区里没提交的东西进不了包。" >&2
  echo "  请先提交(或 git stash),再重跑本脚本。" >&2
  git status --short -- ticket_desk web >&2
  exit 2
fi

# ★ticket-desk 可能是仓根,也可能是别的仓里的一个子目录(例如 md-first/ticket-desk)。
#   后者的话,git archive 会给每个条目加上「子目录相对仓根」的前缀,包内就变成
#   ticket-desk/deploy/... —— 而下面的回验和 update.sh 都按「包内以 deploy/ 开头」找文件,
#   于是打包看着成功、回验却读不到,报出来的还是一句误导人的「git 版本太低」。
#   用 <提交>:<子目录> 这种 tree-ish 让子目录当包根,并且**从仓根执行**
#   (否则 pathspec 会被再加一次当前目录前缀),两种布局打出来的包就完全一样。
TOPLEVEL="$(git rev-parse --show-toplevel)"
SUBDIR="$(git rev-parse --show-prefix)"          # 仓根时为空;子目录时形如 ticket-desk/
TREEISH="$REF"
[[ -n "$SUBDIR" ]] && TREEISH="$REF:${SUBDIR%/}"

# --add-virtual-file=<路径>:<内容>（git 2.38+）：不落临时文件，直接把提交号写进包内固定位置。
( cd "$TOPLEVEL" && git archive --format=tar \
    --add-virtual-file="deploy/DEPLOY_HEAD:$HEAD_SHORT" \
    "$TREEISH" ticket_desk deploy tests $(git ls-tree --name-only "$TREEISH" web 2>/dev/null) ) > "$OUT"

# ★★打完必须**回验**,不许只打印一句「已写进包内」就完事。
#   本脚本第一版就是这么错的:用了一个根本不存在的 git 选项
#   (--add-file-with-prefix),错误被 2>/dev/null 吞掉、退回分支也没落对位置,
#   而脚本照样打印「部署头已写进包内」——**包里其实什么都没有**。
#   那正是本仓反复栽的「静默失败」形状:报成功、没做成、没人发现。
PACKED="$(tar xOf "$OUT" deploy/DEPLOY_HEAD 2>/dev/null | tr -d '[:space:]' || true)"
if [[ "$PACKED" != "$HEAD_SHORT" ]]; then
  echo "拦下:部署头没能写进包里(读回「${PACKED:-空}」,期望「$HEAD_SHORT」)。" >&2
  echo "  包内实际的头几项:" >&2
  tar tf "$OUT" 2>/dev/null | head -3 | sed 's/^/    /' >&2
  echo "  ——若这几项带着多余的目录前缀,是包根取错了(本脚本用 $TREEISH 当包根)。" >&2
  echo "  ——若包是空的或根本没这一项,再看 git 版本:--add-virtual-file 需要 2.38+,当前 $(git --version)" >&2
  echo "  兜底:上服时显式带上环境变量 DEPLOY_HEAD=$HEAD_SHORT sudo bash …/update.sh …" >&2
  rm -f "$OUT"   # 半成品不留在盘上,免得有人拿它去上服
  exit 3
fi

echo "包已打好:$OUT"
echo "  来自提交:$HEAD_SHORT($REF)"
echo "  部署头已写进包内并回验通过 ⇒ update.sh 会读它自动建上服记录。"
sha256sum "$OUT"
