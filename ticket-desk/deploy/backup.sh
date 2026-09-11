#!/usr/bin/env bash
# 每日备份。装进 cron,例如:
#   0 3 * * * <安装目录>/app/deploy/backup.sh --install-dir <安装目录>
# ★备份走 `ticket.py dump`,不是直接拷 .sqlite 文件:
#   服务开着 WAL,直接拷到的可能是一份**看起来没坏、实际缺最后几笔**的库,
#   而这种坏法只有恢复那天才会发现。dump 走的是与线上同一套读取口径。
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    *) echo "拦下:不认识的备份参数：$1" >&2; exit 2 ;;
  esac
done
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("拦下:工单台要求 Python 3.10 或更高版本。")
PY
# 与 install.sh / update.sh 同一套安装目录判据:绝对路径、至少两级、不是系统目录。
# 这里下面会 `rm -rf` 过期备份,所以同样一条都不能少。
[[ -n "$INSTALL_DIR" ]] || { echo "拦下:必须用 --install-dir 指定安装目录(或设环境变量 INSTALL_DIR),不设默认值。" >&2; exit 2; }
[[ "$INSTALL_DIR" == /* ]] || { echo "拦下:安装目录必须是绝对路径,收到的是 $INSTALL_DIR。" >&2; exit 2; }
[[ "$(tr -cd '/' <<<"${INSTALL_DIR%/}" | wc -c)" -ge 2 ]] || { echo "拦下:安装目录至少要两级(例如 /var/lib/ticket-desk),收到的是 $INSTALL_DIR。" >&2; exit 2; }
case "${INSTALL_DIR%/}" in
  /|/usr|/usr/*|/etc|/etc/*|/bin|/bin/*|/sbin|/sbin/*|/boot|/boot/*|/dev|/dev/*|/proc|/proc/*|/sys|/sys/*|/root)
    echo "拦下:安装目录不能是系统目录,收到的是 $INSTALL_DIR。" >&2; exit 2 ;;
esac
BACKUPS="$INSTALL_DIR/backups"
TARGET="$BACKUPS/$(date +%F)"
install -d "$TARGET"
python3 "$INSTALL_DIR/app/ticket_desk/ticket.py" dump --db "$INSTALL_DIR/db/tickets.sqlite" --to "$TARGET"
# -mtime +13 = 14 天以上的才删,今天这一份永远在。
find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d -mtime +13 -exec rm -rf -- {} +
echo "备份完成：$TARGET；仅保留最近 14 天。"
