"""工单数据模型的固定口径。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from .config import (
    DISPATCH_FORBIDDEN_SLOTS, LIVE_SCENE, OWNER_ROLE, SHOT_GATE, SLOTS, WINDOW_PLATFORMS,
)

__all__ = [
    "DISPATCH_FORBIDDEN_SLOTS", "LIVE_SCENE", "OWNER_ROLE", "SLOTS", "WINDOW_PLATFORMS",
    "TICKET_TYPES", "TASK_TIERS", "DISPATCH_STATES", "ANSWER_STATES", "IMAGE_ORIGINS",
    "STAFF_STATES", "LIVE_ORIGIN", "TicketError", "now_text", "with_ticket_defaults",
    "normalize_window", "parse_window_hint", "strip_window_prefix", "apply_window_prefix",
    "normalize_sources", "normalize_lines", "new_ticket_record", "apply_ticket_placeholder",
    "deliverable_candidate", "is_image_deliverable", "deliverable_key", "path_segments",
    "resolve_under_root", "provenance_path_for", "read_provenance_dimension", "image_record",
]

# 位名名册、只分发位、开窗平台、主场景名全部来自 config——不要在这里写字面量。
# 加一位、改一个位名,只动 config.json;代码里每一处消费端都跟着变。

TICKET_TYPES = ("派单", "拍板", "疑问", "需求", "阻塞", "总工单")
TASK_TIERS = ("甲", "乙", "丙")
DISPATCH_STATES = ("新建", "已认领", "待判", "待复检", "已合并", "实机复验过", "关闭", "返工", "阻塞", "作废")
ANSWER_STATES = ("新建", "待答", "已答", "关闭")
# 用户可感知单的实机证据来源。第一项是「真的在完整环境里跑起来拍的」,
# 另外两项是「隔离环境拍的」与「别的」——交板闸只认第一项。
LIVE_ORIGIN = f"{LIVE_SCENE}真登录"
IMAGE_ORIGINS = (LIVE_ORIGIN, "隔离场景", "其他")
STAFF_STATES = ("在岗", "已收窗")
# 真源只有一处：派单标题开头的全角方括号标签。单上的「建议窗口」是从标题解析出来的派生值，
# 每次读单(with_ticket_defaults)都重算一遍并覆盖，所以盘上那一份漂了也活不过下一次读取。
WINDOW_PREFIX_RE = re.compile(rf"^\s*【({'|'.join(WINDOW_PLATFORMS)})】\s*", re.IGNORECASE)


class TicketError(RuntimeError):
    """可直接展示给使用者的人话错误。"""


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def with_ticket_defaults(ticket: dict[str, Any]) -> dict[str, Any]:
    """在读取旧单时补新字段，不触发整库迁移。

    「建议窗口」是**派生值**：这里用 setdefault 就会把盘上那一份当真源，
    改了标题却没改字段的单从此永远显示旧标签。所以这里一律**覆盖重算**——
    真源只有标题，盘上存的那一份只是个缓存，读一次就被刷新一次。
    """
    ticket.setdefault("已开窗", None)
    # D9-460:老单没有这两格。用 setdefault 而不是覆盖——它们是**真结论**不是派生值,
    # 一旦有人复验过/交过闸报告,盘上那份就是唯一真源,读一次刷一次会把结论抹掉。
    ticket.setdefault("复验", {})
    ticket.setdefault("机器闸", {})
    ticket["建议窗口"] = parse_window_hint(ticket.get("标题", ""))
    return ticket


def normalize_window(value: str | None) -> str:
    """把「建议窗口」收敛成名册里的某个平台名，或空串。

    建议不是硬闸：留空一律放行，不许因为没填就拦住建单。
    只有填了个不认识的值才拒绝，并且把合法值原样列给人看——
    ★报错里必须把合法值列全:只回一句「不认识」会让人反复猜,
      而合法值是配置出来的,他不可能猜得到。
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text not in WINDOW_PLATFORMS:
        raise TicketError(
            f"建议窗口不认识：{str(value).strip()}。"
            f"只能填这几个之一，或者留空：{'、'.join(WINDOW_PLATFORMS)}。"
        )
    return text


