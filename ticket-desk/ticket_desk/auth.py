"""工单台账号、首次设密、会话与个人 API token。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .model import TicketError


PBKDF2_ROUNDS = 310_000
SETUP_SECONDS = 30 * 60
SESSION_SECONDS = 8 * 60 * 60
ROLES = {"设计者", "总编排", "总监", "员工"}


class AccountManager:
    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).resolve()
        self.ensure()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def ensure(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    username TEXT PRIMARY KEY,
                    password_salt BLOB,
                    password_hash BLOB,
                    role TEXT NOT NULL,
                    slot TEXT NOT NULL DEFAULT '',
                    pending_setup INTEGER NOT NULL DEFAULT 1,
                    setup_deadline REAL,
                    token_salt BLOB,
                    token_hash BLOB
                )
                """
            )
            # 会话原先只存在内存字典里，服务一重启就全丢，用户被踢下线、每次写操作都要重新输令牌。
            # 上服一次就踢一次人，这是最常撞到的摩擦，所以落库。
            # ★列名与服务器库里已有的 sessions 表保持一字不差(T-000014 R2 建的)：
            #   CREATE TABLE IF NOT EXISTS 不会改已存在的表，列名对不上就会在运行时炸登录。
            # 存的是会话令牌的 SHA-256，不是令牌本身：库被拿走也换不出一个能用的会话。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    user_agent_digest TEXT NOT NULL,
                    sliding_seconds INTEGER NOT NULL,
                    FOREIGN KEY(username) REFERENCES accounts(username) ON DELETE CASCADE
                )
                """
            )
            connection.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))

    @staticmethod
    def _derive(secret: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, PBKDF2_ROUNDS)

    def init_admin(self, username: str, reopen: bool = False) -> dict[str, Any]:
        name = username.strip()
        if not name:
            raise TicketError("管理员用户名不能为空。")
        deadline = time.time() + SETUP_SECONDS
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT pending_setup FROM accounts WHERE username=?", (name,)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO accounts(username,role,pending_setup,setup_deadline) VALUES(?,?,1,?)",
                    (name, "设计者", deadline),
                )
            elif reopen:
                if not bool(row[0]):
                    raise TicketError("管理员已经设过密码，不能重新开放首次设密页。")
                connection.execute("UPDATE accounts SET setup_deadline=? WHERE username=?", (deadline, name))
        return {"用户名": name, "待首设": self.setup_available(name), "有效分钟": 30}

    def setup_available(self, username: str = "") -> bool:
        with closing(self._connect()) as connection, connection:
            if username:
                row = connection.execute(
                    "SELECT pending_setup,setup_deadline FROM accounts WHERE username=?", (username,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT pending_setup,setup_deadline FROM accounts WHERE pending_setup=1 ORDER BY rowid LIMIT 1"
                ).fetchone()
        return bool(row and row[0] and row[1] is not None and float(row[1]) >= time.time())

    def pending_username(self) -> str:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT username FROM accounts WHERE pending_setup=1 AND setup_deadline>=? ORDER BY rowid LIMIT 1",
                (time.time(),),
            ).fetchone()
        return str(row[0]) if row else ""

    def set_initial_password(self, username: str, password: str) -> str:
        if len(password) < 12:
            raise TicketError("密码至少需要 12 个字符。")
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT pending_setup,setup_deadline FROM accounts WHERE username=?", (username,)
            ).fetchone()
            if not row or not row[0] or row[1] is None or float(row[1]) < time.time():
                raise TicketError("首次设密页当前不可用。")
            salt = secrets.token_bytes(16)
            derived = self._derive(password, salt)
            connection.execute(
                "UPDATE accounts SET password_salt=?,password_hash=?,pending_setup=0,setup_deadline=NULL WHERE username=?",
                (salt, derived, username),
            )
        return self.create_session(username)

    def login(self, username: str, password: str) -> str:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT password_salt,password_hash,pending_setup FROM accounts WHERE username=?", (username,)
            ).fetchone()
        if not row or row[2] or row[0] is None or row[1] is None:
            raise TicketError("用户名或密码不对。")
        derived = self._derive(password, bytes(row[0]))
        if not hmac.compare_digest(derived, bytes(row[1])):
            raise TicketError("用户名或密码不对。")
        return self.create_session(username)

    @staticmethod
    def _session_hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    @staticmethod
    def _agent_digest(user_agent: str) -> str:
        if not user_agent:
            return ""
        return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:16]

    def create_session(self, username: str, user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            connection.execute(
                "INSERT OR REPLACE INTO sessions"
                "(session_id_hash,username,issued_at,expires_at,last_used_at,user_agent_digest,sliding_seconds)"
                " VALUES(?,?,?,?,?,?,?)",
                (self._session_hash(token), username, now, now + SESSION_SECONDS, now,
                 self._agent_digest(user_agent), SESSION_SECONDS),
            )
        return token

    def session_user(self, token: str) -> str:
        """认一个会话，顺便滑动续期。

        只要还在用就不会到期；停用超过 sliding_seconds 才失效。
        续期是「做到一半不被踢出去」，落库是「重启不掉线」，两件事都要。
        """
        if not token:
            return ""
        now = time.time()
        digest = self._session_hash(token)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT username,expires_at,sliding_seconds FROM sessions WHERE session_id_hash=?",
                (digest,),
            ).fetchone()
            if not row:
                return ""
            if float(row["expires_at"]) < now:
                connection.execute("DELETE FROM sessions WHERE session_id_hash=?", (digest,))
                return ""
            sliding = int(row["sliding_seconds"] or SESSION_SECONDS)
            connection.execute(
                "UPDATE sessions SET expires_at=?, last_used_at=? WHERE session_id_hash=?",
                (now + sliding, now, digest),
            )
            return str(row["username"])

    def end_session(self, token: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM sessions WHERE session_id_hash=?", (self._session_hash(token),)
            )

    def end_all_sessions(self, username: str) -> int:
        """注销该账号在所有设备上的会话。"""
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute("DELETE FROM sessions WHERE username=?", (username,))
            return int(cursor.rowcount or 0)

    def issue_api_token(self, username: str) -> str:
        token = secrets.token_urlsafe(40)
        salt = secrets.token_bytes(16)
        derived = self._derive(token, salt)
        with closing(self._connect()) as connection, connection:
            changed = connection.execute(
                "UPDATE accounts SET token_salt=?,token_hash=? WHERE username=?", (salt, derived, username)
            ).rowcount
        if not changed:
            raise TicketError("找不到当前账号。")
        return token

    def revoke_api_token(self, username: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE accounts SET token_salt=NULL,token_hash=NULL WHERE username=?", (username,))

    def token_user(self, token: str) -> str:
        if not token:
            return ""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT username,token_salt,token_hash FROM accounts WHERE token_hash IS NOT NULL"
            ).fetchall()
        for row in rows:
            if hmac.compare_digest(self._derive(token, bytes(row[1])), bytes(row[2])):
                return str(row[0])
        return ""

    def account(self, username: str) -> dict[str, str]:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT username,role,slot FROM accounts WHERE username=?", (username,)).fetchone()
        if not row:
            raise TicketError("找不到当前账号。")
        return {"用户名": str(row[0]), "角色": str(row[1]), "总监位": str(row[2])}
