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

# --add-virtual-file=<路径>:<内容>（git 2.38+）：不落临时文件，直接把提交号写进包内固定位置。
git archive --format=tar \
  --add-virtual-file="deploy/DEPLOY_HEAD:$HEAD_SHORT" \n  "$REF" ticket_desk deploy tests $(git ls-tree --name-only "$REF" web 2>/dev/null) > "$OUT"

# ★★打完必须**回验**,不许只打印一句「已写进包内」就完事。
#   本脚本第一版就是这么错的:用了一个根本不存在的 git 选项
#   (--add-file-with-prefix),错误被 2>/dev/null 吞掉、退回分支也没落对位置,
#   而脚本照样打印「部署头已写进包内」——**包里其实什么都没有**。
#   那正是本仓反复栽的「静默失败」形状:报成功、没做成、没人发现。
PACKED="$(tar xOf "$OUT" deploy/DEPLOY_HEAD 2>/dev/null | tr -d '[:space:]' || true)"
if [[ "$PACKED" != "$HEAD_SHORT" ]]; then
  echo "拦下:部署头没能写进包里(读回「${PACKED:-空}」,期望「$HEAD_SHORT」)。" >&2
  echo "  多半是这台机器的 git 不支持 --add-virtual-file(需要 2.38+):$(git --version)" >&2
  echo "  升级 git,或上服时显式带上环境变量:DEPLOY_HEAD=$HEAD_SHORT sudo bash …/update.sh …" >&2
  rm -f "$OUT"   # 半成品不留在盘上,免得有人拿它去上服
  exit 3
fi

echo "包已打好:$OUT"
echo "  来自提交:$HEAD_SHORT($REF)"
echo "  部署头已写进包内并回验通过 ⇒ update.sh 会读它自动建上服记录。"
sha256sum "$OUT"
