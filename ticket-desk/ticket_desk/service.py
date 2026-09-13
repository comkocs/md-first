"""工单业务规则。CLI 与网页回落写入器共用本模块口径。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import config
from .model import (
    DISPATCH_FORBIDDEN_SLOTS, LIVE_ORIGIN, LIVE_SCENE, OWNER_ROLE, SLOTS, TicketError,
    apply_ticket_placeholder, apply_window_prefix,
    deliverable_candidate, deliverable_key, image_record, is_image_deliverable, new_ticket_record,
    normalize_lines, normalize_sources, normalize_window, now_text, path_segments,
    resolve_under_root, with_ticket_defaults,
)
from .store import TicketStore


ORIGIN_MAP = {"world": LIVE_ORIGIN, "isolated": "隔离场景", "other": "其他"}
SHOT_VALUES = {"同图", "独图"}
SHOT_REQUIRED_MESSAGE = "live 必须说明这张图是同图(全批共用一张)还是独图(专为这张单拍):--shot 同图 或 --shot 独图。"
# D9-402 ②:诊断类单与验证对象已退役的单可以免掉那张独图。
# 「免独图」故意不进 SHOT_VALUES——SHOT_VALUES 是首次 live 的合法拍图值,把它塞进去
# 会让「已合并」态的单一上来就免图,等于把 D9-402 ① 一起绕过。豁免只走 shot_exempt 这一条通道。
SHOT_EXEMPT = "免独图"
SHOT_EXEMPT_REASON = "免独图原因"
MAX_IMAGE_BYTES = 200 * 1024
MAX_IMAGE_EDGE = 1280
MIN_IMAGE_QUALITY = 50
IMAGE_QUALITY_STEP = 6
SECOND_IMAGE_SCALE = 0.85
STAFF_PATTERN = re.compile(r"^(?P<slot>.+)-(?P<number>\d{2,3})$")  # T-001659:-100 起的三位号也要能收敛回总监位
DECISION_GATE_REASON = f"拍板单要写成三段人话:这是什么 / 选了会怎样 / 推荐哪个,缺一不能送{OWNER_ROLE}。"
DECISION_HEADERS = ("一、这是什么", "二、选了会怎样", "三、推荐")
# 建完之后还能改口径的三态：交了板的活不许改口径，也不许一笔勾销。
EDITABLE_STATES = ("新建", "已认领", "返工")
# 能一笔勾销的三态：阻塞单也算，否则建错的单只能永远挂着（T-000070）。
VOIDABLE_STATES = ("新建", "已认领", "阻塞")
# 需求单答复的三种口径（T-000770，答 T-000409）：收到 / 排了哪张 / 哪张做完了。
# 三种之外一律拒——一句「好的」也能把单从待答列表里抹掉，排期与交板单号却没有任何地方记。
DEMAND_ANSWER_FORMS = (
    "受理(后面可跟预计)",
    "已排期→T-xxxxxx(派单号)",
    "已完成→T-xxxxxx(交板单号)",
)
DEMAND_ANSWER_REFERENCE_PREFIXES = ("已排期→", "已完成→")
# 单号形状：T- 加正好六位数字；后面可以接说明，但不能再多一位数字。
DEMAND_ANSWER_TICKET_ID = re.compile(r"^T-\d{6}(?!\d)")
PROFESSIONAL_ID_PATTERN = re.compile(r"\b(?:D9|DA|PV|BE)-[A-Za-z0-9]+\b", re.IGNORECASE)
# 停滞阈值的服务端真源；前端 web/tickets.js 里若有同名常量,必须同步修改。
# 「新建·已开窗」的 4 小时档由前端根据服务端「已开窗」字段判断；旧包回落快照才读 localStorage。
STALE_STATE_HOURS = {
    "新建": 24,
    "已认领": 8,
    "返工": 8,
    "待判": 4,
    "待复检": 8,
    "已合并": 24,
    "待答": 24,
}
NON_STALE_STATES = {"关闭", "作废", "实机复验过", "阻塞"}

# 内部单交板时可带的机器闸报告项（来自 config「机器闸项」）。顺序就是报告里要出现的顺序。
# ★这些是**闸**不是清单:少一项、或任一项不是「过」,都不置「机器闸绿」标,
#   也就不会当成复验过——宁可让人多跑一次 verify,不能让一份缺项的报告混成全绿。
GATE_REPORT_ITEMS = config.GATE_REPORT_ITEMS
GATE_PASS_WORDS = ("过", "绿", "pass", "ok", "通过")
GATE_FAIL_WORDS = ("不过", "红", "fail", "退", "未过")

# ★T-001322:列表页那一趟不发的键。见 TicketService.card_view 上面那段注释——
#   这四个键是**逐个 grep 网页脚本得出的零消费端**,不是按体积挑的。
#   往这里加键之前先 grep 一遍消费端:少发一个页面在用的键 = 屏上直接空一块。
LIST_OMITTED_KEYS = ("正文", "答复", "接线证据", "备注")
# 「正文」只有 answerCard() 会显示,而它只渲染这三类里指派给设计者的待答单。
DESIGNER_ANSWER_TYPES = ("拍板", "疑问", "需求")


def is_terminal(ticket: dict[str, Any]) -> bool:
    """这张单还需不需要有人再动它（T-001053，落宪法 v2.20 ④）。

    ★内部单并线即到头：它没有用户可见的产出，也就没有「真登录图」这一步可做。
    工具原来只把 关闭/作废/实机复验过/阻塞 当终态，于是内部单并线之后仍按 24 小时老化，
    源源不断涌进设计者的「卡住了」段、也一直挂在「上服」那一格里——
    「这几个 live 部署单为什么卡住」——查出来就是这个：
    T-000559 卡 38 小时、T-000628 卡 37 小时，两张都是已合并的内部单，没有人该动它们。
    ★用户可感知单不适用：它们仍要一张真登录图才算到头。
    """
    state = str(ticket.get("状态", ""))
    if state in NON_STALE_STATES:
        return True
    return state == "已合并" and bool(ticket.get("非用户可感知"))


# ── 到终态自动退役（T-001081）──────────────────────────────
# 非固定工位是一次性的：窗关了，名册那一行还挂着「在岗」，攒进派单下拉里，
# 总监很容易派给一个早就没了的窗。指望人记得跑 staff retire 是不行的——至今一次都没人跑过。
RETIRE_ON_STATES = frozenset({"关闭", "作废", "实机复验过"})


def is_done_for_staff(ticket: dict[str, Any]) -> bool:
    """这张单还占不占着执行方的手（T-001081）。

    ★与 is_terminal 只差一个「阻塞」，但**绝不能共用**，两条问的不是同一件事：
    · is_terminal 问「还需不需要有人再动它」——阻塞单没人该动，所以算终态、不报老化；
    · 这里问「执行方还要不要回来」——阻塞解开之后原员工还得接着做，
      而 claim 只认在岗，把他退了就再也认领不回来（T-000957 栽过同一类的坑）。
    所以阻塞在这里一律**不算做完**：它既不触发退役，也仍旧算这位手上的一张在办单。
    """
    state = str(ticket.get("状态", ""))
    if state in RETIRE_ON_STATES:
        return True
    return state == "已合并" and bool(ticket.get("非用户可感知"))


# 自动收窗的回执前缀。只在返回文本里加这一行，不改工单状态，也不进盘。
AUTO_RETIRE_PREFIX = "名册已自动收窗："

# ── 阻塞分两类（T-001256 ④，D9-453 / 宪法 v2.31 八章 6「非业务闸不停车」）──────
# 只有「活没做好」才配停车:真源缺、接口对不上、判据不达、测试红。
# 账面类的一律记一行继续做——实测里停过的窗几乎全是账面闸
# (交付项写 .jpg 台面存 .webp、目录型交付项、工作树路径与主检出路径、任务书待回核、
#  远端镜像滞后、本机 CLI 命令行落后),没有一件是活没做好。
BLOCK_BUSINESS = "业务"
BLOCK_NON_BUSINESS = "非业务"
BLOCK_KINDS = (BLOCK_BUSINESS, BLOCK_NON_BUSINESS)
# 非业务的账归平台位集中清（list --nonbiz），不打扰总编排、也不进设计者队列。
CONTENT_SLOT = config.CONTENT_SLOT
# 改真源表时,常常还要在**同一笔**里重刷由它生成的派生件(生成的文档、索引、快照)。
# 这里只在交板时提一句——真闸是「跑一次 build 逐字比对」那种测试。
# ★为什么提示不做成硬闸:服务端看不见交板人那台机器的 git,判不了「他到底刷没刷」。
#   看得见的那一端是测试,所以硬闸放在那儿;这里只保证人**被提醒过**。
# ★换成自己项目的重刷命令:改这一段文字即可。
TRUTH_SOURCE_HINT_SLOTS = (CONTENT_SLOT,)
REBUILD_HINT = (
    "★交板检查项:这张单若改过真源表,请确认已在**同一笔提交**里重刷过由它生成的派生件,"
    "并把变动的生成物一起交上来。"
    "不刷的话页面上给人看的是作废口径,而且不报错——原版曾积压到 4893 行。"
)

PLATFORM_SLOT = config.PLATFORM_SLOT
# 判过之后球传给谁：复检席。写成常量，免得下一个人在字符串里手打错一个字就静默不通知。
REVIEW_SLOT = config.REVIEW_SLOT

# ── 当前值面 ──────────────────────────────────────────────────────────────────
# 「全项目当天都在变」的那几个数放这里一处。谁把它转抄进自己的接管件，
# 抄件就再也不会自己更新——原版有整张单正是照着抄错的判据图尺寸做的，做完全废。
# 所以：各位**读它，不要抄它**；能改它的只有复检席与总编排。
#
# 项与类型来自 config「值面」。加一项、改一项的说明,都只动配置。
STATE_SPECS = {str(row["键"]): row for row in config.STATE_SPECS}
STATE_KEYS = tuple(STATE_SPECS)
STATE_WRITERS = (REVIEW_SLOT, config.CONDUCTOR_SLOT)
STATE_UNSET = "未填"
STATE_UNSET_HINT = f"{STATE_UNSET}，请复检席用 state set 填"
STATE_HELP = {key: str(row.get("说明", "")) for key, row in STATE_SPECS.items()}
STATE_TYPES = {key: str(row.get("类型", "文本")) for key, row in STATE_SPECS.items()}
# 数对类型必填哪几个键，例如 {"走","跑"}；没写就不校验键名。
STATE_PAIR_KEYS = {key: tuple(row.get("键名") or ()) for key, row in STATE_SPECS.items()}


def _state_items() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """顶栏与 receipt 摘要上的分组：标签相同的多项合成一格显示，键仍各自独立。

    ★按**首次出现顺序**分组,不排序:顶栏顺序是配置里写的顺序,
      排序会让人改了配置却发现顶栏没跟着动。
    """
    grouped: dict[str, list[str]] = {}
    for key, row in STATE_SPECS.items():
        grouped.setdefault(str(row.get("标签") or key), []).append(key)
    return tuple((label, tuple(keys)) for label, keys in grouped.items())


STATE_ITEMS = _state_items()
# 上服目标名 → 值面键 / 上服记录归哪一位（来自 config「部署目标」）。
DEPLOY_HEAD_KEYS = {str(row["名字"]): str(row["值面键"]) for row in config.DEPLOY_TARGETS}
DEPLOY_TARGET_SLOTS = {str(row["名字"]): str(row["归位"]) for row in config.DEPLOY_TARGETS}
DEPLOY_TARGETS = tuple(DEPLOY_HEAD_KEYS)
STATE_RESOLUTION = re.compile(r"^(\d{2,5})[xX×](\d{2,5})$")
STATE_COMMIT = re.compile(r"^[0-9a-fA-F]{7,40}$")
STATE_SPLIT = re.compile(r"[;；]")
STATE_PAIR_SPLIT = re.compile(r"[,，;；]")


def state_text(value: Any) -> str:
    """任何一项值的一行人话；没填就是「未填」，永远不出现空白或 None。"""
    if value is None:
        return STATE_UNSET
    if isinstance(value, dict):
        return "/".join(f"{name}{number}" for name, number in value.items()) or STATE_UNSET
    if isinstance(value, list):
        if not value:
            return "无"
        names = "、".join(str(row.get("名字", "")) for row in value if isinstance(row, dict))
        return f"{len(value)} 项:{names}" if names else f"{len(value)} 项"
    return str(value)


def _state_number(text: str) -> float | int:
    value = str(text).strip()
    try:
        return int(value)
    except ValueError:
        return float(value)


def _parse_pairs(key: str, raw: str) -> dict[str, Any]:
    """数对：`a=1,b=2` 或一段 JSON 对象。配置里列了「键名」就逐个必填。"""
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        pairs: dict[str, Any] = loaded
    else:
        pairs = {}
        for chunk in STATE_PAIR_SPLIT.split(raw):
            name, separator, number = chunk.partition("=")
            if separator:
                pairs[name.strip()] = number.strip()
    required = STATE_PAIR_KEYS.get(key) or tuple(pairs)
    if not required:
        raise TicketError(f"{key} 写法不对：{STATE_HELP[key]}。收到的是：{raw}")
    try:
        return {name: _state_number(pairs[name]) for name in required}
    except (KeyError, TypeError, ValueError) as exc:
        raise TicketError(f"{key} 写法不对：{STATE_HELP[key]}。收到的是：{raw}") from exc


def _parse_name_status(raw: str, key: str) -> list[dict[str, str]]:
    stripped = raw.strip()
    if stripped in {"无", "空", "[]"}:
        return []
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        loaded = None
    chunks: list[Any] = loaded if isinstance(loaded, list) else STATE_SPLIT.split(stripped)
    rows: list[dict[str, str]] = []
    for chunk in chunks:
        if isinstance(chunk, dict):
            name, status = str(chunk.get("名字", "")).strip(), str(chunk.get("状态", "")).strip()
        else:
            head, _, tail = str(chunk).partition("=")
            name, status = head.strip(), tail.strip()
        if not str(chunk).strip():
            continue
        if not name or not status:
            raise TicketError(
                f"{key} 每一项都要有名字和一句状态说明：{STATE_HELP[key]}。"
                f"这一条不合格：{chunk}"
            )
        rows.append({"名字": name, "状态": status})
    if not rows:
        raise TicketError(f"{key} 写法不对：{STATE_HELP[key]}。收到的是：{raw}")
    return rows


def parse_state_value(key: str, raw: str) -> Any:
    """把命令行上的一串字，变成该键该有的形状；形状不对当场拒，并把写法原样告诉人。

    ★拒的时候必须把**该怎么写**原样贴出来(STATE_HELP)。只回一句「写法不对」,
      人只能一遍遍试——而这几个值一天要改好几次。
    """
    text = str(raw).strip()
    if not text:
        raise TicketError(f"{key} 不能填空值：{STATE_HELP[key]}。要清掉一项请找总编排。")
    kind = STATE_TYPES.get(key, "文本")
    if kind == "分辨率":
        matched = STATE_RESOLUTION.match(text)
        if not matched:
            raise TicketError(f"{key} 写法不对：{STATE_HELP[key]}。收到的是：{raw}")
        return f"{int(matched.group(1))}x{int(matched.group(2))}"
    if kind == "提交号":
        if not STATE_COMMIT.match(text):
            raise TicketError(f"{key} 写法不对：{STATE_HELP[key]}。收到的是：{raw}")
        return text.lower()
    if kind == "数对":
        return _parse_pairs(key, text)
    if kind == "名状清单":
        return _parse_name_status(text, key)
    return text


def with_state_summary(text: str, payload: Any) -> str:
    """当前值面摘要永远是 receipt 的最后一行（T-000892 R2）。

    本机与远程两条路都要经过这里；服务端旧版不回这一格时安静跳过，不许打半行空摘要。
    """
    summary = payload.get("值面摘要") if isinstance(payload, dict) else ""
    return f"{text}\n{summary}" if summary else text


def _quality_steps(start: int) -> tuple[int, ...]:
    values: list[int] = []
    quality = start
    while True:
        values.append(quality)
        if quality == MIN_IMAGE_QUALITY:
            return tuple(values)
        quality = max(MIN_IMAGE_QUALITY, quality - IMAGE_QUALITY_STEP)


def _bounded_image(data: bytes) -> tuple[str, int, int] | None:
    if len(data) > MAX_IMAGE_BYTES:
        return None
    width = height = 0
    extension = ""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        extension = ".png"
    elif data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], "big")
            if marker in range(0xC0, 0xC4) and index + 7 < len(data):
                height = int.from_bytes(data[index + 3:index + 5], "big")
                width = int.from_bytes(data[index + 5:index + 7], "big")
                extension = ".jpg"
                break
            index += max(length, 2)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
        elif chunk == b"VP8 ":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
        elif chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        extension = ".webp"
    if extension and 0 < width <= MAX_IMAGE_EDGE and 0 < height <= MAX_IMAGE_EDGE:
        return extension, width, height
    return None


def compress_image(path_or_bytes: str | Path | bytes, stem: str = "") -> tuple[str, bytes]:
    """Return one deterministic, bounded representation for every image entry path."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise TicketError("本机缺少图片压缩组件，不能处理原始图片。") from exc

    if isinstance(path_or_bytes, (str, Path)):
        source = Path(path_or_bytes)
        if not source.is_file():
            raise TicketError(f"找不到图片：{source}")
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise TicketError(f"读不懂图片：{source.name}") from exc
        output_stem = stem or source.stem
    else:
        data = bytes(path_or_bytes)
        output_stem = stem or "image"
    if not data:
        raise TicketError("上传图片是空文件。")

    # 已经由本函数产出的 JPEG/WEBP 可原样穿过服务端，避免远程上传二次有损编码。
    bounded = _bounded_image(data)
    if bounded and bounded[0] in {".jpg", ".webp"}:
        try:
            with Image.open(io.BytesIO(data)) as bounded_image:
                orientation = bounded_image.getexif().get(274, 1)
        except OSError:
            orientation = 0
        if orientation in {None, 1}:
            return output_stem + bounded[0], data

    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened).copy()
        has_alpha = image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info)
        extension = ".webp" if has_alpha else ".jpg"
        image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
        qualities = _quality_steps(85 if has_alpha else 88)
        encoded = b""
        for size_pass in range(2):
            for quality in qualities:
                output = io.BytesIO()
                if has_alpha:
                    image.convert("RGBA").save(output, "WEBP", quality=quality, method=6)
                else:
                    image.convert("RGB").save(output, "JPEG", quality=quality, optimize=True, progressive=True)
                encoded = output.getvalue()
                if len(encoded) <= MAX_IMAGE_BYTES:
                    return output_stem + extension, encoded
            if size_pass == 0:
                reduced = tuple(max(1, round(edge * SECOND_IMAGE_SCALE)) for edge in image.size)
                image = image.resize(reduced, Image.Resampling.LANCZOS)
    except TicketError:
        raise
    except Exception as exc:
        raise TicketError(f"图片压缩失败：{exc}") from exc

    longest_edge = max(image.size)
    raise TicketError(
        f"图片压缩失败：已降到质量 {qualities[-1]} / 长边 {longest_edge},仍 {len(encoded) / 1024:.1f}KB。"
    )
CONDUCTOR_SLOT = config.CONDUCTOR_SLOT

# ── 固定工位的工位记忆（T-001073）────────────────────────────
# 窗是一次性的，固定工位却跨窗复用：没有记忆件，下一窗要么从零翻工单，要么把上一窗
# 踩过的坑再踩一遍。骨架由工具从「这位做过的单」自动生成，每一行都回指到某张单——
# 自述不可信，员工能填的只有一小节「留给下一窗的底数与坑」。
#
# ★这一段是任务书要求原文照抄的第一段，改一个字就等于把「先核分支头与绿数」这道
#   提醒改掉了。记忆件是**快照**：它写下那一刻起就在过期，里面的数字只当线索。
MEMORY_STEP_ZERO = (
    "## 第 0 步(每次开窗必做,不许跳)\n"
    "1. `git fetch` 之后核主线短号与本文件记的是否一致;\n"
    "2. 在自己的工作树上跑一次全量测试,拿到**当下**的绿数;\n"
    "3. 本文件里的分支头、绿数、行号**只当线索不当事实**——它是快照,写下那一刻起就在过期。\n"
    "   对不上就以现在跑出来的为准,并在本单里报一行。"
)
# 记忆件的路径**约定**。工具不按它拼路径、也不校验，只在报错与
# README 里把话说清楚：路径由 staff fix --memory 显式给，写歪了是人的事，不是工具替人猜。
MEMORY_PATH_HINT = config.memory_path_hint()
# 记忆 md 的总行数上限：员工窗只有 300K 上下文，记忆件无限长就等于没有记忆件。
DEFAULT_MEMORY_MAX_LINES = 400
# 自动重刷的回执前缀。submit/judge 只在返回文本里加这一行，成败都不改状态；
# 远程模式下客户端还要靠这个前缀把服务端那条（服务器上没有 D: 盘，必失败）换掉。
MEMORY_REFRESH_PREFIX = "记忆 md 重刷"
# 「已经交过板」的判据：现态在这几个里，或者证据文字非空（返工态的单也交过板）。
SUBMITTED_STATES = frozenset({"待判", "待复检", "已合并", "实机复验过", "关闭"})
# 从证据里捞分支名与提交号：捞到什么写什么，捞不到写「证据里没写」，绝不替员工编一个。
MEMORY_BRANCH_PATTERN = re.compile(r"\b(?:feat|fix|chore|test|docs|merge|refactor|perf)/[\w./\-]+")
MEMORY_COMMIT_PATTERN = re.compile(r"\b[0-9a-f]{7,40}\b")


def memory_archive_path(path: Path) -> Path:
    """主文件 X.md 的归档件是同目录的 X.archive.md（追加，不覆盖）。"""
    return path.with_name(f"{path.stem}.archive.md")


# 「盘符开头」或「\\ 开头」= Windows 风格的绝对路径。
WINDOWS_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def path_is_for_another_machine(path: str) -> bool:
    """这个路径写的是**另一台机器**的盘吗——判「本机查得了它吗」，不判「远程模式吗」。

    远程模式下路径由客户端给、命令在服务端跑：服务器上根本没有 D: 盘，
    拿它去查服务端自己的文件系统，结论永远是「不存在」。这里判的是路径的**形状**，
    不需要谁把「我是远程」这个标志一路传进来——标志会漏传，形状不会。
    ★同一个文件里 write_memory 早就把这件事写明了（「远程模式下服务器上没有 D: 盘，
      所以由客户端在自己这台机器上写」），只有 staff_fix 那条校验没跟上，于是恒定误报。
    """
    text = str(path or "").strip()
    if not text:
        return False
    if os.name == "nt":
        # 本机是 Windows：posix 绝对路径（/home/… 这种）是别人家的。
        # ★只认 / 开头，不认 \ 开头——后者在 Windows 上是本盘根目录的合法写法。
        return text.startswith("/")
    return bool(WINDOWS_ABSOLUTE_PATH.match(text))


def normalize_handoff(value: str | list[Any] | None) -> list[dict[str, str]]:
    """把 --handoff 的多行文本收成「留给下一窗」的行清单。

    存成一行一条而不是一整段，是因为 R3 的划掉是**按行号**落的：
    判卷人划掉第 2 行，下一窗要看见的是「这一行还在，但有人认为它错了」。
    """
    if value is None:
        return []
    if isinstance(value, list):
        rows = value
    else:
        rows = str(value).splitlines()
    result: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, dict):
            text = str(row.get("文字", "")).strip()
            if text:
                result.append({
                    "文字": text,
                    "划掉判卷人": str(row.get("划掉判卷人", "")).strip(),
                    "划掉时间": str(row.get("划掉时间", "")).strip(),
                })
            continue
        text = str(row).strip()
        if text:
            result.append({"文字": text, "划掉判卷人": "", "划掉时间": ""})
    return result


def handoff_rows(ticket: dict[str, Any]) -> list[dict[str, str]]:
    """读「留给下一窗」的行；老单没有这一格，返回空清单而不是 KeyError。"""
    section = ticket.get("留给下一窗") or {}
    if not isinstance(section, dict):
        return []
    return normalize_handoff(section.get("行"))


def normalize_model_name(value: Any) -> str:
    """模型记账键：大小写不敏感，空白与下划线统一折成单个连字符。"""
    return re.sub(r"[\s_]+", "-", str(value or "").strip().lower())