def parse_window_hint(title: str | None) -> str:
    """从派单标题开头的【平台名】里解析出建议窗口（合法平台名见 config「开窗平台」）。

    不以名册里某个平台名开头的标题（包括改名之后作废的旧标签）一律返回空串：
    标签是建议不是硬闸，解析不出来不报错、不拦单。
    """
    matched = WINDOW_PREFIX_RE.match(str(title or ""))
    return matched.group(1).lower() if matched else ""


def strip_window_prefix(title: str | None) -> str:
    """去掉标题开头的建议窗口标签；没有标签就原样返回（只去一层，不递归）。"""
    return WINDOW_PREFIX_RE.sub("", str(title or ""), count=1)


def apply_window_prefix(title: str | None, window: str) -> str:
    """把建议窗口写成标题前缀。已有前缀是**替换**不是叠加；给空串就是撤回建议。"""
    body = strip_window_prefix(title)
    return f"【{window}】{body}" if window else body


def normalize_sources(values: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return [str(value).strip() for value in values if str(value).strip()]


def normalize_lines(values: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = values.splitlines()
    return [str(value).strip() for value in values if str(value).strip()]


def new_ticket_record(
    ticket_id: str,
    ticket_type: str,
    slot: str,
    title: str,
    initiator: str,
    sources: list[str] | tuple[str, ...] | str | None = None,
    consumer: str = "",
    assign: str = "",
    body: str = "",
    notes: str = "",
    task_tier: str = "乙",
    context_lines: int | None = None,
    deliverables: list[str] | tuple[str, ...] | str | None = None,
    source_slot: str = "",
    internal: bool = False,
    taskbook: str = "",
    window: str = "",
) -> dict[str, Any]:
    """生成字段齐全、可向后兼容扩列的一张工单。"""
    if ticket_type not in TICKET_TYPES:
        raise TicketError(f"工单类型不认识：{ticket_type}。可用类型为：{'、'.join(TICKET_TYPES)}")
    if slot not in SLOTS:
        raise TicketError(f"总监位不在名册里：{slot}")
    if not title.strip():
        raise TicketError("标题不能为空。")
    if not initiator.strip():
        raise TicketError("发起人不能为空。")
    if task_tier not in TASK_TIERS:
        raise TicketError("任务档只可填甲、乙或丙。")
    if context_lines is not None and (not isinstance(context_lines, int) or context_lines < 0):
        raise TicketError("上下文预算必须是大于等于 0 的整数，单位为行。")
    if task_tier == "丙" and context_lines is None:
        raise TicketError("丙档不能建单：必须填写上下文预算，单位为行，且不超过 2000。")
    if task_tier == "丙" and context_lines > 2000:
        raise TicketError(f"丙档不能建单：上下文预算是 {context_lines} 行，超过 2000 行上限；请缩小范围或升为乙档。")
    # window 只是「替你把前缀写进标题」的快捷方式：折进标题之后就没它的事了，
    # 后面「建议窗口」一律从最终标题解析，免得参数与标题各说一套。
    window = normalize_window(window)
    title = apply_window_prefix(title.strip(), window) if window else title.strip()
    stamp = now_text()
    state = "新建" if ticket_type == "派单" else "待答"
    return {
        "编号": ticket_id,
        "类型": ticket_type,
        "所属总监位": slot,
        "标题": title,
        "发起人": initiator.strip(),
        "发起时间": stamp,
        "最后更新时间": stamp,
        "状态": state,
        "任务档": task_tier,
        # 派生值，真源是上面那个「标题」；每次读单都按标题重算并覆盖（with_ticket_defaults）。
        "建议窗口": parse_window_hint(title),
        "实际模型": "",
        "模型低档提醒过": False,
        "已开窗": None,
        "上下文预算": context_lines,
        "指派给": assign.strip(),
        "判卷人": "",
        "判语": "",
        "判退责任": "",
        "复检人": "",
        "真源指针": normalize_sources(sources),
        "任务书路径": taskbook.strip(),
        "任务书校验": "",
        "实机消费者": consumer.strip(),
        "非用户可感知": bool(internal),
        "实机图标记": "",
        "免独图原因": "",
        "发起位": source_slot.strip(),
        "交付项": normalize_lines(deliverables),
        "转交历史": [],
        "转交可见位": [],
        "流程提示": "",
        "接线证据": {"文字": "", "验证命令": "", "原样输出": "", "图片列表": []},
        # 交板时员工填的「留给下一窗」：只写底数与坑，不写流水账（T-001073 R3）。
        # 存成 {"填写人","实际模型","时间","行":[{"文字","划掉判卷人","划掉时间"}]}；
        # 老单没有这一格，读的时候一律走 service.handoff_rows，不做整库迁移。
        "留给下一窗": {},
        "关联op号": "",
        "关联素材登记": "",
        "返工次数": 0,
        "返工原因列表": [],
        "阻塞原因": "",
        "阻塞前状态": "",
        "备注": notes.strip(),
        "正文": body.strip(),
        "答复": "",
        "图片列表": [],
        # D9-460 ①:判卷与复验**并行**。复验不再是「判过之后的下一档状态」,
        # 而是挂在单上的一格独立结论,judge 与 verify 谁先到都行,两道齐了才能并。
        # 存成 {"复验人","时间","结论","闸输出","说明"};空字典 = 还没人复验过。
        "复验": {},
        # D9-460 ②:内部单交板时带的六项机器闸报告。
        # 存成 {"时间","报告":[{"项","结论"}...],"全绿":bool};六项全过时「全绿」为真,
        # 并**视为复验过**(见 service.submit)。空字典 = 这张单没交过闸报告。
        "机器闸": {},
        "事件序号": 0,
    }


DELIVERABLE_BULLET = re.compile(r"^(?:[-*\u2022]|\d+[.)\u3001])\s*")
TICKET_PLACEHOLDER = "{ticket}"
TICKET_IMAGE_NAME = re.compile(r"^T-\d{6}-\d{2}\.[A-Za-z0-9]+$")


def apply_ticket_placeholder(value: str, ticket_id: str) -> str:
    """把任务书路径里的 {ticket} 换成真单号。

    建单前猜不到单号，建完又改不了，于是猜号重开一轮废掉 4 个号（T-000070 实测）。
    占位符只在服务端分配到单号之后替换：分配不到号就根本不会有这张单，
    库里也就永远不会留下一个没替换的 {ticket}。
    """
    return str(value or "").replace(TICKET_PLACEHOLDER, ticket_id)


def deliverable_candidate(row: str) -> str:
    """把交付项一行剥成一个干净的路径候选。客户端与服务端必须用同一把尺子。"""
    return DELIVERABLE_BULLET.sub("", str(row)).strip().strip('"')


def is_image_deliverable(candidate: str) -> bool:
    """这一行写的是工单 img/ 里的证据图，而不是工作机上的文件。

    图片类交付项永远由服务端核（图片就存在服务器上）；其余的按文件系统核，
    而文件系统只有提交端那台机器说了算。
    """
    return bool(TICKET_IMAGE_NAME.fullmatch(PurePosixPath(str(candidate).replace("\\", "/")).name))


# ── 非业务闸:交付项归一化（T-001256 ①，D9-453 / 宪法 v2.31 八章 6）──────────────
# 「非业务闸不停车」不是把闸拆了，是把闸分成两种:拦「活没做好」的一个不动，
# 拦「字没写对」的一律降成记一行继续走。下面三条全属后者——
# 同一份产物，写 .jpg 还是 .webp、写目录还是目录下的文件、写工作树路径还是主检出路径，
# 语义完全一样，不该停掉一整扇窗（近两日实例:T-000979 / T-001191 / T-001223 / T-000876）。
# ★这三条服务端与客户端都要用,所以放在 model 里一处——两处各写一套必然漂。


def deliverable_key(candidate: str) -> str:
    """一条交付项的**去扩展名**键，用来跨写法比对同一份产物。

    T-000979:交付项写 `T-000979-01.jpg`，台面实际存的是 `T-000979-01.webp`——
    工单台自己的压缩管线按有没有透明通道决定落 webp 还是 jpg，员工建单时根本猜不到，
    却要为此停一窗。编号对上即同一份产物。
    """
    name = PurePosixPath(str(candidate).replace("\\", "/")).name
    stem, dot, _ = name.rpartition(".")
    return (stem if dot else name).lower()


def path_segments(candidate: str) -> tuple[str, ...]:
    """把一条交付项拆成路径段（统一斜杠、去掉盘符与空段）。"""
    text = str(candidate).replace("\\", "/").strip().strip('"')
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if parts and parts[0].endswith(":"):  # 去掉 D: 这类盘符段
        parts = parts[1:]
    return tuple(parts)


def resolve_under_root(candidate: str, root: Any) -> Any:
    """在给定仓根下找这条交付项:整条当相对路径试，再逐段掐掉前缀试，取**最长**的那个匹配。

    同一份产物在不同树上写法不同——主检出 `/repo/tools/x.py`、
    工作树 `/repo-worktrees/wt-abc/tools/x.py`、仓相对 `tools/x.py`，三者语义相同，
    只是前缀不同（原版为此停过两次窗）。约定写法是**仓相对路径**，
    这里把任意前缀折回那一种再核。
    ★用 exists() 不用 is_file():**目录型交付项**（T-001191/T-001223）照样算数。
    ★从整条开始往后掐，所以优先命中最具体的那个，不会被一个同名短尾巴蒙混过去。
    """
    from pathlib import Path as _Path

    base = _Path(root)
    segments = path_segments(candidate)
    for index in range(len(segments)):
        probe = base.joinpath(*segments[index:])
        if probe.exists():
            return probe
    return None


# ── 判据图尺寸闸 ──────────────────────────────────────────────────────────────
# 项目定了判据图必须是某个尺寸,可交单口一直没有闸——于是任务书抄了过期前提的窗
# 会按旧尺寸一路交到底,没有任何一处拦得住。这里补上那道闸。
#
# ★★ 必须读**出处小文件里的尺寸行**，绝不能量入单附件本身。
#    工单台自己的图片管线是 MAX_IMAGE_EDGE=1280：每一张入单的图都被压到长边 ≤1280
#    （判据图原图不入仓，仓里只留压缩件）。照附件量，**张张误伤**。
# ★ 只盖命令行这条路：网页上传拿不到同目录的出处 txt，
#   而且网页上传多是随手贴图，判卷人自己开原图核即可。
# ★★ 默认**关**：这道闸的前提是你有一条取图链（拍图的同时写出处小文件）。
#    没有那条链，任何手工截图都会被拒——那时候它不是一道闸，是一条走不通的路。
#    搭好之后在 config「判据图闸」里把「开」改成 true。
# ★ 四个值全部从 config 读，不许在这里写死：写死意味着「换条取图链就得改代码」，
#   而那正是这种闸被人整条绕过的原因。
SHOT_GATE_ON: bool = bool(SHOT_GATE.get("开", False))
JUDGING_RESOLUTION: str = str(SHOT_GATE.get("尺寸", "") or "")
LAYOUT_EXTRA_RESOLUTION: str = str(SHOT_GATE.get("布局例外尺寸", "") or "")
PROVENANCE_SUFFIX: str = str(SHOT_GATE.get("出处后缀", "") or ".出处.txt")
PROVENANCE_DIM_KEY = "PNG_DIM"


def provenance_path_for(image_path: str) -> str:
    """一张判据图对应的出处小文件路径：去掉扩展名再接 `.出处.txt`。

    取图工具在拍成的同时写一份同名小文件,里面记 `PNG_DIM = 宽x高`。
    ★出处**只在取图成功那一趟才写**，所以「没有出处」= 这张图不是取图链正常产出的。
    """
    text = str(image_path).replace("\\", "/")
    head, dot, _ = text.rpartition(".")
    return (head if dot else text) + PROVENANCE_SUFFIX


def read_provenance_dimension(provenance_text: str) -> str:
    """从出处小文件正文里取 `PNG_DIM = 2560x1440` 那一行的值；没有就返回空串。"""
    for line in str(provenance_text).splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().upper() == PROVENANCE_DIM_KEY:
            return value.strip().lower()
    return ""


def image_record(filename: str, original_path: str, origin: str, uploader: str) -> dict[str, str]:
    if origin not in IMAGE_ORIGINS:
        raise TicketError(f"图片来源不认识：{origin}")
    return {
        "文件名": filename,
        "原图本地路径": original_path,
        "来源标注": origin,
        "上传人": uploader,
        "时间": now_text(),
    }
