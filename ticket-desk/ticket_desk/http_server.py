"""工单台本地 HTTP 服务：静态页面和与 CLI 共用规则的 JSON API。"""

from __future__ import annotations

import base64
import hmac
import gzip
import json
import mimetypes
import socket
import ssl
import sys
import threading
import time
import webbrowser
from collections import defaultdict, deque
from functools import partial
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import config
from .channel import PROTOCOL_VERSION
from .model import TicketError, now_text
from .service import TicketService
from .auth import AccountManager, SESSION_SECONDS


MAX_REQUEST_BYTES = 16 * 1024 * 1024
# 压缩阈值与级别(T-001304)。1 KB 以下不值得压;级别 6 是 gzip 的默认折中——
# 实测 7.48 MB 中文 JSON 压到 2.50 MB 只花 0.28 秒,再往上调收益很小、CPU 明显变贵。
GZIP_MIN_BYTES = 1024
GZIP_LEVEL = 6


class FailureLimiter:
    def __init__(self) -> None:
        self.failures: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.blocked_until: dict[tuple[str, str], float] = {}
        self.lock = threading.Lock()

    def blocked(self, category: str, address: str) -> bool:
        with self.lock:
            return self.blocked_until.get((category, address), 0) > time.time()

    def fail(self, category: str, address: str, limit: int) -> bool:
        now = time.time()
        key = (category, address)
        with self.lock:
            rows = self.failures[key]
            while rows and rows[0] < now - 600:
                rows.popleft()
            rows.append(now)
            if len(rows) >= limit:
                self.blocked_until[key] = now + 600
                rows.clear()
                return True
        return False

    def clear(self, category: str, address: str) -> None:
        with self.lock:
            self.failures.pop((category, address), None)
            self.blocked_until.pop((category, address), None)


class TicketHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    # socketserver 默认 backlog 只有 5。11 个总监窗加网页轮询,一阵子就排满;
    # 排满之后新连接的 SYN 直接被内核丢掉,客户端看到的是「超时」而不是「拒绝连接」,
    # 极难和「服务器宕机」区分开(报上来的现象就是这个)。
    request_queue_size = 128

    # 单条连接的读写上限。卡住的客户端只拖死自己那条工作线程,拖不动别人。
    connection_timeout = 30

    def __init__(
        self, address: tuple[str, int], handler: type[SimpleHTTPRequestHandler], service: TicketService,
        token: str = "", auth: AccountManager | None = None,
    ) -> None:
        super().__init__(address, handler)
        self.service = service
        self.token = token
        self.auth = auth
        self.limiter = FailureLimiter()
        self.server_log = service.store.root / "server.log"
        # TLS 上下文挂在这里,由 serve() 填;绝不要去包监听套接字,原因见 get_request。
        self.ssl_context: ssl.SSLContext | None = None

    def get_request(self) -> tuple[Any, Any]:
        """每条连接单独包 TLS,并且把握手推迟到工作线程里做。

        ★线上卡死的真因就在这里。原先是在 serve() 里写
            server.socket = context.wrap_socket(server.socket, server_side=True)
        把「监听套接字」整个包成了 SSLSocket。这样 accept() 返回的就已经是 SSLSocket,
        **TLS 握手是在 accept 那一步、也就是 serve_forever 的主循环线程里做的**,而且没有超时。
        于是任何一个连上来却不完成握手的客户端(端口扫描器、断掉的浏览器标签、半开连接)
        都能把整个 accept 循环永久钉死;ThreadingHTTPServer 的多线程要等 accept 返回之后
        才轮得到,永远轮不到。现场证据:ss 显示 Recv-Q 6 / Send-Q 5(accept 队列满),
        systemd 说服务 active,进程 Tasks 只剩 1(一个工作线程都没起来),
        外面看到的是连接超时,restart 之后立刻恢复。

        改法:accept 先拿到裸 socket(不阻塞),设好超时,再逐连接包 TLS,并且
        do_handshake_on_connect=False —— 握手推迟到工作线程第一次 recv 时才做。
        这样握手慢或者根本不握手的客户端,只会拖死自己那条线程,accept 循环照转。
        """
        sock, address = super().get_request()
        sock.settimeout(self.connection_timeout)
        if self.ssl_context is not None:
            sock = self.ssl_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        """握手失败/超时/对端掐断不值得刷一整页栈。

        HTTPS 端口上被人用明文 HTTP 探一下就打一屏 traceback,真出事时反而看不见。
        """
        error = sys.exc_info()[1]
        if isinstance(error, (ssl.SSLError, TimeoutError, ConnectionError, socket.timeout)):
            return
        super().handle_error(request, client_address)

