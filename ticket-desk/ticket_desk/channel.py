"""ticket.py 怎么决定「连服务器还是用本机库」：三级取值 + --local，取不到也不许静默。

取值顺序写死，前面取到就不再往下找：
  ① 环境变量 TICKET_REMOTE / TICKET_TOKEN_FILE / TICKET_CA_SHA256；
  ② 环境变量 TICKET_ENV 指向的配置文件；
  ③ 按仓库位置推算出来的 tasks/tickets/remote.env（从仓根往上逐级找，不写死盘符）；
  ④ 都没有 → 本机模式。
--local 强制本机模式，忽略以上三级；测试与离线自查用它。

本模块只读令牌文件「在不在」，永不读它的内容；令牌只在 RemoteClient 里读。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from . import config
from .store import default_root

REPO_ROOT = config.repo_root()
ENV_FILE_RELATIVE = Path("tickets") / "remote.env"
CHANNEL_VARIABLES = ("TICKET_REMOTE", "TICKET_TOKEN_FILE", "TICKET_CA_SHA256")
LEVEL_ENVIRONMENT = "①环境变量"
LEVEL_POINTER = "②TICKET_ENV"
LEVEL_DERIVED = "③仓根推算的 remote.env"
LEVEL_NONE = "④无配置"
LEVEL_FORCED = "--local"
PROTOCOL_VERSION = 3


@dataclass
class Channel:
    """一次命令要走的通道。

    configured 为真表示①②③某一级取到了远程配置，命令就该走远程；此时 problem 非空
    说明那份配置用不了（缺令牌文件之类），必须报错，不许悄悄退回本机。
    """

    level: str
    source: str
    local_root: Path
    configured: bool = False
    remote: str = ""
    token_file: str = ""
    ca_sha256: str = ""
    problem: str = ""

    @property
    def is_remote(self) -> bool:
        return self.configured

    @property
    def host(self) -> str:
        if not self.remote:
            return "未知"
        parsed = urlparse(self.remote)
        return parsed.netloc or self.remote

    @property
    def token_file_exists(self) -> bool:
        return bool(self.token_file) and Path(self.token_file).is_file()

    @property
    def mode_label(self) -> str:
        if self.is_remote:
            return "远程模式(配置有误)" if self.problem else "远程模式"
        return "本机模式(--local 强制)" if self.level == LEVEL_FORCED else "本机模式(未接通道)"


def forward_slashes(value: str) -> str:
    """路径一律按正斜杠处理：反斜杠在 bash 的 source 里会被吃掉，踩过一次。"""
    return value.strip().replace("\\", "/")


def local_root(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("TICKET_ROOT") or default_root()).expanduser().resolve()


def derived_env_file(search_from: Path | None = None) -> tuple[Path, bool]:
    """从仓根往上逐级找 tickets/remote.env。

    ★为什么往上找而不是写死一个路径:同一个人常常同时开着主检出与好几个 worktree,
      它们的仓根各不相同,但都在同一级父目录下——往上走都会撞到同一份 remote.env,
      于是「配一次,所有树都接通」。换台机器、换个盘照样成立,因为一个盘符都没写死。
    找不到时返回「最合理的放置位置」供提示用：优先是已经有 tickets/ 目录的那一级，
    再退到仓库的上一级。
    """
    start = (search_from or REPO_ROOT).resolve()
    ancestors = list(start.parents)
    for ancestor in ancestors:
        candidate = ancestor / ENV_FILE_RELATIVE
        if candidate.is_file():
            return candidate, True
    for ancestor in ancestors:
        if (ancestor / ENV_FILE_RELATIVE.parent).is_dir():
            return ancestor / ENV_FILE_RELATIVE, False
    fallback = ancestors[0] if ancestors else start
    return fallback / ENV_FILE_RELATIVE, False


def parse_env_file(path: Path) -> dict[str, str]:
    """读 KEY=VALUE 文件：跳过空行与 # 注释，容忍前缀 export，去掉成对引号。"""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        if text.startswith("export "):
            text = text[len("export "):].strip()
        key, _, value = text.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def resolve(force_local: bool = False, environ: Mapping[str, str] | None = None, search_from: Path | None = None) -> Channel:
    environ = os.environ if environ is None else environ
    root = local_root(environ)
    if force_local:
        return Channel(LEVEL_FORCED, "命令行 --local", root)

    remote = environ.get("TICKET_REMOTE", "").strip()
    if remote:
        return _remote_channel(
            LEVEL_ENVIRONMENT, "环境变量 TICKET_REMOTE", root, remote,
            environ.get("TICKET_TOKEN_FILE", ""), environ.get("TICKET_CA_SHA256", ""),
            missing_token_hint="请 export TICKET_TOKEN_FILE=<令牌文件的正斜杠路径>（令牌文件向本位总监领）",
            base_dir=None,
        )

    pointer = environ.get("TICKET_ENV", "").strip()
    if pointer:
        path = Path(forward_slashes(pointer))
        if not path.is_file():
            return Channel(
                LEVEL_POINTER, f"环境变量 TICKET_ENV={forward_slashes(pointer)}", root, configured=True,
                problem=f"环境变量 TICKET_ENV 指向的配置文件不存在：{forward_slashes(pointer)}；请把总监发的 remote.env 放到这个路径，或把 TICKET_ENV 改成它实际所在的正斜杠路径。",
            )
        return _from_file(LEVEL_POINTER, f"环境变量 TICKET_ENV={forward_slashes(str(path))}", path, root)

    derived, exists = derived_env_file(search_from)
    if exists:
        return _from_file(LEVEL_DERIVED, forward_slashes(str(derived)), derived, root)
    return Channel(LEVEL_NONE, f"三级都没取到（TICKET_REMOTE 未设 / TICKET_ENV 未设 / {forward_slashes(str(derived))} 不存在）", root)


