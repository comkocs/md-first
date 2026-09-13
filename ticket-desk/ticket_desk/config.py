"""开源版的可配置项：位名名册、路径约定、值面项、部署标识。

原版是给一个具体项目写死的——位名是那个项目的十三个总监位，判据图来源写死成那个
引擎的主场景名，数据根写死成一个 Windows 盘符。开源版把这些全部收进这一个模块：

  · 代码里**任何一处**要用到项目特定的名字，都从这里取，不许再写字面量；
  · 默认值是一套**示例**名册，开箱即跑，但它不是谁的真实名册；
  · 要换成自己的，写一份 JSON，用环境变量 `TICKET_CONFIG` 指过来，或者放在
    本包同目录下叫 `config.json`——两条路都不用改代码。

★为什么做成「一处配置」而不是「到处留参数」：
  原版真正的教训是**同一个事实写在两个地方必然漂**（见 service.py 里那一堆
  「两处各拼必然漂」的注释）。位名尤其如此：它同时出现在名册、对话线文件名、
  权限闸、通知路由、日览分组里，漏改一处就是「单发出去了但收件位看不见」。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent

# ── 默认值 = 一套示例名册 ────────────────────────────────────────────────────
# 这不是任何人的真实名册,是「一个中大型软件项目大概会怎么切位」的示例。
# 换成自己的:照着这份结构写一份 JSON,别改这个文件。
DEFAULTS: dict[str, Any] = {
    "产品名": "Ticket Desk",

    # 工单真数据落在哪。留空 = <用户主目录>/.ticket-desk/tickets。
    # ★真数据永远不进 Git:它每分钟都在变,进仓就是每次提交都冲突。
    "数据根目录": "",

    # 任务书、工位记忆、各位章程放在哪(工具只用它拼**提示**里的约定路径,不替人建目录)。
    # 留空 = 本仓的上一级目录。
    "工作区根目录": "",

    # 开窗指令第一行要贴的那条命令行路径。留空 = 按本文件位置推算出 ticket.py 的绝对路径。
    "命令行路径": "",

    # 「用户可感知单」的实机证据取自哪个场景/环境。原版是图形引擎的主场景文件名;
    # 换成 Web 项目就写「生产环境」,换成 CLI 就写「真实终端」。
    "主场景": "主场景",

    # 位名名册。顺序就是日览与下拉框里的顺序。
    # ★最后三个是**有特殊权限**的位,名字可以改,但角色不能少(见下面四个键)。
    "位名": [
        "前端·界面与交互",
        "前端·视觉与资源",
        "后端·服务与接口",
        "后端·数据与规则",
        "内容·文案与真源",
        "测试·质量与用例",
        "设计·裁定分发",
        "复检·合并与部署",
        "平台·工单系统",
        "总编排",
    ],

    # 四个有特殊权限的位,必须是「位名」里的某一个:
    "总编排位": "总编排",            # 公共接口:协议号、登记表、合并授权、裁定落笔
    "复检位": "复检·合并与部署",     # 独立于指挥链:复验、并线、部署
    "平台位": "平台·工单系统",       # 工单系统本身的开发运维,不碰业务
    "内容位": "内容·文案与真源",     # 交板时提醒「生成物刷了没」的那一位

    # 只发需求/疑问/拍板、不派实现单、不判卷、不并线的位(可以为空清单)。
    "只分发不派单位": ["设计·裁定分发"],

    # 唯一拍板人的称呼。它不是「位」,是人。
    "拍板人": "设计者",

    # 建议开窗平台的合法值。写进派单标题开头的【X】里;建议不是硬闸,留空一律放行。
    "开窗平台": ["claude", "codex", "vscode", "zcode"],

    # 模型名册。「任务档上限」= 这个模型最高能吃哪一档任务书(甲>乙>丙)。
    # 「状态」∈ 可用 / 需总编排批准 / 退役。退役的模型不能再 staff new,历史记录保留。
    "主力模型集合": ["model-a", "model-b", "model-e"],
    "模型名册": [
        # 主力:能吃甲档。「可选档位」是同一个模型的算力档,写成 <模型>-<档位>,
        # 「档位任务档」逐档细分——同一个模型跑在低算力档上只配吃丙档单。
        {"模型": "model-a", "可选档位": ["high", "middle", "low"],
         "档位任务档": {"high": "甲", "middle": "甲", "low": "丙"},
         "任务档上限": "甲", "状态": "可用"},
        {"模型": "model-b", "可选档位": ["xhigh", "max", "high"], "任务档上限": "甲", "状态": "可用"},
        {"模型": "model-e", "可选档位": ["top"], "任务档上限": "甲", "状态": "需总编排批准"},
        # 非主力:任务档上限就是它的天花板。
        {"模型": "model-c", "可选档位": [], "任务档上限": "乙", "状态": "可用"},
        {"模型": "model-d", "可选档位": [], "任务档上限": "丙", "状态": "可用"},
        # 退役:历史记录保留,但不能再 staff new。
        {"模型": "model-legacy", "可选档位": [], "任务档上限": "丙", "状态": "退役"},
    ],
    "停用阈值": {"同位": 3, "全项目": 5},

    # ── 当前值面 ────────────────────────────────────────────────────────────
    # 「全项目当天都在变、谁抄进自己的接管件谁就永远读到旧值」的那几个数。
    # 类型只有五种,决定怎么校验:
    #   分辨率   宽x高,例如 2560x1440
    #   提交号   7~40 位十六进制
    #   数对     形如 a=1,b=2 或一段 JSON 对象;「键」里要列出必填的那几个键名
    #   名状清单 形如 名字=一句状态;名字=一句状态,或 JSON 数组;可以写「无」
    #   文本     不校验,原样存
    # 「标签」相同的多项会合成顶栏上的一格显示(例如两个仓的部署头)。
    "值面": [
        {"键": "screenshot_resolution", "标签": "判据图", "类型": "分辨率",
         "说明": "判据图尺寸，写成 宽x高，例如 2560x1440"},
        {"键": "deploy_head", "标签": "部署头", "类型": "提交号",
         "说明": "生产环境当前部署头，7～40 位十六进制短提交号"},
        {"键": "staging_head", "标签": "部署头", "类型": "提交号",
         "说明": "预发布环境当前部署头，7～40 位十六进制短提交号"},
        # 「数对」示例:一项里有好几个数,少写一个就拒。「键名」列出必填的那几个。
        {"键": "latency_budget", "标签": "延迟预算", "类型": "数对", "键名": ["p50", "p99"],
         "说明": "接口延迟预算(毫秒)，写成 p50=<数>,p99=<数>，"
                 "或一段 JSON {\"p50\":<数>,\"p99\":<数>}"},
        {"键": "pending_shared_tools", "标签": "已改未并公共工具", "类型": "名状清单",
         "说明": "已改未并的公共工具，写成 名字=一句状态；名字=一句状态，"
                 "或一段 JSON [{\"名字\":\"…\",\"状态\":\"…\"}]；一个都没有就写 无"},
    ],

    # ── 上服目标 ────────────────────────────────────────────────────────────
    # `deploy-record` 的 --target 认哪几个值。每个目标绑一个值面键(上服后自动写回它,
    # 免得那一格靠人手抄、抄漏就全台面读到旧值),再绑一个「上服记录归哪一位」。
    # ★「值面键」必须是上面「值面」里类型为「提交号」的某一项,否则写回时会被类型闸拒。
    "部署目标": [
        {"名字": "production", "值面键": "deploy_head", "归位": "平台·工单系统"},
        {"名字": "staging", "值面键": "staging_head", "归位": "复检·合并与部署"},
    ],

    # 交板时带的机器闸报告有哪几项。少一项或任一项不过都不算全绿。
    # ★这是**闸**不是清单:全绿会让内部单跳过人复验直接可并,所以宁可严。
    "机器闸项": ["构建", "测试", "静态检查", "交付项"],

    # ── 判据图闸（默认关）─────────────────────────────────────────────────
    # 命令行用 `attach --origin world` 交真实环境判据图时，要不要核这两件事：
    #   ① 图旁边有没有一份同名的「出处小文件」(<图名>.出处.txt，里面一行 PNG_DIM = 宽x高)；
    #   ② 那一行写的尺寸对不对。
    #
    # ★为什么默认关：这道闸的前提是**你有一条取图链**——拍图的同时自动写那份出处小文件。
    #   没有那条链，任何一张手工截图都会被拒。那时候它不是一道闸，是一条走不通的路。
    #
    # ★开着它你得到的是什么（想清楚再决定关）：它挡住的不是尺寸不对，
    #   是「这张图不是那条链跑出来的」——也就是**有人手工拼了一张图冒充实机证据**。
    #   闸只读出处小文件里的尺寸，绝不量附件本身：入单的图都会被压到长边 ≤1280，
    #   照附件量会张张误伤。
    #
    # 自己搭好取图链之后：把「开」改成 true，「尺寸」改成你取图工具的默认输出尺寸。
    # 「布局例外尺寸」是给 --layout-extra 那条路用的，用不上就留空。
    "判据图闸": {
        "开": False,
        "尺寸": "2560x1440",
        "布局例外尺寸": "1920x1080",
        "出处后缀": ".出处.txt",
    },

    # 网页离线回落包挂在 window 上的全局名。
    "离线包全局名": "TICKET_DESK",
}


def _load_overrides() -> dict[str, Any]:
    """读覆盖配置：环境变量 TICKET_CONFIG 指的文件优先，其次本包同目录的 config.json。

    ★读不出来一律**报错**,不悄悄用默认值:配置文件写歪了却按示例名册跑起来,
      是这一类工具最坏的失败形态——单都建出去了,收件位是示例里的名字。
    """
    pointer = os.environ.get("TICKET_CONFIG", "").strip()
    path = Path(pointer) if pointer else PACKAGE_DIR / "config.json"
    if not path.is_file():
        if pointer:
            raise RuntimeError(f"环境变量 TICKET_CONFIG 指向的配置文件不存在：{path}")
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"读不懂配置文件 {path}：{exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"配置文件 {path} 的顶层必须是一个 JSON 对象。")
    unknown = sorted(set(loaded) - set(DEFAULTS))
    if unknown:
        raise RuntimeError(
            f"配置文件 {path} 里有不认识的键：{'、'.join(unknown)}。"
            f"合法键只有：{'、'.join(sorted(DEFAULTS))}。"
        )
    return loaded


_VALUES: dict[str, Any] = {**DEFAULTS, **_load_overrides()}


def get(key: str) -> Any:
    return _VALUES[key]


# ── 派生常量：模块加载时算一次，代码各处只读这些 ──────────────────────────
PRODUCT_NAME: str = str(_VALUES["产品名"])
SLOTS: tuple[str, ...] = tuple(_VALUES["位名"])
CONDUCTOR_SLOT: str = str(_VALUES["总编排位"])
REVIEW_SLOT: str = str(_VALUES["复检位"])
PLATFORM_SLOT: str = str(_VALUES["平台位"])
CONTENT_SLOT: str = str(_VALUES["内容位"])
DISPATCH_FORBIDDEN_SLOTS: tuple[str, ...] = tuple(_VALUES["只分发不派单位"])
OWNER_ROLE: str = str(_VALUES["拍板人"])
WINDOW_PLATFORMS: tuple[str, ...] = tuple(_VALUES["开窗平台"])
LIVE_SCENE: str = str(_VALUES["主场景"])
BUNDLE_GLOBAL: str = str(_VALUES["离线包全局名"])
GATE_REPORT_ITEMS: tuple[str, ...] = tuple(_VALUES["机器闸项"])
SHOT_GATE: dict[str, Any] = dict(_VALUES["判据图闸"])
MAIN_MODELS: list[str] = list(_VALUES["主力模型集合"])
MODEL_ROSTER: list[dict[str, Any]] = [dict(row) for row in _VALUES["模型名册"]]
BAN_THRESHOLDS: dict[str, int] = dict(_VALUES["停用阈值"])
STATE_SPECS: tuple[dict[str, Any], ...] = tuple(dict(row) for row in _VALUES["值面"])
DEPLOY_TARGETS: tuple[dict[str, Any], ...] = tuple(dict(row) for row in _VALUES["部署目标"])


def _check_roster() -> None:
    """名册自洽性：四个特殊位必须在册，只分发位也必须在册。

    不检查的话，症状是「单建出去了，通知落进一个不存在的对话线」——
    append_jsonl 会安静地建一个新文件，谁都不会发现（原版 _notify_slots 那一条
    `if slot not in SLOTS: continue` 就是在这里兜底，兜得太安静）。
    """
    missing = [
        f"{label}={value}"
        for label, value in (
            ("总编排位", CONDUCTOR_SLOT), ("复检位", REVIEW_SLOT),
            ("平台位", PLATFORM_SLOT), ("内容位", CONTENT_SLOT),
        )
        if value not in SLOTS
    ]
    missing += [f"只分发不派单位={value}" for value in DISPATCH_FORBIDDEN_SLOTS if value not in SLOTS]
    if missing:
        raise RuntimeError(
            "配置不自洽：这些位不在「位名」名册里——" + "、".join(missing)
            + f"。当前名册：{'、'.join(SLOTS)}。"
        )
    if len(set(SLOTS)) != len(SLOTS):
        raise RuntimeError(f"配置不自洽：「位名」里有重名。当前名册：{'、'.join(SLOTS)}。")
    if OWNER_ROLE in SLOTS:
        raise RuntimeError(
            f"配置不自洽：拍板人「{OWNER_ROLE}」不能同时是一个位——"
            "他是人不是位，对话线与权限闸对这两者的处理完全不同。"
        )
    # 部署目标要指向真实存在、且类型对得上的值面项与位。
    # ★不检查的话,症状是「上服记录建成功了,值面那一格纹丝不动」——
    #   写回被静默吞掉,而全台面继续读着旧的部署头。
    commit_keys = {
        str(row["键"]) for row in STATE_SPECS if str(row.get("类型", "")) == "提交号"
    }
    for row in DEPLOY_TARGETS:
        name, key, slot = str(row.get("名字", "")), str(row.get("值面键", "")), str(row.get("归位", ""))
        if key not in commit_keys:
            raise RuntimeError(
                f"配置不自洽：部署目标「{name}」的值面键 {key} 不是「值面」里类型为「提交号」的项。"
                f"当前提交号类项：{'、'.join(sorted(commit_keys)) or '（一个都没有）'}。"
            )
        if slot not in SLOTS:
            raise RuntimeError(f"配置不自洽：部署目标「{name}」的归位 {slot} 不在「位名」名册里。")


_check_roster()


def repo_root() -> Path:
    """本仓根目录（ticket-system 这个包的上一级）。"""
    return PACKAGE_DIR.parent


def workspace_root() -> Path:
    """任务书 / 工位记忆 / 章程 的根。留空就用本仓上一级。"""
    configured = str(_VALUES["工作区根目录"]).strip()
    return Path(configured).expanduser() if configured else repo_root().parent


def data_root() -> Path:
    """工单真数据根。优先级：显式配置 > 环境变量 TICKET_ROOT > 用户主目录下的默认位置。

    ★环境变量排在配置文件**后面**是故意的:测试与 CI 用环境变量临时改根,
      不该被一份提交进仓的 config.json 盖掉。
    """
    configured = str(_VALUES["数据根目录"]).strip()
    if configured:
        return Path(configured).expanduser()
    from_env = os.environ.get("TICKET_ROOT", "").strip()
    if from_env:
        return Path(from_env).expanduser()
    return Path.home() / ".ticket-desk" / "tickets"


def cli_path() -> str:
    """开窗指令里贴的那一条 ticket.py 绝对路径。"""
    configured = str(_VALUES["命令行路径"]).strip()
    return configured or str(PACKAGE_DIR / "ticket.py")


def charter_path(slot: str) -> str:
    """这一位的章程 md 的**约定**路径。工具不建目录、不校验，只用于提示。"""
    return str(workspace_root() / "_office" / slot / "章程.md").replace("\\", "/")


def memory_path_hint() -> str:
    """工位记忆 md 的约定写法，给报错与 --help 用。

    ★一律输出**正斜杠**。这串是给人照抄进命令行的示例，而 `str(Path)` 在 Windows 上
    产出的是反斜杠——照抄进 bash 会被转义吃掉，第一次照抄的人就写歪了。
    与 channel.forward_slashes 同一条口径（「反斜杠在 bash 的 source 里会被吃掉，踩过一次」）。
    正斜杠在 Windows 的 Python 里一样认，两边都能直接用。
    """
    return str(workspace_root() / "_office" / "<位名>" / "工位记忆" / "<位名-编号>.md").replace("\\", "/")