class TicketRequestHandler(SimpleHTTPRequestHandler):
    server: TicketHTTPServer
    current_user = ""

    def end_headers(self) -> None:
        # T-001658:静态页与脚本(tickets.js/index.html)原来不带任何缓存指令,
        # 浏览器按启发式缓存会拿**旧 JS 配今天的数据**一起跑(撞到过:待复检
        # 红条两个数都是 undefined,据此误判复检卡了二十多单)。
        # no-cache 不是禁缓存:每次拿 Last-Modified 再验证,没变就 304,不加流量;
        # 已带指令的响应(API 的 no-store 等)不覆盖。
        buffer = getattr(self, "_headers_buffer", None)
        already = bool(buffer) and any(
            line.lower().startswith(b"cache-control:") for line in buffer
        )
        if not already:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        safe_path = urlparse(self.path).path
        self.server.service.store.append_jsonl(
            self.server.server_log,
            {
                "client": self.client_address[0],
                "method": self.command,
                "path": safe_path,
                "message": format % args,
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if self.server.auth and parsed.path == "/setup":
            if not self.server.auth.setup_available():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._auth_page("setup")
            return
        if self.server.auth and parsed.path == "/login":
            self._auth_page("login")
            return
        if self.server.auth and not self._authorized(parsed.path.startswith("/api/") or parsed.path.startswith("/img/")):
            return
        if self.server.auth and parsed.path == "/account":
            self._account_page()
            return
        if parsed.path.startswith("/img/"):
            self._send_image(unquote(parsed.path.removeprefix("/img/")))
            return
        if not parsed.path.startswith("/api/"):
            if parsed.path == "/":
                self.path = "/index.html"
            super().do_GET()
            return
        if not self.server.auth and not self._authorized(True):
            return
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/config":
                # 网页开机第一趟:位名、停滞阈值、实机来源、值面标签、CLI 路径。
                # 前端只在读不到它时才用自带的兜底值(见 tickets.js 顶部 DESK)。
                self._ok(self.server.service.desk_config())
            elif parsed.path == "/api/slots":
                self._ok({
                    "slots": self.server.service.store.read_json(self.server.service.store.slots_path),
                    "staff": self.server.service.store.load_staff(),
                    # D9-462 补 ④:章程路径随名册一起下发,卡片上要看得见——
                    # 那是新人开窗前唯一该读的东西,不该让人自己去猜目录。
                    "charters": self.server.service.slot_charters(),
                })
            elif parsed.path == "/api/tickets":
                slot, state = self._one(query, "slot"), self._one(query, "state")
                kind = self._one(query, "type")
                pending = self._one(query, "shot-pending").lower() in {"1", "true", "yes"}
                needle = self._one(query, "q").strip().lower()
                if needle:
                    # ★T-001322:搜索必须在**全文**上匹配,并且把命中的那几张**整份**回过去。
                    #   列表那一趟为了体积摘掉了正文/答复/接线证据/备注(card_view),
                    #   若搜索也在精简行上匹配,搜「正文里的词」就再也搜不到——
                    #   而且页面不会报错,人只会以为「没有这张单」。那是最坏的静默失败。
                    #   命中集通常只有几张,回全文不值几个字节。
                    rows = [
                        row for row in self.server.service.list_tickets(slot, state, kind, pending)
                        if needle in json.dumps(row, ensure_ascii=False).lower()
                    ]
                else:
                    rows = self.server.service.list_cards(slot, state, kind, pending)
                self._ok(rows)
            elif parsed.path.startswith("/api/ticket/"):
                ticket = self.server.service.store.load_ticket(unquote(parsed.path.removeprefix("/api/ticket/")))
                self._ok(self.server.service.ticket_view(ticket))
            elif parsed.path == "/api/thread-summary":
                # T-001313:13 条线的未读摘要,一次几 KB。全文只有当前在看的那一位才拉。
                self._ok(self.server.service.thread_summaries())
            elif parsed.path == "/api/changes":
                # T-001308:网页增量刷新。「第 N 行流水之后有什么动静」——
                # 稳态下这一趟只回几十字节,而整份是 2.45 MB(压后)。
                self._ok(self.server.service.changes_since(int(self._one(query, "since") or 0)))
            elif parsed.path == "/api/inbox":
                slot = self._one(query, "slot")
                actor = self._one(query, "for")
                if self._one(query, "all") == "1":
                    rows = self.server.service.store.read_jsonl(self.server.service.store.thread_path(slot))
                    # 对话线本来就只追加,所以「第 N 行之后」= 直接切片,不会漏也不会重。
                    # 网页拿它做增量:12 条不看的线每次只回一个空数组。
                    since = self._one(query, "since")
                    if since:
                        try:
                            start = max(0, min(int(since), len(rows)))
                        except ValueError:
                            start = 0
                        self._ok({"起点": start, "总行数": len(rows), "新增": rows[start:]})
                        return
                    self._ok(rows)
                else:
                    mark_read = self._one(query, "mark") == "1"
                    if mark_read:
                        with self.server.service.store.locked():
                            self._ok(self.server.service.inbox(slot, actor, True))
                    else:
                        self._ok(self.server.service.inbox(slot, actor, False))
            elif parsed.path == "/api/digest":
                raw = self._one(query, "hours") or "24"
                self._ok(self.server.service.digest(int(raw)))
            elif parsed.path == "/api/state":
                # 顶栏那四项的唯一来源；写口子只在 CLI 的 state set 上，网页只读。
                self._ok(self.server.service.state_board())
            elif parsed.path == "/api/staff":
                # T-001081：默认只回在岗，?all=1 才要全量——与命令行 staff list --all 同一把尺子。
                self._ok(self.server.service.list_staff(
                    self._one(query, "slot") or None, self._one(query, "all") == "1",
                ))
            elif parsed.path == "/api/me" and self.server.auth:
                self._ok(self.server.auth.account(self.current_user))
            elif parsed.path.startswith("/api/image/"):
                self._send_image(unquote(parsed.path.removeprefix("/api/image/")))
            else:
                self._error(HTTPStatus.NOT_FOUND, "找不到这个工单接口。")
        except (TicketError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # pragma: no cover - safety net is exercised by integration use
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务处理失败：{exc}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if self.server.auth and parsed.path in {"/auth/login", "/auth/setup", "/auth/token-login"}:
            self._login_or_setup(parsed.path)
            return
        if self.server.auth and not self._authorized(True):
            return
        if self.server.auth and parsed.path == "/auth/logout":
            self._logout()
            return
        if self.server.auth and parsed.path == "/api/token":
            self._token_action()
            return
        if not parsed.path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "找不到这个工单接口。")
            return
        if not self.server.auth and not self._authorized(True):
            return
        try:
            payload = self._read_json()
            with self.server.service.store.locked():
                if parsed.path == "/api/action":
                    result = self._action(payload)
                elif parsed.path == "/api/say":
                    result = self.server.service.say_uploaded(
                        str(payload.get("slot", "")),
                        str(payload.get("by", "")),
                        str(payload.get("text", "")),
                        list(payload.get("images") or []),
                        str(payload.get("ref", "")),
                    )
                elif parsed.path == "/api/upload":
                    result = self._upload(payload)
                elif parsed.path == "/api/cli":
                    result = self._cli(payload)
                else:
                    self._error(HTTPStatus.NOT_FOUND, "找不到这个工单接口。")
                    return
                if self.server.auth:
                    self._audit_write(parsed.path, str(payload.get("op", "")))
            self._ok(result)
        except (TicketError, ValueError, TypeError, KeyError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # pragma: no cover - safety net is exercised by integration use
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务处理失败：{exc}")

    def _action(self, data: dict[str, Any]) -> Any:
        service = self.server.service
        op = str(data.get("op", ""))
        ticket_id = str(data.get("ticket", ""))
        actor = str(data.get("by", ""))
        if op == "new":
            return service.create_dispatch(
                str(data.get("slot", "")), str(data.get("title", "")), data.get("source") or [],
                str(data.get("consumer", "")), str(data.get("assign", "")), actor or config.CONDUCTOR_SLOT,
                str(data.get("notes", "")), data.get("tier"), data.get("context_lines"), data.get("deliverables") or [],
                # T-000831:internal 原样透传,缺了就让服务端拒——服务端是唯一真闸,前端勾选只是方便。
                data.get("internal"), str(data.get("body", "")), str(data.get("taskbook", "")),
                window=str(data.get("window", "")),
            )
        if op == "ask":
            return service.create_question(
                str(data.get("type", "需求")), str(data.get("slot", "")), str(data.get("title", "")),
                str(data.get("body", "")), actor or config.OWNER_ROLE, data.get("source") or [],
                str(data.get("consumer", "")), str(data.get("tier", "")), data.get("context_lines"),
                str(data.get("taskbook", "")),
            )
        if op == "set":
            ticket, changes = service.edit(
                ticket_id, actor,
                data.get("taskbook"), data.get("assign"), data.get("source"),
                data.get("body"), data.get("consumer"),
                internal=data.get("internal"), window=data.get("window"),
            )
            return {"工单": ticket, "改动": changes}
        if op == "claim":
            return service.claim(ticket_id, actor)
        if op == "open-window":
            ticket, warning = service.open_window(
                ticket_id, actor, str(data.get("actual_model", "")), str(data.get("actual_platform", "")),
            )
            return {"工单": ticket, "提示": warning}
        if op == "submit":
            return service.submit(
                ticket_id, str(data.get("evidence", "")),
                str(data.get("verify_command", "")), str(data.get("raw_output", "")),
                handoff=str(data.get("handoff", "")),
                gate_report=str(data.get("gate_report", "")),
            )
        if op == "verify":
            # D9-460 ①:判卷与复验并行。这里只记结论,不改状态——
            # 退回照旧走 rework/退回单,那两条路才带责任归属与返工次数。
            ticket, hint = service.verify(
                ticket_id, actor, str(data.get("result", "")),
                str(data.get("gates", "")), str(data.get("evidence", "")),
            )
            return {"工单": ticket, "提示": hint}
        if op == "judge":
            ticket, warning = service.judge(
                ticket_id, bool(data.get("passed")), actor,
                str(data.get("reason", "")), str(data.get("verdict", "")), str(data.get("blame", "")),
                str(data.get("strike_handoff", "")),
            )
            return {"工单": ticket, "提示": warning}
        if op == "deploy-record":
            # D9-460 ③:部署脚本在后置闸 READY 之后 POST 这一条,自动建「上服记录」单。
            # 免员工窗、免判——它记的是既成事实,不是要谁去做的活。
            # ★ticket_id 在这条路上是空的:单还不存在,正是这里要建出来的。
            return service.deploy_record(
                str(data.get("head", "")), str(data.get("probes", "")),
                data.get("tickets"), str(data.get("repo", "server")) or "server",
                actor or "部署脚本",
            )
        if op == "merge":
            return service.merge(ticket_id, actor)
        if op == "live":
            return service.live_uploaded(
                ticket_id, str(data.get("filename", "")), actor, str(data.get("shot", ""))
            )
        if op == "live-batch":
            ticket_ids = data.get("tickets")
            if not isinstance(ticket_ids, list) or not all(isinstance(value, str) for value in ticket_ids):
                raise TicketError("批量 live 的 tickets 必须是工单号列表。")
            return service.live_batch_uploaded(
                ticket_ids, str(data.get("filename", "")), actor, str(data.get("shot", ""))
            )
        if op == "rework-from-review":
            # T-001218:待复检退回重做。blame 默认「出题」,与 CLI 同一个默认值——
            # 两处默认不一样的话,同一个动作从网页点和从命令行跑会记出两本不同的账。
            return service.rework_from_review(
                ticket_id, str(data.get("reason", "")), actor, str(data.get("blame", "") or "出题"),
            )
        if op == "close":
            return service.close(ticket_id, actor)
        if op == "void":
            return service.void(ticket_id, str(data.get("reason", "")), actor)
        if op == "block":
            return service.block(ticket_id, str(data.get("reason", "")), actor or config.CONDUCTOR_SLOT)
        if op == "unblock":
            return service.unblock(ticket_id, actor or config.CONDUCTOR_SLOT)
        if op == "transfer":
            return service.transfer(ticket_id, str(data.get("to", "")), str(data.get("reason", "")), actor)
        if op == "answer":
            return service.answer(ticket_id, str(data.get("answer", "")), actor)
        raise TicketError(f"不认识的动作：{op}")

    def _upload(self, data: dict[str, Any]) -> Any:
        encoded = str(data.get("base64", ""))
        if "," in encoded and encoded.lstrip().startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise TicketError("上传内容不是有效的 base64 图片。") from exc
        filename = Path(str(data.get("filename", "浏览器上传"))).name
        actor = str(data.get("by", ""))
        if data.get("live") is True:
            return {"图片": self.server.service.upload_live_bytes(raw, filename, actor)}
        if data.get("ticket"):
            ticket, image = self.server.service.attach_bytes(
                str(data["ticket"]), raw, filename, str(data.get("origin", "other")), actor
            )
            return {"工单": ticket, "图片": image}
        if data.get("slot"):
            return {"图片": self.server.service.upload_thread_bytes(str(data["slot"]), raw, filename, actor)}
        raise TicketError("上传图片必须写 ticket 或 slot。")

    def _cli(self, data: dict[str, Any]) -> dict[str, Any]:
        from .ticket import execute, parser

        argv = data.get("argv")
        if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
            raise TicketError("远程命令参数必须是字符串列表。")
        try:
            args = parser().parse_args(argv)
        except TicketError as exc:
            try:
                client_protocol = int(data.get("client_protocol", 0))
            except (TypeError, ValueError):
                client_protocol = 0
            if client_protocol > PROTOCOL_VERSION:
                raise TicketError(
                    "你的客户端比服务器新,服务器还没上这一版:"
                    f"客户端 {client_protocol} / 服务端 {PROTOCOL_VERSION};"
                    "请等平台位上服,或改用并线前的客户端。"
                ) from exc
            raise
        if args.command in {"serve", "migrate", "dump", "account"}:
            raise TicketError("这个管理命令只能在服务端本机执行。")
        payload, text = execute(args, self.server.service)
        return {"payload": payload, "text": text}

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise TicketError("请求长度格式不对。") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise TicketError("请求为空或超过 16MB。")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TicketError("请求不是有效的 UTF-8 JSON。") from exc
        if not isinstance(value, dict):
            raise TicketError("请求正文必须是 JSON 对象。")
        return value

    def _login_or_setup(self, path: str) -> None:
        address = self.client_address[0]
        if self.server.limiter.blocked("login", address):
            self._error(HTTPStatus.TOO_MANY_REQUESTS, "登录失败过多，请 10 分钟后再试。")
            return
        try:
            payload = self._read_json()
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            if path == "/auth/token-login":
                username = self.server.auth.token_user(str(payload.get("token", ""))) if self.server.auth else ""
                if not username:
                    raise TicketError("个人令牌无效。")
                session = self.server.auth.create_session(username)
            elif path == "/auth/setup":
                if not self.server.auth or not self.server.auth.setup_available(username):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                session = self.server.auth.set_initial_password(username, password)
            else:
                session = self.server.auth.login(username, password) if self.server.auth else ""
            self.server.limiter.clear("login", address)
            self.current_user = username
            self._json(
                HTTPStatus.OK, {"ok": True, "result": {"用户名": username}},
                cookie=f"ticket_session={session}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}",
            )
        except (TicketError, ValueError, TypeError):
            self.server.limiter.fail("login", address, 10)
            self._error(HTTPStatus.UNAUTHORIZED, "用户名或密码不对，或首次设密窗口已关闭。")

    def _logout(self) -> None:
        session = self._cookie("ticket_session")
        if self.server.auth:
            self.server.auth.end_session(session)
        self._json(
            HTTPStatus.OK, {"ok": True, "result": "已退出"},
            cookie="ticket_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0",
        )

    def _token_action(self) -> None:
        try:
            payload = self._read_json()
            action = str(payload.get("op", "generate"))
            if action == "revoke":
                self.server.auth.revoke_api_token(self.current_user)
                result: Any = {"已吊销": True}
            elif action == "generate":
                result = {"token": self.server.auth.issue_api_token(self.current_user), "提示": "只显示这一次，请立即保存。"}
            else:
                raise TicketError("令牌动作只能是 generate 或 revoke。")
            self._audit_write("/api/token", action)
            self._ok(result)
        except (TicketError, ValueError, TypeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def _audit_write(self, path: str, operation: str) -> None:
        self.server.service.store.append_jsonl(
            self.server.service.store.log_path,
            {
                "时间": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "工单号": "",
                "事件": "http-write",
                "发言人": self.current_user,
                "状态": "",
                "说明": f"{path} {operation}".strip(),
                "事件序号": 0,
                "来源IP": self.client_address[0],
                "账号": self.current_user,
            },
        )

    def _cookie(self, name: str) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(name)
        return morsel.value if morsel else ""

    def _auth_page(self, mode: str) -> None:
        setup = mode == "setup"
        username = self.server.auth.pending_username() if setup and self.server.auth else ""
        title = "首次设置管理员密码" if setup else "登录工单台"
        endpoint = "/auth/setup" if setup else "/auth/login"
        readonly = "readonly" if setup else ""
        token_form = "" if setup else """<hr><h2>使用个人 API token</h2><form id=tokenForm><label>个人令牌</label><input name=token type=password autocomplete=off required><button>用令牌登录</button></form>"""
        html = f"""<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>{title}</title><style>body{{font-family:system-ui;background:#eef2f7;margin:0;display:grid;place-items:center;min-height:100vh}}main{{background:white;padding:32px;border-radius:16px;box-shadow:0 10px 35px #0002;width:min(360px,85vw)}}label,input,button{{display:block;width:100%;box-sizing:border-box}}label{{margin:16px 0 6px}}input,button{{padding:12px;border-radius:8px;border:1px solid #b8c2ce}}button{{margin-top:20px;background:#153b66;color:white}}#message{{color:#a11}}</style>
<main><h1>{title}</h1><p>{'此页面只开放 30 分钟，设完永久关闭。' if setup else '请输入你的工单台账号。'}</p><form id=mainForm><label>用户名</label><input name=username autocomplete=username value={json.dumps(username)} {readonly}><label>密码</label><input name=password type=password autocomplete={'new-password' if setup else 'current-password'} required minlength=12><button>继续</button></form>{token_form}<p id=message></p></main>
<script>async function send(e,url){{e.preventDefault();let f=new FormData(e.target),r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(Object.fromEntries(f))}}),j=await r.json().catch(()=>({{reason:'请求失败'}}));if(r.ok)location='/';else document.querySelector('#message').textContent=j.reason||'未能登录';}}document.querySelector('#mainForm').onsubmit=e=>send(e,'{endpoint}');let tf=document.querySelector('#tokenForm');if(tf)tf.onsubmit=e=>send(e,'/auth/token-login');</script></html>"""
        self._html(html)

    def _account_page(self) -> None:
        html = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>我的令牌</title><style>body{font-family:system-ui;max-width:760px;margin:50px auto;padding:0 20px}button{padding:10px 16px;margin-right:10px}pre{white-space:pre-wrap;word-break:break-all;background:#f3f5f7;padding:16px}</style><h1>我的令牌</h1><p>新令牌只显示一次；生成新令牌会立即让旧令牌失效。</p><button id=g>生成新令牌</button><button id=r>吊销令牌</button><a href='/'>返回工单台</a><pre id=o>令牌不会自动显示。</pre><script>async function go(op){let r=await fetch('/api/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({op})}),j=await r.json();document.querySelector('#o').textContent=r.ok?(j.result.token||'已吊销'):j.reason}g.onclick=()=>go('generate');r.onclick=()=>go('revoke');</script></html>"""
        self._html(html)

    def _html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self, api_request: bool = True) -> bool:
        if not self.server.auth:
            token = self.server.token
            if token and self.headers.get("X-Ticket-Token", "") != token:
                self._error(HTTPStatus.UNAUTHORIZED, "未获授权：请提供正确的 X-Ticket-Token。")
                return False
            return True
        address = self.client_address[0]
        if self.server.limiter.blocked("401", address):
            self._error(HTTPStatus.TOO_MANY_REQUESTS, "该地址认证失败过多，请 10 分钟后再试。")
            return False
        session = self._cookie("ticket_session")
        user = self.server.auth.session_user(session)
        supplied = self.headers.get("X-Ticket-Token", "")
        if not supplied:
            supplied = self._one(parse_qs(urlparse(self.path).query), "t")
        if not user and supplied:
            user = self.server.auth.token_user(supplied)
            if not user and self.server.token and hmac.compare_digest(supplied, self.server.token):
                user = "服务令牌"
        if user:
            self.current_user = user
            return True
        self.server.limiter.fail("401", address, 20)
        if api_request:
            self._error(HTTPStatus.UNAUTHORIZED, "未登录或令牌无效。")
        else:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/login")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        return False

    def _send_image(self, name: str) -> None:
        safe_name = Path(name).name
        if safe_name != name or not safe_name:
            raise TicketError("图片文件名不安全。")
        path = self.server.service.store.images_dir / safe_name
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "找不到这张图片。")
            return
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _ok(self, result: Any) -> None:
        self._json(HTTPStatus.OK, {"ok": True, "result": result})

    def _error(self, status: HTTPStatus, reason: str) -> None:
        self._json(status, {"ok": False, "reason": reason})

    def _json(self, status: HTTPStatus, value: dict[str, Any], cookie: str = "") -> None:
        # server_time(T-001508):服务器本地的真时刻随每个信封下发。
        # 客户端机器的时区五花八门,拿本机 `date` 的墙钟对表必错——
        # 真正可比的是这格与客户端自己算出的绝对时刻。
        value = dict(value, server_protocol=PROTOCOL_VERSION, server_time=now_text())
        payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        # ★T-001304:响应压缩。现象是「刷新工单页要多等十几秒」,量下来
        # 病根不在服务端算得慢——取数+序列化只花 1.1 秒——而在**线路**:
        # 一次刷新下行 11.17 MB(工单 7.48 MB + 13 条对话线 3.59 MB),
        # 跨洋线路约 480 KB/s,光传就要二十多秒。
        # 而这些全是中文 JSON,gzip 压到 32%(7.48 MB → 2.50 MB),压一次只花 0.28 秒。
        # ★只对**主动声明能收 gzip** 的客户端压:命令行那条路(remote.py)用 http.client,
        #   默认不发 Accept-Encoding,所以一个字节都不受影响;浏览器一律会发,并自动解压。
        # ★小响应不压:几百字节的回执压完可能更大,还白费一次 CPU。
        encoding = ""
        if len(payload) >= GZIP_MIN_BYTES and "gzip" in self.headers.get("Accept-Encoding", "").lower():
            payload = gzip.compress(payload, GZIP_LEVEL)
            encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _one(query: dict[str, list[str]], key: str) -> str:
        return query.get(key, [""])[0]


def serve(
    service: TicketService, host: str, port: int, token: str = "", open_browser: bool = False,
    tls_cert: str = "", tls_key: str = "", auth: AccountManager | None = None,
    web_root: str = "",
) -> None:
    # 静态件目录默认是仓根下的 web/。
    # ★目录不在时**明说**,不要让它安静地 404:
    #   「API 通了但页面全白」查起来比「页面根本没装」难十倍,而两者在浏览器里长得一样。
    root = Path(web_root) if web_root else config.repo_root() / "web"
    # 主台面不在就退到 web/minimal（一个只读的最小台面）。
    # ★退了要**说出来**:两者长得完全不一样,不说的话人会以为主台面就是这副样子。
    if not (root / "index.html").is_file() and (root / "minimal" / "index.html").is_file():
        print(f"提醒：{root} 下没有 index.html，已退到最小只读台面 {root / 'minimal'}。")
        root = root / "minimal"
    if not (root / "index.html").is_file():
        print(f"提醒：静态件目录里没有 index.html（找的是 {root}）。"
              "API 照常可用，网页会 404。要换目录用 --web-root。")
    handler = partial(TicketRequestHandler, directory=str(root))
    server = TicketHTTPServer((host, port), handler, service, token, auth)
    scheme = "https" if tls_cert and tls_key else "http"
    if bool(tls_cert) != bool(tls_key):
        server.server_close()
        raise TicketError("TLS 证书与私钥必须同时提供。")
    if tls_cert and tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
        # ★逐连接包,不包监听套接字;为什么见 TicketHTTPServer.get_request。
        server.ssl_context = context
    url = f"{scheme}://127.0.0.1:{server.server_address[1]}/"
    print(f"工单台服务已启动：{url}")
    if token:
        print("API 已启用 X-Ticket-Token 校验。")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
