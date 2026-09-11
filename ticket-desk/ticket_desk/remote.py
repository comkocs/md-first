"""ticket.py 的 HTTPS 远程传输层；写失败不回落本机。"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .channel import PROTOCOL_VERSION, receipt_with_protocol
from .model import TicketError
from .service import compress_image
from .service import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE, SHOT_REQUIRED_MESSAGE, SHOT_VALUES
from .service import with_state_summary


class RemoteUnavailable(TicketError):
    pass


class RemoteClient:
    def __init__(self, remote: str, token_file: str, ca_sha256: str = "") -> None:
        parsed = urlparse(remote.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise TicketError("TICKET_REMOTE 必须是完整的 http(s) 地址。")
        self.parsed = parsed
        path = Path(token_file).resolve() if token_file else None
        if not path or not path.is_file():
            raise TicketError("远程模式找不到 TICKET_TOKEN_FILE。")
        self.token = path.read_text(encoding="utf-8").strip()
        if not self.token:
            raise TicketError("TICKET_TOKEN_FILE 为空。")
        self.fingerprint = ca_sha256.lower().replace(":", "").strip()
        self.server_protocol = 0
        # 服务器在信封里报的本地真时刻(T-001508):核时区/时钟用,请求成功后才有值。
        self.server_time = ""

    def execute(self, argv: list[str]) -> tuple[Any, str]:
        command = argv[0] if argv else ""
        if command == "attach":
            return self._attach(argv)
        if command == "live" and len(self._live_positionals(argv)) >= 2:
            # T-000620:图片路径不一定紧跟单号——复检席写的是 live <单号> --batch … <图> --shot 同图,
            # 旧判据只看 argv[2],于是整条 argv 被原样转发到服务端,服务端拿「客户端本机路径」去开图,
            # 回显「找不到图片:<本机路径>」。凡是带了图片位置参数的 live,一律在客户端读图上传。
            return self._live(argv)
        if command == "say" and "--img" in argv:
            return self._say(argv)
        try:
            response = self.request("POST", "/api/cli", {"argv": argv})
        except TicketError as exc:
            options = self._degradable_options(argv)
            if not options or not self._is_unrecognized_option_error(str(exc)):
                raise
            retry_argv = [value for value in argv if value not in options]
            response = self.request("POST", "/api/cli", {"argv": retry_argv})
            names = "、".join(options)
            # ★T-001256 ⑤(v2.31 八章 6):版本差是**账面事,不是活没做好**,一律警告不拦。
            # 从前这段话把「不会进设计者队列」写成后果,读起来像出了大事,员工据此停车等平台上服;
            # 而现在「待回核」已经不再挡队列了(本单 ③),这条后果本身也不成立了。
            print(
                f"提醒(不影响这次操作):服务端版本较旧({self.server_protocol}),已自动省掉 {names} 重发,命令已经生效。"
                "这张单的任务书校验会是『待回核』——那只是个待补的账面标记,"
                "**不挡开窗、不挡认领、不挡交板**;总监顺手 set --taskbook <同一个路径> 一次就转「已核存在」。"
                "★不要为此停车等谁。",
                file=sys.stderr,
            )
        payload, text = response.get("payload"), str(response.get("text", ""))
        if command == "receipt":
            text = receipt_with_protocol(text, PROTOCOL_VERSION, self.server_protocol)
            if isinstance(payload, dict) and "receipt" in payload:
                payload = dict(payload, receipt=text)
            # 值面摘要由服务端算（值只存服务端一处）；服务端旧版不回这一格时安静没有这一行。
            text = with_state_summary(text, payload)
        return payload, text

    def _degradable_options(self, argv: list[str]) -> list[str]:
        # 延后导入，避免 ticket.py（入口层）与 remote.py（传输层）在加载时互相引用。
        from .ticket import DEGRADABLE_OPTIONS

        return [
            option for option, minimum in DEGRADABLE_OPTIONS.items()
            if option in argv and self.server_protocol < minimum
        ]

    @staticmethod
    def _is_unrecognized_option_error(message: str) -> bool:
        # T-001560 ②:报错尾部现在会接版本差提示行,头尾精确匹配会把降级链掐断
        # (--taskbook-client-checked 对旧服务端的自动剥除就失效了),放宽为包含式。
        return message.startswith("ticket.py") and "参数不对" in message

    def _attach(self, argv: list[str]) -> tuple[Any, str]:
        if len(argv) < 3:
            raise TicketError("ticket.py attach 缺少工单号或图片路径。")
        ticket, image_path = argv[1], argv[2]
        origin = self._option(argv, "--origin")
        actor = self._option(argv, "--by")
        name, raw = self._compress(Path(image_path))
        result = self.request("POST", "/api/upload", {
            "ticket": ticket, "filename": name, "origin": origin, "by": actor,
            "base64": base64.b64encode(raw).decode("ascii"),
        })
        image = result["图片"]
        return result, f"已附图 {image['文件名']} · {image['来源标注']}"

    # --reason 必须在册:否则 live <单号> --shot 免独图 --reason <原因> 会把原因当成图片路径,
    # 走进 _live 去开一个不存在的文件(T-000693)。
    LIVE_VALUE_OPTIONS = {"--by", "--shot", "--batch", "--reason"}

    def _live_positionals(self, argv: list[str]) -> list[str]:
        """live 的位置参数(单号、图片),跳过 --by/--shot/--batch/--reason 及其取值,不管它们排在哪。"""
        return self._positional_after_options(argv[1:], self.LIVE_VALUE_OPTIONS)

    def _live(self, argv: list[str]) -> tuple[Any, str]:
        positionals = self._live_positionals(argv)
        ticket, image_path = positionals[0], positionals[1]
        actor = self._option(argv, "--by")
        shot = self._option(argv, "--shot")
        if shot not in SHOT_VALUES:
            raise TicketError(SHOT_REQUIRED_MESSAGE)
        name, raw = self._compress(Path(image_path))
        uploaded = self.request("POST", "/api/upload", {
            "live": True, "filename": name, "by": actor,
            "base64": base64.b64encode(raw).decode("ascii"),
        })
        batch = self._options(argv, "--batch")
        if batch:
            result = self.request("POST", "/api/action", {
                "op": "live-batch", "tickets": [ticket, *batch],
                "filename": uploaded["图片"]["文件名"], "by": actor, "shot": shot,
            })
            return result, "\n".join(self._live_batch_line(row) for row in result)
        result = self.request("POST", "/api/action", {
            "op": "live", "ticket": ticket, "filename": uploaded["图片"]["文件名"], "by": actor,
            "shot": shot,
        })
        return result, self._compact(result)

    def _say(self, argv: list[str]) -> tuple[Any, str]:
        slot = self._option(argv, "--slot")
        actor = self._option(argv, "--by")
        reference = self._option(argv, "--ref")
        image_path = self._option(argv, "--img")
        text = self._positional_after_options(argv[1:], {"--slot", "--by", "--img", "--ref"})[-1]
        name, raw = self._compress(Path(image_path))
        result = self.request("POST", "/api/say", {
            "slot": slot, "by": actor, "text": text, "ref": reference,
            "images": [{"filename": name, "base64": base64.b64encode(raw).decode("ascii")}],
        })
        return result, f"已写入 {slot} 对话线 · {result['时间']}"

    def request(self, method: str, path: str, value: dict[str, Any]) -> Any:
        value = dict(value)
        if path == "/api/cli":
            value["client_protocol"] = PROTOCOL_VERSION
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "X-Ticket-Token": self.token}
        try:
            connection = self._connection()
            connection.request(method, self._base_path() + path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            connection.close()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise RemoteUnavailable(f"远程工单台不可达：{exc}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteUnavailable("远程工单台返回了无法识别的内容。") from exc
        server_protocol = decoded.get("server_protocol", 0)
        self.server_protocol = server_protocol if isinstance(server_protocol, int) else 0
        server_time = decoded.get("server_time", "")
        self.server_time = server_time if isinstance(server_time, str) else ""
        if response.status >= 400 or not decoded.get("ok"):
            raise TicketError(str(decoded.get("reason") or decoded.get("error") or f"远程请求失败 HTTP {response.status}"))
        return decoded.get("result")

    def _connection(self) -> http.client.HTTPConnection:
        port = self.parsed.port or (443 if self.parsed.scheme == "https" else 80)
        if self.parsed.scheme == "http":
            return http.client.HTTPConnection(self.parsed.hostname, port, timeout=15)
        if not self.fingerprint:
            return http.client.HTTPSConnection(self.parsed.hostname, port, timeout=15)
        connection = http.client.HTTPSConnection(
            self.parsed.hostname, port, timeout=15, context=ssl._create_unverified_context()
        )
        connection.connect()
        certificate = connection.sock.getpeercert(binary_form=True) if connection.sock else b""
        actual = hashlib.sha256(certificate).hexdigest()
        if not certificate or actual != self.fingerprint:
            connection.close()
            raise RemoteUnavailable("远程证书指纹不匹配，已拒绝连接。")
        return connection

    def _base_path(self) -> str:
        value = self.parsed.path.rstrip("/")
        return value

    @staticmethod
    def _option(argv: list[str], name: str) -> str:
        try:
            return argv[argv.index(name) + 1]
        except (ValueError, IndexError):
            return ""

    @staticmethod
    def _options(argv: list[str], name: str) -> list[str]:
        return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == name]

    @staticmethod
    def _live_batch_line(row: dict[str, Any]) -> str:
        if row.get("结果") == "已复验":
            return f"{row['编号']} · 已复验 · {row.get('实机图标记', '')}"
        return f"{row['编号']} · 跳过:{row.get('原因', '')}"

    @staticmethod
    def _positional_after_options(values: list[str], options: set[str]) -> list[str]:
        result: list[str] = []
        skip = False
        for value in values:
            if skip:
                skip = False
                continue
            if value in options:
                skip = True
                continue
            result.append(value)
        return result

    @staticmethod
    def _compact(ticket: dict[str, Any]) -> str:
        budget = f"/{ticket['上下文预算']}行" if ticket.get("上下文预算") is not None else ""
        tier = ticket.get("任务档") or "待总监定"
        suffix = f"{tier}档" if tier in {"甲", "乙", "丙"} else tier
        shot = f" · {ticket.get('实机图标记')}" if ticket.get("实机图标记") else ""
        return f"{ticket['编号']} · {ticket['标题']} · {ticket['状态']} · {suffix}{budget}{shot}"

    @staticmethod
    def _compress(path: Path) -> tuple[str, bytes]:
        return compress_image(path)
