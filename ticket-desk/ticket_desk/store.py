"""共享磁盘存储：原子 JSON、追加日志和每位对话线。"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config
from .model import SLOTS, TicketError, now_text, with_ticket_defaults


def default_root() -> Path:
    """工单真数据根：见 config.data_root()（配置 > 环境变量 TICKET_ROOT > 主目录默认位置）。

    ★每次现算,不做成模块级常量:测试与 CI 靠改环境变量换根,
      模块级常量会在第一次 import 那一刻就把根钉死,之后怎么改环境变量都没用。
    """
    return config.data_root()


class TicketStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or default_root()).expanduser().resolve()
        self.items_dir = self.root / "items"
        self.images_dir = self.root / "img"
        self.threads_dir = self.root / "threads"
        self.slots_path = self.root / "slots.json"
        self.staff_path = self.root / "staff.json"
        self.counter_path = self.root / "counter.json"
        # 当前值面（T-000892）：判据图尺寸、两仓部署头、走跑速度、已改未并的公共工具。
        # 和工单库同一处存，不落到某张工单里，也不在仓里另开散文件——那两种都会被人转抄成过期值。
        self.state_path = self.root / "state.json"
        self.log_path = self.root / "log.jsonl"
        self.lock_path = self.root / ".write.lock"
        self._thread_lock = threading.RLock()
        self._lock_state = threading.local()

    def ensure(self) -> None:
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        if not self.slots_path.exists():
            self.atomic_json(self.slots_path, self._default_slots())
        if not self.staff_path.exists():
            self.atomic_json(
                self.staff_path,
                self._default_staff(),
            )
        if not self.counter_path.exists():
            self.atomic_json(self.counter_path, {"最后编号": 0})
        if not self.state_path.exists():
            # 空字典是「四项都没人填」的合法状态；这里绝不替复检席预置任何一个值。
            self.atomic_json(self.state_path, {})
        self.log_path.touch(exist_ok=True)
        for slot in SLOTS:
            self.thread_path(slot).touch(exist_ok=True)
        self._migrate_metadata()

    # 模型名册来自 config「模型名册」/「主力模型集合」。
    # ★名册是**建库那一刻**的初值:建完之后真源是 slots.json,改 config 不会追改已有的库
    #   (那会把总监手改过的名册冲掉)。要改在跑的库,改 slots.json。
    MAIN_MODELS = list(config.MAIN_MODELS)
    MODEL_ROSTER = [dict(row) for row in config.MODEL_ROSTER]

    @staticmethod
    def _default_slots() -> dict[str, Any]:
        return {
            "主力模型集合": list(TicketStore.MAIN_MODELS),
            "模型名册": [
                dict(row, 可选档位=list(row.get("可选档位") or []))
                for row in TicketStore.MODEL_ROSTER
            ],
            "停用阈值": dict(config.BAN_THRESHOLDS),
            "总监位": [{"名字": name, "启用": True, "主力模型": True} for name in SLOTS],
        }

    @staticmethod
    def _default_staff() -> dict[str, Any]:
        return {
            "模型记分": {},
            "出题记分": {},
            "模型停用": {"全项目": [], "按位": {name: [] for name in SLOTS}},
            "总监位": {name: {"下一个编号": 1, "员工": []} for name in SLOTS},
        }

    def _migrate_metadata(self) -> None:
        # ── 只补「缺的格」，绝不追改已有值 ────────────────────────────────────
        # 名册一旦建出来，真源就是 slots.json：总监会在上面手改主力集合、停用某个模型。
        # 这里若按 config 回写，那些手改会在下一次任何命令跑起来时被悄悄冲掉——
        # 「配置文件改了一个字，线上名册全被重置」是这类迁移最典型的事故形态。
        # ★但这条只对**手改项**成立（主力模型集合 / 模型名册 / 停用阈值），对**位名**不成立，
        #   见下面「位名成员必须跟着 config 走」那一段。
        slots = self.read_json(self.slots_path, self._default_slots())
        changed_slots = False
        defaults = self._default_slots()
        for key in ("主力模型集合", "模型名册", "停用阈值"):
            if key not in slots:
                slots[key] = defaults[key]
                changed_slots = True
        for row in slots.get("总监位", []):
            if "主力模型" not in row:
                row["主力模型"] = True
                changed_slots = True
        # ── 位名**成员**必须跟着 config 走 ──────────────────────────────────
        # 位名不是手改项：它由 config 定，而另一半代码（argparse 的 choices、
        # say / transfer / create 的判据）全程直接读 config.SLOTS。
        # 两边一旦不一致就是劈脑，而且是**静默**的：
        #   · 网页按 slots.json 渲染「转给指定总监」的下拉 ⇒ 列出来的位服务端当场拒收；
        #   · 「要你去唤醒的窗口」也按它逐位查未读 ⇒ **真实位的对话线压根没被遍历**，
        #     say 写进去多少条都不会冒出来——这个症状比前一个更难查，因为它连报错都没有。
        # 触发它不需要谁做错事：装机脚本「先起服务、后写 TICKET_CONFIG」就够了——
        # 首次启动那一下拿示例名册建了库，之后进程读对了配置，库里那份却再没人校正。
        # ★紧挨着的员工桶本来就是按 SLOTS 补的（见下面 for slot in SLOTS）：
        #   同一个函数里一半跟 config、一半不跟，这本身就是那个劈脑的形状。
        roster_before = json.dumps(slots.get("总监位"), ensure_ascii=False, sort_keys=True)
        configured_rows: list[dict[str, Any]] = []
        known = {str(row.get("名字", "")): row for row in slots.get("总监位", [])}
        for name in SLOTS:
            row = known.pop(name, None) or {"名字": name, "主力模型": True}
            row["名字"], row["启用"] = name, True
            configured_rows.append(row)
        # config 里已经没有的位：**停用而不是删掉**——历史单的「所属总监位」还引用着它们，
        # 删了那些单就成了孤儿。前端本来就按「启用!==false」过滤，停用即从下拉里消失。
        for row in known.values():
            row["启用"] = False
            configured_rows.append(row)
        # ★先快照再比对：上面那些 row 是**原地改**的，而它们同时还挂在 slots["总监位"] 里。
        #   不先把「改前」序列化下来，比对时「改前」已经等于「改后」，
        #   于是永远判定没变、永远不落盘——闸看着在跑，其实一次都没生效。
        if roster_before != json.dumps(configured_rows, ensure_ascii=False, sort_keys=True):
            slots["总监位"] = configured_rows
            changed_slots = True
        if changed_slots:
            self.atomic_json(self.slots_path, slots)

        staff = self.read_json(self.staff_path, self._default_staff())
        changed_staff = False
        if "模型记分" not in staff:
            staff["模型记分"] = {}
            changed_staff = True
        if "出题记分" not in staff:
            staff["出题记分"] = {}
            changed_staff = True
        if "模型停用" not in staff:
            staff["模型停用"] = {"全项目": [], "按位": {name: [] for name in SLOTS}}
            changed_staff = True
        for slot in SLOTS:
            if slot not in staff["模型停用"].setdefault("按位", {}):
                staff["模型停用"]["按位"][slot] = []
                changed_staff = True
        # 名册加位后，员工桶也要跟着补；不补的话 staff new / staff list 会直接 KeyError。
        buckets = staff.setdefault("总监位", {})
        for slot in SLOTS:
            if slot not in buckets:
                buckets[slot] = {"下一个编号": 1, "员工": []}
                changed_staff = True
        if changed_staff:
            self.atomic_json(self.staff_path, staff)

    @contextmanager
    def locked(self, timeout_seconds: float = 10.0) -> Iterator[None]:
        with self._thread_lock:
            depth = int(getattr(self._lock_state, "depth", 0))
            if depth:
                self._lock_state.depth = depth + 1
                try:
                    yield
                finally:
                    self._lock_state.depth -= 1
                return

            self.root.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + timeout_seconds
            descriptor: int | None = None
            while descriptor is None:
                try:
                    descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(descriptor, f"pid={os.getpid()} time={now_text()}".encode("utf-8"))
                except FileExistsError:
                    if time.monotonic() >= deadline:
                        raise TicketError("工单盘正被另一扇窗写入，请稍后再试。")
                    time.sleep(0.05)
            self._lock_state.depth = 1
            try:
                yield
            finally:
                self._lock_state.depth = 0
                os.close(descriptor)
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def read_json(path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TicketError(f"读不懂数据文件 {path.name}：{exc}") from exc

    @staticmethod
    def atomic_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def save_image(directory: str | Path, filename: str, data: bytes) -> Path:
        """Atomically replace an image, preserving the existing same-name overwrite rule."""
        if not filename or Path(filename).name != filename:
            raise TicketError("图片文件名不安全。")
        target = Path(directory) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.tmp")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return target

    @staticmethod
    def append_jsonl(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def replace_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        """整体重写一条 jsonl：与 read_jsonl / append_jsonl 同层的写回通道。

        业务层要改写已有行（例如标已读）时只准走这里，不许自己拼临时文件名；
        否则换成 SQLite 后端后会写到一个没有任何读者的路径上。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def next_ticket_id(self) -> str:
        with self.locked():
            counter = self.read_json(self.counter_path, {"最后编号": 0})
            number = int(counter.get("最后编号", 0)) + 1
            self.atomic_json(self.counter_path, {"最后编号": number})
        return f"T-{number:06d}"

    def item_path(self, ticket_id: str) -> Path:
        normalized = ticket_id.upper()
        if not normalized.startswith("T-") or not normalized[2:].isdigit() or len(normalized) != 8:
            raise TicketError(f"工单号格式不对：{ticket_id}，应为 T-000001。")
        return self.items_dir / f"{normalized}.json"

    def load_ticket(self, ticket_id: str) -> dict[str, Any]:
        path = self.item_path(ticket_id)
        ticket = self.read_json(path)
        if ticket is None:
            raise TicketError(f"找不到工单 {ticket_id.upper()}。")
        return with_ticket_defaults(ticket)

    def save_ticket(self, ticket: dict[str, Any], event: str, actor: str, detail: str = "", extra: dict[str, Any] | None = None) -> None:
        ticket["最后更新时间"] = now_text()
        ticket["事件序号"] = int(ticket.get("事件序号", 0)) + 1
        path = self.item_path(str(ticket["编号"]))
        with self.locked():
            previous = self.read_json(path)
            if previous is None or previous.get("状态") != ticket.get("状态"):
                ticket["状态进入时间"] = ticket["最后更新时间"]
            else:
                ticket["状态进入时间"] = previous.get("状态进入时间") or previous.get("最后更新时间") or ticket["最后更新时间"]
            self.atomic_json(path, ticket)
            row = {
                "时间": ticket["最后更新时间"],
                "工单号": ticket["编号"],
                "事件": event,
                "发言人": actor,
                "状态": ticket["状态"],
                "说明": detail,
                "事件序号": ticket["事件序号"],
            }
            row.update(extra or {})
            self.append_jsonl(self.log_path, row)

    # ── 增量书签（T-001308）────────────────────────────────────────────────
    # 网页每次刷新原来都整份重取 1295 张单(压后仍 2.45 MB)。改成「只要变过的」之后,
    # 关键是用什么当书签。用「最后更新时间」有同一秒两笔写入的边界问题;
    # 而**每写一次单必写一行流水**(save_ticket 里两件事在同一把锁内完成),
    # 流水行号严格递增——拿它当书签,一笔改动在物理上躲不过去。
    # ★文件后端的行号就是 log.jsonl 的行数;SQLite 后端是 log.position。两边语义一致。
    # ★这里回的是「被动过的单号」,是真实变更的**超集**:比如 staff-auto-retire 只改名册、
    #   却记了一行引用该单的流水,于是那张单会被多传一次。宁可多传,绝不漏传。

    def log_cursor(self) -> int:
        """现在的流水行号(书签)。"""
        return len(self.read_jsonl(self.log_path))

    def tickets_changed_since(self, cursor: int) -> tuple[list[str], int, bool]:
        """第 cursor 行之后被动过的单号、新书签、要不要整份重取。

        cursor 越界(比服务器现有的还大,例如换过库或回滚过)一律当 0 处理——
        宁可让客户端整份重取一次,也不能因为一个坏书签把它永远停在旧数据上。
        """
        rows = self.read_jsonl(self.log_path)
        total = len(rows)
        start = cursor if 0 <= cursor <= total else 0
        seen: dict[str, None] = {}
        full_reload = False
        for row in rows[start:]:
            if row.get("整份重取"):
                full_reload = True
            ticket_id = str(row.get("工单号", "") or "").strip()
            if ticket_id:
                seen.setdefault(ticket_id, None)
        return list(seen), total, full_reload

    def backfill_state_times(self, force: bool = False) -> dict[str, int]:
        """从审计日志补齐当前状态的进入时间，不改业务更新时间与日志。"""
        transitions: dict[str, str] = {}
        previous_states: dict[str, str] = {}
        for row in self.read_jsonl(self.log_path):
            ticket_id = str(row.get("工单号", ""))
            state = str(row.get("状态", ""))
            timestamp = str(row.get("时间", ""))
            if not ticket_id or not state:
                continue
            if previous_states.get(ticket_id) != state:
                if timestamp:
                    transitions[ticket_id] = timestamp
                previous_states[ticket_id] = state

        changed = 0
        skipped = 0
        with self.locked():
            for ticket in self.list_tickets():
                if ticket.get("状态进入时间") and not force:
                    skipped += 1
                    continue
                ticket["状态进入时间"] = transitions.get(str(ticket.get("编号", ""))) or ticket.get("发起时间") or ticket.get("最后更新时间")
                self.atomic_json(self.item_path(str(ticket["编号"])), ticket)
                changed += 1
        # ★T-001308:这条维护命令是**唯一**一处绕过 save_ticket 直接改盘的路,
        # 它故意不动业务更新时间(见上面的 docstring)。但增量客户端的书签是流水行号——
        # 不写流水,这一批改动对所有缓存着的页面就是**隐形**的,它们会一直显示旧的进入时间。
        # 所以这里补一行「盘被动过了」:内容不重要,行号动一下就够,
        # 客户端下次问增量时会因此把这些单重新取一遍。宁可多传,绝不隐形。
        if changed:
            self.append_jsonl(self.log_path, {
                "时间": now_text(), "工单号": "", "事件": "migrate-backfill-state-time",
                "发言人": "维护", "状态": "",
                "说明": f"补齐状态进入时间 {changed} 张(不改业务更新时间);记这一行是为了让增量客户端重取",
                "事件序号": 0, "整份重取": True,
            })
        return {"已补齐": changed, "已跳过": skipped}

    def list_tickets(self) -> list[dict[str, Any]]:
        self.ensure()
        return [with_ticket_defaults(self.read_json(path)) for path in sorted(self.items_dir.glob("T-*.json"))]

    def thread_path(self, slot: str) -> Path:
        if slot not in SLOTS:
            raise TicketError(f"总监位不在名册里：{slot}")
        return self.threads_dir / f"{slot}.jsonl"

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise TicketError(f"{path.name} 第 {number} 行损坏：{exc}") from exc
        return rows

    def load_staff(self) -> dict[str, Any]:
        self.ensure()
        return self.read_json(self.staff_path)

    def save_staff(self, staff: dict[str, Any]) -> None:
        with self.locked():
            self.atomic_json(self.staff_path, staff)