class TicketService:
    def __init__(self, store: TicketStore | None = None) -> None:
        self.store = store or TicketStore()
        with self.store.locked():
            self.store.ensure()

    def create_dispatch(
        self,
        slot: str,
        title: str,
        sources: list[str] | str | None,
        consumer: str,
        assign: str = "",
        initiator: str = CONDUCTOR_SLOT,
        notes: str = "",
        task_tier: str | None = None,
        context_lines: int | None = None,
        deliverables: list[str] | str | None = None,
        internal: bool | None = None,
        body: str = "",
        taskbook: str = "",
        taskbook_check: str = "",
        window: str = "",
        system_generated: bool = False,
    ) -> dict[str, Any]:
        """system_generated:只给 deploy_record / evidence_ticket 用（D9-460 ③），CLI 不暴露。

        那两张单是**系统按既成事实自动建的**,没有「要谁产出哪个文件」这回事:
        上服记录记的是线上现在跑着哪个头,取证单要的是屏上那一眼。
        交付项必填闸是为「人开的活」设的,套在它们身上只会逼出一句叙述句凑数——
        而叙述句正是 T-000267 那个「交付项死结」的来源。所以这里明着放行,不绕道。
        """
        # ★D9-462 补:第十三位「设计·裁定分发」只发需求/疑问/拍板,不派实现单。
        #   拦在取号之前——建到一半再拒会白烧一个单号(T-000070 那一课)。
        if slot in DISPATCH_FORBIDDEN_SLOTS:
            raise TicketError(
                f"本位不派实现单：「{slot}」只发需求、疑问、拍板三类(宪法 v2.38 / D9-462 补)。"
                f"它是{OWNER_ROLE}在后端/数值/物品/真源类事务上的对接人:逐字记录裁定 → 送{CONDUCTOR_SLOT}落 D9 号 →"
                "按影响面用需求/疑问单广播给各位;实现单由收到广播的那一位自己的总监派。"
                f"要把活派出去,请用 ask --type 需求 --slot <干活的那一位> --by {slot}。"
            )
        # ★缺项一次报全,不要撞一条补一条。派单有五条必填,分散在两层闸上;
        #   逐条拒的话人要重敲三四遍长命令行,而每敲一遍都是一次手抖的机会。
        # ★「给的值不合法」与「没给」是两件事,次序照旧:档位给了就先验它的值
        #   (丙档的预算闸挂在这里),验完再数缺项。
        delivery_rows = normalize_lines(deliverables)
        if task_tier is not None:
            self._validate_task_tier(task_tier, context_lines)
        missing = self.missing_dispatch_requirements(
            task_tier=task_tier, deliverables=delivery_rows, internal=internal,
            system_generated=system_generated,
        )
        if missing:
            raise TicketError(self.format_missing_requirements(missing))
        # 先验后取号：非法值要在 next_ticket_id() 之前拒掉，否则一个错字白烧一个单号（T-000070）。
        window = normalize_window(window)
        if assign:
            self.require_active_staff(slot, assign)
        ticket_id = self.store.next_ticket_id()
        source_slot = self._actor_slot(initiator)
        ticket = new_ticket_record(ticket_id, "派单", slot, title, initiator, sources, consumer, assign, body=body, notes=notes, task_tier=task_tier, context_lines=context_lines, deliverables=delivery_rows, source_slot=source_slot if source_slot and source_slot != slot else "", internal=internal, taskbook=apply_ticket_placeholder(taskbook, ticket_id), window=window)
        ticket["任务书校验"] = taskbook_check
        self.store.save_ticket(ticket, "新建", initiator, "派单已建立")
        self._add_staff_history(assign, ticket_id)
        self._notify_slots((slot,), initiator, f"{initiator} 建了派单 {ticket_id} · {title}", ticket_id)
        return ticket

    @staticmethod
    def missing_dispatch_requirements(
        *,
        consumer: str | None = None,
        sources: list[str] | str | None = None,
        task_tier: str | None = "",
        deliverables: list[str] | None = None,
        internal: bool | None = False,
        system_generated: bool = False,
        include_entry_gates: bool = False,
    ) -> list[str]:
        """派单缺哪几项——**一次数完**，一条缺项一句话。

        派单的必填闸有五条，分在两层：`--consumer`/`--source` 由入口拦，
        `--tier`/`--deliverable`/可感知标记由服务端拦（服务端仍是唯一真闸）。
        原先五条各自 raise，撞一条补一条：一张单要重敲三四遍长命令行，
        而每重敲一遍都是一次手抖的机会。判据抽在这里给两层共用，报的时候一次列全。
        ★每条的话**一字未改**，还是原来那句——这些话里写着各自是哪次事故换来的，
          改写它们是另一件事，不该搭本次的车。这里变的只有「一次说几条」。
        ★include_entry_gates 只给入口用：服务端不默认查那两条，那会收紧现有服务层口径。
        """
        missing: list[str] = []
        if include_entry_gates:
            if not str(consumer or "").strip():
                missing.append(f"派单必须写实机消费者(本单产物在{LIVE_SCENE}链上被谁加载),填不出=不发车。")
            rows = [sources] if isinstance(sources, str) else list(sources or [])
            if not any(str(value).strip() for value in rows):
                missing.append("派单必须写真源指针(本单执行依据在哪个文件或哪条决定),填不出=不发车。")
        if task_tier is None:
            missing.append("任务档必填，请用 --tier 甲、乙或丙。")
        if not normalize_lines(deliverables or []) and not system_generated:
            missing.append("交付项必填，请逐行写出本单必须产出的文件。")
        if internal is None:
            missing.append(
                "这张单是内部工具单（交板给验证命令与原样输出）还是用户可感知单"
                f"（交板必须附 {LIVE_ORIGIN}图）？"
                "--internal 与 --user-facing 二选一，必须明写——"
                "漏标会让内部单在交板那一刻才炸（T-000807）。"
            )
        return missing

    @staticmethod
    def format_missing_requirements(missing: list[str]) -> str:
        if len(missing) == 1:
            return f"不能建派单：{missing[0]}"
        head = f"不能建派单：还缺 {len(missing)} 项，请一次补齐——"
        return head + "".join(f"\n  · {line}" for line in missing)

    def create_question(
        self,
        ticket_type: str,
        slot: str,
        title: str,
        body: str,
        initiator: str = OWNER_ROLE,
        sources: list[str] | str | None = None,
        consumer: str = "",
        task_tier: str = "",
        context_lines: int | None = None,
        taskbook: str = "",
        taskbook_check: str = "",
    ) -> dict[str, Any]:
        if ticket_type not in {"拍板", "疑问", "需求", "阻塞", "总工单"}:
            raise TicketError("这里只用于拍板、疑问、需求、阻塞、总工单。")
        if task_tier:
            self._validate_task_tier(task_tier, context_lines)
        source_slot = self._actor_slot(initiator)
        # T-000957(答 T-000953 与设计者「美术总监之间发不了工单」)：
        # 「指派给」是各位扫自己活的那一格。D9-424 ⑥ 把需求/阻塞的答复权交给了所属总监位，
        # 可这里还把它们一律指给总编排——两个字段打架，收件位按「指派给」扫**看不见本该自己答的单**，
        # 发的人以为没发出去。实测:全台面待答 40 张里 22 张这么错位，最老的三张单号还在三四百段。
        # 只改需求/阻塞：拍板本来就该设计者拍、总工单本来就归总编排、疑问的跨位规则原来就是对的。
        if ticket_type == "拍板":
            target = OWNER_ROLE
        elif ticket_type in {"需求", "阻塞"}:
            target = slot if slot in SLOTS and slot != CONDUCTOR_SLOT else CONDUCTOR_SLOT
        elif ticket_type == "疑问":
            target = slot if (source_slot and source_slot != slot) else OWNER_ROLE
        else:  # 总工单
            target = CONDUCTOR_SLOT
        if ticket_type == "拍板" and target == OWNER_ROLE:
            self._validate_decision_body(body)
        ticket_id = self.store.next_ticket_id()
        ticket = new_ticket_record(ticket_id, ticket_type, slot, title, initiator, sources, consumer, body=body, task_tier=task_tier or "乙", context_lines=context_lines, source_slot=source_slot if source_slot and source_slot != slot else "", taskbook=apply_ticket_placeholder(taskbook, ticket_id))
        ticket["任务书校验"] = taskbook_check
        if not task_tier:
            ticket["任务档"] = ""
            ticket["上下文预算"] = None
        ticket["指派给"] = target
        self.store.save_ticket(ticket, "待答", initiator, body)
        # 所属位要知道自己名下多了一张；真正该动手的是「指派给」那一方（需求/阻塞的 target 是总编排，
        # 常常与所属位不是同一个），所以两边都通知，_notify_slots 内部去重。
        self._notify_slots((slot, target), initiator, f"{initiator} 建了{ticket_type}单 {ticket_id} · {title}", ticket_id)
        return ticket

    def edit(
        self,
        ticket_id: str,
        actor: str,
        taskbook: str | None = None,
        assign: str | None = None,
        sources: list[str] | str | None = None,
        body: str | None = None,
        consumer: str | None = None,
        deliverables: list[str] | str | None = None,
        taskbook_check: str | None = None,
        internal: bool | None = None,
        window: str | None = None,
        task_tier: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """改一张已经建好的单。

        以前建完之后任何字段都改不了：复工令要各位「把任务书路径填进已派的单」，
        CLI 里却没有任何子命令能改（T-000051、T-000070）；员工按名册改名后，
        老单的「指派给」永久停在已收窗的旧编号上，卡片自动生成的开窗指令
        第二行会写错工具名，设计者照着开窗就开错模型（T-000048）。
        """
        ticket = self.store.load_ticket(ticket_id)
        requested: list[tuple[str, Any]] = []
        if taskbook is not None:
            requested.append(("任务书路径", str(taskbook).strip()))
            if taskbook_check is not None:
                requested.append(("任务书校验", taskbook_check))
        if assign is not None:
            requested.append(("指派给", str(assign).strip()))
        if sources is not None:
            rows = normalize_sources(sources)
            if not rows:
                raise TicketError(
                    f"不能改 {ticket['编号']}：--source 给了，但一条有内容的都没有。"
                    "真源指针清空了这张单就认领不了；要改请把新的依据原样写出来。"
                )
            requested.append(("真源指针", rows))
        if body is not None:
            requested.append(("正文", str(body).strip()))
        if consumer is not None:
            requested.append(("实机消费者", str(consumer).strip()))
        if deliverables is not None:
            # 交付项写成叙述句的单永远交不了板:submit 逐条当文件路径核,叙述句核不到。
            # 曾在一天内撞两次(T-000018、T-000108),而员工改不了交付项、
            # set 又不收这个字段,单子就只能卡死。这里把它补上,让总监能就地改成真路径。
            rows = normalize_lines(deliverables)
            if not rows:
                raise TicketError(
                    f"不能改 {ticket['编号']}：--deliverable 给了，但一条有内容的都没有。"
                    "交付项清空这张单就交不了板；要改请把每个产物的路径逐条写出来。"
                )
            requested.append(("交付项", rows))
        if internal is not None:
            # T-000489/T-000514:建单时标错「用户可感知」,交板就被实机图闸拦死,
            # 而这个标记以前只能在 new 时定、set 改不了 —— 活全干完了也只能作废重建,
            # 或者拿一张无关截图去糊弄闸(那比卡住还坏)。这里补上,权限沿用本位总监/总编排。
            requested.append(("非用户可感知", bool(internal)))
        if window is not None:
            # 真源是标题开头的【X】，所以 --window 改的其实是**标题**：已有前缀替换、没有就加，
            # 给空串就是把建议撤回。改起来跟 --assign 一个待遇；待判态照旧被下面那道闸拦住。
            requested.append(("标题", apply_window_prefix(ticket.get("标题", ""), normalize_window(window))))
        if task_tier is not None:
            # T-001560/T-001449:任务书从丙重写成乙,档位字段却改不了——卡片与开窗指令
            # 都还按丙走,设计者照丙选模型。档位是总监的判断,允许就地改,记进改动日志。
            tier = str(task_tier).strip()
            if tier not in {"甲", "乙", "丙"}:
                raise TicketError(
                    f"不能改 {ticket['编号']}：任务档只能是 甲/乙/丙，写的是「{tier}」。"
                )
            requested.append(("任务档", tier))
        if not requested:
            raise TicketError(
                f"不能改 {ticket['编号']}：一个可改项都没给。"
                "至少给一个：--taskbook 任务书路径、--assign 指派给、--source 真源指针、"
                "--body 正文、--consumer 实机消费者、--deliverable 交付项、"
                "--internal/--user-facing 是否用户可感知、--window 建议窗口、--tier 任务档。"
            )
        if ticket["状态"] == "待判":
            if len(requested) != 1 or requested[0][0] != "指派给":
                raise TicketError(
                    f"不能改 {ticket['编号']}：现在是「待判」，待判态只允许改指派给（--assign）；"
                    "其他字段要改请先判退返工。"
                )
        else:
            self._require_editable_state(ticket, "改")
        if internal is not None and len(requested) == 1:
            # T-000831:单独翻可感知开关时,设计者也放进来——建单漏标会把内部单拦死在交板口,
            # 设计者是最高权,给他一条一行命令就能解卡的路。
            # 只放开这一个开关;连着别的可改项一起给,或改其他项,仍走原来的署名闸。
            self._require_facing_gate(ticket, actor)
        elif self._is_own_deliverable_reshape(ticket, actor, requested):
            # T-001256 ②:员工可以改**自己这张单**交付项的写法(不改语义)。
            # 从前员工核不到交付项就只能停下来等总监改一次,而多半只是后缀或前缀写法不同。
            # 语义由 _is_own_deliverable_reshape 逐条比对,变了一条就落不到这里。
            self._audit_worker_reshape(ticket, actor, requested)
        else:
            self._require_owner_or_conductor(ticket, actor, "改")
        for field, value in requested:
            if field == "指派给" and value:
                if ticket["状态"] == "待判":
                    found = self.find_staff(value)
                    if not found:
                        raise TicketError(f"员工名册里没有 {value}，请先用 staff new 登记。")
                    if found[1]["状态"] != "在岗":
                        raise TicketError(f"{value} 已收窗，不能认领或被指派；修本人 BUG 才可 staff reopen。")
                else:
                    self.require_active_staff(str(ticket["所属总监位"]), value)
        changes: list[dict[str, Any]] = []
        for field, value in requested:
            before = ticket.get(field, [] if field in ("真源指针", "交付项") else "")
            if before == value:
                continue
            ticket[field] = value
            changes.append({"字段": field, "旧值": before, "新值": value})
        # 标题一改，派生的「建议窗口」就得跟着重算——这里和读单走同一个函数，两处不会各算各的。
        with_ticket_defaults(ticket)
        if not changes:
            raise TicketError(
                f"不能改 {ticket['编号']}：给的值和单上现有的值一模一样，没有要改的东西。"
            )
        detail = "；".join(
            f"{row['字段']}：{self._field_text(row['旧值']) or '（空）'} → {self._field_text(row['新值']) or '（空）'}"
            for row in changes
        )
        self.store.save_ticket(
            ticket, "set", actor, detail,
            {"op": "set", "by": actor, "改动": changes},
        )
        for row in changes:
            if row["字段"] == "指派给" and row["新值"]:
                self._add_staff_history(str(row["新值"]), ticket["编号"])
        return ticket, changes

    def set_blame(self, ticket_id: str, blame: str, reason: str, actor: str) -> tuple[dict[str, Any], str, str]:
        """判卷人发现自己把判退责任记反了，回头改过来（T-000770，答 T-000747）。

        `judge` 只认「待判」态，一判完就再也进不去；`set` 的可改项里又没有责任归属。
        于是归属一旦记错就永远改不回来——而模型判退是会累计到停用线的硬账：
        曾有工位正是在「再判退 1 次就到停用线」的提示下回头复核，
        发现自己把出题责任记成了模型责任，却没有任何路径改回来。归属错了改不回来，
        等于让模型替出题人背停用风险。

        字段、返工原因列表的最后一条、两本账（模型记分 / 出题记分）三处一起改，
        少改哪一处，单上写的和账上记的就永久对不上。
        """
        ticket = self.store.load_ticket(ticket_id)
        blame = blame.strip()
        reason = reason.strip()
        if blame not in {"模型", "出题"}:
            raise TicketError("判退责任只可填“模型”或“出题”。")
        if not reason:
            raise TicketError(
                f"不能改 {ticket['编号']} 的判退责任：--reason 不能为空，"
                "要用一句话写清原来那一笔为什么记错了。"
            )
        if ticket["状态"] == "待判":
            raise TicketError(
                f"不能改 {ticket['编号']} 的判退责任：现在是「待判」，这张单还没判过。"
                "待判态请直接用 judge --blame 一次填对，不给第二条路。"
            )
        self._require_owner_or_conductor(ticket, actor, "改判退责任")
        before = str(ticket.get("判退责任", "")).strip()
        if not before:
            raise TicketError(
                f"不能改 {ticket['编号']} 的判退责任：这张单上没有判退责任可改。"
                "只有判退过的单才记责任归属，判过的单本来就不记。"
            )
        if before == blame:
            raise TicketError(
                f"不能改 {ticket['编号']} 的判退责任：单上现在就是「{blame}」，没有要改的东西。"
            )
        ticket["判退责任"] = blame
        history = ticket.get("返工原因列表") or []
        if history:
            # judge 往最后一条里写了「判退责任」与「责任」两个键，两个都要跟着改，
            # 只改一个的话 digest 与前端各读各的，同一笔会显示成两种归属。
            history[-1]["判退责任"] = blame
            history[-1]["责任"] = blame
        staff = self.store.load_staff()
        direction = -1 if blame == "出题" else 1
        model = self._adjust_model_score(staff, ticket, direction)
        owner = self._adjust_question_score(staff, ticket, -direction)
        self.store.save_staff(staff)
        detail = (
            f"判退责任：{before} → {blame}；理由：{reason}"
            f"；模型记分 {model or '无可记模型'} {direction:+d}；出题记分 {owner} {-direction:+d}"
        )
        self.store.save_ticket(
            ticket, "set-blame", actor, detail,
            {
                "op": "set-blame", "by": actor, "旧值": before, "新值": blame, "理由": reason,
                "模型记分": model, "出题记分": owner,
            },
        )
        # 停不停用是总编排核过归属之后落 D9 的事（D9-388），账一动就得让他知道。
        self._notify_slots(
            (CONDUCTOR_SLOT, str(ticket.get("所属总监位", ""))), actor,
            f"{actor} 把 {ticket['编号']} 的判退责任从「{before}」改成「{blame}」。理由：{reason}",
            ticket["编号"],
        )
        return ticket, before, blame

    def void(self, ticket_id: str, reason: str, actor: str) -> dict[str, Any]:
        """作废一张建错的单。

        建错的单 close 不掉（派单只有「实机复验过」能关闭），只能 block 挂成阻塞，
        而阻塞单同样作废不了，永远挂在台面上（T-000048②、T-000070）。
        """
        ticket = self.store.load_ticket(ticket_id)
        if not str(reason).strip():
            raise TicketError(f"不能作废 {ticket['编号']}：原因必填，请用一句话写清这张单错在哪。")
        if ticket["状态"] == "作废":
            raise TicketError(f"{ticket['编号']} 已经是「作废」，不用再废一次。")
        follow_up = ticket["状态"] == "返工" and re.search("T-[0-9]{6}", str(reason)) is not None
        # 母单结案(T-000543 口径②):窗已关、总监已立续单的返工母单可作废,原因里必须写续单号;
        # 母单的返工次数保留,仍计入模型合格率,续单不重复计。
        if ticket["状态"] not in VOIDABLE_STATES and not follow_up:
            raise TicketError(
                f"不能作废 {ticket['编号']}：现在是「{ticket['状态']}」，"
                f"只有「{' / '.join(VOIDABLE_STATES)}」三态能作废。"
                "交了板的活不许一笔勾销；要否掉请走判退返工，判过之后的单只能关闭。"
            )
        self._require_owner_or_conductor(ticket, actor, "作废")
        ticket["阻塞前状态"] = ""
        ticket["阻塞原因"] = ""
        ticket["状态"] = "作废"
        ticket["流程提示"] = f"这张单已作废：{str(reason).strip()}。不要再往下推，需要的话另建一张新单。"
        self.store.save_ticket(
            ticket, "void", actor, str(reason).strip(),
            {"op": "void", "by": actor, "reason": str(reason).strip()},
        )
        return self._with_retire_notice(ticket)

    @staticmethod
    def _field_text(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            return "；".join(str(row) for row in value)
        return str(value if value is not None else "")

    @staticmethod
    def _require_editable_state(ticket: dict[str, Any], action: str) -> None:
        if ticket["状态"] not in EDITABLE_STATES:
            raise TicketError(
                f"不能{action} {ticket['编号']}：现在是「{ticket['状态']}」，"
                f"只有「{' / '.join(EDITABLE_STATES)}」三态能{action}口径。"
                "已经交板判卷的单不许改口径；要改请先判退返工，或作废后另建一张。"
            )

    @staticmethod
    def _require_demand_answer_prefix(ticket: dict[str, Any], answer: str) -> None:
        """需求单的答复第一句必须写成三种口径之一（T-000770 / T-000409）。

        放开答复权之后，若不同时把口径钉死，需求单会退化成「谁都能回一句『好的』」：
        单从待答列表里消失了，但排期派到哪张、交板是哪张，一个字都没留下。
        三种前缀把这件事写死在第一句上，带单号的两种还要核单号形状——
        写成「已排期→T-12」等于没写，查过去只会查到一张不存在的单。
        """
        head = answer.strip().splitlines()[0].strip()
        forms = "；".join(DEMAND_ANSWER_FORMS)
        for prefix in DEMAND_ANSWER_REFERENCE_PREFIXES:
            if head.startswith(prefix):
                rest = head[len(prefix):].strip()
                if not DEMAND_ANSWER_TICKET_ID.match(rest):
                    raise TicketError(
                        f"不能答 {ticket['编号']}：「{prefix}」后面要跟一个 T- 加六位数字的单号，"
                        f"你写的是「{rest or '空'}」。需求单答复只收这三种写法：{forms}。"
                    )
                return
        if head.startswith("受理"):
            return
        raise TicketError(
            f"不能答 {ticket['编号']}：需求单的答复首词必须是这三种之一：{forms}。"
            f"你写的是「{head or '空'}」。"
        )

    @staticmethod
    def _require_owner_or_conductor(ticket: dict[str, Any], actor: str, action: str) -> None:
        slot = str(ticket.get("所属总监位", ""))
        if actor.strip() not in {slot, CONDUCTOR_SLOT}:
            raise TicketError(
                f"不能{action} {ticket['编号']}：--by 写的是「{actor.strip() or '空'}」。"
                f"这张单挂在「{slot}」位，只有该位总监「{slot}」本人或总编排能{action}；"
                f"员工窗和别位总监都不行，请找本位总监或{CONDUCTOR_SLOT}代办。"
            )

    @staticmethod
    def _require_facing_gate(ticket: dict[str, Any], actor: str) -> None:
        """可感知开关（--internal/--user-facing）的署名闸：本位总监、总编排之外，设计者也能翻（T-000831）。"""
        slot = str(ticket.get("所属总监位", ""))
        if actor.strip() not in {slot, CONDUCTOR_SLOT, OWNER_ROLE}:
            raise TicketError(
                f"不能改 {ticket['编号']} 的可感知标记：--by 写的是「{actor.strip() or '空'}」。"
                f"这个开关只有该位总监「{slot}」本人、总编排或设计者能翻；"
                f"员工窗和别位总监都不行，请找本位总监、{CONDUCTOR_SLOT}或{OWNER_ROLE}代办。"
            )

    def claim(self, ticket_id: str, actor: str) -> dict[str, Any]:
        ticket = self.store.load_ticket(ticket_id)
        self._require_dispatch(ticket)
        if ticket["状态"] not in {"新建", "返工"}:
            raise TicketError(f"{ticket['编号']} 现在是“{ticket['状态']}”，只有“新建”或“返工”能认领。")
        if not normalize_sources(ticket.get("真源指针")):
            raise TicketError("不能认领：真源指针还没填。请先写清依据在哪个文件或哪一条决定里。")
        if not str(ticket.get("实机消费者", "")).strip():
            raise TicketError(f"不能认领：实机消费者还没填。请先写清 {LIVE_SCENE} 链上由谁加载。")
        self.require_active_staff(ticket["所属总监位"], actor)
        ticket["指派给"] = actor
        ticket["状态"] = "已认领"
        self.store.save_ticket(ticket, "claim", actor, "已认领")
        self._add_staff_history(actor, ticket["编号"])
        return ticket

    def attach(self, ticket_id: str, image_path: str, origin_key: str, actor: str = "") -> tuple[dict[str, Any], dict[str, str]]:
        ticket = self.store.load_ticket(ticket_id)
        if origin_key not in ORIGIN_MAP:
            raise TicketError("图片来源只可填 world、isolated 或 other。")
        record = self._compress_ticket_image(ticket, Path(image_path), ORIGIN_MAP[origin_key], actor or ticket.get("指派给") or "未署名")
        ticket.setdefault("图片列表", []).append(record)
        ticket.setdefault("接线证据", {"文字": "", "图片列表": []}).setdefault("图片列表", []).append(record)
        self.store.save_ticket(ticket, "attach", record["上传人"], f"附图 {record['文件名']}（{record['来源标注']}）")
        return ticket, record

    def attach_bytes(self, ticket_id: str, data: bytes, original_name: str, origin_key: str, actor: str = "") -> tuple[dict[str, Any], dict[str, str]]:
        ticket = self.store.load_ticket(ticket_id)
        if origin_key not in ORIGIN_MAP:
            raise TicketError("图片来源只可填 world、isolated 或 other。")
        uploader = actor.strip() or ticket.get("指派给") or "未署名"
        index = len(ticket.get("图片列表", [])) + 1
        stem = f"{ticket['编号']}-{index:02d}"
        filename = self._write_compressed_image(data, stem)
        record = image_record(filename, Path(original_name).name or "浏览器上传", ORIGIN_MAP[origin_key], uploader)
        ticket.setdefault("图片列表", []).append(record)
        ticket.setdefault("接线证据", {"文字": "", "图片列表": []}).setdefault("图片列表", []).append(record)
        self.store.save_ticket(ticket, "attach", uploader, f"附图 {filename}（{record['来源标注']}）")
        return ticket, record

    def upload_live_bytes(self, data: bytes, original_name: str, actor: str = "") -> dict[str, str]:
        """暂存远程 live 图片；等动作校验通过后才把同一文件引用进各张工单。"""
        digest = hashlib.sha256(data).hexdigest()[:8]
        stem = f"LIVE-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{digest}"
        if not data:
            raise TicketError("上传图片是空文件。")
        # T-000038 把压缩与原子落盘合一成 _write_compressed_image;T-000550 的暂存路径改走它,不再有第二份实现。
        filename = self._write_compressed_image(data, stem)
        return image_record(
            filename, Path(original_name).name or "命令行上传", ORIGIN_MAP["world"], actor.strip() or "未署名",
        )

    @staticmethod
    def parse_gate_report(text: str) -> dict[str, Any]:
        """解析六项机器闸报告（D9-460 ②）。

        收的是「每项一行」的原样输出,形如 `合并树构建: 过`。项名与结论之间
        用冒号(中英文都认)或空白分隔。**只认 GATE_REPORT_ITEMS 里那六项**,
        多写的行忽略、少写的项按「缺」算——缺项不是全绿。

        ★为什么解析得这么严:这份报告一旦判成全绿,这张单就**跳过人复验**直接可并。
          宽松解析在这里等于把闸拆了:一行写歪就当成过,比没有闸更坏,
          因为台面上会显示「机器闸绿」,谁都不会再去看。
        """
        found: dict[str, str] = {}
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            for item in GATE_REPORT_ITEMS:
                if not line.startswith(item):
                    continue
                tail = line[len(item):].strip().lstrip(":：-=").strip().lower()
                if any(word in tail for word in GATE_FAIL_WORDS):
                    found[item] = "不过"
                elif any(word in tail for word in GATE_PASS_WORDS):
                    found[item] = "过"
                else:
                    found[item] = "读不懂"
                break
        rows = [{"项": item, "结论": found.get(item, "缺")} for item in GATE_REPORT_ITEMS]
        return {
            "时间": now_text(),
            "报告": rows,
            "全绿": all(row["结论"] == "过" for row in rows),
        }

    def submit(
        self,
        ticket_id: str,
        evidence: str,
        verify_command: str = "",
        raw_output: str = "",
        deliverable_check: str = "",
        handoff: str = "",
        gate_report: str = "",
    ) -> dict[str, Any]:
        """交板。

        deliverable_check 是提交端在本机核完交付项后带上来的结论，形如 "6/6"。
        给了就说明文件系统类交付项已经在提交端那台机器上核过了，这里不再拿本机
        （远程模式下就是服务器）的文件系统去查——服务器上根本没有 D: 这个盘。
        图片类交付项照旧在这里核，图片本来就存在服务端。
        """
        ticket = self.store.load_ticket(ticket_id)
        self._require_state(ticket, "已认领", "只有已认领的派单能交板。")
        delivery_rows = normalize_lines(ticket.get("交付项"))
        if not delivery_rows:
            raise TicketError("不能交板：这张派单没有交付项，请由总监逐行补齐产物后再交。")
        checked = self._parse_deliverable_check(deliverable_check, delivery_rows)
        missing = self._missing_deliverables(ticket, delivery_rows, check_filesystem=not checked)
        if missing:
            raise TicketError(self.missing_deliverables_error(ticket, missing))
        # 验证命令与原样输出两类单都收：以前只在内部工具单那一支保存，用户可感知单的 else 支
        # 收下就扔——员工传了 --verify-command/--raw-output，submit 成功、状态到待判，show 出来却是空的，
        # 判卷人根本看不到执行方跑了什么、输出是什么。证据静默丢失比报错更坏，
        # 因为没有人会发现。（T-000315，在 T-000201 上撞到并报出行号）
        # 两支各自的硬闸不变：内部工具单仍必须两样都填；用户可感知单仍必须附实机真登录图。
        if verify_command.strip():
            ticket["接线证据"]["验证命令"] = verify_command.strip()
        if raw_output.strip():
            ticket["接线证据"]["原样输出"] = raw_output.strip()
        if ticket.get("非用户可感知"):
            if not verify_command.strip() or not raw_output.strip():
                raise TicketError("不能交板：内部工具单必须同时填写验证命令与原样输出。")
            ticket["接线证据"]["文字"] = evidence.strip() or "内部工具验证完成。"
        else:
            world_images = self._world_images(ticket)
            if not world_images:
                # T-000831:拦下不是终点,要给出路。活停在半路时,员工要知道找谁能一步解卡。
                raise TicketError(
                    f"不能交板：至少要附一张来源为 {LIVE_ORIGIN} 的接线证据图；隔离场景图不算。"
                    "如果这其实是内部工具单（任务书写的是改代码/改数据、没有用户能看见的产出），"
                    f"请该单所属总监位、总编排或设计者跑：ticket.py set {ticket['编号']} --internal --by <你的位名>，"
                    "然后你再原样重跑一次 submit。★员工不要自己署总监名去翻这个开关，那是冒名。"
                )
            if not evidence.strip():
                raise TicketError("不能交板：请用一句话说明接线证据。")
            ticket["接线证据"]["文字"] = evidence.strip()
        if checked:
            ticket["接线证据"]["文字"] = (
                ticket["接线证据"]["文字"] + f"\n交付项本机核验:{checked} 齐（在提交端本机核过，非服务端文件系统）"
            ).strip()
        # ★D9-460 ②:内部单可以带六项机器闸报告;六项全过 ⇒ 置「机器闸绿」并**视为复验过**,
        #   复检 merge 时只看闸输出、不必再读码。任一不过、或缺项,都不置标——
        #   那种情形照旧要复检席跑一次 verify,人这道眼睛不能因为报告写得漂亮就省掉。
        # ★只对内部单开这条路:用户可感知单的真登录复验是 D9-460 明写「不简化」的,
        #   机器闸看不见屏上那一眼。
        gate_note = ""
        if gate_report.strip():
            if not ticket.get("非用户可感知"):
                raise TicketError(
                    "--gate-report 只用于内部单(--internal)。"
                    "用户可感知单的真登录复验不简化(D9-460 边界),机器闸替代不了屏上那一眼。"
                )
            report = self.parse_gate_report(gate_report)
            ticket["机器闸"] = report
            bad = [row for row in report["报告"] if row["结论"] != "过"]
            if report["全绿"]:
                ticket["复验"] = {
                    "复验人": "机器闸",
                    "时间": report["时间"],
                    "结论": "过",
                    "闸输出": "；".join(f"{row['项']} 过" for row in report["报告"]),
                    "说明": "六项机器闸全绿,按 D9-460 ② 视为复验过;复检并线时只看闸输出。",
                }
                gate_note = "六项机器闸全绿 ⇒ 已自动记「复验过」,判过之后即可并线(D9-460 ②)。"
            else:
                gate_note = (
                    "机器闸**没有**全绿,不置标、不算复验过:"
                    + "、".join(f"{row['项']}{row['结论']}" for row in bad)
                    + "。修好后重交,或请复检席跑 verify 人工复验。"
                )
        ticket["状态"] = "待判"
        worker = ticket.get("指派给") or "未署名"
        # 「留给下一窗」只收底数与坑（T-001073 R3）：非固定工位的单也能填，只是没人导出。
        # 重交一次就整节重写——半截旧底数混着新底数，比没有底数更容易把下一窗带沟里。
        rows = normalize_handoff(handoff)
        if rows:
            ticket["留给下一窗"] = {
                "填写人": worker,
                "实际模型": str(ticket.get("实际模型", "")).strip(),
                "时间": now_text(),
                "行": rows,
            }
        self.store.save_ticket(
            ticket, "submit", worker, ticket["接线证据"]["文字"],
            {
                "实际模型": ticket.get("实际模型", ""),
                "记账模型": self._accounting_model_for_ticket(ticket)[0],
                "任务档": ticket.get("任务档") or "未标",
            },
        )
        # 员工交板 = 球传给总监。不写这一行,设计者的唤醒段就不会亮,
        # 那位总监永远不知道有单等他判(曾出现 3 张待判、0 个唤醒提示)。
        self._notify_slots(
            (ticket.get("所属总监位", ""),), worker,
            f"{worker} 交板了 {ticket['编号']} · {ticket.get('标题', '')},等你判卷", ticket["编号"],
        )
        # 重刷挂在**落库之后**，且只往返回值上挂一句话：这一格不进盘，
        # 下一次 load_ticket 读不到它，也就不会有人把一句提示当成单上的字段。
        notice = self.refresh_slot_memory(ticket)
        # D9-463 补 ③:真源位交板时提一句「生成物刷了没」。只提示、不拦——
        # 服务端看不见交板人那台机器的 git,判不了他刷没刷;硬闸在测试那一端。
        rebuild_note = REBUILD_HINT if ticket.get("所属总监位") in TRUTH_SOURCE_HINT_SLOTS else ""
        extra = {
            k: v for k, v in (
                ("记忆重刷提示", notice), ("机器闸提示", gate_note), ("重刷提示", rebuild_note),
            ) if v
        }
        return dict(ticket, **extra) if extra else ticket

    def judge(
        self, ticket_id: str, passed: bool, actor: str, reason: str = "", verdict: str = "", blame: str = "",
        strike_handoff: str = "",
    ) -> tuple[dict[str, Any], str]:
        ticket = self.store.load_ticket(ticket_id)
        self._require_state(ticket, "待判", "只有“待判”的工单能判卷。")
        self._refuse_execution_role(actor, "判卷")
        if actor == ticket.get("指派给"):
            raise TicketError("不能判卷：判卷人不能与执行员工是同一个人。")
        if not actor.strip():
            raise TicketError("判卷人不能为空。")
        # 划行号先验后改：越界要在动任何字段之前拒掉，不能判到一半留下半张改过的单。
        strike_indexes = self._parse_strike_handoff(ticket, strike_handoff)
        if not verdict.strip():
            raise TicketError("判语不能为空。请写清验收结论。")
        blame = blame.strip()
        if passed and blame:
            raise TicketError("判过不需要责任归属。")
        if not passed:
            if not blame:
                # 网页判退与旧客户端不带 --blame:判语首行(D9-388 ③ 硬要求)本身就写明了归属,照它推导。
                head = verdict.strip().splitlines()[0].strip()
                blame = "出题" if head.startswith("出题责任") else ("模型" if head.startswith("模型责任") else "")
            if not blame:
                raise TicketError(
                    "判退必须写清责任归属:--blame 模型(执行方做错) 或 --blame 出题(任务书/判据本身写错)。"
                    "出题责任不计入模型判退累计,但会记进该总监的出题账。"
                )
            if blame not in {"模型", "出题"}:
                raise TicketError("判退责任只可填“模型”或“出题”。")
            first_line = verdict.strip().splitlines()[0].strip()
            expected = f"{blame}责任"
            if not first_line.startswith(expected):
                written = next((prefix for prefix in ("模型责任", "出题责任") if first_line.startswith(prefix)), first_line)
                raise TicketError(
                    f"判退责任不一致:--blame 写的是“{blame}”,判语首行写的是“{written}”；"
                    f"判语首行必须以“{expected}”开头。"
                )
        # 这道闸要的是判卷人写一句**具体的到达路径**，不是要他写「用户」两个字。
        # 原来只对内部单放行「设计者怎么打开它」，于是取证/事实核查那一类单卡死：
        # 它们要真登录图（所以标不了 --internal），产出却是屏上那一眼、不是用户能打开的功能
        # （T-001499 ①，在 T-001470 上撞到）。用户可感知那条纪律不靠这句话守——
        # 靠 --consumer、交板的实机真登录图、独图与实机复验，那几道都比一句措辞硬。
        if passed and not any(phrase in verdict for phrase in ("用户怎么打开它", f"{OWNER_ROLE}怎么打开它")):
            raise TicketError(
                f"判过时判语必须写清怎么打开它:“用户怎么打开它”或“{OWNER_ROLE}怎么打开它”，两句写哪一句都放行。"
                "用户打不开的活(取证、报告、内部工具)写后一句。"
            )
        ticket["判卷人"] = actor.strip()
        ticket["判语"] = verdict.strip()
        struck = self._apply_strike_handoff(ticket, strike_indexes, actor)
        warning = self.strict_review_notice(ticket)
        if passed:
            ticket["状态"] = "待复检"
            detail = reason.strip() or "判卷通过"
            event = "judge-pass"
        else:
            if not reason.strip():
                raise TicketError("判退必须填写返工原因。")
            ticket["状态"] = "返工"
            ticket["判退责任"] = blame
            ticket["返工次数"] = int(ticket.get("返工次数", 0)) + 1
            ticket["已开窗"] = None
            ticket.setdefault("返工原因列表", []).append({
                "时间": now_text(), "判卷人": actor, "原因": reason.strip(), "判退责任": blame,
            })
            detail = f"{reason.strip()}；已开窗标记已清,等设计者重新开窗"
            event = "judge-rework"
            assigned = ticket.get("指派给", "")
            member = self.find_staff(assigned) if assigned else None
            if member and member[1]["状态"] == "已收窗":
                retired_warning = f"返工已指回原员工 {assigned}，但该员工已收窗；请总监改派在岗员工，或仅在修本人 BUG 时 reopen。"
                warning = "\n".join(filter(None, [warning, retired_warning]))
                ticket["备注"] = (ticket.get("备注", "") + "\n" + retired_warning).strip()
            ticket["返工原因列表"][-1]["责任"] = blame
            if blame == "模型":
                ban_warning = self._record_model_rework(ticket, actor)
            else:
                self._record_question_rework(ticket)
                ban_warning = "判语首行标「出题责任」:本次判退不计模型判退累计(D9-388 ③)。"
            warning = "\n".join(filter(None, [warning, ban_warning]))
        self.store.save_ticket(
            ticket, event, actor,
            f"{f'判退责任：{blame}；' if not passed else ''}{detail}；判语：{verdict.strip()}",
            {
                "实际模型": ticket.get("实际模型", ""),
                "记账模型": self._accounting_model_for_ticket(ticket)[0],
                "任务档": ticket.get("任务档") or "未标",
                **({"判退责任": blame} if not passed else {}),
            },
        )
        owner = str(ticket.get("所属总监位", ""))
        if passed:
            # 判过 = 球传给复检席。★D9-460 ① 之后两道并行,所以这句话要看复验做没做:
            # 复验已经过了就别再喊「等你复验」——那会让复检席白开一次单,
            # 而他真正要做的是并线。
            if self.is_verified(ticket):
                who = str((ticket.get("复验") or {}).get("复验人", "")) or "复验"
                self._notify_slots(
                    (REVIEW_SLOT,), actor,
                    f"{actor} 判过了 {ticket['编号']} · {ticket.get('标题', '')}；"
                    f"复验已由「{who}」记过 ⇒ 两道齐,可以并线了", ticket["编号"],
                )
            else:
                self._notify_slots(
                    (REVIEW_SLOT,), actor,
                    f"{actor} 判过了 {ticket['编号']} · {ticket.get('标题', '')},等你复验", ticket["编号"],
                )
        elif actor != owner:
            # 判退 = 球回到设计者手里重新传达(他自己队列的「要你传达的」已经覆盖,不重复打扰);
            # 但判卷人不是本位总监时(转给总编排判的那种),本位要知道自己的单被判退了。
            self._notify_slots(
                (owner,), actor,
                f"{actor} 判退了 {ticket['编号']} · {ticket.get('标题', '')},已回返工", ticket["编号"],
            )
        notice = self.refresh_slot_memory(ticket)
        warning = "\n".join(filter(None, [warning, struck, notice]))
        return ticket, warning

    @staticmethod
    def _parse_strike_handoff(ticket: dict[str, Any], value: str) -> list[int]:
        """把 --strike-handoff "2,3" 解析成 0 基下标；越界要说得出这一节共有几行。"""
        text = str(value or "").replace("，", ",").strip()
        if not text:
            return []
        rows = handoff_rows(ticket)
        if not rows:
            raise TicketError(
                f"不能划行：{ticket['编号']} 的「留给下一窗」是空的——"
                "交板时没填 --handoff，就没有行可划。"
            )
        indexes: list[int] = []
        for piece in text.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if not piece.isdigit():
                raise TicketError(f"划行号只能是正整数，用逗号分隔；看不懂的是「{piece}」。")
            number = int(piece)
            if not 1 <= number <= len(rows):
                raise TicketError(
                    f"划行号 {number} 越界：{ticket['编号']} 的「留给下一窗」这一节共有 {len(rows)} 行。"
                )
            indexes.append(number - 1)
        return indexes

    @staticmethod
    def _apply_strike_handoff(ticket: dict[str, Any], indexes: list[int], actor: str) -> str:
        """划掉**不是删掉**：原文留着，只多一个「谁在什么时候认为它错了」的标记。

        理由本身就是下一窗要知道的事——一句被划掉的底数，比一句凭空消失的底数
        信息量大得多：下一窗至少知道有人试过、有人不认。
        """
        if not indexes:
            return ""
        section = dict(ticket.get("留给下一窗") or {})
        rows = handoff_rows(ticket)
        stamp = now_text()
        for index in indexes:
            rows[index]["划掉判卷人"] = actor.strip()
            rows[index]["划掉时间"] = stamp
        section["行"] = rows
        ticket["留给下一窗"] = section
        numbers = "、".join(str(index + 1) for index in indexes)
        return f"已划掉「留给下一窗」第 {numbers} 行（原文保留，标了判卷人 {actor.strip()} 与时间）。"

    @staticmethod
    def _refuse_execution_role(actor: str, action: str) -> None:
        """第十三位不判卷、不 merge、不 live（D9-462 补）。

        它只做「记录裁定 → 广播」这一段。让它去判卷或并线,等于让传话的人给活打分——
        而它手上根本没有实现单,也不该有。
        ★用前缀匹配:员工编号是「设计·裁定分发-01」这种形态,只比位名会漏掉员工。
        """
        name = str(actor or "").strip()
        for slot in DISPATCH_FORBIDDEN_SLOTS:
            if name == slot or name.startswith(f"{slot}-"):
                raise TicketError(
                    f"「{slot}」不{action}(宪法 v2.38 / D9-462 补)。"
                    f"本位只做「逐字记录{OWNER_ROLE}裁定 → 送{CONDUCTOR_SLOT}落 D9 号 → 用需求/疑问单广播」这一段;"
                    f"{action}归干活那一位的总监与复检席。"
                )

    @staticmethod
    def is_verified(ticket: dict[str, Any]) -> bool:
        """这张单复验过了没有（D9-460 ①）。

        两条路都算数:复检席跑 `verify --result 过`,或内部单交板时六项机器闸全绿
        (D9-460 ②「复检只看闸输出」)。两者都往「复验」那一格写结论,这里只读结论。
        """
        return str((ticket.get("复验") or {}).get("结论", "")) == "过"

    def verify(
        self, ticket_id: str, actor: str, result: str, gates: str = "", evidence: str = "",
    ) -> tuple[dict[str, Any], str]:
        """复验（D9-460 ①:判卷与复验并行）。

        ★与 judge 的关系:两道**互不等待**。交板即可复验,不必先等总监判过;
          judge 与 verify 谁先到都行,两道齐了 merge 才放行。
          这一条就是「简化复检」的落点——
          以前复检席要干等总监判卷,一张单的两个人被串成一条线。

        ★受理态只有「待判」与「待复检」:
          「待判」= 员工交板了、总监还没判,这时复验先做完等着;
          「待复检」= 总监已判过,复验还没做。
          别的状态一律拒——已合并的单再复验没有意义,返工的单代码已经变了。
        """
        ticket = self.store.load_ticket(ticket_id)
        state = str(ticket.get("状态", ""))
        if state not in {"待判", "待复检"}:
            raise TicketError(
                f"只有“待判”“待复检”的工单能复验，这张是“{state}”。"
                "（判卷与复验并行:员工一交板就能复验,不必等总监判过；但并线仍要两道齐。）"
            )
        if not actor.strip():
            raise TicketError("复验人不能为空。")
        if actor.strip() == str(ticket.get("指派给", "")).strip():
            raise TicketError("不能复验：复验人不能与执行员工是同一个人。")
        outcome = str(result or "").strip()
        if outcome not in {"过", "退"}:
            raise TicketError("复验结论只可填“过”或“退”。")
        if outcome == "退" and not (gates.strip() or evidence.strip()):
            raise TicketError("复验判退必须写清哪一条不过:用 --gates 或 --evidence 说明。")
        ticket["复验"] = {
            "复验人": actor.strip(),
            "时间": now_text(),
            "结论": outcome,
            "闸输出": gates.strip(),
            "说明": evidence.strip(),
        }
        self.store.save_ticket(
            ticket, "verify", actor,
            f"复验{outcome}" + (f"；闸输出：{gates.strip()}" if gates.strip() else ""),
        )
        if outcome == "退":
            # 复验判退**不自己改状态**:退回照旧走 rework(待复检→返工)或退回单,
            # 那两条路各自带着「责任归属」「返工次数」「清已开窗戳记」一整套账,
            # 在这里另写一份必然与它们漂开。这里只留结论,并把球明确传回所属位。
            self._notify_slots(
                (str(ticket.get("所属总监位", "")),), actor,
                f"{actor} 复验退回了 {ticket['编号']} · {ticket.get('标题', '')}，"
                f"请走 rework 或退回单；不过的是:{gates.strip() or evidence.strip()}",
                ticket["编号"],
            )
            return ticket, "复验已记「退」。★状态没动:退回请走 rework(待复检)或另开退回单,那两条路才会记责任归属与返工次数。"
        if state == "待判":
            hint = "复验已记「过」。这张还没判卷——总监判过之后即可并线,不必再复验一次。"
        elif self.is_judged_for_merge(ticket):
            hint = "复验已记「过」，判卷也过了 ⇒ 两道齐,现在可以并线。"
        else:
            hint = "复验已记「过」。"
        return ticket, hint

    @staticmethod
    def is_judged_for_merge(ticket: dict[str, Any]) -> bool:
        """判卷这一道过了没有。判过的单会停在「待复检」,并写下判卷人。"""
        return str(ticket.get("状态", "")) == "待复检" and bool(str(ticket.get("判卷人", "")).strip())

    def merge(self, ticket_id: str, actor: str) -> dict[str, Any]:
        ticket = self.store.load_ticket(ticket_id)
        self._require_state(ticket, "待复检", "只有“待复检”的工单能合并。")
        self._refuse_execution_role(actor, "并线")
        if not actor.strip():
            raise TicketError("复检人不能为空。")
        # 三方互斥闸在别位是对的：那里执行方是别位员工、判卷人是别位总监、复检人是复检席，三个人真的不同。
        # 但本位自有单（复检席的部署单、平台位的工单台单）执行方是本位员工、判卷人与复检人都只能是本位总监，
        # 三者互不相同在这里是**数学上无解**：T-000849/T-000851 判过之后出不去，永远卡在待复检（T-000899 实测）。
        # 宪法 v2.20 ⑤ 已批「部署单免总编排代记」，所以这里放行这一种情形——
        # 但不伪装成第三方：复检人写成「自记·待设计者终验」，谁看都知道这一笔没有第三只眼。
        self_owned = actor.strip() == str(ticket.get("所属总监位", "")).strip() == str(ticket.get("判卷人", "")).strip()
        # ★D9-460 ①:并线前置由「判过」改成「判过 ∧ 复验过」。
        # 判过只说明总监认了活,复验是另一双眼睛看闸输出;两道谁先到都行,但缺一不能并。
        # 内部单六项机器闸全绿会自动把「复验」那一格置成过(D9-460 ②),那条路不用人再跑一次 verify。
        # ★self_owned 例外,与三方互斥闸是**同一个道理**:本位自有单的复验人也只能是本位总监,
        #   要求「另一双眼睛」在这里同样数学上无解,加了这道闸等于把平台位与复检席自己的单
        #   全部永久卡死在待复检。D9-460 边界原文就写着「工单台自有单免复检」。
        #   ——这一条不是为了让用例过而开的口子:不开它,本位这一窗自己的两张单都并不出去。
        if not self_owned and not self.is_verified(ticket):
            raise TicketError(
                "不能合并：这张单判过了,但还没复验。并线要「判过 ∧ 复验过」两道齐。"
                f"请复检席跑:ticket.py verify {ticket['编号']} --by <你的位名> --result 过 --gates \"<闸输出摘要>\"；"
                "内部单也可以在交板时带 --gate-report,各项全绿会自动记复验过。"
            )
        if actor in {ticket.get("指派给"), ticket.get("判卷人")} and not self_owned:
            raise TicketError(
                "不能合并：复检人必须与执行员工、判卷人都不同。"
                "（本位自有单例外：所属位、判卷人、复检人三者都是同一个位名时可自记，"
                f"落库会标「自记·待{OWNER_ROLE}终验」——宪法 v2.20 ⑤）"
            )
        ticket["复检人"] = f"{actor.strip()}（自记·待设计者终验）" if self_owned else actor.strip()
        ticket["状态"] = "已合并"
        self.store.save_ticket(ticket, "merge", actor, f"自记并线·待{OWNER_ROLE}终验" if self_owned else "已合并")
        # 并线 = 球传给「上服的那一方」：工单台自己的单归平台位，其余归复检席（D9-337）。
        # 所属位与复检席都通知，_notify_slots 内部去重。
        self._notify_slots(
            (str(ticket.get("所属总监位", "")), REVIEW_SLOT), actor,
            f"{actor} 并线了 {ticket['编号']} · {ticket.get('标题', '')},等上服", ticket["编号"],
        )
        # 内部单并线即终态（T-001053），执行方的手就此空出来；用户可感知单还欠一张真登录图，
        # is_done_for_staff 在那里回假，这一句自然什么都不做。
        return self._with_retire_notice(ticket)

    def live(self, ticket_id: str, image_path: str, actor: str, shot: str = "") -> dict[str, Any]:
        self._refuse_execution_role(actor, "做实机复验")
        ticket, supplement = self._ticket_for_live(ticket_id, shot)
        if ticket.get("非用户可感知"):
            ticket["实机图标记"] = shot
            ticket["状态"] = "实机复验过"
            self.store.save_ticket(ticket, "live", actor, f"{shot}·内部单免图")
            return self._with_retire_notice(ticket)
        if not image_path:
            raise TicketError(f"不能通过实机复验：请提供一张新的 {LIVE_ORIGIN} 图。")
        before = len(self._world_images(ticket))
        ticket, _ = self.attach(ticket_id, image_path, "world", actor)
        if len(self._world_images(ticket)) <= before:
            raise TicketError(f"不能通过实机复验：请再附一张 {LIVE_ORIGIN} 图。")
        ticket["实机图标记"] = "独图" if shot == "独图" else "待独图"
        ticket["状态"] = "实机复验过"
        detail = "补独图" if supplement else f"实机复验通过 · {shot}"
        self.store.save_ticket(ticket, "live", actor, detail)
        return self._with_retire_notice(ticket)

    def live_uploaded(self, ticket_id: str, filename: str, actor: str, shot: str = "") -> dict[str, Any]:
        ticket, supplement = self._ticket_for_live(ticket_id, shot)
        if ticket.get("非用户可感知"):
            ticket["实机图标记"] = shot
            ticket["状态"] = "实机复验过"
            self.store.save_ticket(ticket, "live", actor, f"{shot}·内部单免图")
            return self._with_retire_notice(ticket)
        matches = [row for row in self._world_images(ticket) if row.get("文件名") == filename]
        if not matches:
            self._link_uploaded_world_image(ticket, filename, actor)
        ticket["实机图标记"] = "独图" if shot == "独图" else "待独图"
        ticket["状态"] = "实机复验过"
        detail = f"补独图 · {filename}" if supplement else f"实机复验通过 · {shot} · {filename}"
        self.store.save_ticket(ticket, "live", actor, detail)
        return self._with_retire_notice(ticket)

    def live_batch(
        self, ticket_ids: list[str], image_path: str, actor: str, shot: str,
    ) -> list[dict[str, Any]]:
        return self._live_batch_rows(
            ticket_ids, lambda ticket_id: self.live(ticket_id, image_path, actor, shot)
        )

    def live_batch_uploaded(
        self, ticket_ids: list[str], filename: str, actor: str, shot: str,
    ) -> list[dict[str, Any]]:
        return self._live_batch_rows(
            ticket_ids, lambda ticket_id: self.live_uploaded(ticket_id, filename, actor, shot)
        )

    @staticmethod
    def _live_batch_rows(ticket_ids: list[str], apply: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_ticket_id in ticket_ids:
            ticket_id = str(raw_ticket_id).upper()
            if ticket_id in seen:
                rows.append({"编号": ticket_id, "结果": "跳过", "原因": "批内单号重复"})
                continue
            seen.add(ticket_id)
            try:
                ticket = apply(ticket_id)
                rows.append({
                    "编号": ticket["编号"], "结果": "已复验",
                    "实机图标记": ticket.get("实机图标记", ""),
                    # 批量复验一次能收好几个窗，回执要逐张带上，别只在单张 live 里说（T-001081）。
                    "自动退役提示": str(ticket.get("自动退役提示", "")),
                })
            except TicketError as exc:
                rows.append({"编号": ticket_id, "结果": "跳过", "原因": str(exc)})
        return rows

    def _link_uploaded_world_image(self, ticket: dict[str, Any], filename: str, actor: str) -> None:
        safe_name = Path(filename).name
        if safe_name != filename or not (self.store.images_dir / safe_name).is_file():
            raise TicketError(f"不能批量实机复验：共用的 {LIVE_ORIGIN} 图不存在。")
        record = image_record(safe_name, safe_name, ORIGIN_MAP["world"], actor or "未署名")
        ticket.setdefault("图片列表", []).append(record)
        ticket.setdefault("接线证据", {"文字": "", "图片列表": []}).setdefault("图片列表", []).append(record)
        self.store.save_ticket(ticket, "attach", record["上传人"], f"共用实机图 {safe_name}（{record['来源标注']}）")

    def _ticket_for_live(self, ticket_id: str, shot: str) -> tuple[dict[str, Any], bool]:
        if shot not in SHOT_VALUES:
            raise TicketError(SHOT_REQUIRED_MESSAGE)
        ticket = self.store.load_ticket(ticket_id)
        if ticket.get("状态") == "已合并":
            return ticket, False
        if ticket.get("状态") == "实机复验过":
            if shot != "独图":
                raise TicketError("已经实机复验过的工单只能用 --shot 独图补图。")
            if ticket.get("实机图标记", "") != "待独图":
                raise TicketError("只有实机图标记为“待独图”的工单能补独图。")
            return ticket, True
        raise TicketError(f"只有“已合并”的工单能做实机复验。 当前状态是“{ticket['状态']}”。")

    @staticmethod
    def _signer_slot(actor: str) -> str:
        """把「复检·合并与部署-12」这种员工署名收敛回总监位；不带两位编号的原样返回。"""
        match = STAFF_PATTERN.fullmatch(actor.strip())
        return match.group("slot") if match else actor.strip()

    def shot_exempt(self, ticket_id: str, actor: str, reason: str) -> dict[str, Any]:
        """给「实机复验过·待独图」的单打免独图（D9-402 ②）。不碰 attach、不要求 world 图，但必须留下原因。"""
        signer = actor.strip()
        ticket = self.store.load_ticket(ticket_id)
        if self._signer_slot(signer) not in {REVIEW_SLOT, CONDUCTOR_SLOT}:
            raise TicketError(
                f"不能给 {ticket['编号']} 打{SHOT_EXEMPT}：--by 写的是「{signer or '空'}」。"
                f"{SHOT_EXEMPT}只有「{REVIEW_SLOT}」或「{CONDUCTOR_SLOT}」能打；"
                f"本位总监与员工窗都不行，请找复检席或{CONDUCTOR_SLOT}代办。"
            )
        if ticket.get("状态") != "实机复验过":
            raise TicketError(
                f"不能给 {ticket['编号']} 打{SHOT_EXEMPT}：现在是「{ticket['状态']}」。"
                "豁免只对已经实机复验过、只欠一张独图的单成立；没复验过的单请照常 live。"
            )
        if ticket.get("实机图标记", "") != "待独图":
            raise TicketError(
                f"不能给 {ticket['编号']} 打{SHOT_EXEMPT}：实机图标记现在是「{ticket.get('实机图标记', '') or '空'}」。"
                "只有「待独图」的单欠着一张独图，才谈得上豁免。"
            )
        text = reason.strip()
        if not text:
            raise TicketError(
                f"不能给 {ticket['编号']} 打{SHOT_EXEMPT}：--reason 不能为空，"
                "要写清这张单为什么不用补独图（诊断类单无用户可见产出，或验证对象已退役）。"
            )
        ticket["实机图标记"] = SHOT_EXEMPT
        ticket[SHOT_EXEMPT_REASON] = text
        self.store.save_ticket(ticket, "live", signer, f"{SHOT_EXEMPT} · {text}")
        return ticket

    def _is_own_deliverable_reshape(
        self, ticket: dict[str, Any], actor: str, requested: list[tuple[str, Any]],
    ) -> bool:
        """这一次 set 是不是「员工改自己单交付项的**写法**」（T-001256 ②）。

        四条全中才算:只改了交付项这一项、署名就是这张单的指派人、
        条数一样、而且**逐条折成归一键之后集合完全相同**。
        归一键 = 去扩展名的文件名 + 路径段——所以 `.jpg`→`.webp`、
        `/repo-worktrees/wt-abc/tools/x.py`→`tools/x.py`、`a/b/`→`a/b` 都算同一条,
        而换成另一个文件、多一条少一条,键就对不上,当场落回原来的署名闸。
        """
        if len(requested) != 1 or requested[0][0] != "交付项":
            return False
        if not actor.strip() or actor.strip() != str(ticket.get("指派给", "")).strip():
            return False
        before = normalize_lines(ticket.get("交付项"))
        after = normalize_lines(requested[0][1])
        if len(before) != len(after):
            return False
        remaining = list(after)
        for row in before:
            match = next((other for other in remaining if self._same_deliverable(row, other)), None)
            if match is None:
                return False
            remaining.remove(match)
        return not remaining

    @staticmethod
    def _same_deliverable(left: str, right: str) -> bool:
        """两条交付项指的是不是同一份产物——只是写法不同。

        ★两条都要满足才算同一份:
        ① 去扩展名的文件名相同(`.jpg` ↔ `.webp`,T-000979);
        ② 路径段**互为后缀**(`/repo-worktrees/wt-abc/tools/x.py` ↔ `tools/x.py`)。
        只比 ① 是不够的:那样员工可以把 `ticket_desk/service.py` 悄悄换成
        `别处/service.py`——同名不同物,闸就成了摆设。② 把这条路堵死:
        换目录会让两串路径段互不为后缀,当场落回原来的署名闸。
        """
        left_key, right_key = deliverable_key(left), deliverable_key(right)
        if not left_key or left_key != right_key:
            return False
        # 段比对同样去掉扩展名,免得最后一段因为 .jpg/.webp 之差判成不同
        def stems(candidate: str) -> tuple[str, ...]:
            parts = path_segments(candidate)
            return tuple(part.lower() for part in parts[:-1]) + (deliverable_key(candidate),) if parts else ()

        left_parts, right_parts = stems(left), stems(right)
        shorter, longer = sorted((left_parts, right_parts), key=len)
        return longer[len(longer) - len(shorter):] == shorter

    def _audit_worker_reshape(
        self, ticket: dict[str, Any], actor: str, requested: list[tuple[str, Any]],
    ) -> None:
        """员工自改路径形式要留痕:判卷人得看得见这张单的交付项被谁动过。"""
        self.store.append_jsonl(self.store.log_path, {
            "时间": now_text(), "工单号": str(ticket["编号"]), "事件": "worker-reshape-deliverable",
            "发言人": actor.strip(), "状态": str(ticket.get("状态", "")),
            "说明": (
                "员工自改路径形式(语义未变,逐条归一键相同):"
                f"{'；'.join(normalize_lines(ticket.get('交付项')))} → {'；'.join(normalize_lines(requested[0][1]))}"
            ),
            "事件序号": 0,
        })

    def rework_from_review(
        self, ticket_id: str, reason: str, actor: str, blame: str = "出题",
    ) -> dict[str, Any]:
        """把「待复检」的单退回原位重做（T-001218，复检席报）。

        ★缺的是一条状态边,不是一个开关:judge 只吃「待判」,于是**判过之后**才发现要重做的单
        谁都翻不动——曾有一批画在拍板环节被否,而当时「待复检态只有复检席能翻」,
        复检席手上也没有这个动作,最后只能 close --not-merged 一刀切成终态,单号作废、另开新单。
        这与 close --not-merged 是同一个缺口的两半:那边是「这活到此为止」(终态),
        这边是「这活还要接着做、单号要留住」(回返工)。

        为什么不去放开 judge 的受理态:judge 是**判卷**那个动作,它只该吃待判;
        把待复检塞进去,「谁在什么时候判的」这条线就糊了。另起一条边,事件单独记。

        ★blame 默认「出题」不是「模型」。触发这条边的典型情形正是复检席报的那种:
        任务书写的口径被否了、执行方照做没错——默认记模型账等于凭空给执行方
        记一次判退,模型合格率就脏了。要记模型账必须显式写 --blame 模型。
        """
        ticket = self.store.load_ticket(ticket_id)
        self._require_dispatch(ticket)
        self._require_state(ticket, "待复检", "只有“待复检”的单能退回重做；待判的请照常用 judge --rework。")
        if blame not in {"模型", "出题"}:
            raise TicketError("判退责任只可填“模型”或“出题”。")
        if not str(reason).strip():
            raise TicketError(
                f"不能退回 {ticket['编号']}：--reason 必填,要写清为什么判过了还要退回重做"
                f"(例如:{OWNER_ROLE}当面否了这一批的画;口径写错了要重出)。"
                "没有原因的退回,事后与「判错了重判」分不清。"
            )
        owner = str(ticket.get("所属总监位", ""))
        allowed = {owner, REVIEW_SLOT, CONDUCTOR_SLOT, OWNER_ROLE}
        if actor.strip() not in allowed:
            raise TicketError(
                f"不能退回 {ticket['编号']}：--by 写的是「{actor.strip() or '空'}」。"
                f"只有该单所属位「{owner}」、{REVIEW_SLOT}、{CONDUCTOR_SLOT} 或设计者可以。"
                "★四个都放行是故意的:今天这一张正是「本位翻不动、复检席也翻不动」卡住的。"
            )
        ticket["状态"] = "返工"
        ticket["返工次数"] = int(ticket.get("返工次数", 0)) + 1
        ticket["判退责任"] = blame
        # 与 judge --rework、unblock 同一条口径:戳记清掉,单子当场回到设计者的「要你传达的」,
        # 不必等它熬过返工那 8 小时线才从折叠着的「卡住了」里冒出来(T-001122)。
        ticket["已开窗"] = None
        ticket.setdefault("返工原因列表", []).append({
            "时间": now_text(), "判卷人": actor, "原因": str(reason).strip(),
            "判退责任": blame, "责任": blame, "来自": "待复检退回",
        })
        ticket["流程提示"] = (
            "判过之后被退回重做:单号、任务书与断点都留着,返工次数已累计。"
            "要换任务书就现在换(set --taskbook),员工 claim 一次即回「已认领」。"
        )
        if blame == "模型":
            ban_warning = self._record_model_rework(ticket, actor)
        else:
            self._record_question_rework(ticket)
            ban_warning = "判退责任记「出题」:本次退回不计模型判退累计(D9-388 ③)。"
        self.store.save_ticket(
            ticket, "rework-from-review", actor,
            f"待复检退回重做(责任:{blame});已开窗标记已清,单子回到「要你传达的」;原因:{str(reason).strip()}",
            {
                "实际模型": ticket.get("实际模型", ""),
                "记账模型": self._accounting_model_for_ticket(ticket)[0],
                "任务档": ticket.get("任务档") or "未标",
                "判退责任": blame,
            },
        )
        # 球回到本位手里(要重新传达开窗指令);复检席也要知道自己手上那张不用再等了。
        self._notify_slots(
            (owner, REVIEW_SLOT), actor,
            f"{actor} 把 {ticket['编号']} 从待复检退回重做 · {ticket.get('标题', '')}:{str(reason).strip()}",
            ticket["编号"],
        )
        return dict(ticket, 提示=ban_warning)

    def close(
        self, ticket_id: str, actor: str, not_merged: bool = False, reason: str = "", verdict: str = "",
        not_deployed: bool = False,
    ) -> dict[str, Any]:
        ticket = self.store.load_ticket(ticket_id)
        if not_deployed:
            # T-001509 / T-001124:「已合并」的单,上服失败已回滚或原命题不再成立,
            # 原来四条路全堵——close 要实机复验过、免独图只认已复验欠独图、
            # live 要新的真登录图、rework 只认待复检。宪法五章 5 本就允许
            # 「验证对象已退役或原命题不再成立」免图,工具却把它限在已复验之后。
            # 这条边只做一件事:把「这活并过,但没上服、也不用再上服」如实记账结案。
            if not_merged:
                raise TicketError("不能同时给 --not-merged 与 --not-deployed:一个判过不并线,一个并过未上服,二选一。")
            if ticket["状态"] != "已合并":
                raise TicketError(
                    f"不能未上服结案 {ticket['编号']}：现在是「{ticket['状态']}」。"
                    "这条路只给并过线、但上服失败已回滚或原命题不再成立的单;"
                    "没并过的请走 close --not-merged,判过要重做的请走 rework。"
                )
            # ★已上服的(提交号在当前部署头里)不适用:它欠的只是一笔 live 记账,
            #   结成「未上服」会把线上正跑着的活说成没上(D9-461 那条判据的反向保护)。
            if self.awaiting_live_record(ticket):
                raise TicketError(
                    f"不能未上服结案 {ticket['编号']}：它的提交号已在当前值面部署头里,代码在线上跑着,"
                    "欠的只是一笔 live 记账。请照常 live 补记账,别结成「未上服」。"
                )
            owner = str(ticket.get("所属总监位", ""))
            if actor.strip() not in {owner, CONDUCTOR_SLOT, OWNER_ROLE}:
                raise TicketError(
                    f"不能未上服结案 {ticket['编号']}：--by 写的是「{actor.strip() or '空'}」。"
                    f"只有该单所属位「{owner}」、总编排或设计者能这么结——"
                    "「确认它真的没上服」这件事只有这三个角色说得清。"
                )
            text = str(reason).strip()
            if not text:
                raise TicketError(
                    f"不能未上服结案 {ticket['编号']}：--reason 必填,要写清为什么并过线却不按上服收尾"
                    "(例如:部署单#15 失败已回滚,内容随 #16 上服;原命题被 T-xxxxxx 取代)。"
                    "没有原因的「未上服」,事后和「忘了补 live」分不清。"
                )
            ticket["状态"] = "关闭"
            ticket["未上服结案"] = {"时间": now_text(), "结案人": actor.strip(), "原因": text}
            ticket["备注"] = (str(ticket.get("备注", "")) + f"\n未上服结案:{text}").strip()
            self.store.save_ticket(ticket, "close-not-deployed", actor, f"未上服结案:{text}")
            # 总编排点名他要来收 T-001124;所属位也该知道自家单子这么结了。
            self._notify_slots(
                (owner, CONDUCTOR_SLOT), actor,
                f"{actor} 把 {ticket['编号']} 按「已合并·未上服·已结案」收口:{text}", ticket["编号"],
            )
            return self._with_retire_notice(ticket)
        if not_merged:
            # 状态机原来只有一条出路：并 → 上服 → 实机复验过。可是「判过了，但正确的处置就是不并线」
            # 是常态而不是特例：画风被设计者判废(T-000652/D9-407②)、需求撤回或被后来的单取代、
            # 探路单的产物是结论不是代码、两支互斥只留一支。这些单原来全烂在「待复检」里，
            # 每位开窗扫台面都要重新判断一次要不要并，日后还会被倒推成「这单没做好」冤枉执行方(T-000902)。
            # ★原因必填是这条路的关键：没有原因的「不并线」和「忘了并」事后分不清。
            if ticket["类型"] != "派单":
                raise TicketError("不能不并线结案：这条路只给派单，其他类型照原来的关闭流程走。")
            if ticket["状态"] not in {"待复检", "待判"}:
                raise TicketError(
                    f"不能不并线结案 {ticket['编号']}：现在是「{ticket['状态']}」。"
                    "这条路只给判过之后、确定不并线的单（待复检 / 待判）。"
                )
            owner = str(ticket.get("所属总监位", ""))
            if actor.strip() not in {owner, REVIEW_SLOT, CONDUCTOR_SLOT, OWNER_ROLE}:
                raise TicketError(
                    f"不能不并线结案 {ticket['编号']}：--by 写的是「{actor.strip() or '空'}」。"
                    f"只有该单所属位「{owner}」、复检·合并与部署、总编排或设计者能这么结。"
                )
            if not reason.strip():
                raise TicketError(
                    "不能不并线结案：--reason 必填，要写清为什么这张单判过了却不并线"
                    f"（例如：画风作废，{OWNER_ROLE} D9-407②；需求被 T-xxxxxx 取代；探路单产物是结论）。"
                    "没有原因的不并线，事后和「忘了并」分不清。"
                )
            # 「待复检」是判过之后的态，判语已经在单上；「待判」还没判过。
            # 这条路原来对两态一视同仁，于是从待判直接关掉的单落成终态却**判卷人与判语都是空**——
            # 事后没人说得出这活到底行不行、是谁看过的（T-001499 ②，在 T-001470 上撞到）。
            # ★这里不套 judge 那道「怎么打开它」的措辞闸：不并线的单本来就没有「打开它」这回事。
            unjudged = ticket["状态"] == "待判"
            verdict = verdict.strip()
            if unjudged and not verdict:
                raise TicketError(
                    f"不能不并线结案 {ticket['编号']}：现在是「待判」，还没判过，这么关会留下一张没有判语的终态单。"
                    "两条路二选一：先 judge 判过再 close --not-merged；"
                    "或者就在本条命令上带 --verdict \"<判语>\" 一并落。"
                )
            if verdict and not unjudged:
                raise TicketError(
                    f"不能不并线结案 {ticket['编号']}：现在是「{ticket['状态']}」，判语已经在单上了，"
                    "--verdict 只给「待判」态一并补判用；要改判语请另走判卷路径。"
                )
            if unjudged:
                ticket["判卷人"] = actor.strip()
                ticket["判语"] = verdict
            ticket["状态"] = "关闭"
            ticket["备注"] = (str(ticket.get("备注", "")) + f"\n不并线结案：{reason.strip()}").strip()
            self.store.save_ticket(ticket, "close-not-merged", actor, f"不并线结案：{reason.strip()}")
            self._notify_slots(
                (str(ticket.get("所属总监位", "")), REVIEW_SLOT), actor,
                f"{actor} 把 {ticket['编号']} 判过但不并线结案：{reason.strip()}", ticket["编号"],
            )
            return self._with_retire_notice(ticket)
        required = "实机复验过" if ticket["类型"] == "派单" else "已答"
        self._require_state(ticket, required, f"{ticket['类型']}工单只有处于“{required}”才能关闭。")
        if ticket["类型"] == "需求":
            # 答复权交到所属位手上了(R1),关闭权还留在总编排那儿的话,单子照样落不了地。
            # 只给需求加人判：派单的关闭路径与其他类型的现有行为一个字没动。
            owner = str(ticket.get("所属总监位", ""))
            if actor.strip() not in {owner, OWNER_ROLE, CONDUCTOR_SLOT}:
                raise TicketError(
                    f"不能关闭 {ticket['编号']}：--by 写的是「{actor.strip() or '空'}」。"
                    f"这张需求单挂在「{owner}」位，只有该位总监「{owner}」、设计者或总编排能关闭。"
                )
        ticket["状态"] = "关闭"
        self.store.save_ticket(ticket, "close", actor, "关闭")
        return self._with_retire_notice(ticket)

    def block(
        self, ticket_id: str, reason: str, actor: str = CONDUCTOR_SLOT, kind: str = BLOCK_BUSINESS,
    ) -> dict[str, Any]:
        ticket = self.store.load_ticket(ticket_id)
        self._require_dispatch(ticket)
        if kind not in BLOCK_KINDS:
            raise TicketError(f"阻塞类型只可填“{BLOCK_BUSINESS}”或“{BLOCK_NON_BUSINESS}”。")
        if not reason.strip():
            raise TicketError("阻塞原因不能为空。")
        if kind == BLOCK_NON_BUSINESS:
            # ★T-001256 ④(D9-453 / v2.31 八章 6):非业务阻塞**不停车**。
            # 「活没做好」才该停:真源缺、接口对不上、判据不达、测试红。
            # 账面类的(交付项写法、路径前缀、待回核、远端滞后、CLI 落后)记一行继续做——
            # 近两日这类事停掉了一窗又一窗,而员工继续跑根本不影响。
            # 所以这里**一个字都不改状态**:单子还在原来那一档,员工窗接着做,不等任何人答复。
            return self._record_non_business_block(ticket, reason.strip(), actor)
        if ticket["状态"] in {"阻塞", "关闭"}:
            raise TicketError(f"{ticket['编号']} 现在是“{ticket['状态']}”，不能重复阻塞。")
        ticket["阻塞类型"] = BLOCK_BUSINESS
        ticket["阻塞前状态"] = ticket["状态"]
        ticket["阻塞原因"] = reason.strip()
        ticket["状态"] = "阻塞"
        ticket["流程提示"] = "先把不依赖它的部分做完并交板,再收窗"
        self.store.save_ticket(ticket, "block", actor, reason.strip())
        # 阻塞 = 球传给总编排（解阻塞归他），本位也要知道自己的单被挂起了。
        self._notify_slots(
            (CONDUCTOR_SLOT, str(ticket.get("所属总监位", ""))), actor,
            f"{actor} 挂起了 {ticket['编号']} · {ticket.get('标题', '')}，原因：{reason.strip()}", ticket["编号"],
        )
        return ticket

    def _record_non_business_block(
        self, ticket: dict[str, Any], reason: str, actor: str,
    ) -> dict[str, Any]:
        """非业务阻塞:记一行,状态一个字不动,员工接着做(T-001256 ④)。

        它不是「阻塞态」,所以不进老化告警、不占设计者队列、不用等谁答复——
        这正是「员工不等」的意思。代价是没人主动看得见它,
        所以 list --nonbiz 与日览那两个数是唯一的出口,做成默认显眼、不藏在折叠段里。
        """
        rows = list(ticket.get("非业务阻塞") or [])
        rows.append({"时间": now_text(), "报告人": actor.strip(), "原因": reason, "已清": False})
        ticket["非业务阻塞"] = rows
        self.store.save_ticket(
            ticket, "block-nonbiz", actor,
            f"非业务阻塞(带记录继续,状态不变:{ticket.get('状态', '')}):{reason}",
        )
        # 只知会本位总监与平台位:非业务的清账归平台看板,不打扰总编排、也不惊动设计者队列。
        self._notify_slots(
            (str(ticket.get("所属总监位", "")), PLATFORM_SLOT), actor,
            f"{actor} 在 {ticket['编号']} 报了一条非业务阻塞(已继续做):{reason}", ticket["编号"],
        )
        return dict(
            ticket,
            流程提示=(
                "非业务阻塞已记下,**这张单状态没变、你接着做**(v2.31 八章 6 非业务闸不停车)。"
                f"这一条会进平台位的非业务看板(ticket.py list --nonbiz)集中清,你不用等答复。"
            ),
        )

    # 页面要从对话线上算的东西,只有这三类:未读条数(按不同人算)、最新一条未读的摘要、总行数。
    # 全都能在服务端现算——现算就没有「本地那份旧了」的问题,所以这一条**不进缓存**。
    THREAD_SUMMARY_ACTORS = (OWNER_ROLE, CONDUCTOR_SLOT)
    THREAD_PREVIEW_CHARS = 40

    def thread_summaries(self) -> dict[str, Any]:
        """每位对话线的未读摘要（T-001313）。

        原来网页每次刷新把 13 条线的**全文**都拉下来(压后约 1.01 MB / 近 5 秒),
        可它真正要用的只有几个数:各位的未读条数、最新一条未读长什么样。
        现在这些在服务端算好再发,一次请求几 KB;只有**当前正在看的那一位**才拉全文。
        ★故意不缓存:每次现算,所以「标已读之后计数没跟着变」这种旧数据问题根本不存在——
        对话线的已读标记是会被回头改的(inbox --mark-read),按行数切片的缓存会看不见那种改动。
        """
        result: dict[str, Any] = {}
        for slot in SLOTS:
            rows = self.store.read_jsonl(self.store.thread_path(slot))
            counts = {
                actor: sum(
                    1 for row in rows
                    if str(row.get("发言人", "")) != actor and actor not in (row.get("已读标记") or [])
                )
                for actor in (*self.THREAD_SUMMARY_ACTORS, slot)
            }
            unread_for_owner = [
                row for row in rows
                if str(row.get("发言人", "")) != slot and slot not in (row.get("已读标记") or [])
            ]
            latest = unread_for_owner[-1] if unread_for_owner else None
            result[slot] = {
                "总行数": len(rows),
                "未读": counts,
                "最新未读": None if latest is None else {
                    "时间": str(latest.get("时间", "")),
                    "发言人": str(latest.get("发言人", "")),
                    "摘要": str(latest.get("文字", ""))[: self.THREAD_PREVIEW_CHARS],
                },
            }
        return result

    def changes_since(self, cursor: int) -> dict[str, Any]:
        """「第 cursor 行流水之后有什么动静」——网页增量刷新的唯一入口（T-001308）。

        为什么用流水行号当书签、而不是「最后更新时间」:
        每写一次单必写一行流水,两件事在 save_ticket 的同一把锁里完成,行号严格递增。
        时间戳会撞「同一秒两笔写入」的边界,行号不会——**一笔改动躲不过去**。

        为什么还要回「总数」:单虽然基本不删,但 demo archive 那条维护命令真的删过
        (T-000001~008 就只剩流水、表里没有了)。客户端拿总数一对,对不上就整份重取,
        这样「本地留着一张服务器已经没有的鬼单」也活不过一次刷新。

        ★回的是真实变更的**超集**:staff-auto-retire 只改名册却记了一行引用该单的流水,
        于是那张单会被多传一次。宁可多传,绝不漏传——这是这类同步唯一可接受的偏向。
        """
        try:
            start = int(cursor)
        except (TypeError, ValueError):
            start = 0
        # 「整份重取」由 store 在同一趟里顺手判掉:维护类流水行不指向具体某张单,
        # 但它确实动过盘——不给这个信号,那一批改动对所有缓存着的页面就是隐形的。
        ids, latest, full_reload = self.store.tickets_changed_since(start)
        tickets: list[dict[str, Any]] = []
        for ticket_id in ids:
            try:
                # ★★ 必须过 card_view:整份那条路(/api/tickets → list_cards)每一行都过它。
                # 「开窗指令」是 ticket_view 生成的**派生字段**,「未发送字段」是 card_view
                # 生成的。这里少过任何一道,凡是走增量来的单在页面上就跟整份来的长得不一样——
                # 曾撞到一次:少过 ticket_view,于是走增量来的单没有开窗指令,
                # 而新建的单必然走增量,「要你传达的」卡片那一栏整个是空的(T-001314)。
                # ⇒ 增量与整份必须回**完全一样形状**的行,差一个键都不行;
                #    两条路共用 card_view 这一个函数,就是为了让它们没法各走各的。
                tickets.append(self.card_view(self.store.load_ticket(ticket_id)))
            except TicketError:
                # 流水里有、表里已经没有的(演示数据被归档掉的那八张):跳过,
                # 让下面的「总数」去兜——客户端一对数就会整份重取。
                continue
        return {
            "游标": latest,
            "整份重取": bool(full_reload),
            "总数": len(self.store.list_tickets()),
            "工单": tickets,
        }

    def deploy_record(
        self,
        head: str,
        probes: str,
        tickets: list[str] | tuple[str, ...] | str | None = None,
        repo: str = "",
        actor: str = "部署脚本",
    ) -> dict[str, Any]:
        """上服记录:部署脚本在后置闸通过之后自动建的一张单,免员工窗、免判。

        ★为什么免判:它记的是**已经发生的事实**(线上现在跑的是哪个头、探针什么结果),
          不是要谁去做的活。给它开一扇甲档窗、再走一遍判卷复检,是拿流程空转。
        ★取证(独图/试玩)**另开一张取证单**,取不到图不挡上服:
          上服成没成看探针,不看有没有人截到图。两件事绑在一起,过去让一批部署单
          卡在「待独图」上,而线上其实早就跑起来了。

        建完直接落终态,并把部署头写进当前值面——那一格以前靠人手抄,抄漏就全台面读到旧值。
        """
        head = str(head or "").strip()
        if not head:
            raise TicketError("上服记录必须写明部署头(提交号)。")
        repo = str(repo or "").strip() or DEPLOY_TARGETS[0]
        if repo not in DEPLOY_HEAD_KEYS:
            raise TicketError(
                f"上服目标不认识：{repo}。可用目标为：{'、'.join(DEPLOY_TARGETS)}。"
            )
        rows = normalize_lines(tickets)
        listed = "、".join(rows) if rows else "（未列批内单）"
        record = self.create_dispatch(
            DEPLOY_TARGET_SLOTS[repo],
            f"上服记录 · {repo} {head}",
            [f"部署脚本自动建于 {now_text()}"],
            "线上服务(部署脚本在后置闸通过之后自动建)",
            assign="",
            task_tier="丙",
            context_lines=0,
            deliverables=[],
            internal=True,
            system_generated=True,
        )
        record["部署类"] = "上服记录"
        record["部署头"] = head
        record["部署仓"] = repo
        record["批内单"] = rows
        record["接线证据"] = {
            "文字": f"批内单:{listed}", "验证命令": "deploy/update.sh",
            "原样输出": str(probes or "").strip(), "图片列表": [],
        }
        # 免判免复检:它是既成事实的记录,不是活。直接落「实机复验过」——
        # 那是终态里语义最贴的一个(线上真跑起来了,探针是证据),而且自动退役那条链认它。
        record["状态"] = "实机复验过"
        record["判卷人"] = "部署脚本(免判)"
        record["判语"] = f"上服记录免判:线上 {repo} 头 = {head}；探针见原样输出。"
        record["复检人"] = "部署脚本(免复检)"
        record["复验"] = {
            "复验人": "部署脚本", "时间": now_text(), "结论": "过",
            "闸输出": str(probes or "").strip(), "说明": "部署脚本的后置闸(端口真在听)通过之后自动建。",
        }
        record["实机图标记"] = SHOT_EXEMPT
        record["免独图原因"] = "上服记录免图;取证(独图/试玩)另开取证单——取不到图不挡上服。"
        self.store.save_ticket(record, "deploy-record", actor, f"上服记录 · {repo} {head}")
        # 部署头自动写进当前值面。以前这一格靠人手抄,抄漏全台面读到旧值(state 那四项的老毛病)。
        key = DEPLOY_HEAD_KEYS.get(repo, "")
        try:
            self.state_set(key, head, REVIEW_SLOT)
        except TicketError as error:  # pragma: no cover - 值面写失败不该把上服记录也废掉
            record["备注"] = (record.get("备注", "") + f"\n值面 {key} 没写成:{error}").strip()
            self.store.save_ticket(record, "note", actor, f"值面 {key} 没写成")
        return record

    def evidence_ticket(
        self, head: str, slot: str, assign: str = "", tickets: list[str] | tuple[str, ...] | str | None = None,
    ) -> dict[str, Any]:
        """取证单（D9-460 ③）:上服之后专门去取独图/试玩的那张,开员工窗、欠图记待独图。

        与上服记录**分开**的意义:上服成没成看探针,取证是另一件事。
        绑在一起的时候,一批部署单卡在「待独图」上,而线上其实早就跑起来了。
        """
        rows = normalize_lines(tickets)
        ticket = self.create_dispatch(
            slot,
            f"取证 · {head}（独图/试玩）",
            [f"上服记录头 {head}"],
            f"{LIVE_ORIGIN}（取证单要的就是屏上那一眼）",
            assign=assign,
            task_tier="丙",
            context_lines=200,
            deliverables=[],
            internal=False,
            system_generated=True,
        )
        ticket["部署类"] = "取证"
        ticket["部署头"] = str(head).strip()
        ticket["批内单"] = rows
        ticket["实机图标记"] = "待独图"
        self.store.save_ticket(ticket, "evidence-ticket", slot, f"取证单 · {head}")
        return ticket

    @staticmethod
    def is_deploy_record(ticket: dict[str, Any]) -> bool:
        return str(ticket.get("部署类", "")) == "上服记录"

    def pending_verify(self, slot: str = "") -> list[dict[str, Any]]:
        """待复验队列(D9-460 ④):已交板、还没复验过的单。

        ★含「待判」——这正是并行的意义:员工一交板复检席就能动手,不必等总监判卷。
        只列这两态:别的状态要么还没交板,要么已经并过了。
        """
        return [
            row for row in self._filtered_tickets(slot)
            if row.get("状态") in {"待判", "待复检"} and not self.is_verified(row)
        ]

    def ready_to_merge(self, slot: str = "") -> list[dict[str, Any]]:
        """可并队列(D9-460 ④):判过 ∧ 复验过,两道都齐、就等复检席按一下。"""
        return [
            row for row in self._filtered_tickets(slot)
            if self.is_judged_for_merge(row) and self.is_verified(row)
        ]

    def non_business_blocked(self, slot: str | None = None) -> list[dict[str, Any]]:
        """非业务阻塞看板:哪些单身上还挂着没清的非业务记录（T-001256 ④）。"""
        rows: list[dict[str, Any]] = []
        for ticket in self.list_tickets(slot):
            pending = [row for row in (ticket.get("非业务阻塞") or []) if not row.get("已清")]
            if not pending:
                continue
            rows.append({
                "编号": ticket["编号"], "标题": ticket.get("标题", ""),
                "所属总监位": ticket.get("所属总监位", ""), "状态": ticket.get("状态", ""),
                "条数": len(pending), "非业务阻塞": pending,
            })
        return rows

    def unblock(self, ticket_id: str, actor: str = CONDUCTOR_SLOT) -> dict[str, Any]:
        ticket = self.store.load_ticket(ticket_id)
        self._require_state(ticket, "阻塞", "只有“阻塞”的派单能解除阻塞。")
        previous = ticket.get("阻塞前状态")
        if not previous:
            raise TicketError("这张工单没有记住阻塞前状态，无法安全恢复。")
        # T-000891:解阻塞一律落「返工」,不再还原「阻塞前状态」。
        # 单子会被阻塞,多半正说明任务书要改;而还原成「已认领」就换不了书了
        # (可改态白名单是 新建/已认领/返工,已认领其实也能改——但那一档还留着旧窗的
        #  「已开窗」戳记,设计者队列不会再提示重新传达)。落返工:单号、断点、返工次数、
        # 模型账全留着,总监 set --taskbook 换书,员工 claim 一次即回「已认领」。
        # 「阻塞前状态」仍然写进事件线,便于事后查这张单当初卡在哪一档。
        ticket["状态"] = "返工"
        ticket["阻塞原因"] = ""
        # ★T-001122:落「返工」之后必须把「已开窗」戳记一起清掉,和 judge --rework 一模一样。
        # 前端的 isOpened() 判的是「已开窗.轮次 == 返工次数」,而 unblock 两个数都不动——
        # 于是 0 == 0 恒成立,wantsDispatch() 把这张单挡在「要你传达的」外面,
        # 设计者队列里根本不出现它;等它熬过返工那 8 小时线,才从折叠着的「卡住了」段冒出来。
        # 撞到过:T-001057 解阻塞后,重新派发的工单在队列里看不到。
        # 上面那段注释当初就写明了「已认领那一档还留着旧窗的戳记,设计者队列不会再提示重新传达」,
        # 落点改成返工是对的,但漏了把戳记清掉——半个修法,同一个洞换了个状态继续在。
        # ★这一句与本方法「不要复用旧窗」的口径是同一件事:旧窗既然不复用,旧戳记就不该还算数。
        ticket["已开窗"] = None
        # 「阻塞前状态」不再清空:落点统一成「返工」之后,这一格是唯一还记得
        # 「这张单当初卡在哪一档」的地方,查因要靠它。下一次 block 会原地覆盖。
        ticket["流程提示"] = "阻塞已解除,落「返工」；要换任务书就现在换（单号不变），换完请发续单，不要复用旧窗。"
        self.store.save_ticket(
            ticket, "unblock", actor,
            f"解除阻塞,落「返工」(阻塞前是 {previous})；已开窗标记已清,这张单会回到设计者的「要你传达的」；"
            "要换任务书就现在换,换完请发续单,不要复用旧窗",
        )
        # 解阻塞 = 球回到本位手里（要发续单，不许复用旧窗）。
        self._notify_slots(
            (str(ticket.get("所属总监位", "")),), actor,
            f"{actor} 解除了 {ticket['编号']} 的阻塞 · {ticket.get('标题', '')}，"
            f"已落「返工」（阻塞前是{previous}）,可 set --taskbook 换书后发续单", ticket["编号"],
        )
        return ticket

    def _notify_slots(self, slots, actor: str, text: str, ticket_id: str = "", stamp: str = "") -> list[str]:
        """往这些位的对话线各落一行通知，让设计者队列的「要你去唤醒的窗口」亮起来。

        设计者看的只有那一段；他不需要记单号，他只需要知道「该唤醒谁」。
        所以凡是「A 位对 B 位做了一个动作、B 位那扇窗必须动手」的场合，都要在这里落一行——
        不落，唤醒段就不亮，B 位那扇窗永远不知道有事等它（各位总监的窗口不会自己醒，
        没人把话贴进去它就不运行）。连撞两次：T-000259、T-000249。

        ★为什么绕开 say 的署名闸：say 的位名一侧现在已经放开到「任何在册位名」，
          但**发起人可能是员工**——员工走 say 要带 --ref 且那张单指派给他本人，
          而这里落的通知既不带单号也未必是他自己的单，照 say 的判据会被拦下。
          何况这一行是「系统替 A 在 B 的账上记一笔」，不是 A 跑到 B 的对话线里发言。
          下一个人别把那道闸补到这里来——补上去唤醒段就又不亮了。
        ★发言人一律填真正的动作发起人，不能填成目标位自己：前端 slotUnreadForOwner 的过滤条件是
          「发言人 !== 该位」，填成目标位这一行永远不会被算成它的未读，等于没写。
          自己给自己位建单时发言人本来就等于该位，那一行不计未读是对的——不需要唤醒自己。
        """
        speaker = actor.strip() or "未署名"
        moment = stamp or now_text()
        notified: list[str] = []
        for slot in dict.fromkeys(str(item or "") for item in slots):
            if slot not in SLOTS:
                continue
            self.store.append_jsonl(self.store.thread_path(slot), {
                "时间": moment,
                "发言人": speaker,
                "文字": text,
                "图片列表": [],
                "引用工单号": ticket_id,
                "已读标记": [],
            })
            notified.append(slot)
        return notified

    def transfer(self, ticket_id: str, target: str, reason: str, actor: str) -> dict[str, Any]:
        ticket = self.store.load_ticket(ticket_id)
        if target not in set(SLOTS) | {OWNER_ROLE}:
            raise TicketError(f"转交目标只能是{OWNER_ROLE}、{CONDUCTOR_SLOT}、{REVIEW_SLOT}或名册内总监位。")
        if not reason.strip():
            raise TicketError("不能转交：请用一句话写明原因。")
        if target == OWNER_ROLE and ticket.get("类型") == "拍板":
            self._validate_decision_body(str(ticket.get("正文", "")))
        source = str(ticket.get("所属总监位") or ticket.get("指派给") or "未定")
        stamp = now_text()
        history = {"从": source, "到": target, "原因": reason.strip(), "发言人": actor.strip() or "未署名", "时间": stamp}
        ticket.setdefault("转交历史", []).append(history)
        visible = ticket.setdefault("转交可见位", [])
        for slot in (source, target):
            if slot in SLOTS and slot not in visible:
                visible.append(slot)
        if target in SLOTS:
            ticket["所属总监位"] = target
        if ticket.get("类型") != "派单" or ticket.get("状态") in {"新建", "已认领", "返工", "阻塞"}:
            ticket["指派给"] = target
        if ticket.get("类型") != "派单":
            ticket["状态"] = "待答"
        self.store.save_ticket(
            ticket,
            "transfer",
            actor.strip() or "未署名",
            reason.strip(),
            {"op": "transfer", "from": source, "to": target, "reason": reason.strip(), "by": actor.strip() or "未署名"},
        )
        self._notify_slots(
            (source, target),
            actor,
            f"转交 {ticket['编号']}：{source}→{target}。原因：{reason.strip()}",
            ticket["编号"],
            stamp,
        )
        return ticket

    def answer(self, ticket_id: str, answer: str, actor: str) -> dict[str, Any]:
        ticket = self.store.load_ticket(ticket_id)
        self._require_state(ticket, "待答", "只有“待答”的工单能答复。")
        if not answer.strip():
            raise TicketError("答复不能为空。")
        owner = str(ticket.get("所属总监位", ""))
        if ticket["类型"] == "拍板":
            if actor not in {OWNER_ROLE, CONDUCTOR_SLOT}:
                raise TicketError(
                    f"这张拍板单 {ticket['编号']} 只有设计者或总编排能答；你署名的是“{actor}”。"
                    "要提意见请在本位对话线 say，或另建疑问单。"
                )
        elif ticket["类型"] == "疑问":
            # 疑问是「总监向别位提事」的通道（宪法 2.4.4）：收件位答不了，这条通道就是死的。
            if actor not in {OWNER_ROLE, CONDUCTOR_SLOT, owner}:
                raise TicketError(
                    f"这张疑问单 {ticket['编号']} 挂在“{owner}”位待答，能答的是"
                    f"“{owner}”、设计者、总编排三方；你署名的是“{actor}”。"
                    "不是你该答的就别代答，请转交或在本位对话线 say。"
                )
        elif ticket["类型"] == "阻塞":
            # 宪法八章 6 原文是「阻塞单由总监或总编排答完后另发续单」：挡住所属位，
            # 这条通道就死在半路——总编排把单转回本位，本位却按不动，void 又只吃
            # 新建/已认领/阻塞，单子永远停在待答（T-000575 实测撞到）。
            # transfer 会把「所属总监位」改成接收位，所以判 owner 就已经含了转交后的接收位。
            # 与疑问的区别只有一个：阻塞不放行设计者——协调阻塞是总监与总编排的事。
            if actor not in {CONDUCTOR_SLOT, owner}:
                raise TicketError(
                    f"这张阻塞单 {ticket['编号']} 只能由总编排或所属位“{owner}”答复；"
                    f"你署名的是“{actor}”。不是你该答的就别代答，请转交或在本位对话线 say。"
                )
        elif ticket["类型"] == "需求":
            # 需求单以前只有总编排能答:接收位把活干完了也答不动,只能另建一张疑问单回话,
            # 原单永远挂在「待答」——设计者队列上看着是总编排卡了 30 小时,其实活早做完了
            # (T-000367 / T-000370 实测,T-000409 记账)。
            # transfer 会把「所属总监位」改成接收位,所以判 owner 就已经含了转交后的接收位。
            # 总工单不在这一支:那是总编排自己的账本,口径一个字没动。
            if actor not in {OWNER_ROLE, CONDUCTOR_SLOT, owner}:
                raise TicketError(
                    f"这张需求单 {ticket['编号']} 挂在“{owner}”位待答，能答的是"
                    f"“{owner}”、设计者、总编排三方；你署名的是“{actor}”。"
                    "不是你该答的就别代答，请转交或在本位对话线 say。"
                )
            # 三种前缀对设计者与总编排同样生效:开了后门,队列上就又会出现「已答」却查不到排期的单。
            self._require_demand_answer_prefix(ticket, answer)
        elif ticket["类型"] == "总工单":
            if actor != CONDUCTOR_SLOT:
                raise TicketError(
                    f"这张{ticket['类型']}单 {ticket['编号']} 只能由总编排答复；你署名的是“{actor}”。"
                )
        else:
            raise TicketError("派单不走 answer，请按判卷、复检、实机复验流程推进。")
        ticket["答复"] = answer.strip()
        ticket["状态"] = "已答"
        self.store.save_ticket(ticket, "answer", actor, answer.strip())
        # 答复要回到「问的那一方」手上：所属位，外加跨位单的发起位。
        self._notify_slots(
            (owner, str(ticket.get("发起位", ""))),
            actor,
            f"{actor} 答了 {ticket['编号']} · {ticket.get('标题', '')}",
            ticket["编号"],
        )
        return ticket

    def staff_new(self, slot: str, tool: str = "sol") -> dict[str, Any]:
        if slot not in SLOTS:
            raise TicketError(f"总监位不在名册里：{slot}")
        tool = tool.strip().lower()
        if not tool:
            raise TicketError("模型名不能为空。")
        accounting_tool = normalize_model_name(tool)
        with self.store.locked():
            staff = self.store.read_json(self.store.staff_path)
            slots = self.store.read_json(self.store.slots_path)
            roster = list(slots.get("模型名册") or [])
            roster_row = next((
                row for row in sorted(roster, key=lambda item: len(str(item.get("模型", ""))), reverse=True)
                if accounting_tool == normalize_model_name(row.get("模型"))
                or accounting_tool.startswith(normalize_model_name(row.get("模型")) + "-")
            ), {})
            model_name = normalize_model_name(roster_row.get("模型") or tool)
            if roster_row.get("状态") == "退役":
                raise TicketError(f"不能登记：模型 {model_name} 已退役；历史记录保留，但不可再 staff new。")
            self._assert_model_not_banned(staff, accounting_tool, slot)
            group = staff.setdefault("总监位", {}).setdefault(slot, {"下一个编号": 1, "员工": []})
            group.setdefault("下一个编号", 1)
            group.setdefault("员工", [])
            number = int(group["下一个编号"])
            # T-001659:两位不够用了(后端·世界服务端登记到 -99),扩到三位。
            # 不走回收重用:员工号被判语/断点件/章程/记忆大量引用,同一个 -37 在两个
            # 时期指向两个人,查判退责任会错到别人头上。
            if number > 999:
                raise TicketError(f"{slot} 的员工编号已用完(上限 -999),请找平台·工单系统扩规则。")
            name = f"{slot}-{number:02d}"
            # 「固定工位」默认假、「记忆md路径」默认空：绝大多数工位是一次性的，
            # 打标记是 staff fix 的显式动作，不能靠新建时手滑变成默认（T-001073 R1）。
            member = {
                "编号": number, "员工名": name, "工具/窗类型": tool, "开窗时间": now_text(),
                "状态": "在岗", "经手工单号列表": [], "固定工位": False, "记忆md路径": "",
            }
            group["员工"].append(member)
            group["下一个编号"] = number + 1
            self.store.atomic_json(self.store.staff_path, staff)
        slot_row = next((row for row in slots.get("总监位", []) if row.get("名字") == slot), {})
        main_models = list(slots.get("主力模型集合") or TicketStore.MAIN_MODELS)
        warning = ""
        if tool != "待定" and slot_row.get("主力模型", False) and model_name not in main_models:
            warning = (
                f"提醒：{tool} 不在主力模型名册里。当前主力是 {'、'.join(main_models)}，"
                f"只有主力能吃甲档和乙档；{tool} 这一类只吃丙档——步骤逐条写死、"
                "上下文预算不超过 2000 行的模板任务书。"
                f"「{slot}」这一位标着优先用主力，本次仍已登记 {name}；"
                "要派甲档乙档请换主力模型，派丙档就照丙档模板发，并由总监首检从严。"
            )
        return dict(member, 提示=warning)

    def _assert_model_not_banned(self, staff: dict[str, Any], model: str, slot: str, action: str = "开窗") -> None:
        """手工停用之后，这一道就是真正拦住模型的闸——两条开窗路径都要过它。

        D9-388 之后自动停用已关（见 _record_model_rework），bans 里只会有设计者/总编排
        手工 staff ban 写进去的项。只拦 staff_new 是不够的：把窗先开成“待定”，再
        open_window 填上被停的模型，就整个绕过去了；所以 open_window 也调这一道。
        """
        value = normalize_model_name(model)
        bans = staff.get("模型停用", {})
        global_bans = {normalize_model_name(item) for item in bans.get("全项目", [])}
        local_bans = {normalize_model_name(item) for item in bans.get("按位", {}).get(slot, [])}
        if value in global_bans:
            raise TicketError(
                f"不能{action}：模型 {value} 已被手工停用（全项目）。"
                f"请由设计者或总编排核查后执行 staff unban --tool {value} --by 总编排。"
            )
        if value in local_bans:
            raise TicketError(
                f"不能{action}：模型 {value} 已被手工停用（限“{slot}”）。请换模型，"
                f"或由设计者/总编排执行 staff unban --tool {value} --slot {slot} --by 总编排。"
            )

    def staff_ban(self, tool: str, actor: str, slot: str = "", reason: str = "") -> str:
        """手工停用一个模型：D9-388「乙口径」缺的那后半截。

        到停用线只通知、不自动停（_record_model_rework），停不停由总编排核过责任归属后
        跑这条命令。权限与 staff_unban 对称：只有设计者/总编排能落笔——工具自动
        停用两次误伤 sol，一停就是所有位停摆，所以这一步必须是人的动作，且必须留下原因。
        主力模型（R1.5）在此之上再收紧一道：只有设计者能停，总编排署名也拒——
        主力模型只做算力调整，不因判退次数停用。
        """
        model = normalize_model_name(tool)
        if actor not in {OWNER_ROLE, CONDUCTOR_SLOT}:
            raise TicketError(f"只有{OWNER_ROLE}或{CONDUCTOR_SLOT}可以停用模型。")
        if not model:
            raise TicketError("模型名不能为空。")
        slots = self.store.read_json(self.store.slots_path)
        main_models = {normalize_model_name(value) for value in slots.get("主力模型集合") or TicketStore.MAIN_MODELS}
        # 变体也算主力:opus-high / opus5 归并到 opus 之后对闸,不许换个写法绕过去(T-000794 R1.5)。
        base, _ = self._model_base_name(model, self._roster_base_names(slots))
        if base in main_models and actor != OWNER_ROLE:
            raise TicketError(
                f"主力模型 {model} 不停用：只有设计者能停主力模型，总编排署名也拒。"
                "主力模型只做算力调整、绝对不能停用；这一条不随判退次数变。"
            )
        if slot and slot not in SLOTS:
            raise TicketError(f"总监位不在名册里：{slot}")
        reason = str(reason or "").strip()
        if not reason:
            raise TicketError("停用必须写原因（--reason 一句话）：误停一次就是那些位全部停摆，理由要留在事件线上备查。")
        staff = self.store.load_staff()
        bans = staff.setdefault("模型停用", {"全项目": [], "按位": {name: [] for name in SLOTS}})
        global_bans = bans.setdefault("全项目", [])
        if any(normalize_model_name(value) == model for value in global_bans):
            raise TicketError(f"模型 {model} 已在停用中（全项目）。要解禁请跑 staff unban --tool {model} --by 总编排。")
        if slot:
            current = bans.setdefault("按位", {}).setdefault(slot, [])
            scope = f"限“{slot}”"
            unban_hint = f"staff unban --tool {model} --slot {slot} --by 总编排"
        else:
            current = global_bans
            scope = "全项目"
            unban_hint = f"staff unban --tool {model} --by 总编排"
        if any(normalize_model_name(value) == model for value in current):
            raise TicketError(f"模型 {model} 已在停用中（{scope}）。要解禁请跑 {unban_hint}。")
        current.append(model)
        detail = f"模型 {model} 已手工停用（{scope}），原因：{reason}；解禁 {unban_hint}。"
        self.store.save_staff(staff)
        self.store.append_jsonl(self.store.log_path, {"时间": now_text(), "工单号": "", "事件": "staff-ban", "发言人": actor, "状态": "", "说明": detail, "事件序号": 0})
        self._notify_slots((CONDUCTOR_SLOT,), actor, detail)
        return detail

    def staff_unban(self, tool: str, actor: str, slot: str = "") -> str:
        tool = normalize_model_name(tool)
        if actor not in {OWNER_ROLE, CONDUCTOR_SLOT}:
            raise TicketError(f"只有{OWNER_ROLE}或{CONDUCTOR_SLOT}可以解禁模型。")
        if slot and slot not in SLOTS:
            raise TicketError(f"总监位不在名册里：{slot}")
        staff = self.store.load_staff()
        bans = staff.setdefault("模型停用", {"全项目": [], "按位": {name: [] for name in SLOTS}})
        if slot:
            current = bans.setdefault("按位", {}).setdefault(slot, [])
            current[:] = [value for value in current if normalize_model_name(value) != tool]
            detail = f"模型 {tool} 已解除在“{slot}”的停用。"
        else:
            current = bans.setdefault("全项目", [])
            current[:] = [value for value in current if normalize_model_name(value) != tool]
            detail = f"模型 {tool} 已解除全项目停用。"
        self.store.save_staff(staff)
        self.store.append_jsonl(self.store.log_path, {"时间": now_text(), "工单号": "", "事件": "staff-unban", "发言人": actor, "状态": "", "说明": detail, "事件序号": 0})
        return detail

    def staff_fix(self, name: str, actor: str, memory_path: str) -> dict[str, Any]:
        """把一位员工标成固定工位，并记下他的工位记忆 md 路径（T-001073 R1）。

        路径**约定**见 config.memory_path_hint()，但工具不替人建目录、也不要求文件
        必须已经存在——记忆件是 memory export 生成的，先有标记才有第一次导出。
        父目录不在只提醒一句，不拦：拦了就是先有鸡还是先有蛋。
        """
        path = str(memory_path or "").strip()
        if not path:
            raise TicketError("记忆 md 路径不能为空。约定写法：" + MEMORY_PATH_HINT)
        staff = self.store.load_staff()
        found = self._find_staff_in(staff, name)
        if not found:
            raise TicketError(f"员工名册里找不到 {name}。")
        slot, member = found
        self._assert_may_fix(slot, actor, name)
        member["固定工位"] = True
        member["记忆md路径"] = path
        member["固定工位标记时间"] = now_text()
        member["固定工位标记人"] = actor.strip()
        self.store.save_staff(staff)
        warning = ""
        if path_is_for_another_machine(path):
            # 远程模式:命令在服务端跑,而这个路径指的是客户端那台机器的盘。
            # 拿它去查服务端自己的文件系统,答案永远是「不存在」——那不是提醒,是噪音,
            # 而恒定响的提醒会把人训练成不看提醒。
            warning = "（服务端看不到你那台机器的盘，父目录这一条没校验。）"
        else:
            parent = Path(path).expanduser().parent
            if not parent.is_dir():
                warning = (
                    f"提醒：记忆件的父目录还不在（{parent}）。标记照常打上了；"
                    f"第一次跑 memory export --staff {name} 时会替你把目录建出来。"
                )
        return dict(member, 所属总监位=slot, 提示=warning)

    def staff_unfix(self, name: str, actor: str) -> dict[str, Any]:
        """取消固定工位标记；**路径保留**，便于回看上一轮的记忆件（T-001073 R1）。"""
        staff = self.store.load_staff()
        found = self._find_staff_in(staff, name)
        if not found:
            raise TicketError(f"员工名册里找不到 {name}。")
        slot, member = found
        self._assert_may_fix(slot, actor, name)
        if not member.get("固定工位"):
            raise TicketError(f"{name} 本来就不是固定工位。")
        member["固定工位"] = False
        member["固定工位取消时间"] = now_text()
        return_value = dict(member, 所属总监位=slot)
        self.store.save_staff(staff)
        return return_value

    @staticmethod
    def _assert_may_fix(slot: str, actor: str, name: str) -> None:
        """谁能给这位员工打固定工位标记：他所属的总监位，或者总编排。

        拒的时候必须说得出谁可以——只回一句「没有权限」会把人卡在原地（T-000831 的口径）。
        """
        if actor.strip() in {slot, CONDUCTOR_SLOT}:
            return
        raise TicketError(
            f"不能改 {name} 的固定工位标记：只有他所属的「{slot}」或「{CONDUCTOR_SLOT}」可以。"
            f"你署的是「{actor.strip() or '（空）'}」——别位总监与员工窗都不行，请找这两位之一代办。"
        )

    def slot_charter_path(self, slot: str) -> str:
        """这一位的章程 md 在哪（登记为开窗来源，卡片上要看得见）。

        先读 slots.json 里那一位的「章程」登记；没登记就按工作区目录的约定推算。
        ★推算而不是留空:各位的章程本来就在 `_office/<位名>/章程.md`,
          留空只会让卡片上多一格「未填」,而那格恰恰是新人开窗前唯一该读的东西。
        ★只读不写:本方法不碰盘,登记由 slots.json 那边管。
        """
        if slot not in SLOTS:
            return ""
        rows = (self.store.read_json(self.store.slots_path, {}) or {}).get("总监位") or []
        for row in rows:
            if isinstance(row, dict) and str(row.get("名字", "")) == slot:
                registered = str(row.get("章程", "")).strip()
                if registered:
                    return registered
        return config.charter_path(slot)

    def slot_charters(self) -> dict[str, str]:
        """全部位的章程路径,网页 /api/slots 一次带走。"""
        return {slot: self.slot_charter_path(slot) for slot in SLOTS}

    def staff_memory_path(self, name: str) -> str:
        """这位是不是固定工位、记忆件在哪；不是固定工位或没记路径一律返回空串。"""
        found = self.find_staff(name) if name else None
        if not found:
            return ""
        member = found[1]
        if not member.get("固定工位"):
            return ""
        return str(member.get("记忆md路径", "")).strip()

    def staff_retire(self, name: str) -> dict[str, Any]:
        staff = self.store.load_staff()
        found = self._find_staff_in(staff, name)
        if not found:
            raise TicketError(f"员工名册里找不到 {name}。")
        _, member = found
        if member["状态"] == "已收窗":
            raise TicketError(f"{name} 已经收窗。")
        member["状态"] = "已收窗"
        member["收窗时间"] = now_text()
        self.store.save_staff(staff)
        return member

    def staff_reopen(self, name: str) -> dict[str, Any]:
        staff = self.store.load_staff()
        found = self._find_staff_in(staff, name)
        if not found:
            raise TicketError(f"员工名册里找不到 {name}，reopen 不能新造编号。")
        _, member = found
        if member["状态"] == "在岗":
            raise TicketError(f"{name} 已经在岗。")
        member["状态"] = "在岗"
        member["重开时间"] = now_text()
        self.store.save_staff(staff)
        return member

    def auto_retire_worker(self, ticket: dict[str, Any]) -> str:
        """单子进终态时，把非固定工位的执行方自动收窗；回一行人话，出错一律吞掉。

        ★这是**附加动作**，绝不许把主流程弄失败（与 T-001073 R2 的记忆件重刷同一条口径）：
        名册读写出任何问题都只多一行字，关闭 / 作废 / 实机复验 / 并线本身照常落库成功。
        """
        try:
            return self._auto_retire_worker(ticket)
        except Exception as exc:  # noqa: BLE001 —— 附加动作不许卡住结案，什么都得吞
            return (
                f"{AUTO_RETIRE_PREFIX}没做成：{exc}。"
                f"这张单本身已经落库，名册请手动跑 staff retire {str(ticket.get('指派给', '')).strip()}。"
            )

    def _auto_retire_worker(self, ticket: dict[str, Any]) -> str:
        """三条都满足才退：不是固定工位、手上没有别的在办单、现在还在岗。

        固定工位一律不退——固定工位跨窗复用，退了下一窗连 claim 都进不来，
        正好砸掉 D9-438 那一批的目的（T-001073 做的标记就是给这里用的）。
        """
        if not is_done_for_staff(ticket):
            return ""
        name = str(ticket.get("指派给", "")).strip()
        if not name:
            return ""
        staff = self.store.load_staff()
        found = self._find_staff_in(staff, name)
        if not found or found[1].get("状态") != "在岗":
            return ""
        slot, member = found
        if member.get("固定工位"):
            return ""
        busy = self._open_ticket_ids(member, name, ticket)
        if busy:
            return ""
        state = str(ticket.get("状态", ""))
        member["状态"] = "已收窗"
        member["收窗时间"] = now_text()
        member["自动收窗依据"] = f"{ticket['编号']} 进入「{state}」"
        self.store.save_staff(staff)
        self.store.append_jsonl(self.store.log_path, {
            "时间": now_text(), "工单号": str(ticket["编号"]), "事件": "staff-auto-retire",
            "发言人": slot, "状态": state,
            "说明": f"{name} 非固定工位、手上已无在办单，随 {ticket['编号']} 进入「{state}」自动收窗",
            "事件序号": 0,
        })
        return (
            f"{AUTO_RETIRE_PREFIX}{name}（非固定工位，手上已无在办单）。"
            f"编号仍在册，history 与模型合格率一分不少（账按模型统计，不按编号）；"
            f"要再用同一个窗请跑 staff reopen {name}。"
        )

    def _open_ticket_ids(self, member: dict[str, Any], name: str, current: dict[str, Any]) -> list[str]:
        """这位手上还剩几张在办单。

        ★只数**现在仍然指派给他**的单：「经手工单号列表」是履历，单子被转走或改派之后
        并不会从里面撤回；照履历数，一个早就没活干的人永远退不掉。
        触发本次的那张单按它刚落库的新状态算，不再回盘上读一次（盘上那份就是它）。
        """
        current_id = str(current.get("编号", ""))
        busy: list[str] = []
        for row in member.get("经手工单号列表", []):
            ticket_id = str(row)
            if ticket_id == current_id:
                continue
            try:
                other = self.store.load_ticket(ticket_id)
            except TicketError:
                continue  # 履历里指着一张已经不在的单：不让它卡死整张名册
            if str(other.get("指派给", "")).strip() != name:
                continue
            if not is_done_for_staff(other):
                busy.append(ticket_id)
        return busy

    def _with_retire_notice(self, ticket: dict[str, Any]) -> dict[str, Any]:
        """结案类动作的统一出口：顺手收窗，并把回执挂在**返回值**上，不进盘。"""
        notice = self.auto_retire_worker(ticket)
        return dict(ticket, 自动退役提示=notice) if notice else ticket

    def list_staff(self, slot: str | None = None, include_retired: bool = False) -> list[dict[str, Any]]:
        """默认**只列在岗**（T-001081）：已收窗的编号仍在册，加 --all / ?all=1 才出全量。

        名册是「现在能派给谁」的清单，不是履历表；履历走 history 与 memory export，
        模型合格率也另有一条路（按模型统计，不按编号），都读全量，退役一分不丢。
        """
        staff = self.store.load_staff()
        slots = [slot] if slot else list(SLOTS)
        if slot and slot not in SLOTS:
            raise TicketError(f"总监位不在名册里：{slot}")
        buckets = staff.get("总监位") or {}
        # 两个新格用 setdefault 补在**读出来的副本**上：老名册里没有它们，
        # 在这里补一次就够了，不必为两个默认值触发一次整库迁移。
        return [
            dict(
                member, 所属总监位=name,
                固定工位=bool(member.get("固定工位", False)),
                记忆md路径=str(member.get("记忆md路径", "")),
            )
            for name in slots
            for member in (buckets.get(name) or {}).get("员工", [])
            if include_retired or member.get("状态") == "在岗"
        ]

    def history(self, name: str) -> dict[str, Any]:
        found = self.find_staff(name)
        if not found:
            raise TicketError(f"员工名册里找不到 {name}。")
        slot, member = found
        tickets = []
        for ticket_id in member.get("经手工单号列表", []):
            try:
                ticket = self.store.load_ticket(ticket_id)
                tickets.append({"编号": ticket["编号"], "标题": ticket["标题"], "状态": ticket["状态"], "返工次数": ticket.get("返工次数", 0)})
            except TicketError:
                tickets.append({"编号": ticket_id, "标题": "数据文件缺失", "状态": "未知", "返工次数": 0})
        return {"所属总监位": slot, "员工": member, "工单": tickets}

    def _staff_may_say(self, slot: str, actor: str, reference: str) -> bool:
        """员工能不能往本位对话线写一句（T-000837）。

        位名那一侧的判据见 _slot_may_say（现在收「拍板人 + 任何在册位名」）；这里管的是
        **不在名册里的署名**——员工。原口径连员工也一并拒，本意是别让别位串门，
        但它顺带把**执行方在自己那张单上留一句话**也堵死了：block 会改状态、submit 要等活干完，
        于是员工遇到卡点只能停在半路等人来问（T-000814 上撞到过）。
        设计者当天定的口径是「不能因为其他客观原因阻塞员工」，所以这里只开一条很窄的缝：

        · 必须带 `--ref`，且那张单的「指派给」正是他本人；
        · 他必须是**这个位**在册在岗的员工，别位的线仍然进不去；
        · 不带 `--ref`、或不是自己那张单的，照旧拒。
        """
        if not reference:
            return False
        found = self.find_staff(actor, slot)
        if not found:
            return False
        try:
            ticket = self.store.load_ticket(reference)
        except TicketError:
            return False
        return str(ticket.get("指派给", "")).strip() == actor.strip()

    @staticmethod
    def _slot_may_say(actor: str) -> bool:
        """署名人能不能往**任何**位的对话线发言（员工那条窄缝不看这里，在 _staff_may_say）。

        原口径是「对话线只收拍板人、本位总监、总编排三方」，本意是别让别位串门。
        但它把「总监 A 找总监 B 说一句」也一并堵死了：一句「我这边上完服了，你那边可以开窗」
        的事，只剩下建一张疑问单这条路——而疑问单是**要人答的**，会进对方的待答队列，
        它不是用来传话的。为一句话占一个单号，人就干脆不说了，这是缺了一条路，不是权限设计。
        所以放开成「拍板人 + 任何在册位名」。别位串门的顾虑由留痕兜着：
        对话线每一行都写死发言人，谁说的赖不掉。
        ★员工那条窄缝一个字没放松：仍然必须带 --ref，且那张单指派给他本人。
        ★say 与 say_uploaded 共用这一个判据。同一个事实两处各拼一份必然漂——本仓在别处栽过。
        """
        return actor == OWNER_ROLE or actor in SLOTS

    SAY_REFUSED = (
        f"对话线收{OWNER_ROLE}与任何在册位名(总监之间可互相对话);你署名的不在名册里。"
        "★员工要在自己经手的单上留一句话,请带上 --ref <你那张单号>——"
        "指派给你本人的单可以留言,不必停下来等人问。"
    )

    def say(self, slot: str, actor: str, text: str, image_path: str = "", reference: str = "") -> dict[str, Any]:
        if slot not in SLOTS:
            raise TicketError(f"总监位不在名册里：{slot}")
        if not text.strip() and not image_path:
            raise TicketError("对话文字和图片不能同时为空。")
        by_staff = self._staff_may_say(slot, actor, reference)
        allowed = self._slot_may_say(actor) or by_staff
        if not allowed:
            raise TicketError(self.SAY_REFUSED)
        if reference:
            self.store.load_ticket(reference)
        images: list[dict[str, str]] = []
        if image_path:
            images.append(self._compress_thread_image(slot, Path(image_path), actor))
        text = f"【员工留言】{text.strip()}" if by_staff else text.strip()
        row = {"时间": now_text(), "发言人": actor, "文字": text, "图片列表": images, "引用工单号": reference.upper(), "已读标记": [actor]}
        with self.store.locked():
            self.store.append_jsonl(self.store.thread_path(slot), row)
        return row

    def upload_thread_bytes(self, slot: str, data: bytes, original_name: str, uploader: str) -> dict[str, str]:
        if slot not in SLOTS:
            raise TicketError(f"总监位不在名册里：{slot}")
        slot_key = hashlib.sha1(slot.encode("utf-8")).hexdigest()[:8]
        stem = f"THREAD-{slot_key}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        filename = self._write_compressed_image(data, stem)
        return image_record(filename, Path(original_name).name or "浏览器上传", "其他", uploader.strip() or "未署名")

    def say_uploaded(self, slot: str, actor: str, text: str, images: list[dict[str, str]] | None = None, reference: str = "") -> dict[str, Any]:
        if slot not in SLOTS:
            raise TicketError(f"总监位不在名册里：{slot}")
        images = images or []
        if not text.strip() and not images:
            raise TicketError("对话文字和图片不能同时为空。")
        by_staff = self._staff_may_say(slot, actor, reference)
        allowed = self._slot_may_say(actor) or by_staff
        if not allowed:
            raise TicketError(self.SAY_REFUSED)
        if reference:
            self.store.load_ticket(reference)
        text = f"【员工留言】{text.strip()}" if by_staff else text.strip()
        row = {"时间": now_text(), "发言人": actor, "文字": text, "图片列表": images, "引用工单号": reference.upper(), "已读标记": [actor]}
        self.store.append_jsonl(self.store.thread_path(slot), row)
        return row

    def inbox(self, slot: str, actor: str, mark_read: bool = False) -> list[dict[str, Any]]:
        path = self.store.thread_path(slot)
        rows = self.store.read_jsonl(path)
        unread = [row for row in rows if actor not in row.get("已读标记", []) and row.get("发言人") != actor]
        if mark_read and unread:
            for row in unread:
                row.setdefault("已读标记", []).append(actor)
            self.store.replace_jsonl(path, rows)
        return unread

    def pending_answers(self, slot: str) -> list[dict[str, Any]]:
        """这一位还欠着没答的单（T-000957 R1，答 T-000953）。

        每位总监章程的第 0 步都是跑 inbox——这是全台面唯一保证会被执行的命令，
        所以「你位还欠几张没答」挂在它尾巴上，谁都漏不掉。

        ★判据是「指派给 == 本位」，不是「所属总监位 == 本位」（总编排判退 T-000957 第一轮）：
        拍板单与非跨位的疑问单送设计者时，所属位是**发起位**、指派给才是设计者——
        按所属位数，发起位会把「我在等设计者拍」的单算成「我欠着没答」。
        真例 T-000959：建筑位发的拍板单，它自己根本答不了，却会出现在它的 inbox 尾行里。
        拍板单天天有，这一行一失真就没人信了。本单第 ① 条落地后「指派给」对所有类型都是对的，
        所以这里直接认它。
        """
        return [
            row for row in self.store.list_tickets()
            if row.get("状态") == "待答"
            and str(row.get("指派给", "")).strip() == slot
            and row.get("类型") in {"需求", "疑问", "阻塞", "总工单", "拍板"}
        ]

    def backfill_question_assignees(self, apply: bool = False) -> dict[str, Any]:
        """把历史需求/阻塞单的「指派给」回填成所属总监位（T-000957 R3，答 T-000953）。

        判据照地面位给的：类型 ∈ {需求, 阻塞} 且 所属总监位既不是总编排也不是空、而「指派给」是总编排。
        ★默认只打清单不写——里面可能真有该总编排答的，先给人看一眼再决定动不动。
        """
        hits: list[dict[str, Any]] = []
        for row in self.store.list_tickets():
            slot = str(row.get("所属总监位", "")).strip()
            if row.get("类型") not in {"需求", "阻塞"}:
                continue
            if row.get("状态") != "待答":
                continue
            if slot in {"", CONDUCTOR_SLOT} or slot not in SLOTS:
                continue
            if str(row.get("指派给", "")).strip() != CONDUCTOR_SLOT:
                continue
            hits.append({
                "编号": row["编号"], "类型": row["类型"], "所属总监位": slot,
                "旧值": CONDUCTOR_SLOT, "新值": slot, "标题": row.get("标题", ""),
            })
        if apply:
            for hit in hits:
                ticket = self.store.load_ticket(hit["编号"])
                ticket["指派给"] = hit["新值"]
                self.store.save_ticket(
                    ticket, "backfill-assign", CONDUCTOR_SLOT,
                    f"指派给回填：{hit['旧值']} → {hit['新值']}（T-000957，D9-424 ⑥ 需求由所属位自答）",
                )
        return {"命中": len(hits), "已写入": bool(apply), "明细": hits}

    def pending_answer_line(self, slot: str) -> str:
        """inbox 尾巴上那一行；一张都不欠时返回空串，不打空行。"""
        rows = self.pending_answers(slot)
        if not rows:
            return ""
        ids = " / ".join(str(row.get("编号", "")) for row in rows)
        return f"★你位当前待答 {len(rows)} 张:{ids}"

    def _filtered_tickets(
        self, slot: str = "", state: str = "", ticket_type: str = "", shot_pending: bool = False,
    ) -> list[dict[str, Any]]:
        """筛选这一段被 list_tickets 与 list_cards 共用，免得两条路的口径慢慢漂开。"""
        rows = self.store.list_tickets()
        if slot:
            rows = [row for row in rows if row["所属总监位"] == slot or slot in row.get("转交可见位", [])]
        if state:
            rows = [row for row in rows if row["状态"] == state]
        if ticket_type:
            rows = [row for row in rows if row["类型"] == ticket_type]
        if shot_pending:
            rows = [row for row in rows if row.get("实机图标记", "") == "待独图"]
        return rows

    def list_tickets(
        self, slot: str = "", state: str = "", ticket_type: str = "", shot_pending: bool = False,
    ) -> list[dict[str, Any]]:
        """全文那一份：CLI 的 list、build_bundle 的离线包、服务端搜索都走它。"""
        return [
            self.ticket_view(row)
            for row in self._filtered_tickets(slot, state, ticket_type, shot_pending)
        ]

    def dispatch_instructions(self, ticket: dict[str, Any]) -> list[str]:
        """开窗指令的唯一生成处；网页与 CLI 只消费这里返回的三行。

        指派给固定工位时第二行多接一句「开工前先读 <记忆 md>」（T-001073 R4）——
        非固定工位的单三行**逐字不变**，有钉测守着：一个人加记忆件，不能把全项目
        每一张单的开窗指令都改掉。
        """
        if ticket.get("类型") != "派单":
            return []
        taskbook = str(ticket.get("任务书路径", "")).strip()
        worker = str(ticket.get("指派给", "")).strip()
        if not taskbook or not STAFF_PATTERN.fullmatch(worker):
            return []
        tier = str(ticket.get("任务档", "")).strip()
        tier = tier if tier in {"甲", "乙", "丙"} else "待总监定"
        memory = self.staff_memory_path(worker)
        if memory:
            second = (
                f"执行 {taskbook} 的全部指令,从第 0 步做到收尾问答完。这是任务不是资料,读完立即开工;"
                f"开工前先读 {memory}(本工位的底数与坑,先核分支头与绿数再干活)。"
            )
        else:
            second = f"执行 {taskbook} 的全部指令,从第 0 步做到收尾问答完。这是任务不是资料,读完立即开工。"
        # ★开窗指令三行一个平台名都不许出现：
        #   建议窗口只写在派单标题开头的【X】那一处，拍板人在卡片上就看得见，不必再往这三行里塞。
        #   前两行还要原样贴进员工窗给 AI 读，多一个平台名就是诱导它去猜自己跑在哪个窗上。
        return [
            f"先跑这一条认领:python {config.cli_path()} claim {ticket['编号']} --by {worker}",
            second,
            f"【操作提示·只给{OWNER_ROLE}】新开线程,任务档 {tier},模型你定,贴上面那句。",
        ]

    def dispatch_instruction_text(self, ticket: dict[str, Any]) -> str:
        lines = self.dispatch_instructions(ticket)
        if lines:
            return "\n".join(lines)
        if not str(ticket.get("任务书路径", "")).strip():
            return "这张单还没有任务书路径；请先用 set --taskbook 补上，暂不打印半截开窗指令。"
        return "这张单还没有指派给合法员工；请先补指派，暂不打印半截开窗指令。"

    def ticket_view(self, ticket: dict[str, Any]) -> dict[str, Any]:
        view = dict(ticket)
        view["开窗指令"] = self.dispatch_instructions(ticket)
        return view

    @staticmethod
    def _body_is_rendered(ticket: dict[str, Any]) -> bool:
        """这张单的「正文」在网页上真的会被显示出来吗（T-001322）。

        对应 tickets.js 的 answerCard()——它只渲染 wantsDesignerAnswer 挑出来的那几张:
        状态=待答 且 类型∈{拍板,疑问,需求} 且 指派给=设计者。
        条件改了这里要一起改,否则「要你答的」那一段会空着正文。
        """
        return (
            ticket.get("状态") == "待答"
            and ticket.get("类型") in DESIGNER_ANSWER_TYPES
            and ticket.get("指派给") == OWNER_ROLE
        )

    def card_view(self, ticket: dict[str, Any]) -> dict[str, Any]:
        """列表页那一份:在 ticket_view 之上，把网页一个字都不显示的重键摘掉（T-001322）。

        ★这个函数是**唯一**的精简处,`/api/tickets` 与 `changes_since` 都必须走它。
          已经栽过一次:增量少过了一道 ticket_view,走增量来的单
          在页面上就没有「开窗指令」(T-001313/T-001314)。整份与增量差一个键都不行,
          test_3b_a_delta_row_is_shaped_exactly_like_a_full_list_row 守着这条。

        ★为什么不做在 list_tickets 里:那条路还供着 build_bundle(file:// 离线回落包)。
          离线模式背后没有服务端可以按需取全文,包里少了正文就是**永久**少了。
          CLI 的 list、non_business_blocked 也走它,一并保持全文。

        摘掉哪些键是逐个 grep 网页脚本得出的,不是按体积猜的:
          · 答复/备注   —— 只在 ticketShape() 初始化里出现,没有任何渲染处读它们;
          · 接线证据     —— 只被 worldImages() 读,而 worldImages 全仓**无调用处**;
          · 正文        —— 只有 answerCard() 读,见 _body_is_rendered。
        要看被摘掉的内容走 GET /api/ticket/<编号>,那条路照旧回全文。
        """
        view = self.ticket_view(ticket)
        omitted = [key for key in LIST_OMITTED_KEYS if key in view]
        if "正文" in view and self._body_is_rendered(ticket):
            omitted.remove("正文")
        for key in omitted:
            view.pop(key)
        # ★把摘掉的键名如实列出来,而不是留一个空串顶着。
        #   「这个字段是空的」和「这个字段没发过来」必须能分得清——
        #   分不清就会有人对着空正文去改单,那是最坏的一种静默失败。
        view["未发送字段"] = omitted
        return view

    def list_cards(
        self, slot: str = "", state: str = "", ticket_type: str = "", shot_pending: bool = False,
    ) -> list[dict[str, Any]]:
        """整份那一趟:与 list_tickets 同样的筛选，回的是 card_view 精简行（T-001322）。"""
        return [
            self.card_view(row)
            for row in self._filtered_tickets(slot, state, ticket_type, shot_pending)
        ]

    def deployed_heads(self) -> set[str]:
        """当前值面里那两个部署头（D9-460 ③ 补 / D9-461）。

        取的是**值面**而不是 git:值面是「线上现在跑的是哪个头」的唯一机器可读真源,
        而这台机器上的 git 只说明本地有什么。上服记录会自动写它(deploy_record)。
        """
        values = self._state_raw()
        heads: set[str] = set()
        for key in DEPLOY_HEAD_KEYS.values():
            row = values.get(key)
            value = row.get("值") if isinstance(row, dict) else None
            if value:
                heads.add(str(value).strip().lower())
        return heads

    def awaiting_live_record(self, ticket: dict[str, Any]) -> bool:
        """已合并、代码其实**早就上服了**、只差一笔 live 记账（D9-461）。

        这类单别再按「卡 24 小时」报——它不是卡住了,
        活已经在线上跑着,欠的只是台面上那一笔记账。报错成「卡住」会让人
        去催一个根本不存在的活,而真正该做的是补 live。

        判据:状态=已合并,且单上记的提交号出现在当前值面的部署头里。
        提交号从「接线证据/判语/备注」里找 7～40 位十六进制——
        ★用**前缀双向匹配**:值面写的可能是 9 位短号,单上写的可能是 40 位全号,
          直接字符串相等在这里几乎永远不成立。
        """
        if str(ticket.get("状态", "")) != "已合并":
            return False
        heads = self.deployed_heads()
        if not heads:
            return False
        haystack = " ".join(str(ticket.get(key, "")) for key in ("判语", "备注")) + " " + json.dumps(
            ticket.get("接线证据") or {}, ensure_ascii=False)
        for candidate in re.findall(r"\b[0-9a-f]{7,40}\b", haystack.lower()):
            for head in heads:
                if candidate.startswith(head) or head.startswith(candidate):
                    return True
        return False

    def stale_info(self, ticket: dict[str, Any], now: datetime | None = None, opened: bool = False) -> dict[str, Any] | None:
        """返回已越线工单的阈值与时长；未越线及单列的阻塞/终态返回 None。"""
        state = str(ticket.get("状态", ""))
        if is_terminal(ticket) or ticket.get("类型") == "阻塞":
            return None
        # D9-461:已经上了服的「已合并」单不算卡住——它欠的是一笔 live 记账,不是活没人做。
        # 单独在日览里报「欠 live 记账」,不占「卡住了」那一段(那一段的意思是「有人该动手却没动」)。
        if self.awaiting_live_record(ticket):
            return None
        threshold = 4 if state == "新建" and opened else STALE_STATE_HOURS.get(state)
        if threshold is None:
            return None
        entered_text = ticket.get("状态进入时间") or ticket.get("最后更新时间")
        entered = self._parse_time(entered_text)
        current = now or datetime.now().astimezone()
        elapsed_seconds = max(0.0, (current - entered).total_seconds())
        if elapsed_seconds <= threshold * 3600:
            return None
        return {
            "状态": state,
            "阈值小时": threshold,
            "卡住小时": int(elapsed_seconds // 3600),
            "状态进入时间": entered_text,
        }

    def digest(self, hours: int = 24) -> list[str]:
        now = datetime.now().astimezone()
        since = now - timedelta(hours=hours)
        tickets = self.store.list_tickets()
        new_rows = [row for row in tickets if self._parse_time(row["发起时间"]) >= since]
        stale = [(row, info) for row in tickets if (info := self.stale_info(row, now)) is not None]
        blocked = [row for row in tickets if row["状态"] == "阻塞" or (row["类型"] == "阻塞" and row["状态"] != "关闭")]
        # T-001256 ④:两类分开数。非业务的**不在**上面那一段里——它根本不改状态,
        # 所以既不算阻塞、也不进老化告警;日览这一行是它除 list --nonbiz 之外唯一的出口。
        nonbiz = [
            row for row in tickets
            if any(not item.get("已清") for item in (row.get("非业务阻塞") or []))
        ]
        waiting = [row for row in tickets if row["类型"] in {"需求", "总工单"} and row["状态"] == "待答"]
        shot_pending = [row for row in tickets if row.get("实机图标记", "") == "待独图"]
        transfers = [
            (ticket, transfer)
            for ticket in tickets
            for transfer in ticket.get("转交历史", [])
            if self._parse_time(transfer.get("时间", "")) >= since
        ]
        cross_slot = [row for row in tickets if row.get("发起位") and row.get("发起位") != row.get("所属总监位")]
        unread_counts = []
        for slot in SLOTS:
            count = sum(1 for row in self.store.read_jsonl(self.store.thread_path(slot)) if CONDUCTOR_SLOT not in row.get("已读标记", []) and row.get("发言人") != CONDUCTOR_SLOT)
            if count:
                unread_counts.append((slot, count))
        state_counts = [f"{state} {sum(1 for row, _ in stale if row['状态'] == state)}" for state in STALE_STATE_HOURS if any(row["状态"] == state for row, _ in stale)]
        stale_summary = f"停滞 {len(stale)} 张" + (f"({' · '.join(state_counts)})" if state_counts else "")
        # D9-461:已合并且代码已在线上跑着的单,单列「欠 live 记账」,不进「卡住了」。
        awaiting_live = [row for row in tickets if self.awaiting_live_record(row)]
        # T-001509:「已合并·未上服·已结案」的单——终态,但既不是上服也不是卡住。
        not_deployed_closed = [row for row in tickets if row.get("未上服结案")]
        # D9-462 补 ④:第十三位的待答单独数一份。
        dispatch_slot_waiting = [
            row for row in tickets
            if row.get("所属总监位") in DISPATCH_FORBIDDEN_SLOTS and row.get("状态") == "待答"
        ]
        # D9-460 ④:两条新队列。待复验含「待判」——并行之后交板即可复验,不等判卷。
        awaiting_verify = [row for row in tickets if row.get("状态") in {"待判", "待复检"} and not self.is_verified(row)]
        mergeable = [row for row in tickets if self.is_judged_for_merge(row) and self.is_verified(row)]
        # 老化按「待复验超 24 小时」单独报:它与 STALE_STATE_HOURS 那套按状态计时的口径不同——
        # 一张单可以在「待判」里只待 2 小时(没越线),却已经等复验等了 30 小时。
        verify_stale = [
            row for row in awaiting_verify
            if (datetime.now().astimezone() - self._parse_time(
                row.get("状态进入时间") or row.get("最后更新时间"))).total_seconds() > 24 * 3600
        ]
        lines = [
            f"总编排日览 · 最近 {hours} 小时",
            f"新单 {len(new_rows)} · {stale_summary} · 阻塞 {len(blocked)}"
            f"(业务 {len(blocked)} · 非业务 {len(nonbiz)},非业务不停车、不占队列)"
            f" · 待答需求/总工单 {len(waiting)}",
            f"待复验 {len(awaiting_verify)} 张(其中超 24 小时 {len(verify_stale)} 张) · 可并 {len(mergeable)} 张",
            # ★「待独图 N 张」这一行别再往后接东西:有用例按**整行相等**断言它
            #   (assertIn 在 list 上比的是元素,不是子串)。新数另起一行,不动它。
            f"待独图 {len(shot_pending)} 张",
            f"欠 live 记账 {len(awaiting_live)} 张(已合并、代码已在线上,补一笔 live 即可,不算卡住)",
            # T-001509:「已合并·未上服·已结案」是终态,既不算卡住也不算上服——
            # 不单列的话,它会永远混在「已合并 N」的存量里让人以为还欠活。
            *(
                [f"未上服结案 {len(not_deployed_closed)} 张(已合并但回滚未上服/原命题不成立,不算上服、不算卡住)"]
                if not_deployed_closed else []
            ),
            # D9-462 补 ④:第十三位单列。它是设计者后端类事务的唯一对接人,
            # 积在它手上的待答 = 设计者那一侧的问题没人归并,与别位的待答不是一回事。
            *(
                [f"设计·裁定分发 待答 {len(dispatch_slot_waiting)} 张(设计者后端类事务的对接口,积在这里=问题没归并)"]
                if dispatch_slot_waiting else []
            ),
        ]
        for row, info in stale:
            lines.append(
                f"[停滞] {row['编号']} · {row['标题']} · {row['状态']} 卡了 {self._stale_duration_text(info['卡住小时'])}"
                f" · 轮到 {self._turn_of(row)}"
            )
        # D9-460 ④ 两条新段:排在停滞之后(停滞更急),但排在转交/新单之前——
        # 它们是「现在有人能动手」的两件事,不是流水。
        for row in verify_stale:
            lines.append(
                f"[待复验超时] {row['编号']} · {row.get('标题', '')} · {row.get('状态')}"
                f" · 归 {REVIEW_SLOT} 复验"
            )
        for row in mergeable:
            lines.append(f"[可并] {row['编号']} · {row.get('标题', '')} · 判过∧复验过,等 {REVIEW_SLOT} 按 merge")
        for row in awaiting_live:
            lines.append(
                f"[欠 live 记账] {row['编号']} · {row.get('标题', '')}"
                f" · 代码已在线上跑着(提交在当前值面部署头里),补一笔 live 即可,不是卡住"
            )
        for row in not_deployed_closed:
            record = row.get("未上服结案") or {}
            lines.append(
                f"[未上服结案] {row['编号']} · {row.get('标题', '')}"
                f" · {record.get('原因', '')}(结案人 {record.get('结案人', '')})· 不算上服、不算卡住"
            )
        lines.append(f"今日转交 {len(transfers)}")
        for ticket, transfer in transfers:
            lines.append(f"[转交] {ticket['编号']} · {transfer['从']}→{transfer['到']} · {transfer['原因']}")
        for label, rows in (("新单", new_rows), ("阻塞", blocked), ("非业务", nonbiz), ("待答", waiting)):
            for row in rows:
                lines.append(f"[{label}] {row['编号']} · {row['标题']} · {row['状态']} · {row['所属总监位']}")
        for row in cross_slot:
            lines.append(f"[跨位单] {row['编号']} · {row['发起位']}→{row['所属总监位']} · 抄送总编排")
        for slot, count in unread_counts:
            lines.append(f"[未读对话] {slot} · {count} 条")
        lines.append("各模型合格率：模型 | 任务档 | 交板 | 判过 | 判退 | 合格率 | 状态")
        for row in self.model_statistics():
            lines.append(
                f"[模型] {row['模型']} | {row['任务档']} | {row['交板数']} | {row['判过']} | "
                f"{row['判退']} | {row['合格率']} | {row['状态']}"
            )
        lines.append("总监出题账:总监位 | 出题判退次数")
        for slot, score in sorted((self.store.load_staff().get("出题记分") or {}).items()):
            lines.append(f"[出题] {slot} | {int(score.get('合计', 0))}")
        # 停滞段紧跟抬头；超过旧 60 行上限时只扩到足以容纳全部停滞行，不能把告警砍半。
        # ★D9-460 ④ 加了「待复验超时」「可并」两段之后,上限必须把它们一起算进来:
        #   否则新加的行会把停滞行挤出 60 行窗口——加一段告警反而弄丢了另一段告警,
        #   而且日览看着一切正常(test_digest_never_truncates_stale_section 钉住)。
        return lines[:max(60, 5 + len(stale) + len(verify_stale) + len(mergeable) + len(awaiting_live))]

    @staticmethod
    def _stale_duration_text(hours: int) -> str:
        return f"{hours // 24} 天" if hours > 48 else f"{hours} 小时"

    @staticmethod
    def _turn_of(ticket: dict[str, Any]) -> str:
        """与 tickets.js turnOf() 同口径；服务端没有 localStorage，已认领按执行员工处理。"""
        if ticket.get("类型") in {"拍板", "疑问", "需求"}:
            return str(ticket.get("指派给") or "<未指派>")
        state = ticket.get("状态")
        if state == "新建":
            return str(ticket.get("发起人") or ticket.get("所属总监位") or "<未指派>")
        if state == "已认领":
            return str(ticket.get("指派给") or "<未指派>")
        if state == "待判":
            return str(ticket.get("所属总监位") or "<未指派>")
        if state == "返工":
            return OWNER_ROLE
        if state == "待复检":
            return REVIEW_SLOT
        if state == "已合并":
            return f"复检·合并与部署 或 {ticket.get('所属总监位') or '<未指派>'}"
        return str(ticket.get("所属总监位") or "<未指派>")

    def archive_demo(self) -> dict[str, int]:
        """把 T6 的 T-000001~T-000009 演示台面移入 demo/；重复执行不重复归档。"""
        demo_root = self.store.root / "demo"
        demo_items = demo_root / "items"
        demo_images = demo_root / "img"
        demo_threads = demo_root / "threads"
        for directory in (demo_items, demo_images, demo_threads):
            directory.mkdir(parents=True, exist_ok=True)

        def is_demo_id(value: str) -> bool:
            return bool(re.fullmatch(r"T-\d{6}", value)) and 1 <= int(value[2:]) <= 9

        def move_once(source: Path, destination: Path) -> bool:
            if not source.exists():
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if source.read_bytes() != destination.read_bytes():
                    raise TicketError(f"演示归档发生同名冲突：{destination}")
                source.unlink()
                return False
            source.replace(destination)
            return True

        item_count = 0
        for path in sorted(self.store.items_dir.glob("T-*.json")):
            if is_demo_id(path.stem):
                item_count += int(move_once(path, demo_items / path.name))

        image_count = 0
        for path in sorted(self.store.images_dir.glob("T-*")):
            ticket_id = path.name[:8]
            if is_demo_id(ticket_id):
                image_count += int(move_once(path, demo_images / path.name))

        conversation_count = 0
        for slot in SLOTS:
            source_path = self.store.thread_path(slot)
            rows = self.store.read_jsonl(source_path)
            archived = [row for row in rows if is_demo_id(str(row.get("引用工单号", "")))]
            if not archived:
                continue
            official = [row for row in rows if row not in archived]
            archive_path = demo_threads / source_path.name
            existing = self.store.read_jsonl(archive_path)
            for row in archived:
                if row not in existing:
                    existing.append(row)
                    conversation_count += 1
            self.store.replace_jsonl(archive_path, existing)
            self.store.replace_jsonl(source_path, official)

        staff = self.store.load_staff()
        demo_slots = {
            slot
            for slot, group in staff.get("总监位", {}).items()
            if any(any(is_demo_id(str(ticket_id)) for ticket_id in member.get("经手工单号列表", [])) for member in group.get("员工", []))
        }
        archived_staff: dict[str, Any] = self.store.read_json(
            demo_root / "staff.json", {"总监位": {}, "模型记分": {}, "出题记分": {}, "模型停用": {}},
        )
        staff_count = 0
        for slot in demo_slots:
            group = staff["总监位"][slot]
            real_members = []
            demo_members = []
            for member in group.get("员工", []):
                history = [str(ticket_id) for ticket_id in member.get("经手工单号列表", [])]
                if not any(not is_demo_id(ticket_id) for ticket_id in history):
                    demo_members.append(member)
                else:
                    real_members.append(member)
            archive_group = archived_staff.setdefault("总监位", {}).setdefault(slot, {"下一个编号": group.get("下一个编号", 1), "员工": []})
            known = {member.get("员工名") for member in archive_group.get("员工", [])}
            archive_group.setdefault("员工", []).extend(member for member in demo_members if member.get("员工名") not in known)
            staff_count += len(demo_members)
            group["员工"] = real_members
            group["下一个编号"] = max((int(member.get("编号", 0)) for member in real_members), default=0) + 1
        if staff_count:
            archived_staff["模型记分"] = staff.get("模型记分", {})
            archived_staff["出题记分"] = staff.get("出题记分", {})
            archived_staff["模型停用"] = staff.get("模型停用", {})
            staff["模型记分"] = {}
            staff["出题记分"] = {}
            staff["模型停用"] = {"全项目": [], "按位": {slot: [] for slot in SLOTS}}
            self.store.atomic_json(demo_root / "staff.json", archived_staff)
            self.store.save_staff(staff)

        return {"工单": item_count, "图片": image_count, "对话": conversation_count, "员工": staff_count}

    def export(
        self,
        ticket_id: str,
        out: str | Path | None = None,
        workspace_root: Path | None = None,
    ) -> Path:
        ticket = self.store.load_ticket(ticket_id)
        return self.export_ticket(ticket, out, workspace_root)

    @classmethod
    def export_ticket(
        cls,
        ticket: dict[str, Any],
        out: str | Path | None = None,
        workspace_root: Path | None = None,
    ) -> Path:
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", str(ticket["标题"])).strip(" .")[:60] or str(ticket["编号"])
        if out:
            path = Path(out).expanduser().resolve()
        else:
            root = (workspace_root or cls._default_workspace_root()).resolve()
            path = root / "_office" / str(ticket["所属总监位"]) / "任务书" / f"{ticket['编号']}_{safe_title}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        sources = "、".join(ticket.get("真源指针", [])) or "未填"
        if ticket.get("任务档", "乙") == "丙":
            body = cls._render_c_tier_export(ticket)
        else:
            body = (
                f"# {ticket['标题']}\n\n"
                f"工单：{ticket['编号']} · 类型：{ticket['类型']} · 状态：{ticket['状态']} · 任务档：{ticket.get('任务档', '乙')}\n"
                f"所属总监位：{ticket['所属总监位']} · 指派给：{ticket.get('指派给') or '未指派'}\n"
                f"真源指针：{sources}\n"
                f"实机消费者：{ticket.get('实机消费者') or '未填'}\n\n"
                f"{ticket.get('正文') or ticket.get('备注') or '请按本工单字段与状态完成任务。'}\n"
            )
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(body, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
        return path

    # ── 固定工位的工位记忆（T-001073 R2）────────────────────────────────────
    # 骨架从「这位做过、且已交板的单」自动生成，每一行都回指到某张单：做过什么、
    # 分支头、判语、交付项、断点。员工自己只补一小节「留给下一窗」——自述不可信，
    # 所以自述那一小节被单独框起来，且标着是哪个模型在哪张单上写的。

    def memory_payload(self, name: str) -> dict[str, Any]:
        """导出记忆件要的全部原料；只读，服务端也能跑（远程模式下客户端拿它落盘）。"""
        found = self.find_staff(name)
        if not found:
            raise TicketError(f"员工名册里找不到 {name}。")
        slot, member = found
        seen: dict[str, dict[str, Any]] = {}
        for ticket_id in member.get("经手工单号列表", []):
            try:
                seen[str(ticket_id)] = self.store.load_ticket(str(ticket_id))
            except TicketError:
                continue
        for row in self.store.list_tickets():
            if str(row.get("指派给", "")).strip() == name:
                seen[str(row["编号"])] = row
        # 没交板的不进：还没交板的单上没有分支头、没有判语、没有「留给下一窗」，
        # 进来就是一行猜测，而记忆件里的每一行都必须回指到一件已经发生过的事。
        rows = [row for row in seen.values() if self._has_submitted(row)]
        rows.sort(key=lambda row: str(row.get("编号", "")))
        return {
            "员工名": name,
            "所属总监位": slot,
            "固定工位": bool(member.get("固定工位", False)),
            "记忆md路径": str(member.get("记忆md路径", "")),
            "生成时间": now_text(),
            "工单": rows,
        }

    @staticmethod
    def _has_submitted(ticket: dict[str, Any]) -> bool:
        """这张单交过板没有。现态在交板后的几档里，或者证据文字非空（返工态也交过）。"""
        if str(ticket.get("状态", "")) in SUBMITTED_STATES:
            return True
        return bool(str((ticket.get("接线证据") or {}).get("文字", "")).strip())

    def memory_export(
        self, name: str, out: str | Path | None = None, max_lines: int = DEFAULT_MEMORY_MAX_LINES,
    ) -> Path:
        return self.write_memory(self.memory_payload(name), out, max_lines)

    @classmethod
    def write_memory(
        cls,
        payload: dict[str, Any],
        out: str | Path | None = None,
        max_lines: int = DEFAULT_MEMORY_MAX_LINES,
    ) -> Path:
        """把 memory_payload 渲染成记忆 md 并落盘；超出封顶的旧条目**追加**进归档件。

        这是个纯落盘动作，不碰工单库：远程模式下服务器上没有 D: 盘，所以由客户端
        拿着服务端回的 payload 在自己这台机器上写（与 export_ticket 同一条路子）。
        """
        target = str(out or "").strip() or str(payload.get("记忆md路径", "")).strip()
        if not target:
            raise TicketError(
                f"{payload.get('员工名', '这位员工')} 还没有记忆 md 路径：先跑 "
                f"staff fix {payload.get('员工名', '<员工名>')} --memory <绝对路径> --by <本位或总编排>，"
                f"或者这次显式给 --out。约定写法：{MEMORY_PATH_HINT}"
            )
        path = Path(target).expanduser().resolve()
        archive = memory_archive_path(path)
        head, blocks = cls._memory_sections(payload)
        kept, dropped = cls._memory_fit(head, blocks, max_lines, archive)
        path.parent.mkdir(parents=True, exist_ok=True)
        if dropped:
            stamp = payload.get("生成时间") or now_text()
            banner = [f"<!-- 归档于 {stamp} · {payload.get('员工名', '')} -->", ""]
            body = "\n".join(banner + [line for _, lines in dropped for line in lines]) + "\n"
            with archive.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
        notice = (
            [f"> 更早的 {len(dropped)} 条已归档到 {archive}", ""] if dropped else []
        )
        text = "\n".join(head[:2] + notice + head[2:] + [line for _, lines in kept for line in lines]) + "\n"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
        return path

    @classmethod
    def _memory_sections(cls, payload: dict[str, Any]) -> tuple[list[str], list[tuple[str, list[str]]]]:
        """返回（固定头，[(单号, 该条的行)]）。第 0 步写死在头里，不是可选项。"""
        name = str(payload.get("员工名", ""))
        slot = str(payload.get("所属总监位", ""))
        rows = list(payload.get("工单") or [])
        head = [
            f"# 工位记忆 · {name}（{slot}）",
            "",
            f"> 由工单台 `memory export` 自动生成于 {payload.get('生成时间') or now_text()}，共 {len(rows)} 张已交板的单。",
            "> 骨架每一行都回指到某张单，自述只在各条的「留给下一窗」小节里；别手改本文件，下一次交板就会把它整个重写。",
            "> ★本文件是**快照**：分支头、绿数、行号写下那一刻起就在过期，只当线索不当事实。",
            "",
            MEMORY_STEP_ZERO,
            "",
            "## 做过的单（都已交板；没交板的不进本文件）",
            "",
        ]
        if not rows:
            head.append("（还没有已交板的单。）")
            head.append("")
        blocks = [(str(row.get("编号", "")), cls._memory_block(row, name)) for row in rows]
        return head, blocks

    @classmethod
    def _memory_block(cls, ticket: dict[str, Any], name: str) -> list[str]:
        ticket_id = str(ticket.get("编号", ""))
        # 「这一节是哪个模型写的」：取单上的「实际模型」，空则写「未标」。
        # 下一窗要能分辨「这句话是 opus 在那张单上说的」还是「sol 说的」。
        model_name = str(ticket.get("实际模型", "")).strip() or "未标"
        verdict = str(ticket.get("判语", "")).strip()
        verdict_head = verdict.splitlines()[0].strip() if verdict else "还没判"
        deliverables = normalize_lines(ticket.get("交付项")) or ["未列"]
        lines = [
            f"### {ticket_id} · {ticket.get('标题', '')}",
            f"* 状态：{ticket.get('状态', '')} · 任务档：{ticket.get('任务档') or '未标'} · 本节由模型 {model_name} 写",
            f"* 分支与提交号：{cls._memory_branch_text(ticket)}",
            f"* 判语首行：{verdict_head}",
            "* 交付项：" + "；".join(deliverables),
        ]
        rework = ticket.get("返工原因列表") or []
        if rework:
            last = rework[-1]
            lines.append(
                f"* 断点：被判退过 {len(rework)} 次，最后一次 {last.get('时间', '')} · "
                f"{last.get('判卷人', '')} · 责任 {last.get('判退责任') or last.get('责任') or '未标'} · {last.get('原因', '')}"
            )
        section = ticket.get("留给下一窗") or {}
        rows = handoff_rows(ticket)
        if rows:
            author = str(section.get("填写人", "")).strip() or name
            author_model = str(section.get("实际模型", "")).strip() or model_name
            lines.append(f"* 留给下一窗（{author} 本人填 · 模型 {author_model} · {section.get('时间', '')}）：")
            for index, row in enumerate(rows, start=1):
                if row.get("划掉判卷人"):
                    lines.append(
                        f"  {index}. ~~已划掉~~ ~~{row['文字']}~~ "
                        f"（判卷人划掉：{row['划掉判卷人']} · {row.get('划掉时间', '')}）"
                    )
                else:
                    lines.append(f"  {index}. {row['文字']}")
        else:
            lines.append("* 留给下一窗：交板时没填。")
        lines.append("")
        return lines

    @classmethod
    def _memory_branch_text(cls, ticket: dict[str, Any]) -> str:
        """从证据里捞分支与提交号：捞到什么写什么，捞不到就明说「证据里没写」。"""
        evidence = ticket.get("接线证据") or {}
        haystack = "\n".join(
            str(evidence.get(key, "")) for key in ("文字", "验证命令", "原样输出")
        )
        branches = list(dict.fromkeys(MEMORY_BRANCH_PATTERN.findall(haystack)))
        commits = list(dict.fromkeys(MEMORY_COMMIT_PATTERN.findall(haystack)))
        parts = []
        if branches:
            parts.append("分支 " + "、".join(branches[:3]))
        if commits:
            parts.append("提交 " + "、".join(commits[:3]))
        return " · ".join(parts) if parts else "证据里没写"

    @staticmethod
    def _memory_fit(
        head: list[str], blocks: list[tuple[str, list[str]]], max_lines: int, archive: Path,
    ) -> tuple[list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
        """总行数封顶：超了就把**最旧**的条目往归档件挪，直到装得下。

        ★这是为了 300K 的员工窗装得下。头（含第 0 步）永远不挪：第 0 步被挪走的
        记忆件比没有记忆件更坏——下一窗会照着过期的分支头直接干活。
        """
        if max_lines <= 0:
            raise TicketError("--max-lines 必须是大于 0 的整数。")
        kept = list(blocks)
        dropped: list[tuple[str, list[str]]] = []
        # 归档提示自己也占两行，挪走第一条的那一刻就要把这两行算进去。
        while kept and len(head) + 2 + sum(len(lines) for _, lines in kept) > max_lines:
            dropped.append(kept.pop(0))
        return kept, dropped

    def refresh_slot_memory(self, ticket: dict[str, Any]) -> str:
        """submit / judge 落库之后自动重刷一次记忆件；成败都只回一行文字。

        ★硬要求：重刷失败**不许**把 submit/judge 弄失败。工具的附加动作不能反过来
        卡住员工交板——路径写错、目录不在、盘符不存在，都是「记一行、继续走」。
        """
        name = str(ticket.get("指派给", "")).strip()
        if not name or not self.staff_memory_path(name):
            return ""
        try:
            path = self.memory_export(name)
        except Exception as exc:  # noqa: BLE001 —— 附加动作不许卡住交板，什么都得吞
            return f"{MEMORY_REFRESH_PREFIX}失败:{exc}"
        return f"{MEMORY_REFRESH_PREFIX}完成:{path}"

    @staticmethod
    def _default_workspace_root() -> Path:
        """任务书落盘的根。先找**已经存在**的 `_office/`，找不到才退到配置值。

        ★先找已存在的目录是有意的:一个人常常同时开着主检出与几个 worktree,
          它们各自的上一级不一定是同一个——照配置硬拼,任务书会落到一个
          谁都不会去看的新目录里,而命令本身「成功」了。
        """
        for candidate in (config.repo_root(), *config.repo_root().parents):
            if (candidate / "_office").is_dir():
                return candidate
        return config.workspace_root()

    def build_bundle(self, output: Path | None = None) -> Path:
        path = output or config.repo_root() / "web" / "data" / "tickets-bundle.js"
        threads = {slot: self.store.read_jsonl(self.store.thread_path(slot)) for slot in SLOTS}
        bundle = {
            "slots": self.store.read_json(self.store.slots_path),
            "staff": self.store.read_json(self.store.staff_path),
            "items": self.list_tickets(),
            "threads": threads,
            "log": self.store.read_jsonl(self.store.log_path),
            "modelStats": self.model_statistics(),
            "state": self.state_board(),
        }
        payload = f"window.{config.BUNDLE_GLOBAL} = " + json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + ";\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
        return path

    @staticmethod
    def receipt(ticket: dict[str, Any]) -> str:
        """员工开工第一条命令的回执；返工态时把「上一轮为什么被退」一并带进新窗。

        开窗指令只有三行、只带任务书路径,判退的判语只留在卡片上——新窗看不见,
        照着旧任务书又交一模一样的板,空转两轮(T-000282)。D9-388 之后判语首行
        写的是责任归属(模型/出题),员工更该看见:他要知道这一次到底是不是他的错。
        ★判语原样贴,不截断不改写;★只有返工态才追加——receipt 是每次开工都要跑的,
        其余状态一个字都不能加,否则它会变成一堵没人读的墙。
        ★网页与 CLI 消费的都是这里生成的同一份文本,别在前端另拼一份(两处各拼必然漂)。
        """
        line = f"已进入工单 {ticket['编号']} · {ticket['标题']} · {ticket['状态']}"
        # T-001256 ③:任务书「待回核」是账面事,不拦人。员工照常 claim 照常做,
        # 只在这里提一行、让他知道这一格还没核上,顺手能提醒总监补一次(不必停车等)。
        if str(ticket.get("任务书校验", "")).strip() == "待回核":
            line += (
                "\n提示:这张单的任务书是「待回核」——建单那台机器当时核不到那个路径而已,"
                f"不影响你开工。请总监顺手补一次 set {ticket['编号']} --taskbook <同一个路径>,"
                "补完自动转「已核存在」;你不用等它。"
            )
        if ticket.get("状态") != "返工":
            return line
        rows = ticket.get("返工原因列表") or []
        last = rows[-1] if rows else {}
        times = int(ticket.get("返工次数") or len(rows) or 1)
        judge = str(last.get("判卷人", "")).strip()
        verdict = str(ticket.get("判语", "")).strip()
        reason = str(last.get("原因", "")).strip()
        parts = [
            line,
            f"── 上一轮为什么被退 · 第 {times} 次判退{f' · 判卷:{judge}' if judge else ''} ──",
        ]
        parts.append(f"判语(原样):\n{verdict}" if verdict else "判语(原样):(这一次判退没有留下判语)")
        if reason:
            parts.append(f"返工原因:{reason}")
        parts.append("按上面这段改;任务书若已换,以卡片上的新路径为准。")
        return "\n".join(parts)

    # ── 当前值面：读写口子在这里，值一个都不预置 ────────────────────────────
    def _state_raw(self) -> dict[str, Any]:
        value = self.store.read_json(self.store.state_path, {})
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def desk_config() -> dict[str, Any]:
        """网页台面开机时取的那一份配置。

        ★为什么要有这个接口:这些值原来在前端各写一份,注释里靠一句「与服务端同步修改」约束。
          那句注释拦不住任何人——位名、停滞阈值、实机来源标签都真的漂开过,
          而漂开的症状是**页面安静地显示错的东西**:少一位,那一位的单在离线模式下整个看不见,
          页面一个字都不报。现在前端只在**读不到服务端**时才用自带的兜底值。
        ★只发口径,不发数据:这一趟必须小且可缓存,它在每次开机时都要跑。
        """
        return {
            "产品名": config.PRODUCT_NAME,
            "位名": list(SLOTS),
            "只分发不派单位": list(DISPATCH_FORBIDDEN_SLOTS),
            "总编排位": CONDUCTOR_SLOT,
            "复检位": REVIEW_SLOT,
            "平台位": PLATFORM_SLOT,
            "拍板人": OWNER_ROLE,
            "开窗平台": list(config.WINDOW_PLATFORMS),
            "主场景": LIVE_SCENE,
            # 三个来源键与 ORIGIN_MAP 同一份,前端的下拉框直接照它渲染。
            "实机来源": dict(ORIGIN_MAP),
            "值面标签": [label for label, _ in STATE_ITEMS],
            # 停滞阈值与终态:前端画红条、算「卡住了」都要用,原来是抄一份在 js 里。
            "停滞阈值": dict(STALE_STATE_HOURS),
            "非停滞态": sorted(NON_STALE_STATES),
            "新建已开窗阈值": 4,
            # 开窗指令第一行贴的那条路径;催办句里也要用。
            "命令行路径": config.cli_path(),
            # 任务书路径输入框的 placeholder 按这个拼:<工作区>/_office/<位名>/任务书/<单号>_<主题>.md
            "任务书目录": str(config.workspace_root() / "_office"),
            "离线包全局名": config.BUNDLE_GLOBAL,
        }

    def state_board(self) -> dict[str, Any]:
        """机器可读的一份当前值面：值、每项最近一次改动、还没人填的键、顶栏用的四项。"""
        raw = self._state_raw()
        values: dict[str, Any] = {}
        changes: dict[str, Any] = {}
        missing: list[str] = []
        for key in STATE_KEYS:
            row = raw.get(key)
            if not isinstance(row, dict) or "值" not in row:
                values[key], changes[key] = None, None
                missing.append(key)
                continue
            values[key] = row["值"]
            changes[key] = {
                "改动人": str(row.get("改动人", "")),
                "时间": str(row.get("时间", "")),
                "旧值": row.get("旧值"),
                "新值": row["值"],
            }
        items = [
            {
                "标签": label,
                "键": list(keys),
                "文本": "/".join(state_text(values[key]) for key in keys),
                "已填": all(values[key] is not None for key in keys),
                "最近改动": {key: changes[key] for key in keys},
                "最近改动文本": self._state_change_text(keys, changes),
            }
            for label, keys in STATE_ITEMS
        ]
        return {"值": values, "最近改动": changes, "未填": missing, "项": items}

    @staticmethod
    def _state_change_text(keys: tuple[str, ...], changes: dict[str, Any]) -> str:
        lines = [
            f"{key}:{change['改动人']} {change['时间']} {state_text(change['旧值'])} → {state_text(change['新值'])}"
            for key in keys if (change := changes.get(key))
        ]
        return "\n".join(lines) if lines else "还没有人填过这一项"

    def state_summary_line(self) -> str:
        """压成一行的当前值面；员工窗第 0 步跑 receipt 就能看见，不必再去读接管件。"""
        board = self.state_board()
        body = " · ".join(f"{row['标签']} {row['文本']}" for row in board["项"])
        return f"当前值面:{body} · 改动只认 {STATE_WRITERS[0]}/{STATE_WRITERS[1]};读它别抄它"

    def state_set(self, key: str, raw_value: str, actor: str) -> dict[str, Any]:
        name = str(actor or "").strip()
        if name not in STATE_WRITERS:
            raise TicketError(
                f"不能改当前值面：只有 {STATE_WRITERS[0]} 与 {STATE_WRITERS[1]} 可以写，"
                f"「{name or '（空）'}」不行。别位与员工读 state get 就好。"
            )
        if key not in STATE_KEYS:
            raise TicketError(
                f"没有 {key} 这一项。合法键只有这几个：" + "、".join(STATE_KEYS) + "。"
            )
        value = parse_state_value(key, raw_value)
        with self.store.locked():
            raw = self._state_raw()
            existing = raw.get(key)
            previous = existing.get("值") if isinstance(existing, dict) else None
            stamp = now_text()
            raw[key] = {"值": value, "改动人": name, "时间": stamp, "旧值": previous}
            self.store.atomic_json(self.store.state_path, raw)
            self.store.append_jsonl(self.store.log_path, {
                "时间": stamp, "工单号": "", "事件": "state-set", "发言人": name, "状态": "",
                "说明": f"{key}：{state_text(previous)} → {state_text(value)}", "事件序号": 0,
                "值面键": key, "旧值": previous, "新值": value,
            })
        return {"键": key, "旧值": previous, "新值": value, "改动人": name, "时间": stamp}

    def confirm_taskbook(self, ticket_id: str, actor: str) -> dict[str, Any]:
        """接收提交端的存在性结论；服务器本身不读取提交端路径。"""
        ticket = self.store.load_ticket(ticket_id)
        allowed = {str(ticket.get("发起人", "")), str(ticket.get("所属总监位", "")), CONDUCTOR_SLOT}
        if actor not in allowed:
            raise TicketError(f"不能回核 {ticket['编号']}：只有发起人、该位总监或总编排可以写回任务书校验。")
        if ticket.get("任务书校验") == "已核存在":
            return ticket
        ticket["任务书校验"] = "已核存在"
        self.store.save_ticket(ticket, "taskbook-checked", actor, "提交端已核任务书存在")
        return ticket

    def open_window(
        self, ticket_id: str, actor: str, actual_model: str, actual_platform: str = "",
    ) -> tuple[dict[str, Any], str]:
        """设计者登记实际模型，并在服务端记录本轮已开窗标记。

        「实际平台」是可选的：不填就是空串，任何现有行为都不受影响。
        以后要按平台看合格率（哪个窗跑什么活更容易判过），得先有人记这一格。
        """
        ticket = self.store.load_ticket(ticket_id)
        self._require_dispatch(ticket)
        if actor != OWNER_ROLE:
            raise TicketError(f"只有{OWNER_ROLE}可以登记已开窗的实际模型。")
        if ticket["状态"] not in {"新建", "已认领", "返工"}:
            raise TicketError(f"不能登记已开窗：{ticket['编号']} 现在是「{ticket['状态']}」。")
        model = actual_model.strip().lower()
        if not model or model == "待定":
            raise TicketError("实际模型不能为空，也不能仍填“待定”。")
        platform = normalize_window(actual_platform)
        assigned = str(ticket.get("指派给", ""))
        self.require_active_staff(str(ticket["所属总监位"]), assigned)
        staff = self.store.load_staff()
        self._assert_model_not_banned(staff, model, str(ticket["所属总监位"]), "登记实际模型")
        found = self._find_staff_in(staff, assigned, str(ticket["所属总监位"]))
        if not found:  # pragma: no cover - require_active_staff 已给出人话错误
            raise TicketError(f"员工名册里没有 {assigned}。")
        found[1]["工具/窗类型"] = model
        found[1]["最近模型写回时间"] = now_text()
        ticket["实际模型"] = model
        round_number = int(ticket.get("返工次数", 0))
        ticket["已开窗"] = {"轮次": round_number, "时间": now_text(), "实际模型": model, "实际平台": platform}
        warning = ""
        capacity = self._model_task_tier(model)
        ranks = {"丙": 1, "乙": 2, "甲": 3}
        if (
            capacity in ranks
            and ticket.get("任务档") in ranks
            and ranks[capacity] < ranks[str(ticket["任务档"])]
            and not ticket.get("模型低档提醒过", False)
        ):
            warning = (
                f"提醒：实际模型 {model} 低于本单{ticket['任务档']}档。"
                "任务档是下限，本次不拦；请留意交付质量。"
            )
            ticket["模型低档提醒过"] = True
        self.store.save_staff(staff)
        self.store.save_ticket(
            ticket, "window-opened", actor, f"已开窗·第 {round_number} 轮；实际模型：{model}",
            {"实际模型": model, "提示": warning},
        )
        return ticket, warning

    def audit_taskbook_skip(self, ticket: dict[str, Any], actor: str, command: str) -> None:
        """逃生口只跳过文件检查，不跳过审计。"""
        self.store.append_jsonl(self.store.log_path, {
            "时间": now_text(),
            "工单号": ticket["编号"],
            "事件": "taskbook-unchecked",
            "发言人": actor,
            "状态": ticket["状态"],
            "说明": f"{command} 跳过提交端任务书存在性校验",
            "事件序号": ticket.get("事件序号", 0),
            "命令": command,
            "任务书绝对路径": ticket.get("任务书路径", ""),
        })

    def audit_deliverable_skip(
        self, ticket: dict[str, Any], actor: str, command: str, rows: list[str] | str | None,
    ) -> None:
        """交付项逃生口只跳过提交端校验，逐条留下原文与本机绝对路径。"""
        skipped = normalize_lines(rows)
        self.store.append_jsonl(self.store.log_path, {
            "时间": now_text(),
            "工单号": ticket["编号"],
            "事件": "deliverable-unchecked",
            "发言人": actor,
            "状态": ticket["状态"],
            "说明": f"{command} 跳过提交端交付项形态与存在性校验",
            "事件序号": ticket.get("事件序号", 0),
            "命令": command,
            "跳过的交付项": skipped,
            "核验绝对路径": [str(Path(deliverable_candidate(row)).expanduser().resolve()) for row in skipped],
        })

    @staticmethod
    def _powershell_literal(value: Any) -> str:
        """生成可直接粘贴到本项目 Windows 工位 PowerShell 的单引号参数。"""
        return "'" + str(value).replace("'", "''") + "'"

    @classmethod
    def missing_deliverables_error(cls, ticket: dict[str, Any], missing: list[str]) -> str:
        """列全改单参数，并把缺失项另列在命令下方，兼容 PowerShell 与 Bash。"""
        rows = normalize_lines(ticket.get("交付项"))
        script = Path(__file__).with_name("ticket.py").resolve()
        command = [
            "python", cls._powershell_literal(script), "set", cls._powershell_literal(ticket.get("编号", "")),
        ]
        for row in rows:
            command.extend(["--deliverable", cls._powershell_literal(row)])
        owner = str(ticket.get("所属总监位", ""))
        command.extend(["--by", cls._powershell_literal(owner)])
        return (
            "不能交板：以下交付项找不到对应文件：\n"
            + "\n".join(f"- {row}" for row in missing)
            + f"\n交付项只能由本位总监或总编排改(你没有这个权限,这是故意的)。请把下面这条原样发给 {owner}:\n"
            + " ".join(command)
            + "\n核不到的交付项（请在上面的命令里改这几条）：\n"
            + "\n".join(f"- {row}" for row in missing)
        )

    def require_active_staff(self, slot: str, name: str) -> dict[str, Any]:
        match = STAFF_PATTERN.fullmatch(name)
        if not match or match.group("slot") != slot:
            raise TicketError(f"员工名格式或所属位不对：{name}。应为“{slot}-两位编号”。")
        found = self.find_staff(name, slot)
        if not found:
            raise TicketError(f"员工名册里没有 {name}，请先用 staff new 登记。")
        member = found[1]
        if member["状态"] != "在岗":
            raise TicketError(f"{name} 已收窗，不能认领或被指派；修本人 BUG 才可 staff reopen。")
        return member

    def strict_review_notice(self, ticket: dict[str, Any]) -> str:
        found = self.find_staff(ticket.get("指派给", "")) if ticket.get("指派给") else None
        first_ticket = bool(found and len(found[1].get("经手工单号列表", [])) <= 1)
        if first_ticket or ticket.get("任务档", "乙") in {"乙", "丙"}:
            return "首检从严:逐行核,自己重跑验证命令"
        return ""

    def model_statistics(self) -> list[dict[str, Any]]:
        staff = self.store.load_staff()
        people = {member["员工名"]: (slot, member.get("工具/窗类型", "未知")) for slot, group in staff.get("总监位", {}).items() for member in group.get("员工", [])}
        stats: dict[tuple[str, str], dict[str, int]] = {}
        tickets = {ticket["编号"]: ticket for ticket in self.store.list_tickets()}
        for event in self.store.read_jsonl(self.store.log_path):
            ticket = tickets.get(event.get("工单号"), {})
            person = people.get(ticket.get("指派给", ""))
            model = normalize_model_name(event.get("记账模型"))
            if not model:
                actual_model = event.get("实际模型") if "实际模型" in event else ticket.get("实际模型")
                model, _ = self._accounting_model(
                    actual_model, person[1] if person else "",
                )
            if not model:
                continue
            task_tier = str(event.get("任务档") or ticket.get("任务档") or "未标")
            row = stats.setdefault((model, task_tier), {"交板数": 0, "判过": 0, "判退": 0})
            if event.get("事件") == "submit":
                row["交板数"] += 1
            elif event.get("事件") == "judge-pass":
                row["判过"] += 1
            elif event.get("事件") == "judge-rework":
                row["判退"] += 1
        bans = staff.get("模型停用", {})
        global_bans = {normalize_model_name(value) for value in bans.get("全项目", [])}
        local_bans = {
            normalize_model_name(value)
            for values in bans.get("按位", {}).values()
            for value in values
        }
        models = (
            {model for model, _ in stats}
            | {normalize_model_name(value) for value in staff.get("模型记分", {})}
            | global_bans
            | local_bans
        )
        for model in models:
            if not any(key_model == model for key_model, _ in stats):
                stats[(model, "未标")] = {"交板数": 0, "判过": 0, "判退": 0}
        output = []
        for model, task_tier in sorted(stats):
            row = stats[(model, task_tier)]
            judged = row["判过"] + row["判退"]
            rate = f"{row['判过'] * 100 / judged:.1f}%" if judged else "—"
            if model in global_bans:
                status = "全项目停用"
            elif model in local_bans:
                status = "本位停用"
            else:
                status = "可用"
            output.append({"模型": model, "任务档": task_tier, **row, "合格率": rate, "状态": status})
        return output

    def find_staff(self, name: str, slot: str | None = None) -> tuple[str, dict[str, Any]] | None:
        return self._find_staff_in(self.store.load_staff(), name, slot)

    @staticmethod
    def _find_staff_in(staff: dict[str, Any], name: str, slot: str | None = None) -> tuple[str, dict[str, Any]] | None:
        slots = [slot] if slot else list(staff.get("总监位", {}))
        for slot_name in slots:
            for member in staff.get("总监位", {}).get(slot_name, {}).get("员工", []):
                if member.get("员工名") == name:
                    return slot_name, member
        return None

    def _add_staff_history(self, name: str, ticket_id: str) -> None:
        if not name:
            return
        staff = self.store.load_staff()
        found = self._find_staff_in(staff, name)
        if not found:
            return
        _, member = found
        if ticket_id not in member.setdefault("经手工单号列表", []):
            member["经手工单号列表"].append(ticket_id)
            self.store.save_staff(staff)

    @staticmethod
    def _accounting_model(actual_model: Any, tool: Any) -> tuple[str, bool]:
        actual = normalize_model_name(actual_model)
        if actual and actual != "待定":
            return actual, False
        fallback = normalize_model_name(tool)
        if not fallback or fallback == "待定":
            return "", False
        return f"{fallback}-未标", True

    def _accounting_model_for_ticket(self, ticket: dict[str, Any]) -> tuple[str, bool]:
        found = self.find_staff(ticket.get("指派给", ""))
        tool = found[1].get("工具/窗类型", "") if found else ""
        return self._accounting_model(ticket.get("实际模型"), tool)

    @staticmethod
    def _roster_model_names(slots: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        for row in slots.get("模型名册") or []:
            if isinstance(row, str):
                base = normalize_model_name(row)
                levels: list[Any] = []
            else:
                base = normalize_model_name(row.get("模型"))
                levels = list(row.get("可选档位") or [])
            if not base:
                continue
            names.add(base)
            names.update(f"{base}-{level}" for level in map(normalize_model_name, levels) if level)
        return names

    def _record_question_rework(self, ticket: dict[str, Any]) -> None:
        owner = str(ticket.get("所属总监位") or "未标")
        staff = self.store.load_staff()
        score = staff.setdefault("出题记分", {}).setdefault(owner, {"合计": 0})
        score[owner] = int(score.get(owner, 0)) + 1
        score["合计"] = int(score.get("合计", 0)) + 1
        self.store.save_staff(staff)

    def _adjust_model_score(self, staff: dict[str, Any], ticket: dict[str, Any], delta: int) -> str:
        """给这张单对应的「模型 × 位」记分加减一笔，返回记到哪个模型键上（记不出来就返回空串）。

        判退当时按哪个键记的，回滚就得按同一个键减，所以模型与位一律照 _record_model_rework
        的老算法重算一遍，不另存一份。
        """
        found = self.find_staff(ticket.get("指派给", ""))
        if not found:
            return ""
        slot, _ = found
        model, _ = self._accounting_model_for_ticket(ticket)
        if not model:
            return ""
        score = staff.setdefault("模型记分", {}).setdefault(model, {"合计": 0})
        # 不许减成负数：判退那一笔可能是在 digest 归档清账之前记的，这边再减就会记出 -1，
        # 停用线从此永远比真实少读一次。已经是 0 就保持 0。
        score[slot] = max(0, int(score.get(slot, 0)) + delta)
        score["合计"] = max(0, int(score.get("合计", 0)) + delta)
        return model

    def _adjust_question_score(self, staff: dict[str, Any], ticket: dict[str, Any], delta: int) -> str:
        """给所属总监位的出题账加减一笔，返回记在哪一位上。键的形状照 _record_question_rework。"""
        owner = str(ticket.get("所属总监位") or "未标")
        score = staff.setdefault("出题记分", {}).setdefault(owner, {"合计": 0})
        score[owner] = max(0, int(score.get(owner, 0)) + delta)
        score["合计"] = max(0, int(score.get("合计", 0)) + delta)
        return owner

    @staticmethod
    def _roster_base_names(slots: dict[str, Any]) -> list[str]:
        """名册里的模型基名,长的在前:归并变体时先试更具体的名字(如 codely-glm-5.3-flash 先于 glm-5.3)。"""
        names: set[str] = set()
        for row in slots.get("模型名册") or []:
            base = normalize_model_name(row if isinstance(row, str) else row.get("模型"))
            if base:
                names.add(base)
        return sorted(names, key=len, reverse=True)

    @staticmethod
    def _model_base_name(value: str, base_names: list[str]) -> tuple[str, bool]:
        """把一个记账模型键归并到名册基名,返回 (基名, 是否未标)（T-000794 R1）。

        opus / opus5 / opus-high / opus xhigh 一律归并成 opus,sol 同理;`<工具>-未标`
        归到工具名下,未标这笔回答调用方要在通知里标出来。照名册前缀匹配,不写死变体表;
        名册里没有的按字面返回。
        """
        normalized = normalize_model_name(value)
        unmarked = normalized.endswith("-未标")
        if unmarked:
            normalized = normalized[: -len("未标")].strip("-")
        for name in base_names:
            if normalized.startswith(name):
                return name, unmarked
        return normalized, unmarked

    def _model_blame_rows(self, ticket: dict[str, Any], base_names: list[str]) -> list[dict[str, Any]]:
        """一张工单里 判退责任=模型 的返工条目,折成停用线的逐笔证据（T-000794 R1）。

        出题、空字符串、其他值一律不计;返工次数为 0 的单一条都不贡献。
        """
        found = self.find_staff(ticket.get("指派给", ""))
        slot = found[0] if found else (str(ticket.get("所属总监位") or "未标") or "未标")
        model, _ = self._accounting_model_for_ticket(ticket)
        if not model:
            return []
        base, unmarked = self._model_base_name(model, base_names)
        tier = str(ticket.get("任务档") or "未标") or "未标"
        rows: list[dict[str, Any]] = []
        for entry in ticket.get("返工原因列表") or []:
            blame = str(entry.get("判退责任") or entry.get("责任") or "").strip()
            if blame != "模型":
                continue
            rows.append({"单号": str(ticket.get("编号", "")), "位": slot, "任务档": tier, "基名": base, "未标": unmarked})
        return rows

    def _record_model_rework(self, ticket: dict[str, Any], actor: str = "") -> str:
        found = self.find_staff(ticket.get("指派给", ""))
        if not found:
            return ""
        slot, _ = found
        model, actual_missing = self._accounting_model_for_ticket(ticket)
        if not model:
            return ""
        staff = self.store.load_staff()
        score = staff.setdefault("模型记分", {}).setdefault(model, {"合计": 0})
        score[slot] = int(score.get(slot, 0)) + 1
        score["合计"] = int(score.get("合计", 0)) + 1
        slots = self.store.read_json(self.store.slots_path)
        limits = slots.get("停用阈值", {"同位": 3, "全项目": 5})
        bans = staff.setdefault("模型停用", {"全项目": [], "按位": {name: [] for name in SLOTS}})
        notices = []
        conductor_notices = []
        if actual_missing:
            notices.append(f"这张单没填实际模型，已按 {model} 单独记账。")
        if model not in self._roster_model_names(slots):
            notices.append(f"模型名 {model} 不在名册里，已按字面记账。")
        # D9-388 / T-000529(答「乙」):自动写 bans 关掉。
        # 宪法八章 10 原文是「跨位累计 5 次判退,全项目停用,总编排落笔」——落笔是人的动作,
        # 工具自动写停用是实现时加的,不是宪法要求。今天它两次把 sol 全项目停掉,
        # 8 次判退里 2 次是出题责任、3 次是变体被归并,停用是误判,而一停就是所有位停摆。
        # 现在:到阈值-1 与到阈值各往总编排线与所属位线写一行,bans 一律不写,停不停由总编排核过责任归属后落 D9。
        # ★停用线与「模型记分」是两套数,谁也别拿谁当谁(T-000794):模型记分是给合格率与
        #   digest 看的累计账,按记账模型键记所有判退,一行都不改;停用线在下面从工单现算,
        #   只数 判退责任=模型 的条目,按模型基名+任务档归并。曾把 opus 乙档误报到
        #   停用线,就是把账本累计直接对上了阈值——账本里混着出题责任与旧口径的旧账。
        owner = str(ticket.get("所属总监位") or slot)
        tier = str(ticket.get("任务档") or "未标")
        same_limit = int(limits.get("同位", 3))
        all_limit = int(limits.get("全项目", 5))
        base_names = self._roster_base_names(slots)
        base, _ = self._model_base_name(model, base_names)
        rows: list[dict[str, Any]] = []
        for other in self.store.list_tickets():
            if other.get("编号") == ticket.get("编号"):
                continue  # 本单以内存里的为准:库里还没存上本次刚记的这一笔,漏了就会少数一次
            rows.extend(self._model_blame_rows(other, base_names))
        rows.extend(self._model_blame_rows(ticket, base_names))
        cell = [row for row in rows if row["基名"] == base and row["任务档"] == tier]
        same_count = sum(1 for row in cell if row["位"] == slot)
        all_count = len(cell)
        details = "\n".join(
            f"{row['单号']} · {row['位']} · {row['任务档']} · 模型{'(未标)' if row['未标'] else ''}" for row in cell
        )
        if details:
            details = "计入的每一笔(单号 · 所属位 · 任务档 · 责任字段):\n" + details
        main_models = {normalize_model_name(value) for value in slots.get("主力模型集合") or TicketStore.MAIN_MODELS}
        head = f"模型 {base}({tier}档)在「{owner}」"
        if base in main_models:
            # R1.5(口径见 staff_ban):主力模型永不停用,到线也只作质量提示。
            hint = (
                f"{head}记模型责任判退 {same_count} 次,跨位 {all_count} 次(本次 {ticket['编号']})。"
                "主力模型不停用,这条只作质量提示,请核一遍归属是否记对(判错用 set --blame 更正)。"
            )
            if same_count == same_limit - 1 or all_count == all_limit - 1 or same_count >= same_limit or all_count >= all_limit:
                notices.append(hint + ("\n" + details if details else ""))
        else:
            if same_count == same_limit - 1 or all_count == all_limit - 1:
                notices.append(
                    head + f"累计判退 {same_count} 次,跨位累计 {all_count} 次(本次 {ticket['编号']})"
                    + ";再判退 1 次就到停用线。若其中有出题责任判错了归属,现在用判语首行「出题责任」纠正还来得及。"
                    + ("\n" + details if details else "")
                )
            if same_count >= same_limit or all_count >= all_limit:
                notices.append(
                    head + f"累计判退 {same_count} 次,跨位累计 {all_count} 次(本次 {ticket['编号']})"
                    + f";已到停用线。自动停用已关(D9-388),模型仍算可用;停不停由{CONDUCTOR_SLOT}核过责任归属后落 D9 拍板。"
                    + ("\n" + details if details else "")
                )
        if notices:
            self._notify_slots((CONDUCTOR_SLOT, owner), str(ticket.get("判卷人") or "工单台"), "\n".join(notices), str(ticket["编号"]))
        self.store.save_staff(staff)
        for text in conductor_notices:
            self._notify_slots((CONDUCTOR_SLOT,), actor or str(ticket.get("判卷人") or slot), text, ticket.get("编号", ""))
        return "\n".join(notices)

    def _model_task_tier(self, actual_model: str) -> str:
        value = normalize_model_name(actual_model)
        roster = self.store.read_json(self.store.slots_path).get("模型名册", [])
        for row in sorted(roster, key=lambda item: len(str(item.get("模型", ""))), reverse=True):
            name = normalize_model_name(row.get("模型"))
            if value == name or value.startswith(name + "-"):
                level = value[len(name):].strip("-").split("-", 1)[0]
                tier_map = {normalize_model_name(key): tier for key, tier in (row.get("档位任务档") or {}).items()}
                if level in tier_map:
                    return str(tier_map[level])
                return str(row.get("任务档上限", ""))
        return ""

    @staticmethod
    def _render_c_tier_export(ticket: dict[str, Any]) -> str:
        template_path = Path(__file__).resolve().parent / "templates" / "丙档任务书模板.md"
        if not template_path.is_file():
            raise TicketError(f"找不到丙档任务书模板：{template_path}")
        template = template_path.read_text(encoding="utf-8-sig")
        sources = ticket.get("真源指针", [])
        source_rows = "\n".join(f"{index}. {value}            只读真源" for index, value in enumerate(sources, 1)) or "1. （工单未填写真源指针，禁止执行）"
        task = ticket.get("正文") or ticket.get("备注") or ticket["标题"]
        consumer = ticket.get("实机消费者") or "（工单未填写实机消费者，禁止执行）"
        evidence = ticket.get("接线证据", {}).get("文字") or f"附一张来源标注为 {LIVE_ORIGIN} 的图片，再执行 submit。"
        text = template.replace("T-______", ticket["编号"]).replace("<一句话标题>", ticket["标题"]).replace("<标题>", ticket["标题"])
        text = text.replace("<位名-编号>", ticket.get("指派给") or "未指派").replace("<位名>", ticket["所属总监位"])
        text = re.sub(r"<例:把 <某目录> 下 28 个文件的文件名、大小、类型写成一张 CSV。>", lambda _: task, text)
        text = re.sub(r"1\. <绝对路径>\s+<用途,读哪几行>\n2\. <绝对路径>\s+<用途>", lambda _: source_rows + f"\n# 合计预算：{ticket.get('上下文预算')} 行", text)
        text = re.sub(r"1\. <绝对路径>\s+<内容规格:列名 / 格式 / 编码 UTF-8>", lambda _: f"1. {consumer}            按工单标题完成接线或产物", text)
        text = re.sub(r"1\. <命令或动作,写死>\n2\. <命令或动作,写死>\n3\. 验证:运行 <命令>,期望输出 <原样写出>。不一致就停下,把实际输出原样写进 §5 回执,不要自己修。", lambda _: f"1. {task}\n2. 验证要求：{evidence}\n3. 验证：运行工单指定验证命令；必须附 {LIVE_ORIGIN} 图，不一致就停下并原样回报。", text)
        text = text.replace("<§3 第 1 个路径>", consumer)
        return text

    @staticmethod
    def _require_dispatch(ticket: dict[str, Any]) -> None:
        if ticket["类型"] != "派单":
            raise TicketError(f"{ticket['编号']} 是“{ticket['类型']}”，不走派单状态机。")

    def _actor_slot(self, actor: str) -> str:
        if actor in SLOTS:
            return actor
        found = self.find_staff(actor) if actor else None
        return found[0] if found else ""

    @staticmethod
    def _validate_decision_body(body: str) -> None:
        positions = [body.find(header) for header in DECISION_HEADERS]
        if any(position < 0 for position in positions) or positions != sorted(positions):
            raise TicketError(DECISION_GATE_REASON)
        for index, header in enumerate(DECISION_HEADERS):
            start = positions[index] + len(header)
            end = positions[index + 1] if index + 1 < len(positions) else len(body)
            content = body[start:end].strip(" \t\r\n:：")
            if not content:
                raise TicketError(DECISION_GATE_REASON)
        without_parentheses = re.sub(r"（[^（）]*）|\([^()]*\)", "", body)
        if len(PROFESSIONAL_ID_PATTERN.findall(without_parentheses)) > 3:
            raise TicketError(DECISION_GATE_REASON)

    def _missing_deliverables(
        self, ticket: dict[str, Any], rows: list[str], check_filesystem: bool = True
    ) -> list[str]:
        image_names = {
            str(image.get("文件名", ""))
            for image in ticket.get("图片列表", [])
            if image.get("文件名") and (self.store.images_dir / str(image["文件名"])).is_file()
        }
        # T-001256 ①:按**去扩展名**的编号比对。交付项写 T-000979-01.jpg、台面存 T-000979-01.webp
        # 是工单台自己的压缩管线决定的(有没有透明通道),员工建单时猜不到,不该为此停一窗。
        image_keys = {deliverable_key(name) for name in image_names if name}
        missing: list[str] = []
        for row in rows:
            candidate = deliverable_candidate(row)
            if any(filename and filename in candidate for filename in image_names):
                continue
            if (self.store.images_dir / candidate).is_file():
                continue
            if is_image_deliverable(candidate):
                # 图片类：附件不在就是真缺，跟哪台机器提交无关。
                # ——但「同一张图换了个扩展名」不算缺,那是写法不是活(T-000979)。
                if deliverable_key(candidate) in image_keys:
                    continue
                missing.append(row)
                continue
            if not check_filesystem:
                # 文件系统类：提交端已经在自己那台机器上核过，这里不再重核。
                continue
            if Path(candidate).is_file() or (self.store.root / candidate).is_file():
                continue
            missing.append(row)
        return missing

    @staticmethod
    def _parse_deliverable_check(value: str, rows: list[str]) -> str:
        """核对提交端报上来的核验结论，对不上就不认。"""
        value = str(value or "").strip()
        if not value:
            return ""
        matched = re.fullmatch(r"(\d+)/(\d+)", value)
        if not matched:
            raise TicketError(f"交付项核验结论写法不对：{value}，应为“齐/共”，例如 6/6。")
        done, total = int(matched.group(1)), int(matched.group(2))
        if total != len(rows):
            raise TicketError(
                f"交付项核验结论对不上：报的是 {total} 项，这张单上写着 {len(rows)} 项。"
                "请重新拉一次工单再交板。"
            )
        if done != total:
            raise TicketError(f"交付项核验没过：{done}/{total} 齐，先把缺的补齐再交板。")
        return value

    @staticmethod
    def _require_state(ticket: dict[str, Any], state: str, message: str) -> None:
        if ticket["状态"] != state:
            raise TicketError(f"{message} 当前状态是“{ticket['状态']}”。")

    @staticmethod
    def _world_images(ticket: dict[str, Any]) -> list[dict[str, Any]]:
        return [image for image in ticket.get("接线证据", {}).get("图片列表", []) if image.get("来源标注") == LIVE_ORIGIN]

    def _compress_ticket_image(self, ticket: dict[str, Any], source: Path, origin: str, uploader: str) -> dict[str, str]:
        index = len(ticket.get("图片列表", [])) + 1
        stem = f"{ticket['编号']}-{index:02d}"
        filename = self._write_compressed_image(source, stem)
        return image_record(filename, str(source.resolve()), origin, uploader)

    def _compress_thread_image(self, slot: str, source: Path, uploader: str) -> dict[str, str]:
        slot_key = hashlib.sha1(slot.encode("utf-8")).hexdigest()[:8]
        stem = f"THREAD-{slot_key}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        filename = self._write_compressed_image(source, stem)
        return image_record(filename, str(source.resolve()), "其他", uploader)

    def _write_compressed_image(self, source: str | Path | bytes, stem: str) -> str:
        filename, data = compress_image(source, stem)
        self.store.save_image(self.store.images_dir, filename, data)
        return filename

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.astimezone()
        except (TypeError, ValueError):
            return datetime.min.astimezone()

    @staticmethod
    def _validate_task_tier(task_tier: str, context_lines: int | None) -> None:
        if task_tier not in {"甲", "乙", "丙"}:
            raise TicketError("任务档只可填甲、乙或丙。")
        if context_lines is not None and (not isinstance(context_lines, int) or context_lines < 0):
            raise TicketError("上下文预算必须是大于等于 0 的整数，单位为行。")
        if task_tier == "丙" and context_lines is None:
            raise TicketError("丙档不能建单：必须填写上下文预算，单位为行，且不超过 2000。")
        if task_tier == "丙" and context_lines > 2000:
            raise TicketError(f"丙档不能建单：上下文预算是 {context_lines} 行，超过 2000 行上限；请缩小范围或升为乙档。")
