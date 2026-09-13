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
DESK_CONFIG=""
PYTHON_BIN=""
NO_FIREWALL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --reopen-setup) REOPEN_SETUP=1; shift ;;
    --rotate-token) ROTATE_TOKEN=1; shift ;;
    --desk-config) DESK_CONFIG="$2"; shift 2 ;;
    --python-bin) PYTHON_BIN="$2"; shift 2 ;;
    --no-firewall) NO_FIREWALL=1; shift ;;
    *) echo "拦下:不认识的安装参数：$1" >&2; exit 2 ;;
  esac
done

[[ -n "$CONFIG" && -f "$CONFIG" ]] || { echo "拦下:必须用 --config 指向 server.json(样例见 deploy/server.example.json)。" >&2; exit 2; }
[[ -n "$SOURCE" && -d "$SOURCE/ticket_desk" ]] || { echo "拦下:必须用 --source 指向工单台源码根目录(里面要有 ticket_desk/)。" >&2; exit 2; }

# ★解释器可以指定,不写死 /usr/bin/python3。
#   上服闸① 硬要求「跑服务的那个 python 能 import Pillow」,而在有些机器上
#   **往系统 python 里装包这件事本身是不被允许的**(同机跑着别的生产服务,
#   全机 apt 装包不在使用方的授权范围内)。写死系统解释器时,那种环境只剩
#   「手改单元文件」一条路——而单元文件正是本脚本下次会覆盖的东西。
#   给了 --python-bin,venv 就是一条正路:建 venv、装 Pillow、装上,不碰系统 python。
PYTHON_BIN="${PYTHON_BIN:-${DESK_PYTHON_BIN:-/usr/bin/python3}}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || [[ -x "$PYTHON_BIN" ]] || {
  echo "拦下:--python-bin 指的解释器跑不起来：$PYTHON_BIN" >&2; exit 2; }
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("拦下:工单台要求 Python 3.10 或更高版本。")
PY
# ★闸放在这里而不是留给 update.sh:装完就会 systemctl start,起不来的话
#   人看到的是「服务反复重启」,不是「你选的解释器没有 Pillow」。装之前就说清楚。
if ! "$PYTHON_BIN" -c "from PIL import Image, ImageOps" >/dev/null 2>&1; then
  echo "拦下:$PYTHON_BIN 里 import 不到 Pillow,装上去服务起不来(带图的操作全会被拦)。" >&2
  echo "  发行版包管理器:apt install python3-pil;或另建 venv 后用 --python-bin 指过来。" >&2
  exit 2
fi

readarray -t SETTINGS < <("$PYTHON_BIN" - "$CONFIG" <<'PY'
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
rm -rf "$APP/ticket_desk" "$APP/web" "$APP/deploy"
cp -a "$SOURCE/ticket_desk" "$APP/ticket_desk"
[[ -d "$SOURCE/web" ]] && cp -a "$SOURCE/web" "$APP/web"
# ★deploy/ 也要拷:backup.sh 头部与上服清单里那条 cron 写的是
#   `<安装目录>/app/deploy/backup.sh`。不拷的话照文档装 cron 的人第一次 03:00 才会发现,
#   而且是**静默失败**——cron 不报错、没人看,等到要恢复那天才知道一次备份都没有。
[[ -d "$SOURCE/deploy" ]] && cp -a "$SOURCE/deploy" "$APP/deploy"

# 自签证书够用:客户端靠 TICKET_CA_SHA256 钉指纹,不靠 CA 链。
# 要换成真证书就把 $CERT/$KEY 换掉,本脚本看见文件已存在就不覆盖。
if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes -subj "/CN=$DOMAIN" -keyout "$KEY" -out "$CERT"
  chmod 600 "$KEY"
fi
if [[ ! -f "$TOKEN_FILE" || "$ROTATE_TOKEN" -eq 1 ]]; then
  "$PYTHON_BIN" "$APP/ticket_desk/ticket.py" account rotate-service-token --token-file "$TOKEN_FILE"
fi
if [[ -n "$DATA_ROOT" && -d "$DATA_ROOT/items" ]]; then
  "$PYTHON_BIN" "$APP/ticket_desk/ticket.py" migrate --from "$DATA_ROOT" --to "$DB"
fi
ACCOUNT_ARGS=(account init --db "$DB" --username "$ADMIN_USER")
if [[ "$REOPEN_SETUP" -eq 1 ]]; then ACCOUNT_ARGS+=(--reopen-setup); fi
"$PYTHON_BIN" "$APP/ticket_desk/ticket.py" "${ACCOUNT_ARGS[@]}"

# ── 名册配置:装在 app/ 之外 ────────────────────────────────────────
# ★config.py 找配置的顺序是「环境变量 TICKET_CONFIG → **本包同目录的 config.json**」,
#   而上服每次都 `rm -rf app/ticket_desk`。照文档把配置放进包目录的人,**第一次上服就丢名册**,
#   而且丢了之后不报错,是**静默回落到示例名册**——config.py 自己就写着
#   「配置文件写歪了却按示例名册跑起来,是这一类工具最坏的失败形态」,
#   「被上服删掉」走的正是这条路。
#   所以装到 $INSTALL_DIR/config.json(在 app/ 之外,上服的 rm 够不着),用 systemd
#   drop-in 把 TICKET_CONFIG 指过去。★用 drop-in 而不是写进单元本体:
#   单元本体是本脚本下次会整个覆盖的文件,写进去等于下次重装再丢一次。
DESK_CONFIG_TARGET="$INSTALL_DIR/config.json"
if [[ -n "$DESK_CONFIG" ]]; then
  [[ -f "$DESK_CONFIG" ]] || { echo "拦下:--desk-config 指的文件不存在：$DESK_CONFIG" >&2; exit 2; }
  "$PYTHON_BIN" -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8-sig'))" "$DESK_CONFIG" \
    || { echo "拦下:--desk-config 不是合法 JSON：$DESK_CONFIG" >&2; exit 2; }
  cp -a "$DESK_CONFIG" "$DESK_CONFIG_TARGET"
fi

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Ticket Desk
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP
ExecStart=$PYTHON_BIN $APP/ticket_desk/ticket.py serve --host 0.0.0.0 --port $PORT --db $DB --tls-cert $CERT --tls-key $KEY --token-file $TOKEN_FILE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

if [[ -f "$DESK_CONFIG_TARGET" ]]; then
  install -d "/etc/systemd/system/$SERVICE_NAME.service.d"
  cat > "/etc/systemd/system/$SERVICE_NAME.service.d/10-config.conf" <<EOF
[Service]
Environment=TICKET_CONFIG=$DESK_CONFIG_TARGET
EOF
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
# ★改防火墙是**全机**动作,不是只动这个目录——在「只授权你动这个目录」的环境里属于越界,
#   所以它必须是能关掉的,而且这里明说它会动 ufw。
if [[ "$NO_FIREWALL" -eq 1 ]]; then
  echo "跳过防火墙:--no-firewall 已给,本脚本不碰 ufw。请自行放行 $PORT/tcp。"
elif command -v ufw >/dev/null 2>&1; then
  echo "★本脚本要改**全机**防火墙:ufw allow $PORT/tcp(不想让它动,请加 --no-firewall)。"
  ufw allow "$PORT/tcp" || true
fi
systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"
systemctl restart "$SERVICE_NAME.service"
echo "安装完成。首次设密页开放 30 分钟：https://$DOMAIN:$PORT/setup"
echo "★这条链接只在 30 分钟内有效,过期要重跑本脚本加 --reopen-setup。"
