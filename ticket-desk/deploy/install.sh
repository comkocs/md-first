#!/usr/bin/env bash
set -euo pipefail
# 首次安装。只做一次;之后的每一次上服走 update.sh(它有三道前置闸)。
#
# 目录布局故意把**代码**与**状态**分开:
#   $INSTALL_DIR/app      代码,每次上服整个替换
#   $INSTALL_DIR/db       SQLite 库与服务令牌  ┐
#   $INSTALL_DIR/img      入单的图片            ├ 上服脚本一个字都不碰
#   $INSTALL_DIR/certs    证书                  ┘
#   $INSTALL_DIR/backups  backup.sh 的落点
# 分开的理由很实在:上服要能随便重来,而重来一次就丢数据的系统没人敢上服。

CONFIG=""
SOURCE=""
DATA_ROOT=""
REOPEN_SETUP=0
ROTATE_TOKEN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --reopen-setup) REOPEN_SETUP=1; shift ;;
    --rotate-token) ROTATE_TOKEN=1; shift ;;
    *) echo "拦下:不认识的安装参数：$1" >&2; exit 2 ;;
  esac
done

[[ -n "$CONFIG" && -f "$CONFIG" ]] || { echo "拦下:必须用 --config 指向 server.json(样例见 deploy/server.example.json)。" >&2; exit 2; }
[[ -n "$SOURCE" && -d "$SOURCE/ticket_desk" ]] || { echo "拦下:必须用 --source 指向工单台源码根目录(里面要有 ticket_desk/)。" >&2; exit 2; }
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("拦下:工单台要求 Python 3.10 或更高版本。")
PY

readarray -t SETTINGS < <(python3 - "$CONFIG" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8-sig"))
keys = ("域名", "端口", "安装目录", "管理员用户名", "服务名", "服务账号")
missing = [key for key in keys if str(value.get(key, "")).strip() == ""]
if missing:
    raise SystemExit("拦下:server.json 缺少：" + "、".join(missing))
for key in keys:
    print(value[key])
PY
)
DOMAIN="${SETTINGS[0]}"
PORT="${SETTINGS[1]}"
INSTALL_DIR="${SETTINGS[2]}"
ADMIN_USER="${SETTINGS[3]}"
SERVICE_NAME="${SETTINGS[4]}"
SERVICE_USER="${SETTINGS[5]}"

# 安装目录护栏,与 update.sh 同一套判据:绝对路径、至少两级、不是系统目录。
# 两处各写一套必然漂,但 shell 之间没有 import——所以这段是**有意重复**的,
# 改一处必须改另一处,DeployScriptTests 会盯着两个文件里都有「绝对路径」这句话。
[[ "$INSTALL_DIR" == /* ]] || { echo "拦下:安装目录必须是绝对路径,收到的是 $INSTALL_DIR。" >&2; exit 2; }
[[ "$(tr -cd '/' <<<"${INSTALL_DIR%/}" | wc -c)" -ge 2 ]] || { echo "拦下:安装目录至少要两级(例如 /var/lib/ticket-desk),收到的是 $INSTALL_DIR。" >&2; exit 2; }
case "${INSTALL_DIR%/}" in
  /|/usr|/usr/*|/etc|/etc/*|/bin|/bin/*|/sbin|/sbin/*|/boot|/boot/*|/dev|/dev/*|/proc|/proc/*|/sys|/sys/*|/root)
    echo "拦下:安装目录不能是系统目录,收到的是 $INSTALL_DIR。" >&2; exit 2 ;;
esac

APP="$INSTALL_DIR/app"
DB_DIR="$INSTALL_DIR/db"
IMG_DIR="$INSTALL_DIR/img"
LOG_DIR="$INSTALL_DIR/log"
CERT_DIR="$INSTALL_DIR/certs"
DB="$DB_DIR/tickets.sqlite"
TOKEN_FILE="$DB_DIR/service.token"
CERT="$CERT_DIR/server.crt"
KEY="$CERT_DIR/server.key"

id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP" "$DB_DIR" "$IMG_DIR" "$LOG_DIR" "$CERT_DIR" "$INSTALL_DIR/backups"
rm -rf "$APP/ticket_desk" "$APP/web"
cp -a "$SOURCE/ticket_desk" "$APP/ticket_desk"
[[ -d "$SOURCE/web" ]] && cp -a "$SOURCE/web" "$APP/web"

# 自签证书够用:客户端靠 TICKET_CA_SHA256 钉指纹,不靠 CA 链。
# 要换成真证书就把 $CERT/$KEY 换掉,本脚本看见文件已存在就不覆盖。
if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes -subj "/CN=$DOMAIN" -keyout "$KEY" -out "$CERT"
  chmod 600 "$KEY"
fi
if [[ ! -f "$TOKEN_FILE" || "$ROTATE_TOKEN" -eq 1 ]]; then
  python3 "$APP/ticket_desk/ticket.py" account rotate-service-token --token-file "$TOKEN_FILE"
fi
if [[ -n "$DATA_ROOT" && -d "$DATA_ROOT/items" ]]; then
  python3 "$APP/ticket_desk/ticket.py" migrate --from "$DATA_ROOT" --to "$DB"
fi
ACCOUNT_ARGS=(account init --db "$DB" --username "$ADMIN_USER")
if [[ "$REOPEN_SETUP" -eq 1 ]]; then ACCOUNT_ARGS+=(--reopen-setup); fi
python3 "$APP/ticket_desk/ticket.py" "${ACCOUNT_ARGS[@]}"

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Ticket Desk
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP
ExecStart=/usr/bin/python3 $APP/ticket_desk/ticket.py serve --host 0.0.0.0 --port $PORT --db $DB --tls-cert $CERT --tls-key $KEY --token-file $TOKEN_FILE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
command -v ufw >/dev/null 2>&1 && ufw allow "$PORT/tcp" || true
systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"
systemctl restart "$SERVICE_NAME.service"
echo "安装完成。首次设密页开放 30 分钟：https://$DOMAIN:$PORT/setup"
echo "★这条链接只在 30 分钟内有效,过期要重跑本脚本加 --reopen-setup。"