def connect_hint(channel: Channel, search_from: Path | None = None) -> str:
    """本机模式下告诉人最短的接通道办法，一行。"""
    if channel.level == LEVEL_FORCED:
        return "去掉 --local 再跑"
    derived, _ = derived_env_file(search_from)
    return (
        f"把总监发的 remote.env 放到 {forward_slashes(str(derived))}（放好后零配置），"
        "或 export TICKET_ENV=<remote.env 的正斜杠路径>"
    )


def describe(channel: Channel) -> str:
    """ticket.py env 的一行输出。只打令牌文件路径与存在与否，绝不打令牌本身。"""
    if channel.is_remote:
        token_state = "存在" if channel.token_file_exists else "不存在"
        parts = [
            channel.mode_label,
            f"服务器 {channel.host}",
            f"令牌文件 {channel.token_file or '（未填）'}（{token_state}）",
        ]
    else:
        parts = [channel.mode_label, "服务器 无", "令牌文件 无"]
    parts.append(f"本机库 {forward_slashes(str(channel.local_root))}")
    parts.append(f"配置来源 {channel.level}：{channel.source}")
    if channel.problem:
        parts.append(f"问题 {channel.problem}")
    return " · ".join(parts)


def describe_payload(channel: Channel) -> dict[str, object]:
    return {
        "模式": channel.mode_label,
        "服务器": channel.host if channel.is_remote else "",
        "令牌文件": channel.token_file,
        "令牌文件存在": channel.token_file_exists,
        "本机库": forward_slashes(str(channel.local_root)),
        "配置级别": channel.level,
        "配置来源": channel.source,
        "问题": channel.problem,
    }


def receipt_with_protocol(receipt: str, client_protocol: int, server_protocol: int) -> str:
    # 返工态的回执是多行的(判语全文跟在后面,T-000891):协议尾巴只贴第一行,
    # 落到判语末尾会让人以为那句提示也是判语的一部分。单行回执的输出与从前逐字相同。
    head, separator, rest = receipt.partition("\n")
    text = f"{head} · 客户端协议 {client_protocol} · 服务端协议 {server_protocol}"
    if client_protocol != server_protocol:
        text += "\n客户端比服务端新,带新开关的命令可能会被降级或拦下"
    return text + separator + rest


def _from_file(level: str, source: str, path: Path, root: Path) -> Channel:
    values = parse_env_file(path)
    shown = forward_slashes(str(path))
    remote = values.get("TICKET_REMOTE", "").strip()
    if not remote:
        return Channel(
            level, source, root, configured=True,
            problem=f"配置文件 {shown} 里没有 TICKET_REMOTE 这一行；请补上 TICKET_REMOTE=https://<服务器>:<端口>。",
        )
    return _remote_channel(
        level, source, root, remote, values.get("TICKET_TOKEN_FILE", ""), values.get("TICKET_CA_SHA256", ""),
        missing_token_hint=f"请在 {shown} 里补上 TICKET_TOKEN_FILE=<令牌文件的正斜杠路径>（令牌文件向本位总监领）",
        base_dir=path.parent,
    )


def _remote_channel(
    level: str, source: str, root: Path, remote: str, token_file: str, ca_sha256: str,
    missing_token_hint: str, base_dir: Path | None,
) -> Channel:
    token = forward_slashes(token_file)
    if not token:
        return Channel(
            level, source, root, configured=True, remote=remote,
            problem=f"{source} 已给出服务器地址，但缺 TICKET_TOKEN_FILE；{missing_token_hint}。",
        )
    token_path = Path(token)
    if base_dir is not None and not token_path.is_absolute():
        token_path = base_dir / token_path
    token = forward_slashes(str(token_path))
    channel = Channel(level, source, root, configured=True, remote=remote, token_file=token, ca_sha256=ca_sha256.strip())
    if not token_path.is_file():
        channel.problem = (
            f"{source} 写的令牌文件 {token} 不存在；请向本位总监领令牌文件放到这个路径，"
            "或把 TICKET_TOKEN_FILE 改成它实际所在的正斜杠路径。"
        )
    return channel