class SqliteStore(TicketStore):
    """SQLite-backed store with the same service-facing interface as TicketStore."""

    def __init__(self, database: str | Path, images_dir: str | Path | None = None) -> None:
        self.database = Path(database).resolve()
        root = self.database.parent
        if root.name == "db":
            root = root.parent
        self.root = root
        self.items_dir = self.root / "items"
        self.images_dir = Path(images_dir).resolve() if images_dir else self.root / "img"
        self.threads_dir = self.root / "threads"
        self.slots_path = self.root / "slots.json"
        self.staff_path = self.root / "staff.json"
        self.counter_path = self.root / "counter.json"
        self.state_path = self.root / "state.json"
        self.log_path = self.root / "log.jsonl"
        self.lock_path = self.root / ".write.lock"
        self._thread_lock = threading.RLock()
        self._lock_state = threading.local()

    def _connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._lock_state, "connection", None)
        if active is not None:
            yield active
            return
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def locked(self, timeout_seconds: float = 10.0) -> Iterator[None]:
        del timeout_seconds
        with self._thread_lock:
            depth = int(getattr(self._lock_state, "depth", 0))
            if depth:
                self._lock_state.depth = depth + 1
                try:
                    yield
                finally:
                    self._lock_state.depth -= 1
                return
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            self._lock_state.depth = 1
            self._lock_state.connection = connection
            try:
                yield
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                self._lock_state.depth = 0
                self._lock_state.connection = None
                connection.close()

    def ensure(self) -> None:
        self.images_dir.mkdir(parents=True, exist_ok=True)
        with self._database() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS images (
                    filename TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    data BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS threads (
                    slot TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (slot, position)
                );
                CREATE TABLE IF NOT EXISTS staff (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slots (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS log (
                    position INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_scores (
                    model TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._put_singleton(connection, "slots", self._default_slots())
            self._put_singleton(connection, "staff", self._default_staff())
            connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('counter','0')")
            connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('state','{}')")
        # _migrate_metadata 会经 read_json / atomic_json 再绕回 ensure()，用一次性开关挡住重入。
        if getattr(self, "_migrated", False):
            return
        self._migrated = True
        try:
            self._migrate_metadata()
        except Exception:
            self._migrated = False
            raise

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _put_singleton(self, connection: sqlite3.Connection, table: str, default: Any) -> None:
        connection.execute(
            f"INSERT OR IGNORE INTO {table}(singleton,payload) VALUES(1,?)",
            (self._json(default),),
        )

    def next_ticket_id(self) -> str:
        with self.locked():
            connection = self._lock_state.connection
            row = connection.execute("SELECT value FROM metadata WHERE key='counter'").fetchone()
            number = int(row[0] if row else 0) + 1
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('counter',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(number),),
            )
        return f"T-{number:06d}"

    def load_ticket(self, ticket_id: str) -> dict[str, Any]:
        normalized = self.item_path(ticket_id).stem
        with self._database() as connection:
            row = connection.execute("SELECT payload FROM tickets WHERE id=?", (normalized,)).fetchone()
        if row is None:
            raise TicketError(f"找不到工单 {normalized}。")
        return with_ticket_defaults(json.loads(row[0]))

    def save_ticket(self, ticket: dict[str, Any], event: str, actor: str, detail: str = "", extra: dict[str, Any] | None = None) -> None:
        ticket["最后更新时间"] = now_text()
        ticket["事件序号"] = int(ticket.get("事件序号", 0)) + 1
        with self.locked():
            connection = self._lock_state.connection
            old_row = connection.execute("SELECT payload FROM tickets WHERE id=?", (str(ticket["编号"]),)).fetchone()
            previous = json.loads(old_row[0]) if old_row else None
            if previous is None or previous.get("状态") != ticket.get("状态"):
                ticket["状态进入时间"] = ticket["最后更新时间"]
            else:
                ticket["状态进入时间"] = previous.get("状态进入时间") or previous.get("最后更新时间") or ticket["最后更新时间"]
            connection.execute(
                "INSERT INTO tickets(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (str(ticket["编号"]), self._json(ticket)),
            )
            row = {
                "时间": ticket["最后更新时间"], "工单号": ticket["编号"], "事件": event,
                "发言人": actor, "状态": ticket["状态"], "说明": detail, "事件序号": ticket["事件序号"],
            }
            row.update(extra or {})
            position = int(connection.execute("SELECT COALESCE(MAX(position),0)+1 FROM log").fetchone()[0])
            connection.execute("INSERT INTO log(position,payload) VALUES(?,?)", (position, self._json(row)))
            self._sync_ticket_images(connection, ticket)

    def _sync_ticket_images(self, connection: sqlite3.Connection, ticket: dict[str, Any]) -> None:
        records = ticket.get("图片列表") or ticket.get("接线证据", {}).get("图片列表") or []
        for record in records:
            filename = str(record.get("文件名", ""))
            path = self.images_dir / filename
            if not filename or not path.is_file():
                continue
            data = path.read_bytes()
            connection.execute(
                "INSERT INTO images(filename,ticket_id,sha256,payload,data) VALUES(?,?,?,?,?) "
                "ON CONFLICT(filename) DO UPDATE SET ticket_id=excluded.ticket_id,sha256=excluded.sha256,payload=excluded.payload,data=excluded.data",
                (filename, str(ticket.get("编号", "")), hashlib.sha256(data).hexdigest(), self._json(record), data),
            )

    def list_tickets(self) -> list[dict[str, Any]]:
        self.ensure()
        with self._database() as connection:
            rows = connection.execute("SELECT payload FROM tickets ORDER BY id").fetchall()
        return [with_ticket_defaults(json.loads(row[0])) for row in rows]

    def thread_path(self, slot: str) -> Path:
        if slot not in SLOTS:
            raise TicketError(f"总监位不在名册里：{slot}")
        return self.threads_dir / f"{slot}.jsonl"

    def read_json(self, path: Path, default: Any = None) -> Any:
        resolved = Path(path).resolve()
        self.ensure()
        with self._database() as connection:
            if resolved == self.slots_path.resolve():
                row = connection.execute("SELECT payload FROM slots WHERE singleton=1").fetchone()
                return json.loads(row[0]) if row else default
            if resolved == self.staff_path.resolve():
                row = connection.execute("SELECT payload FROM staff WHERE singleton=1").fetchone()
                return json.loads(row[0]) if row else default
            if resolved == self.counter_path.resolve():
                row = connection.execute("SELECT value FROM metadata WHERE key='counter'").fetchone()
                return {"最后编号": int(row[0])} if row else default
            if resolved == self.state_path.resolve():
                row = connection.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
                return json.loads(row[0]) if row else default
            if resolved.parent == self.items_dir.resolve() and resolved.suffix == ".json":
                row = connection.execute("SELECT payload FROM tickets WHERE id=?", (resolved.stem,)).fetchone()
                return json.loads(row[0]) if row else default
        return TicketStore.read_json(resolved, default)

    def atomic_json(self, path: Path, value: Any) -> None:
        resolved = Path(path).resolve()
        self.ensure()
        with self._database() as connection:
            if resolved == self.slots_path.resolve():
                connection.execute("UPDATE slots SET payload=? WHERE singleton=1", (self._json(value),))
                return
            if resolved == self.staff_path.resolve():
                connection.execute("UPDATE staff SET payload=? WHERE singleton=1", (self._json(value),))
                self._sync_model_scores(connection, value)
                return
            if resolved == self.counter_path.resolve():
                connection.execute("UPDATE metadata SET value=? WHERE key='counter'", (str(value.get("最后编号", 0)),))
                return
            if resolved == self.state_path.resolve():
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (self._json(value),),
                )
                return
            if resolved.parent == self.items_dir.resolve() and resolved.suffix == ".json":
                connection.execute(
                    "INSERT INTO tickets(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (resolved.stem, self._json(value)),
                )
                return
        TicketStore.atomic_json(resolved, value)

    def log_cursor(self) -> int:
        """SQLite 后端直接问最大行号，不必把三万行流水读出来再数（T-001308）。"""
        self.ensure()
        with self._database() as connection:
            return int(connection.execute("SELECT COALESCE(MAX(position),0) FROM log").fetchone()[0])

    def tickets_changed_since(self, cursor: int) -> tuple[list[str], int, bool]:
        """只捞书签之后那几行流水。

        父类那版要把三万行全读出来再切片——增量接口是每次刷新都调的,
        不能为了回几十字节先解析三万条 JSON。
        """
        self.ensure()
        with self._database() as connection:
            total = int(connection.execute("SELECT COALESCE(MAX(position),0) FROM log").fetchone()[0])
            start = cursor if 0 <= cursor <= total else 0
            rows = connection.execute(
                "SELECT payload FROM log WHERE position > ? ORDER BY position", (start,)
            ).fetchall()
        seen: dict[str, None] = {}
        full_reload = False
        for row in rows:
            entry = json.loads(row[0])
            if entry.get("整份重取"):
                full_reload = True
            ticket_id = str(entry.get("工单号", "") or "").strip()
            if ticket_id:
                seen.setdefault(ticket_id, None)
        return list(seen), total, full_reload

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        resolved = Path(path).resolve()
        self.ensure()
        with self._database() as connection:
            if resolved == self.log_path.resolve():
                rows = connection.execute("SELECT payload FROM log ORDER BY position").fetchall()
                return [json.loads(row[0]) for row in rows]
            if resolved.parent == self.threads_dir.resolve() and resolved.suffix == ".jsonl":
                rows = connection.execute(
                    "SELECT payload FROM threads WHERE slot=? ORDER BY position", (resolved.stem,)
                ).fetchall()
                return [json.loads(row[0]) for row in rows]
        return TicketStore.read_jsonl(self, resolved)

    def append_jsonl(self, path: Path, value: Any) -> None:
        resolved = Path(path).resolve()
        self.ensure()
        with self._database() as connection:
            if resolved == self.log_path.resolve():
                position = int(connection.execute("SELECT COALESCE(MAX(position),0)+1 FROM log").fetchone()[0])
                connection.execute("INSERT INTO log(position,payload) VALUES(?,?)", (position, self._json(value)))
                return
            if resolved.parent == self.threads_dir.resolve() and resolved.suffix == ".jsonl":
                position = int(connection.execute(
                    "SELECT COALESCE(MAX(position),0)+1 FROM threads WHERE slot=?", (resolved.stem,)
                ).fetchone()[0])
                connection.execute(
                    "INSERT INTO threads(slot,position,payload) VALUES(?,?,?)",
                    (resolved.stem, position, self._json(value)),
                )
                return
        TicketStore.append_jsonl(resolved, value)

    def replace_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        resolved = Path(path).resolve()
        self.ensure()
        with self._database() as connection:
            if resolved == self.log_path.resolve():
                connection.execute("DELETE FROM log")
                connection.executemany(
                    "INSERT INTO log(position,payload) VALUES(?,?)",
                    [(index, self._json(row)) for index, row in enumerate(rows, 1)],
                )
                return
            if resolved.parent == self.threads_dir.resolve() and resolved.suffix == ".jsonl":
                slot = resolved.stem
                connection.execute("DELETE FROM threads WHERE slot=?", (slot,))
                connection.executemany(
                    "INSERT INTO threads(slot,position,payload) VALUES(?,?,?)",
                    [(slot, index, self._json(row)) for index, row in enumerate(rows, 1)],
                )
                return
        TicketStore.replace_jsonl(self, resolved, rows)

    def load_staff(self) -> dict[str, Any]:
        return self.read_json(self.staff_path, self._default_staff())

    def save_staff(self, staff: dict[str, Any]) -> None:
        with self.locked():
            self.atomic_json(self.staff_path, staff)

    def _sync_model_scores(self, connection: sqlite3.Connection, staff: dict[str, Any]) -> None:
        connection.execute("DELETE FROM model_scores")
        for model, payload in (staff.get("模型记分") or {}).items():
            connection.execute("INSERT INTO model_scores(model,payload) VALUES(?,?)", (model, self._json(payload)))

    def import_files(self, source_root: str | Path) -> dict[str, Any]:
        source = TicketStore(source_root)
        source.ensure()
        tickets = source.list_tickets()
        log_rows = source.read_jsonl(source.log_path)
        thread_rows = {slot: source.read_jsonl(source.thread_path(slot)) for slot in SLOTS}
        images = sorted(path for path in source.images_dir.iterdir() if path.is_file())
        with self.locked():
            self.ensure()
            connection = self._lock_state.connection
            for ticket in tickets:
                connection.execute(
                    "INSERT INTO tickets(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (str(ticket["编号"]), self._json(ticket)),
                )
            connection.execute("DELETE FROM images")
            image_shas: dict[str, str] = {}
            for path in images:
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                image_shas[path.name] = digest
                ticket_id = path.name[:8] if path.name.startswith("T-") else ""
                connection.execute(
                    "INSERT INTO images(filename,ticket_id,sha256,payload,data) VALUES(?,?,?,?,?)",
                    (path.name, ticket_id, digest, "{}", data),
                )
                self.save_image(self.images_dir, path.name, data)
            connection.execute("DELETE FROM log")
            connection.executemany(
                "INSERT INTO log(position,payload) VALUES(?,?)",
                [(index, self._json(row)) for index, row in enumerate(log_rows, 1)],
            )
            connection.execute("DELETE FROM threads")
            for slot, rows in thread_rows.items():
                connection.executemany(
                    "INSERT INTO threads(slot,position,payload) VALUES(?,?,?)",
                    [(slot, index, self._json(row)) for index, row in enumerate(rows, 1)],
                )
            slots = source.read_json(source.slots_path, source._default_slots())
            staff = source.read_json(source.staff_path, source._default_staff())
            counter = source.read_json(source.counter_path, {"最后编号": 0})
            state = source.read_json(source.state_path, {})
            connection.execute("UPDATE slots SET payload=? WHERE singleton=1", (self._json(slots),))
            connection.execute("UPDATE staff SET payload=? WHERE singleton=1", (self._json(staff),))
            connection.execute("UPDATE metadata SET value=? WHERE key='counter'", (str(counter.get("最后编号", 0)),))
            connection.execute("UPDATE metadata SET value=? WHERE key='state'", (self._json(state or {}),))
            self._sync_model_scores(connection, staff)
        database_tickets = self.list_tickets()
        db_shas = self.image_hashes()
        same_tickets = tickets == database_tickets
        same_images = image_shas == db_shas
        return {
            "工单": len(tickets), "图片": len(images), "对话": sum(map(len, thread_rows.values())),
            "工单字段全同": same_tickets, "图片SHA全同": same_images,
        }

    def image_hashes(self) -> dict[str, str]:
        with self._database() as connection:
            rows = connection.execute("SELECT filename,sha256 FROM images ORDER BY filename").fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def dump_files(self, target_root: str | Path) -> dict[str, Any]:
        target = TicketStore(target_root)
        target.root.mkdir(parents=True, exist_ok=True)
        target.items_dir.mkdir(parents=True, exist_ok=True)
        target.images_dir.mkdir(parents=True, exist_ok=True)
        target.threads_dir.mkdir(parents=True, exist_ok=True)
        with self._database() as connection:
            tickets = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM tickets ORDER BY id")]
            for ticket in tickets:
                target.atomic_json(target.item_path(str(ticket["编号"])), ticket)
            image_rows = connection.execute("SELECT filename,data FROM images ORDER BY filename").fetchall()
            for row in image_rows:
                target.save_image(target.images_dir, str(row[0]), bytes(row[1]))
            for slot in SLOTS:
                rows = connection.execute(
                    "SELECT payload FROM threads WHERE slot=? ORDER BY position", (slot,)
                ).fetchall()
                target.replace_jsonl(target.thread_path(slot), [json.loads(row[0]) for row in rows])
            slots = json.loads(connection.execute("SELECT payload FROM slots WHERE singleton=1").fetchone()[0])
            staff = json.loads(connection.execute("SELECT payload FROM staff WHERE singleton=1").fetchone()[0])
            counter = int(connection.execute("SELECT value FROM metadata WHERE key='counter'").fetchone()[0])
            state_row = connection.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
            logs = connection.execute("SELECT payload FROM log ORDER BY position").fetchall()
        target.atomic_json(target.slots_path, slots)
        target.atomic_json(target.staff_path, staff)
        target.atomic_json(target.counter_path, {"最后编号": counter})
        target.atomic_json(target.state_path, json.loads(state_row[0]) if state_row else {})
        target.replace_jsonl(target.log_path, [json.loads(row[0]) for row in logs])
        return {"工单": len(tickets), "图片": len(image_rows), "对话": sum(len(target.read_jsonl(target.thread_path(slot))) for slot in SLOTS)}
