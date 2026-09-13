#!/usr/bin/env python3
"""AI 工位窗使用的工单命令行主入口。"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ticket_desk import config
    from ticket_desk.model import (
        JUDGING_RESOLUTION, LAYOUT_EXTRA_RESOLUTION, LIVE_ORIGIN, LIVE_SCENE,
        PROVENANCE_DIM_KEY, SHOT_GATE_ON, SLOTS, TicketError,
        deliverable_candidate, deliverable_key, is_image_deliverable, normalize_lines,
        provenance_path_for, read_provenance_dimension, resolve_under_root,
    )
    from ticket_desk.auth import AccountManager
    from ticket_desk.http_server import serve
    from ticket_desk.service import (
        DEFAULT_MEMORY_MAX_LINES, DEPLOY_TARGETS, MEMORY_PATH_HINT, MEMORY_REFRESH_PREFIX,
        SHOT_EXEMPT, SHOT_EXEMPT_REASON, STATE_KEYS, STATE_UNSET_HINT, STATE_WRITERS,
        TicketService, state_text, with_state_summary,
    )
    from ticket_desk.store import SqliteStore, TicketStore
    from ticket_desk.remote import RemoteClient, RemoteUnavailable
    from ticket_desk import channel as channel_config
else:
    from . import config
    from .model import (
        JUDGING_RESOLUTION, LAYOUT_EXTRA_RESOLUTION, LIVE_ORIGIN, LIVE_SCENE,
        PROVENANCE_DIM_KEY, SHOT_GATE_ON, SLOTS, TicketError,
        deliverable_candidate, deliverable_key, is_image_deliverable, normalize_lines,
        provenance_path_for, read_provenance_dimension, resolve_under_root,
    )
    from .auth import AccountManager
    from .http_server import serve
    from .service import (
        DEFAULT_MEMORY_MAX_LINES, DEPLOY_TARGETS, MEMORY_PATH_HINT, MEMORY_REFRESH_PREFIX,
        SHOT_EXEMPT, SHOT_EXEMPT_REASON, STATE_KEYS, STATE_UNSET_HINT, STATE_WRITERS,
        TicketService, state_text, with_state_summary,
    )
    from .store import SqliteStore, TicketStore
    from .remote import RemoteClient, RemoteUnavailable
    from . import channel as channel_config

# 这些命令只在本机操作数据库或起服务，从来不走远程通道。
OFFLINE_COMMANDS = {"serve", "migrate", "dump", "account", "env"}
DEGRADABLE_OPTIONS = {"--taskbook-client-checked": 1}


class HumanArgumentParser(argparse.ArgumentParser):
    """把 argparse 的缺参错误送入统一的人话错误出口。"""

    def error(self, message: str) -> None:
        required = re.fullmatch(r"the following arguments are required: (.+)", message)
        expected = re.fullmatch(r"argument (.+): expected one argument", message)
        if required:
            names = required.group(1).replace(", ", "、")
            raise TicketError(f"{self.prog} 缺少必填参数：{names}。")
        if expected:
            raise TicketError(f"{self.prog} 的 {expected.group(1)} 后面必须填写内容。")
        # T-001560 ②:撞上不认识的 --选项时,多半不是写错,而是本机检出落后——
        # 工单台的新参数从来都是先上服、后并 main(T-001124 上撞到过)。
        # ★服端版本号这里填不出来:解析错误发生在任何网络调用之前,客户端无从知道;
        #   提示里给核法(env --probe),不给假数。
        if re.search(r"--[A-Za-z][A-Za-z0-9-]*", message):
            raise TicketError(
                f"{self.prog} 参数不对，请检查命令写法。"
                "★若是新参数而本机检出较旧：工单台参数常先上服后并 main，服务端可能已认"
                "(env --probe 可核服务端协议)，git pull 主检出后再试。"
            )
        raise TicketError(f"{self.prog} 参数不对，请检查命令写法。")


def parser() -> argparse.ArgumentParser:
    root = HumanArgumentParser(prog="ticket.py", description=f"{config.PRODUCT_NAME} · 工单系统")
    commands = root.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="新建派单")
    new.add_argument("--type", default="派单", choices=["派单", "疑问", "阻塞"])
    new.add_argument("--slot", required=True)
    new.add_argument("--title", required=True)
    new.add_argument("--source", action="append", default=[])
    new.add_argument("--consumer", default="")
    new.add_argument("--assign", default="")
    new.add_argument("--by", default=config.CONDUCTOR_SLOT)
    new.add_argument("--notes", default="")
    new.add_argument("--body", default="")
    new.add_argument("--taskbook", default="", help="任务书 md 的绝对路径；可写 {ticket}，服务端分配到单号后替换")
    new.add_argument("--taskbook-unchecked", action="store_true", help="跳过本机任务书存在性校验；仅供冒烟测试，操作会记入日志")
    new.add_argument("--taskbook-client-checked", action="store_true", help=argparse.SUPPRESS)
    new.add_argument("--deliverable", action="append", default=[])
    new.add_argument("--deliverable-unchecked", action="store_true", help="跳过本机交付项校验；仅供冒烟测试，操作会记入日志")
    # T-000831:派单必须二选一明写可感知标记。两个都给由互斥组当场拒;两个都不给落到服务端拒(唯一真闸),
    # 报错里把两个开关分别是什么意思写清楚。
    new_facing = new.add_mutually_exclusive_group()
    new_facing.add_argument("--internal", dest="internal", action="store_true", default=None,
                            help=f"内部工具单：交板给验证命令与原样输出，不要求{LIVE_SCENE}实机图")
    new_facing.add_argument("--user-facing", dest="internal", action="store_false", default=None,
                            help=f"用户可感知单：交板必须附 {LIVE_ORIGIN} 图")
    new.add_argument("--tier", choices=["甲", "乙", "丙"])
    new.add_argument("--context-lines", type=int)
    # 故意不写 choices：argparse 的报错是「参数不对，请检查命令写法」，看不出四个合法值是哪四个。
    # 交给 model.normalize_window 拒，拒的时候把四个值原样列出来。
    # 真源是标题开头的【X】，这个开关只是「替你把前缀写进标题」的快捷方式，自己写标题也一样。
    new.add_argument("--window", default="", help="建议开窗平台：claude/codex/vscode/zcode 四选一或留空，作用是把【X】写进标题开头。建议不是硬闸；出画芯的活建议 codex")

    editing = commands.add_parser("set", help="改一张已经建好的单：新建/已认领/返工三态，本位总监或总编排")
    editing.add_argument("ticket")
    editing.add_argument("--taskbook", help="任务书 md 的绝对路径")
    editing.add_argument("--taskbook-unchecked", action="store_true", help="跳过本机任务书存在性校验；仅供冒烟测试，操作会记入日志")
    editing.add_argument("--taskbook-client-checked", action="store_true", help=argparse.SUPPRESS)
    editing.add_argument("--assign", help="改派给本位在册在岗员工")
    editing.add_argument("--window", help="改建议开窗平台：claude/codex/vscode/zcode 四选一，改的是标题开头的【X】（已有前缀是替换不是叠加）；给空串就是撤回建议")
    editing.add_argument("--source", action="append", help="真源指针；可重复，整条替换旧的")
    editing.add_argument("--body", help="正文")
    editing.add_argument("--consumer", help="实机消费者")
    editing.add_argument("--deliverable", action="append", help="交付项;可重复,整条替换旧的。必须写成真实产物路径,叙述句永远交不了板")
    editing.add_argument("--deliverable-unchecked", action="store_true", help="跳过本机交付项校验；仅供冒烟测试，操作会记入日志")
    facing = editing.add_mutually_exclusive_group()
    facing.add_argument("--internal", dest="internal", action="store_true", default=None,
                        help=f"改成内部工具单:交板不再索要 {LIVE_ORIGIN} 图(仍要验证命令与原样输出)")
    facing.add_argument("--user-facing", dest="internal", action="store_false", default=None,
                        help=f"改回用户可感知:交板重新索要 {LIVE_ORIGIN} 图")
    editing.add_argument("--blame", choices=["模型", "出题"],
                         help="改判退责任归属：只对已经判过的单（判过/返工），必须同时给 --reason，"
                              "会连同模型记分与出题记分一起回滚；待判态请用 judge --blame")
    editing.add_argument("--reason", help="改判退责任的理由；--blame 专用，不能为空")
    editing.add_argument("--tier", choices=["甲", "乙", "丙"],
                         help="改任务档（T-001560）：新建/已认领/返工三态，本位总监或总编排;"
                              "任务书重写换了档就顺手把字段改齐,别让设计者按旧档选模型")
    editing.add_argument("--by", required=True, help="改单的位名：该单所属总监位或总编排")

    claim = commands.add_parser("claim")
    claim.add_argument("ticket")
    claim.add_argument("--by", required=True)

    attach = commands.add_parser("attach")
    attach.add_argument("ticket")
    attach.add_argument("image")
    attach.add_argument("--origin", required=True, choices=["world", "isolated", "other"])
    attach.add_argument(
        "--layout-extra", action="store_true",
        help="布局类的第二张回归图:允许 1920×1080(宪法四章 1 的唯一例外);其余一律 2560×1440",
    )
    attach.add_argument("--by", default="")

    submit = commands.add_parser("submit")
    submit.add_argument("ticket")
    submit.add_argument("--evidence", default="")
    submit.add_argument("--verify-command", default="")
    submit.add_argument("--raw-output", default="")
    submit.add_argument(
        "--handoff", default="",
        help="留给下一窗：只写底数与坑（这条链的真源在哪、哪个数别信、下一窗从哪起手），不要流水账。"
             "一行一条；固定工位的单会被 memory export 收进工位记忆，非固定工位也能填，只是没人导出",
    )
    # ★项名从 config 现读，不许在这里另写死一份。
    #   写死的那份会随着别人改配置而过期，而**帮助文字过期是最难发现的一种**：
    #   照 help 写报告的人会被判「缺项」，却在 help 里找不到任何线索。
    submit.add_argument(
        "--gate-report", default="",
        help="内部单专用：机器闸报告，每项一行「<项名>: 过|不过」。"
             f"本机配置的项名是 {' / '.join(config.GATE_REPORT_ITEMS)}"
             f"（共 {len(config.GATE_REPORT_ITEMS)} 项，改见 config「机器闸项」）。"
             "★全部过才自动记「复验过」,判过之后即可并线,复检只看闸输出;"
             "缺一项或任一项不过都不置标,照旧要复检席跑 verify。可给路径或原样输出",
    )
    # 内部参数：由本机在发出远程请求前自动填，人手不用写。
    submit.add_argument("--deliverable-verified", default="", help=argparse.SUPPRESS)

    judge = commands.add_parser("judge")
    judge.add_argument("ticket")
    decision = judge.add_mutually_exclusive_group(required=True)
    decision.add_argument("--pass", dest="passed", action="store_true")
    decision.add_argument("--rework", dest="rework", metavar="原因")
    judge.add_argument("--by", required=True)
    judge.add_argument("--verdict", required=True, help="判语；判过时须写明怎么打开它——「用户怎么打开它」或「设计者怎么打开它」两句写哪一句都放行，用户打不开的活（取证/报告/内部工具）写后一句")
    judge.add_argument("--blame", choices=["模型", "出题"], default="")
    judge.add_argument(
        "--strike-handoff", default="",
        help="划掉「留给下一窗」里写错的行，逗号分隔行号（如 \"2,3\"）。"
             "★是划掉不是删除：原文保留，只多标一个「判卷人 · 时间」",
    )

    deploy_record = commands.add_parser("deploy-record")
    deploy_record.add_argument("--head", required=True, help="线上现在跑的提交号")
    deploy_record.add_argument("--probes", default="", help="探针原样输出:服务 active / 端口在听 / 首页 302 …")
    # ★「部署目标」允许为空：不上服的人（只拿工单台当台账）没有部署目标可填。
    #   空表时不能取 [0] 当默认值——那会在**构建参数表的那一刻**就 IndexError 崩掉，
    #   于是 `staff list`、`new` 这些跟部署毫不相干的命令也一起打不开，
    #   而报错指向 argparse 内部，看不出是配置的问题。
    #   空表时把这个选项留成「无默认值、无候选集」，真去跑 deploy-record 才报人话。
    if DEPLOY_TARGETS:
        deploy_record.add_argument("--repo", dest="repo", default=DEPLOY_TARGETS[0], choices=list(DEPLOY_TARGETS),
                                   help="上服目标(见 config「部署目标」);决定写回哪个值面键、记录归哪一位")
    else:
        deploy_record.add_argument("--repo", dest="repo", default="",
                                   help="上服目标——当前配置的「部署目标」是空的，"
                                        "要用 deploy-record 得先在 config 里配一个")
    deploy_record.add_argument("--ticket", dest="batch_tickets", action="append", default=[], help="批内单号,可重复")
    deploy_record.add_argument("--by", default="部署脚本")

    evidence = commands.add_parser("evidence-ticket")
    evidence.add_argument("--head", required=True, help="对应哪一次上服记录的头")
    evidence.add_argument("--slot", required=True, help="取证归哪一位")
    evidence.add_argument("--assign", default="", help="指派给哪个员工编号")
    evidence.add_argument("--ticket", dest="batch_tickets", action="append", default=[], help="批内单号,可重复")

    verify = commands.add_parser("verify")
    verify.add_argument("ticket")
    verify.add_argument("--by", required=True, help="复验人：复检席位名或其员工编号")
    verify.add_argument("--result", required=True, choices=["过", "退"], help="复验结论")
    verify.add_argument("--gates", default="", help="闸输出摘要：跑了什么、结果是什么")
    verify.add_argument("--evidence", default="", help="补充说明（判退时与 --gates 至少给一个）")

    merge = commands.add_parser("merge")
    merge.add_argument("ticket")
    merge.add_argument("--by", required=True)

    live = commands.add_parser("live")
    live.add_argument("ticket")
    live.add_argument("image", nargs="?", default="")
    live.add_argument("--batch", action="append", default=[])
    live.add_argument("--shot", choices=["同图", "独图", "免独图"], default="")
    live.add_argument("--reason", default="", help="只配 --shot 免独图 用：这张单为什么不用补独图")
    live.add_argument(
        "--layout-extra", action="store_true",
        help="布局类的第二张回归图:允许 1920×1080(宪法四章 1 的唯一例外);其余一律 2560×1440",
    )
    live.add_argument("--by", required=True)

    close = commands.add_parser("close")
    close.add_argument("ticket")
    close.add_argument("--by", required=True)
    close.add_argument("--not-merged", action="store_true", help="判过了但正确处置就是不并线：待复检直接结案；待判还没判过，要么先 judge，要么本条命令带 --verdict。--reason 必填")
    close.add_argument(
        "--not-deployed", action="store_true",
        help="并过了但上服失败已回滚/原命题不再成立：所属位、总编排或设计者对「已合并」单结案，"
             "状态显示「已合并·未上服·已结案」，不算上服、不算卡住。--reason 必填",
    )
    close.add_argument("--reason", default="", help="不并线/未上服结案的原因；没有原因的结案事后和「忘了」分不清")
    close.add_argument("--verdict", default="", help="只给「待判」态不并线结案用：一并补上判语，免得关掉的单判卷人与判语都是空的")

    rework = commands.add_parser(
        "rework", help="把「待复检」的单退回原位重做：单号与任务书都留住，回到「返工」",
    )
    rework.add_argument("ticket")
    rework.add_argument("--by", required=True, help=f"署名位：该单所属总监位、{config.REVIEW_SLOT}、{config.CONDUCTOR_SLOT} 或{config.OWNER_ROLE}")
    rework.add_argument("--reason", required=True, help="必填：为什么判过了还要退回重做")
    rework.add_argument(
        "--blame", choices=["出题", "模型"], default="出题",
        help="判退责任，默认「出题」——触发这条边多是任务书口径被否、执行方照做没错；要记模型账须显式写 模型",
    )

    voiding = commands.add_parser("void", help="作废一张建错的单：新建/已认领/阻塞三态，本位总监或总编排")
    voiding.add_argument("ticket")
    voiding.add_argument("--reason", required=True, help="一句话写清这张单错在哪")
    voiding.add_argument("--by", required=True, help="作废的位名：该单所属总监位或总编排")

    block = commands.add_parser("block")
    block.add_argument("ticket")
    block.add_argument("reason")
    block.add_argument("--by", default=config.CONDUCTOR_SLOT)
    block.add_argument(
        "--kind", choices=["业务", "非业务"], default="业务",
        help="业务=真源缺/接口对不上/判据不达/测试红,停车等人;"
             "非业务=账面事(交付项写法、路径前缀、待回核、远端滞后、CLI 落后),"
             "记一行继续做、状态不变、不用等答复(v2.31 八章 6)",
    )

    unblock = commands.add_parser("unblock")
    unblock.add_argument("ticket")
    unblock.add_argument("--by", default=config.CONDUCTOR_SLOT)

    transfer = commands.add_parser("transfer", help="原单号转交")
    transfer.add_argument("ticket")
    transfer.add_argument("--to", required=True, choices=[config.OWNER_ROLE, *SLOTS])
    transfer.add_argument("--reason", required=True)
    transfer.add_argument("--by", required=True)

    ask = commands.add_parser("ask")
    ask.add_argument("--type", required=True, choices=["拍板", "疑问", "需求", "总工单"])
    ask.add_argument("--slot", required=True)
    ask.add_argument("--title", required=True)
    ask.add_argument("--body", required=True)
    ask.add_argument("--by", default=config.OWNER_ROLE)
    ask.add_argument("--source", action="append", default=[])
    ask.add_argument("--consumer", default="")
    ask.add_argument("--tier", default="", choices=["甲", "乙", "丙"])
    ask.add_argument("--context-lines", type=int)

    answer = commands.add_parser("answer")
    answer.add_argument("ticket")
    answer.add_argument("answer")
    answer.add_argument("--by", required=True)

    say = commands.add_parser("say")
    say.add_argument("--slot", required=True)
    say.add_argument("--by", required=True)
    say.add_argument("text")
    say.add_argument("--img", default="")
    say.add_argument("--ref", default="")

    inbox = commands.add_parser("inbox")
    inbox.add_argument("--slot", required=True)
    inbox.add_argument("--for", dest="actor", required=True)
    inbox.add_argument("--mark-read", action="store_true")

    staff = commands.add_parser("staff")
    staff_commands = staff.add_subparsers(dest="staff_command", required=True)
    staff_new = staff_commands.add_parser("new")
    staff_new.add_argument("--slot", required=True)
    staff_new.add_argument("--tool", default=(config.MAIN_MODELS[0] if config.MAIN_MODELS else "待定"),
                           help="这扇窗跑的是哪个模型;名册见 config「模型名册」")
    staff_retire = staff_commands.add_parser("retire")
    staff_retire.add_argument("name")
    staff_reopen = staff_commands.add_parser("reopen")
    staff_reopen.add_argument("name")
    staff_list = staff_commands.add_parser("list")
    staff_list.add_argument("--slot", default="")
    staff_list.add_argument(
        "--all", action="store_true",
        help="连已收窗的一起列；默认只显示在岗（退役编号仍在册，账按模型统计不按编号）",
    )
    staff_ban = staff_commands.add_parser("ban")
    staff_ban.add_argument("--tool", required=True)
    staff_ban.add_argument("--slot", default="")
    staff_ban.add_argument("--by", required=True, choices=[config.OWNER_ROLE, config.CONDUCTOR_SLOT])
    staff_ban.add_argument("--reason", required=True)
    staff_unban = staff_commands.add_parser("unban")
    staff_unban.add_argument("--tool", required=True)
    staff_unban.add_argument("--slot", default="")
    staff_unban.add_argument("--by", required=True, choices=[config.OWNER_ROLE, config.CONDUCTOR_SLOT])
    staff_fix = staff_commands.add_parser("fix", help="把一位员工标成固定工位，并记下他的工位记忆 md 路径")
    staff_fix.add_argument("name")
    staff_fix.add_argument("--memory", required=True, help=f"记忆 md 的绝对路径；约定写法 {MEMORY_PATH_HINT}")
    staff_fix.add_argument("--by", required=True, help="署名位：该员工所属的总监位，或总编排")
    staff_unfix = staff_commands.add_parser("unfix", help="取消固定工位标记；记忆 md 路径保留，便于回看")
    staff_unfix.add_argument("name")
    staff_unfix.add_argument("--by", required=True, help="署名位：该员工所属的总监位，或总编排")

    # 固定工位的工位记忆（T-001073 R2）：骨架由工具从这位做过的单里生成，不靠人手写。
    memory = commands.add_parser("memory", help="固定工位的工位记忆：从已交板的单生成/重刷记忆 md")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_export = memory_commands.add_parser("export")
    memory_export.add_argument("--staff", required=True, help="员工名，形如「总监位-编号」")
    memory_export.add_argument("--out", default="", help="落盘路径；不填就用名册里记的「记忆md路径」")
    memory_export.add_argument(
        "--max-lines", type=int, default=DEFAULT_MEMORY_MAX_LINES,
        help=f"总行数封顶，默认 {DEFAULT_MEMORY_MAX_LINES}；超出时最旧的条目追加进同目录的 <文件名>.archive.md",
    )
    # 内部命令：远程模式下客户端拿它取原料，再在**自己这台机器上**落盘（服务器上没有 D: 盘）。
    memory_data = memory_commands.add_parser("data", help=argparse.SUPPRESS)
    memory_data.add_argument("--staff", required=True)

    history = commands.add_parser("history")
    history.add_argument("name")

    show = commands.add_parser("show")
    show.add_argument("ticket")

    listing = commands.add_parser("list")
    listing.add_argument("--slot", default="")
    listing.add_argument("--state", default="")
    listing.add_argument("--type", dest="ticket_type", default="")
    listing.add_argument("--shot-pending", action="store_true")
    listing.add_argument("--pending-mine", action="store_true", help="等价 --slot <本位> --state 待答：这一位还欠着没答的单（T-000957）")
    listing.add_argument("--nonbiz", action="store_true", help="非业务阻塞看板：身上还挂着没清的非业务阻塞记录的单（T-001256 ④）")

    receipt = commands.add_parser("receipt")
    receipt.add_argument("ticket")

    # 当前值面（T-000892）：全项目当天在变的那几个数，机器可读地放一处。
    # 故意不给 key 写 choices：argparse 的报错是「参数不对」，看不出合法键是哪几个；
    # 交给服务端拒，拒的时候把合法键原样列出来（R4 第 3 条钉的就是这一点）。
    state = commands.add_parser("state", help="当前值面：判据图尺寸、两仓部署头、走跑速度、已改未并的公共工具")
    state_commands = state.add_subparsers(dest="state_command", required=True)
    state_commands.add_parser("get", help="打一份 JSON；各位读它，不要往自己的接管件里转抄")
    state_set = state_commands.add_parser("set", help=f"改一项；只有 {STATE_WRITERS[0]} 与 {STATE_WRITERS[1]} 能改")
    state_set.add_argument("key", help="合法键：" + "、".join(STATE_KEYS))
    state_set.add_argument("value")
    state_set.add_argument("--by", required=True, help=f"署名位：{STATE_WRITERS[0]} 或 {STATE_WRITERS[1]}")

    checked = commands.add_parser("taskbook-check", help=argparse.SUPPRESS)
    checked.add_argument("ticket")
    checked.add_argument("--by", required=True)

    digest = commands.add_parser("digest")
    digest.add_argument("--hours", type=int, default=24)

    export = commands.add_parser("export")
    export.add_argument("ticket")
    export.add_argument("--out", help="在当前客户端落盘的 md 路径；不填则落到本位 _office 任务书目录")
    export.add_argument("--inbox", choices=["codex", "codely", "dsh"], help=argparse.SUPPRESS)

    commands.add_parser("build")

    migrate = commands.add_parser("migrate", help="迁移工单库或补齐状态进入时间")
    migrate.add_argument("--from", dest="source")
    migrate.add_argument("--to", dest="database")
    migrate.add_argument("--backfill-state-time", action="store_true")
    migrate.add_argument("--db", help="补齐 SQLite 库；不填则补本机文件库")
    migrate.add_argument("--force", action="store_true", help="补齐时覆盖已有状态进入时间")
    migrate.add_argument("--backfill-assign", action="store_true", help="回填历史需求/阻塞单的「指派给」：默认只打清单不写（T-000957）")
    migrate.add_argument("--apply", action="store_true", help="配 --backfill-assign 用：真写。不加就只打清单")

    dump = commands.add_parser("dump", help="把 SQLite 工单库导回文件目录")
    dump.add_argument("--db", required=True)
    dump.add_argument("--to", dest="target", required=True)

    demo = commands.add_parser("demo", help="管理演示数据")
    demo.add_argument("--archive", action="store_true", required=True)

    server = commands.add_parser("serve", help="启动工单台本地服务")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.add_argument("--token", default=os.environ.get("TICKET_SERVE_TOKEN", ""))
    server.add_argument("--token-file", default="")
    server.add_argument("--db", default="")
    server.add_argument("--tls-cert", default="")
    server.add_argument("--tls-key", default="")
    server.add_argument("--open", action="store_true")
    server.add_argument("--web-root", default="", help="静态件目录;不填用仓根下的 web/")

    account = commands.add_parser("account", help="管理服务端账号初始化")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    account_init = account_commands.add_parser("init")
    account_init.add_argument("--db", required=True)
    account_init.add_argument("--username", required=True)
    account_init.add_argument("--reopen-setup", action="store_true")
    account_rotate = account_commands.add_parser("rotate-service-token")
    account_rotate.add_argument("--token-file", required=True)

    environment = commands.add_parser("env", help="自检一行:本机还是远程、连哪台、令牌文件路径与在不在、本机库在哪、配置从哪一级取到")
    environment.add_argument("--probe", action="store_true", help="显式向服务器探测一次协议版本")
    return root


def compact_ticket(ticket: dict[str, Any]) -> str:
    budget = f"/{ticket['上下文预算']}行" if ticket.get("上下文预算") is not None else ""
    tier = ticket.get("任务档") or "待总监定"
    suffix = f"{tier}档" if tier in {"甲", "乙", "丙"} else tier
    shot = f" · {ticket.get('实机图标记')}" if ticket.get("实机图标记") else ""
    if ticket.get("实机图标记") == SHOT_EXEMPT and ticket.get(SHOT_EXEMPT_REASON):
        shot += f" · {ticket[SHOT_EXEMPT_REASON]}"
    # T-001509:未上服结案的单终态显示合成状态,不然一眼看去就是一张普通的「关闭」,
    # 与实机复验过后关掉的单分不清。
    state = "已合并·未上服·已结案" if ticket.get("未上服结案") else ticket["状态"]
    return f"{ticket['编号']} · {ticket['标题']} · {state} · {suffix}{budget}{shot}"


def _value_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "；".join(str(row) for row in value)
    return str(value if value is not None else "")


def _taskbook_check_state(args: argparse.Namespace) -> str:
    if not getattr(args, "taskbook", None):
        return ""
    if getattr(args, "taskbook_unchecked", False):
        return "已跳过"
    if getattr(args, "taskbook_client_checked", False):
        return "已核存在"
    return "待回核"


def execute(args: argparse.Namespace, service: TicketService) -> tuple[Any, str]:
    command = args.command
    if command == "new":
        if args.type == "派单":
            # ★五条必填一次数完再报,不要撞一条补一条——逐条拒会让人重敲三四遍长命令行,
            #   而每重敲一遍都是一次手抖的机会。判据与服务端共用同一个方法
            #   (服务端仍是唯一真闸,这里只是提早把话说全)。
            missing = TicketService.missing_dispatch_requirements(
                consumer=args.consumer, sources=args.source, task_tier=args.tier,
                deliverables=args.deliverable, internal=args.internal, include_entry_gates=True,
            )
            if missing:
                raise TicketError(TicketService.format_missing_requirements(missing))
            ticket = service.create_dispatch(args.slot, args.title, args.source, args.consumer, args.assign, args.by, args.notes, args.tier, args.context_lines, args.deliverable, args.internal, args.body, args.taskbook, _taskbook_check_state(args), window=args.window)
        elif args.type == "疑问":
            ticket = service.create_question("疑问", args.slot, args.title, args.body or args.notes or "请对方总监答复。", args.by, args.source, args.consumer, args.tier or "", args.context_lines, args.taskbook, _taskbook_check_state(args))
        else:
            ticket = service.create_question("阻塞", args.slot, args.title, args.notes or "需要总编排处理阻塞。", args.by, args.source, args.consumer, args.tier or "乙", args.context_lines, args.taskbook, _taskbook_check_state(args))
        if args.taskbook_unchecked and args.taskbook:
            service.audit_taskbook_skip(ticket, args.by, "new")
        if args.type == "派单" and args.deliverable_unchecked:
            service.audit_deliverable_skip(ticket, args.by, "new", args.deliverable)
        text = ticket["编号"]
        if args.type == "派单":
            text += "\n" + service.dispatch_instruction_text(ticket)
        return ticket, text
    if command == "set":
        if args.blame:
            # 责任归属走自己那条路：状态闸（判过/返工）与可改项闸（新建/已认领/返工）正好相反，
            # 混在一条命令里两道闸必然互相打架，所以要么改口径要么改归属，不许同时。
            mixed = [
                name for name, value in (
                    ("--taskbook", args.taskbook), ("--assign", args.assign), ("--source", args.source),
                    ("--body", args.body), ("--consumer", args.consumer), ("--deliverable", args.deliverable),
                    ("--internal/--user-facing", getattr(args, "internal", None)),
                ) if value is not None
            ]
            if mixed:
                raise TicketError(
                    f"set --blame 只改判退责任，不能和 {'、'.join(mixed)} 写在同一条命令里；请分两条跑。"
                )
            ticket, before, after = service.set_blame(args.ticket, args.blame, args.reason or "", args.by)
            return ticket, compact_ticket(ticket) + f"\n已改判退责任：{before} → {after}；理由：{args.reason.strip()}"
        if args.reason is not None:
            raise TicketError("set --reason 只在改判退责任时用，请连 --blame 模型|出题 一起给。")
        ticket, changes = service.edit(
            args.ticket, args.by, args.taskbook, args.assign, args.source, args.body, args.consumer,
            args.deliverable, _taskbook_check_state(args) if args.taskbook is not None else None,
            getattr(args, "internal", None), args.window, getattr(args, "tier", None),
        )
        if args.taskbook_unchecked and args.taskbook is not None:
            service.audit_taskbook_skip(ticket, args.by, "set")
        if args.deliverable_unchecked:
            service.audit_deliverable_skip(ticket, args.by, "set", args.deliverable)
        lines = [f"已改 {row['字段']}：{_value_text(row['旧值']) or '（空）'} → {_value_text(row['新值']) or '（空）'}" for row in changes]
        if args.taskbook is not None:
            lines.append(service.dispatch_instruction_text(ticket))
        return {"工单": ticket, "改动": changes}, "\n".join([compact_ticket(ticket), *lines])
    if command == "claim":
        ticket = service.claim(args.ticket, args.by)
        return ticket, compact_ticket(ticket)
    if command == "attach":
        ticket, image = service.attach(args.ticket, args.image, args.origin, args.by)
        return {"工单": ticket, "图片": image}, f"已附图 {image['文件名']} · {image['来源标注']}"
    if command == "submit":
        ticket = service.submit(
            args.ticket, args.evidence, args.verify_command, args.raw_output, args.deliverable_verified,
            args.handoff, _read_gate_report(args.gate_report),
        )
        # 「记忆重刷提示」是 submit 挂在返回值上的一句话，不进盘：成败都只影响这一行文字，
        # 绝不影响交板本身的成败（T-001073 R2 的硬要求）。
        notice = str(ticket.get("记忆重刷提示", ""))
        gate_note = str(ticket.get("机器闸提示", ""))
        rebuild_note = str(ticket.get("重刷提示", ""))
        tail = "".join(f"\n{line}" for line in (notice, gate_note, rebuild_note) if line)
        return ticket, compact_ticket(ticket) + tail
    if command == "judge":
        ticket, warning = service.judge(
            args.ticket, bool(args.passed), args.by, args.rework or "", args.verdict, args.blame,
            args.strike_handoff,
        )
        return {"工单": ticket, "提示": warning}, compact_ticket(ticket) + (f"\n{warning}" if warning else "")
    if command == "verify":
        ticket, hint = service.verify(args.ticket, args.by, args.result, args.gates, args.evidence)
        return {"工单": ticket, "提示": hint}, compact_ticket(ticket) + (f"\n{hint}" if hint else "")
    if command == "deploy-record":
        # 「部署目标」为空时上面那个选项没有候选集，这里给一句人话，
        # 而不是让它带着空 repo 往下走、在更深的地方报一个看不懂的错。
        if not DEPLOY_TARGETS:
            raise TicketError(
                "配置里的「部署目标」是空的，没有可记的上服目标。"
                "只把工单台当台账用的话，不需要 deploy-record；"
                "要用就先在 config 的「部署目标」里配一个（名字 / 值面键 / 归位）。"
            )
        ticket = service.deploy_record(args.head, args.probes, args.batch_tickets, args.repo, args.by)
        return ticket, compact_ticket(ticket) + f"\n上服记录已建,免判免复检;部署头已写进当前值面。取证请另开取证单。"
    if command == "evidence-ticket":
        ticket = service.evidence_ticket(args.head, args.slot, args.assign, args.batch_tickets)
        return ticket, compact_ticket(ticket) + "\n取证单已建(待独图);取不到图不挡上服。"
    if command == "merge":
        ticket = service.merge(args.ticket, args.by)
        return ticket, _with_retire_line(ticket, compact_ticket(ticket))
    if command == "live":
        if args.shot == SHOT_EXEMPT:
            _prepare_shot_exempt_check(args)
            ticket = service.shot_exempt(args.ticket, args.by, args.reason)
            return ticket, compact_ticket(ticket)
        if args.batch:
            if not args.image:
                raise TicketError(f"批量 live 必须提供一张 {LIVE_ORIGIN} 图。")
            rows = service.live_batch([args.ticket, *args.batch], args.image, args.by, args.shot)
            return rows, "\n".join(_with_retire_line(row, _live_batch_line(row)) for row in rows)
        ticket = service.live(args.ticket, args.image, args.by, args.shot)
        return ticket, _with_retire_line(ticket, compact_ticket(ticket))
    if command == "close":
        ticket = service.close(
            args.ticket, args.by, getattr(args, "not_merged", False),
            getattr(args, "reason", ""), getattr(args, "verdict", ""),
            getattr(args, "not_deployed", False),
        )
        return ticket, _with_retire_line(ticket, compact_ticket(ticket))
    if command == "rework":
        ticket = service.rework_from_review(args.ticket, args.reason, args.by, args.blame)
        notice = str(ticket.get("提示", ""))
        return ticket, compact_ticket(ticket) + f"\n{ticket['流程提示']}" + (f"\n{notice}" if notice else "")
    if command == "void":
        ticket = service.void(args.ticket, args.reason, args.by)
        return ticket, _with_retire_line(ticket, compact_ticket(ticket) + f"\n{ticket['流程提示']}")
    if command == "block":
        ticket = service.block(args.ticket, args.reason, args.by, args.kind)
        return ticket, compact_ticket(ticket) + f"\n{ticket['流程提示']}"
    if command == "unblock":
        ticket = service.unblock(args.ticket, args.by)
        return ticket, compact_ticket(ticket) + f"\n{ticket['流程提示']}"
    if command == "transfer":
        ticket = service.transfer(args.ticket, args.to, args.reason, args.by)
        return ticket, f"{compact_ticket(ticket)}\n已转交：{args.to} · {args.reason}"
    if command == "ask":
        ticket = service.create_question(args.type, args.slot, args.title, args.body, args.by, args.source, args.consumer, args.tier, args.context_lines)
        return ticket, ticket["编号"]
    if command == "answer":
        ticket = service.answer(args.ticket, args.answer, args.by)
        return ticket, compact_ticket(ticket)
    if command == "say":
        row = service.say(args.slot, args.by, args.text, args.img, args.ref)
        return row, f"已写入 {args.slot} 对话线 · {row['时间']}"
    if command == "inbox":
        rows = service.inbox(args.slot, args.actor, args.mark_read)
        text = "\n".join(f"{row['时间']} · {row['发言人']}：{row['文字']}" + (f"（引用 {row['引用工单号']}）" if row.get("引用工单号") else "") for row in rows)
        # T-000957 R1：每位第 0 步都跑 inbox，把「你位还欠几张没答」挂在它尾巴上，谁都漏不掉。
        pending = service.pending_answer_line(args.slot)
        body = text or "没有未读对话。"
        return rows, (body + "\n" + pending) if pending else body
    if command == "staff":
        if args.staff_command == "new":
            member = service.staff_new(args.slot, args.tool)
            return member, member["员工名"] + (f"\n{member['提示']}" if member.get("提示") else "")
        if args.staff_command == "retire":
            member = service.staff_retire(args.name)
            return member, f"{member['员工名']} · 已收窗"
        if args.staff_command == "reopen":
            member = service.staff_reopen(args.name)
            return member, f"{member['员工名']} · 已重开"
        if args.staff_command == "ban":
            detail = service.staff_ban(args.tool, args.by, args.slot, args.reason)
            return {"说明": detail}, detail
        if args.staff_command == "unban":
            detail = service.staff_unban(args.tool, args.by, args.slot)
            return {"说明": detail}, detail
        if args.staff_command == "fix":
            member = service.staff_fix(args.name, args.by, args.memory)
            text = f"{member['员工名']} · 固定工位 · 记忆件 {member['记忆md路径']}"
            return member, text + (f"\n{member['提示']}" if member.get("提示") else "")
        if args.staff_command == "unfix":
            member = service.staff_unfix(args.name, args.by)
            return member, f"{member['员工名']} · 已取消固定工位 · 记忆件路径保留 {member.get('记忆md路径', '')}"
        # T-001081：名册默认只列在岗——它是「现在能派给谁」的清单，不是履历表。
        # 履历走 history / memory export，模型合格率按模型统计，两者都读全量，退役一分不丢。
        members = service.list_staff(args.slot or None, args.all)
        rows = "\n".join(
            f"{row['员工名']} · {row['工具/窗类型']} · {row['状态']}"
            + (" · 固定工位" if row.get("固定工位") else "")
            for row in members
        )
        # 「藏了几位」要照实数出来：一个人都没藏的时候提 --all 是纯噪音，
        # 名册整个是空的更不该说成「没有在岗」——那是两回事。
        hidden = 0 if args.all else len(service.list_staff(args.slot or None, True)) - len(members)
        if not hidden:
            return members, rows or "名册为空。"
        tail = f"（另有 {hidden} 位已收窗；加 --all 看全量）"
        return members, (rows + "\n" + tail) if rows else "没有在岗员工。" + tail
    if command == "memory":
        if args.memory_command == "data":
            payload = service.memory_payload(args.staff)
            return payload, json.dumps(payload, ensure_ascii=False, indent=2)
        path = service.memory_export(args.staff, args.out or None, args.max_lines)
        return {"path": str(path)}, str(path)
    if command == "history":
        history = service.history(args.name)
        return history, "\n".join([f"{args.name} · {history['员工']['状态']}"] + [compact_ticket(row) for row in history["工单"]])
    if command == "show":
        ticket = service.store.load_ticket(args.ticket)
        return ticket, json.dumps(ticket, ensure_ascii=False, indent=2)
    if command == "list":
        if getattr(args, "pending_mine", False):
            if not args.slot:
                raise TicketError("--pending-mine 要配 --slot <你的位名>：它就是「这一位还欠着没答的单」。")
            rows = service.pending_answers(args.slot)
            return rows, "\n".join(compact_ticket(row) for row in rows) or "本位没有待答的单。"
        if getattr(args, "nonbiz", False):
            # T-001256 ④:非业务阻塞不改状态、不进老化告警、不占设计者队列——
            # 这张看板就是它唯一的出口,所以做成默认显眼的一段,不藏在折叠里。
            rows = service.non_business_blocked(args.slot or None)
            lines = [
                f"{row['编号']} · {row['标题']} · {row['状态']} · {row['所属总监位']} · 待清 {row['条数']} 条\n"
                + "\n".join(f"    · {item['时间']} {item['报告人']}:{item['原因']}" for item in row["非业务阻塞"])
                for row in rows
            ]
            return rows, "\n".join(lines) or "没有待清的非业务阻塞。"
        rows = service.list_tickets(args.slot, args.state, args.ticket_type, args.shot_pending)
        return rows, "\n".join(compact_ticket(row) for row in rows) or "没有符合条件的工单。"
    if command == "receipt":
        ticket = service.store.load_ticket(args.ticket)
        receipt = service.receipt(ticket)
        # 摘要只放进 payload，不进 text：text 后面还要被 receipt_with_protocol 追加协议号，
        # 摘要要是先并进 text，协议号就会落在摘要那一行的屁股上。拼接统一在最外层做。
        return {"receipt": receipt, "值面摘要": service.state_summary_line()}, receipt
    if command == "state":
        if args.state_command == "get":
            board = service.state_board()
            if board["未填"]:
                # 提示走 stderr：stdout 必须是干净 JSON，不然 json.loads 会被这行字噎死。
                print(
                    f"{STATE_UNSET_HINT}：" + "、".join(board["未填"])
                    + f"。例：ticket.py state set {STATE_KEYS[0]} <值> --by {STATE_WRITERS[0]}",
                    file=sys.stderr,
                )
            return board, json.dumps(board, ensure_ascii=False, indent=2)
        row = service.state_set(args.key, args.value, args.by)
        return row, (
            f"已改 {row['键']}：{state_text(row['旧值'])} → {state_text(row['新值'])}"
            f"（{row['改动人']} · {row['时间']}）"
        )
    if command == "taskbook-check":
        ticket = service.confirm_taskbook(args.ticket, args.by)
        return ticket, compact_ticket(ticket)
    if command == "digest":
        lines = service.digest(args.hours)
        return {"lines": lines}, "\n".join(lines)
    if command == "export":
        if args.inbox:
            raise TicketError("--inbox 已废弃，改用 --out；不写 --out 会自动落到所属总监位的任务书目录。")
        path = service.export(args.ticket, args.out)
        return {"path": str(path)}, str(path)
    if command == "build":
        path = service.build_bundle()
        return {"path": str(path)}, str(path)
    if command == "demo" and args.archive:
        result = service.archive_demo()
        return result, f"演示数据归档完成：工单 {result['工单']}，图片 {result['图片']}，对话 {result['对话']}，员工 {result['员工']}。"
    raise TicketError(f"未实现的命令：{command}")


def _replace_option(arguments: list[str], name: str, value: str) -> None:
    try:
        arguments[arguments.index(name) + 1] = value
    except (ValueError, IndexError):
        pass


def _live_batch_line(row: dict[str, Any]) -> str:
    if row.get("结果") == "已复验":
        return f"{row['编号']} · 已复验 · {row.get('实机图标记', '')}"
    return f"{row['编号']} · 跳过:{row.get('原因', '')}"


def _read_gate_report(value: str) -> str:
    """--gate-report 收「路径」或「原样输出」两种写法（D9-460 ②）。

    ★判断方式是「这个路径在本机存在吗」,不是猜格式:六项报告本来就是多行文本,
      而路径是一行——用行数猜必然在「只跑了一项」的场景上判错。
    ★读不到的路径**不静默当成文本**:那会让一份根本不存在的报告被当成「六项都缺」,
      交板照样成功、只是不置标——人会以为闸没过在改代码,其实是路径写错了。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = Path(text)
    try:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except OSError as error:
        raise TicketError(f"--gate-report 指向的文件读不出来：{candidate}（{error}）") from error
    if "\n" not in text and text.lower().endswith((".txt", ".log", ".md", ".json")):
        raise TicketError(
            f"--gate-report 看着是个文件路径,但本机找不到它：{text}。"
            "请确认路径,或直接把六项报告的原样输出贴进来。"
        )
    return text


def _with_retire_line(row: dict[str, Any], text: str) -> str:
    """把「自动退役提示」接在结案类回执后面（T-001081）。

    与 submit 的「记忆重刷提示」同一条路子：它挂在**返回值**上、不进盘，
    成败都只影响这一行文字。名册被动过就得当场说出来——
    只回一句「已关闭」，谁也不知道那个窗已经被收了。
    """
    notice = str(row.get("自动退役提示", "")).strip()
    return f"{text}\n{notice}" if notice else text


def _prepare_taskbook_check(args: argparse.Namespace, arguments: list[str]) -> bool:
    """在提交端核任务书；返回 True 表示 new 的 {ticket} 要等拿号后回核。"""
    if args.command not in {"new", "set"}:
        return False
    value = getattr(args, "taskbook", None)
    unchecked = bool(getattr(args, "taskbook_unchecked", False))
    if unchecked and not value:
        raise TicketError("--taskbook-unchecked 必须和 --taskbook 一起使用。")
    if not value:
        return False
    if args.command == "set" and "{ticket}" in value:
        value = value.replace("{ticket}", args.ticket.upper())
        args.taskbook = value
        _replace_option(arguments, "--taskbook", value)
    if args.command == "new" and "{ticket}" in value and not unchecked:
        absolute = str(Path(value).expanduser().resolve())
        args.taskbook = absolute
        _replace_option(arguments, "--taskbook", absolute)
        return True
    if unchecked:
        absolute = str(Path(value).expanduser().resolve())
        args.taskbook = absolute
        _replace_option(arguments, "--taskbook", absolute)
        return False
    absolute = Path(value).expanduser().resolve()
    if not absolute.is_file():
        action = "建单" if args.command == "new" else "改单"
        raise TicketError(
            f"任务书还没写，先把 md 落盘再{action}。核验的绝对路径：{absolute}"
        )
    args.taskbook = str(absolute)
    args.taskbook_client_checked = True
    _replace_option(arguments, "--taskbook", str(absolute))
    arguments.append("--taskbook-client-checked")
    return False


DELIVERABLE_NARRATIVE_MARKERS = ("。", "，", ",", "的")


def _prepare_shot_exempt_check(args: argparse.Namespace) -> None:
    """--shot 免独图 的写法闸；放在解析后、发远程前，本机与远程两条路都走得到。"""
    if getattr(args, "command", "") != "live":
        return
    if args.shot != SHOT_EXEMPT:
        if args.reason.strip():
            raise TicketError(f"--reason 只配 --shot {SHOT_EXEMPT} 用；拍了图的 live 请把原因写进判语或事件说明。")
        return
    if args.batch:
        raise TicketError(
            f"--shot {SHOT_EXEMPT} 不能和 --batch 一起用:批量是给一张图配多张单用的,"
            "豁免必须一单一原因,请一张一张打。"
        )
    if args.image:
        raise TicketError(
            f"--shot {SHOT_EXEMPT} 不要图:请去掉图片路径,写成 "
            f"live <单号> --shot {SHOT_EXEMPT} --reason <原因> --by <署名>。"
        )


def _prepare_deliverable_check(args: argparse.Namespace) -> None:
    """建单/改单发出前，在操作人的当前检出里前置 submit 的文件闸。"""
    if args.command not in {"new", "set"}:
        return
    if args.command == "new" and args.type != "派单":
        if getattr(args, "deliverable_unchecked", False):
            raise TicketError("--deliverable-unchecked 只用于带 --deliverable 的派单。")
        return
    rows = normalize_lines(getattr(args, "deliverable", None))
    unchecked = bool(getattr(args, "deliverable_unchecked", False))
    if unchecked and not rows:
        raise TicketError("--deliverable-unchecked 必须和 --deliverable 一起使用。")
    if not rows or unchecked:
        return

    missing: list[tuple[str, Path]] = []
    for row in rows:
        candidate = deliverable_candidate(row)
        if is_image_deliverable(candidate):
            continue
        absolute = Path(candidate).expanduser().resolve()
        if absolute.is_file():
            continue
        if _is_narrative_deliverable(candidate):
            raise TicketError(
                "交付项要写成仓内相对路径或真实文件路径,叙述句永远交不了板。"
                f"这一条:{row}"
            )
        missing.append((row, absolute))

    # T-000447/T-000458:形态对但现在不存在,只提醒不拦下。
    # 派单的交付项按定义就是「还不存在、要执行方做出来」的东西,建单那一刻必然不存在;
    # 在这里硬拦等于把所有产新文件的派单一律拦死,各位只能造空占位文件绕过——
    # 而占位件一存在,submit 那条真闸就永远核得过,闸反而被绕成了摆设。
    # 存在性一律留给 submit 逐条核(那条闸原样不动),这里只把话说在前头。
    if missing:
        print(
            "提醒:以下交付项现在还不存在,单照常建/改,但交板时会被逐条核:\n"
            + "\n".join(f"  · {row} → {absolute}" for row, absolute in missing)
            + "\n要执行方做出来的东西,做出来再交板即可;"
            "如果它只会存在于某个分支上(比如只推 hk 的部署件),交板时仍会被同一条闸拦下——"
            "那一类请现在就改成提交端真的会有的路径。",
            file=sys.stderr,
        )


def _is_narrative_deliverable(candidate: str) -> bool:
    """只按形态判叙述句,不看文件在不在。

    T-000458:旧写法是「(没分隔符也没扩展名) or 含叙述标记」,而那个 or 是无条件的。
    它上面「文件存在就跳过」的保护对派单永远不成立(派单交付项本来就还没产出),
    于是任何带「的」或逗号的合法中文路径,在派单场景 100% 被误判成叙述句,
    报错还把人往「你写的是叙述句」这个错方向指。各位目录清一色中文,人人会撞。
    所以:带路径分隔符的一律当路径;叙述标记只用来判没有分隔符的那一类。
    """
    if "/" in candidate or "\\" in candidate:
        return False
    if not Path(candidate).suffix:
        return True
    return any(mark in candidate for mark in DELIVERABLE_NARRATIVE_MARKERS)


def _missing_deferred_taskbook_text(path: Path) -> str:
    return (
        "单已建但任务书还没写，现在它不会进设计者队列。"
        "请先把 md 落盘再用 set --taskbook 补上。"
        f"核验的绝对路径：{path}"
    )


def _finish_remote_taskbook_check(
    client: RemoteClient, args: argparse.Namespace, payload: Any, output: str,
) -> tuple[Any, str]:
    ticket = payload if isinstance(payload, dict) else {}
    final_path = Path(str(ticket.get("任务书路径", ""))).expanduser().resolve()
    if not final_path.is_file():
        warning = _missing_deferred_taskbook_text(final_path)
        visible = dict(ticket, 任务书提醒=warning)
        return visible, output + "\n" + warning
    try:
        checked, _ = client.execute(["taskbook-check", str(ticket["编号"]), "--by", args.by])
    except TicketError as exc:
        warning = (
            "单已建且本机任务书存在，但校验结果没能写回服务器；"
            "它暂时不会进设计者队列。请重跑 set --taskbook。"
            f"原因：{exc}"
        )
        return dict(ticket, 任务书提醒=warning), output + "\n" + warning
    return checked, output


def _finish_local_taskbook_check(
    service: TicketService, args: argparse.Namespace, payload: Any, output: str,
) -> tuple[Any, str]:
    ticket = payload if isinstance(payload, dict) else {}
    final_path = Path(str(ticket.get("任务书路径", ""))).expanduser().resolve()
    if not final_path.is_file():
        warning = _missing_deferred_taskbook_text(final_path)
        return dict(ticket, 任务书提醒=warning), output + "\n" + warning
    return service.confirm_taskbook(str(ticket["编号"]), args.by), output


def _refresh_memory_here(client: RemoteClient, args: argparse.Namespace, text: str) -> str:
    """远程模式下，交板/判卷之后在**本机**重刷一次固定工位的记忆件（T-001073 R2）。

    服务端也会试着重刷一次，但它跑在 Linux 上、路径是 D:\\...，必然失败并回一行
    「记忆 md 重刷失败」。那一行在这里是噪声：本机知道得更准，所以把服务端那几行
    去掉，换成本机自己这一次的结果。失败照旧只是一行字——不许反过来把交板弄失败。
    """
    kept = [line for line in text.splitlines() if not line.startswith(MEMORY_REFRESH_PREFIX)]
    try:
        # 记忆件跟着**执行方**走，不是跟着跑命令的人走：判卷人是总监，记忆件是员工的。
        ticket, _ = client.execute(["show", args.ticket])
        worker = str((ticket or {}).get("指派给", "")).strip()
        if not worker:
            return "\n".join(kept)
        data, _ = client.execute(["memory", "data", "--staff", worker])
        if not data or not data.get("固定工位") or not str(data.get("记忆md路径", "")).strip():
            return "\n".join(kept)
        path = TicketService.write_memory(data)
    except Exception as exc:  # noqa: BLE001 —— 附加动作不许卡住交板
        return "\n".join([*kept, f"{MEMORY_REFRESH_PREFIX}失败:{exc}"])
    return "\n".join([*kept, f"{MEMORY_REFRESH_PREFIX}完成:{path}"])


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    # --local:强制本机模式,不看任何远程配置。测试与离线自查用它,保证永远碰不到真服务器。
    force_local = "--local" in arguments
    arguments = [value for value in arguments if value not in {"--json", "--local"}]
    channel: channel_config.Channel | None = None
    try:
        args = parser().parse_args(arguments)
        if args.command == "export" and args.inbox:
            raise TicketError("--inbox 已废弃，改用 --out；不写 --out 会自动落到所属总监位的任务书目录。")
        deferred_taskbook = _prepare_taskbook_check(args, arguments)
        _prepare_shot_exempt_check(args)
        _prepare_deliverable_check(args)
        _prepare_world_shot_check(args)
        channel = channel_config.resolve(force_local)
        if args.command == "env":
            # 自检永远不出网、不碰库、不读令牌内容:配置再烂也要把它原样报出来。
            detail = channel_config.describe_payload(channel)
            detail["客户端协议"] = channel_config.PROTOCOL_VERSION
            text = channel_config.describe(channel)
            if not args.probe:
                text += f" · 客户端协议 {channel_config.PROTOCOL_VERSION}(服务端版本要加 --probe 才查)"
            else:
                try:
                    if channel.problem:
                        raise TicketError(channel.problem)
                    if not channel.is_remote:
                        raise TicketError("当前没有配置远程工单台")
                    client = RemoteClient(channel.remote, channel.token_file, channel.ca_sha256)
                    client.request("POST", "/api/cli", {"argv": ["list"]})
                    detail["服务端协议"] = client.server_protocol
                    text += f" · 客户端协议 {channel_config.PROTOCOL_VERSION} · 服务端协议 {client.server_protocol}"
                    # T-001508:核时区/时钟从此看这一格,不用再拿本机 date 猜——
                    # 本机时区与工单台的 +08:00 不一致时,直接比墙钟必错。
                    if client.server_time:
                        detail["服务器时刻"] = client.server_time
                        text += f" · 服务器时刻 {client.server_time}"
                except (TicketError, OSError) as exc:
                    detail["服务端版本没查到"] = str(exc)
                    text += f"\n服务端版本没查到:{exc}"
            print(json.dumps(detail, ensure_ascii=False) if json_output else text)
            return 0
        if args.command == "migrate":
            if getattr(args, "backfill_assign", False):
                target_store = SqliteStore(args.db) if args.db else TicketStore(channel.local_root)
                report = TicketService(target_store).backfill_question_assignees(bool(getattr(args, "apply", False)))
                head = "已回填" if report["已写入"] else "只打清单（未写入；加 --apply 才动）"
                lines = [f"{head}：命中 {report['命中']} 张"] + [
                    f"  {row['编号']} · {row['类型']} · 所属 {row['所属总监位']} · 指派给 {row['旧值']} → {row['新值']}"
                    for row in report["明细"]
                ]
                print(json.dumps(report, ensure_ascii=False, indent=2) if json_output else "\n".join(lines))
            elif args.backfill_state_time:
                if args.source or args.database:
                    raise TicketError("补齐状态进入时间不能与 --from/--to 同时使用。")
                target_store = SqliteStore(args.db) if args.db else TicketStore(channel.local_root)
                report = target_store.backfill_state_times(args.force)
                text = f"状态进入时间补齐完成：已补齐 {report['已补齐']}，已跳过 {report['已跳过']}。"
                print(json.dumps(report, ensure_ascii=False, indent=2) if json_output else text)
            else:
                if not args.source or not args.database:
                    raise TicketError("迁入 SQLite 必须同时填写 --from 和 --to；补时间请用 --backfill-state-time。")
                report = SqliteStore(args.database).import_files(args.source)
                print(json.dumps(report, ensure_ascii=False, indent=2) if json_output else _reconciliation_text(report))
            return 0
        if args.command == "dump":
            report = SqliteStore(args.db).dump_files(args.target)
            print(json.dumps(report, ensure_ascii=False, indent=2) if json_output else _dump_text(report, args.target))
            return 0
        if args.command == "account":
            if args.account_command == "init":
                result = AccountManager(args.db).init_admin(args.username, args.reopen_setup)
                print(json.dumps(result, ensure_ascii=False) if json_output else f"管理员账号已就绪：{result['用户名']} · 首次设密窗口 {result['有效分钟']} 分钟")
            else:
                target = Path(args.token_file).resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass
                print(json.dumps({"ok": True}, ensure_ascii=False) if json_output else "服务令牌已轮换；旧令牌立即失效。")
            return 0
        stale_notice: list[str] = []
        stale_exit = 0
        if channel.is_remote and args.command not in OFFLINE_COMMANDS:
            if channel.problem:
                raise TicketError(channel.problem)
            try:
                client = RemoteClient(channel.remote, channel.token_file, channel.ca_sha256)
                if args.command == "submit":
                    arguments = _check_deliverables_here(client, args.ticket, arguments)
                    arguments = _inline_gate_report_here(arguments)
                if args.command == "export":
                    ticket, _ = client.execute(["show", args.ticket])
                    path = TicketService.export_ticket(ticket, args.out)
                    payload, text = {"path": str(path)}, str(path)
                elif args.command == "memory" and args.memory_command == "export":
                    # 记忆件落在工作机的 D: 盘上，服务器上根本没有这个盘。
                    # 所以和 export 走同一条路子：服务端只出原料，落盘由客户端做。
                    data, _ = client.execute(["memory", "data", "--staff", args.staff])
                    path = TicketService.write_memory(data, args.out or None, args.max_lines)
                    payload, text = {"path": str(path)}, str(path)
                else:
                    payload, text = client.execute(arguments)
                    if args.command in {"submit", "judge"}:
                        text = _refresh_memory_here(client, args, text)
                if deferred_taskbook:
                    payload, text = _finish_remote_taskbook_check(client, args, payload, text)
                print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False) if json_output else text)
                return 0
            except RemoteUnavailable as exc:
                if _is_mutating(args):
                    raise TicketError(f"{exc}；为防止数据分叉，本次写操作没有落回本机。") from exc
                snapshot = _snapshot_time(channel.local_root)
                if os.environ.get("TICKET_ALLOW_STALE", "").strip() != "1":
                    lines = [
                        f"远程不可达，本次没有读到服务器上的数据。原因:{exc}",
                        f"本机另有一份只读快照，快照时间 {snapshot}，但它不是服务器上的数据，"
                        "不要据此判断、建单或报障。",
                        "确实要看这份旧快照，请显式加 TICKET_ALLOW_STALE=1 重跑；退出码仍为 3。",
                    ]
                    _print_both(lines, json_output, {"ok": False, "远程失败原因": str(exc), "本机快照时间": snapshot})
                    return 3
                stale_notice = [
                    f"警告:以下不是服务器上的数据，是本机只读快照，快照时间 {snapshot}。",
                    f"远程失败原因:{exc}",
                    "不要据此判断、建单或报障；通道恢复后请先跑 receipt <一个已知单号> 核通道。",
                ]
                stale_exit = 3
                # 提示必须在数据之前就出现：只挂在末尾的话，长列表一滚就看不见了。
                _print_both(stale_notice, json_output, {})
        elif channel.level == channel_config.LEVEL_NONE and args.command not in OFFLINE_COMMANDS:
            # 什么配置都没有就静默走本机，正是 T-000108 方向一「活白干」的根子；这一行必须出现。
            print(
                f"本机模式(未接通道):本机库 {channel_config.forward_slashes(str(channel.local_root))};"
                f"这里不是服务器上的数据。接通道:{channel_config.connect_hint(channel)}。",
                file=sys.stderr,
            )
        service = TicketService(SqliteStore(args.db) if args.command == "serve" and args.db else None)
        if args.command == "serve":
            token = args.token
            if args.token_file:
                token_path = Path(args.token_file)
                if not token_path.is_file():
                    raise TicketError("找不到服务令牌文件。")
                token = token_path.read_text(encoding="utf-8").strip()
            if args.db and (not args.tls_cert or not args.tls_key):
                raise TicketError("数据库服务模式必须同时提供 --tls-cert 与 --tls-key。")
            if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.db and not token:
                raise TicketError("远程监听必须启用数据库账号或提供令牌文件。")
            if not 0 <= args.port <= 65535:
                raise TicketError("端口必须在 0 到 65535 之间。")
            serve(
                service, args.host, args.port, token, args.open, args.tls_cert, args.tls_key,
                AccountManager(args.db) if args.db else None, args.web_root,
            )
            return 0
        mutating = args.command in {"new", "set", "void", "claim", "attach", "submit", "judge", "verify", "deploy-record", "evidence-ticket", "merge", "live", "close", "block", "unblock", "transfer", "ask", "answer", "say", "export", "build", "demo", "taskbook-check"}
        mutating = mutating or (args.command == "inbox" and args.mark_read) or args.command == "staff"
        mutating = mutating or (args.command == "state" and args.state_command == "set")
        if mutating:
            with service.store.locked():
                payload, text = execute(args, service)
        else:
            payload, text = execute(args, service)
        if args.command == "receipt":
            text = channel_config.receipt_with_protocol(
                text, channel_config.PROTOCOL_VERSION, channel_config.PROTOCOL_VERSION,
            )
            if isinstance(payload, dict) and "receipt" in payload:
                payload = dict(payload, receipt=text)
            text = with_state_summary(text, payload)
        if deferred_taskbook:
            payload, text = _finish_local_taskbook_check(service, args, payload, text)
        if json_output:
            body: dict[str, Any] = {"ok": True, "result": payload}
            if stale_notice:
                body["ok"] = False
                body["警告"] = stale_notice
            print(json.dumps(body, ensure_ascii=False))
        else:
            print(text)
        if stale_notice:
            _print_both([f"以上是本机旧快照，不是服务器上的数据；退出码 {stale_exit}。"], json_output, {})
        return stale_exit
    except (TicketError, OSError) as exc:
        message = _explain_missing_ticket(str(exc), channel)
        if json_output:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        else:
            print(f"拦下:{message}", file=sys.stderr)
        return 2


MISSING_TICKET = re.compile(r"找不到工单 (T-\d{6})")


def _explain_missing_ticket(message: str, channel: "channel_config.Channel | None") -> str:
    """「找不到工单」必须说清是在哪个模式下找不到的。

    美术两个窗 submit T-000069 得到笼统的「找不到工单」，把人引去查单号，而不是查通道——
    其实是压根没接通道，本机库里只有 T-000001~14。本机与远程两种情形分开说，不许混。
    """
    match = MISSING_TICKET.search(message)
    if not match or channel is None:
        return message
    ticket_id = match.group(1)
    if channel.is_remote:
        return f"远程模式:服务器 {channel.host} 上没有这张单 {ticket_id}。"
    root = channel_config.forward_slashes(str(channel.local_root))
    return (
        f"{channel.mode_label}:本机库 {root} 里没有 {ticket_id}。\n"
        f"如果这张单在服务器上,先接通道:{channel_config.connect_hint(channel)}。"
    )


def _inline_gate_report_here(arguments: list[str]) -> list[str]:
    """把 `--gate-report <本机路径>` 就地换成文件内容再发给服务端（D9-460 ②）。

    与 _check_deliverables_here 同一个道理:远程模式下 submit 整个动作在服务器上跑,
    而报告文件在**提交这台机器**上(多半是 D:\\ 开头的 Windows 路径),
    服务器上根本没有那个盘。不在这里内联,服务端会把路径本身当成报告正文去解析,
    六项一个都匹配不上 ⇒ 判成「六项全缺」⇒ 不置标,而交板照样成功。
    人看到的是「闸没过」,于是回去查代码——真因却是路径没被读到。这类静默错最难查。
    """
    for index, value in enumerate(arguments):
        if value == "--gate-report" and index + 1 < len(arguments):
            arguments = list(arguments)
            arguments[index + 1] = _read_gate_report(arguments[index + 1])
            return arguments
        if value.startswith("--gate-report="):
            arguments = list(arguments)
            arguments[index] = "--gate-report=" + _read_gate_report(value.split("=", 1)[1])
            return arguments
    return arguments


def _check_deliverables_here(client: "RemoteClient", ticket_id: str, arguments: list[str]) -> list[str]:
    """交板前在本机核交付项，核过了才把结论带给服务端。

    交付项写的是提交这台机器上的路径（多半是 D:\\ 开头的 Windows 绝对路径）。
    远程模式下 submit 整个动作在服务器上跑，拿服务器的文件系统去查这些路径，
    一张也过不了。所以「文件存不存在」只能在本机问，服务端只收结论。
    图片类交付项不搬，仍旧由服务端核。
    """
    if "--deliverable-verified" in arguments:
        raise TicketError("--deliverable-verified 由本机自动填写，不要手工传。")
    ticket, _ = client.execute(["show", ticket_id])
    rows = normalize_lines((ticket or {}).get("交付项"))
    if not rows:
        return arguments
    missing: list[str] = []
    checked = 0
    root = _submitting_repo_root()
    for row in rows:
        candidate = deliverable_candidate(row)
        if is_image_deliverable(candidate):
            checked += 1
            continue
        if Path(candidate).is_file():
            checked += 1
            continue
        # T-001256 ①:写法不同不算缺。目录型交付项、以及「工作树路径 vs 主检出路径」
        # 这两类前缀差,一律折回仓相对路径再核一次(D9-434 的写法口径)。
        # 只在上面那条原判据没过时才走这里,所以它只会放行、不会新拦任何单。
        if resolve_under_root(candidate, root) is not None:
            checked += 1
            continue
        missing.append(row)
    if missing:
        message = TicketService.missing_deliverables_error(ticket, missing)
        raise TicketError(
            message
            + f"\n（以上是在本机 {Path.cwd()} 核的；远程模式下交付项一律以提交端的文件系统为准。）"
        )
    return arguments + ["--deliverable-verified", f"{checked}/{len(rows)}"]


def _prepare_world_shot_check(args: argparse.Namespace) -> None:
    """真实环境判据图交单口的闸（默认关，见 config「判据图闸」）。

    它挡的不是尺寸不对，是**这张图不是取图链跑出来的**——也就是有人手工拼了
    一张图冒充实机证据。判据是「图旁边有没有那份同名的出处小文件」。

    ★闸读的是**出处小文件里的 PNG_DIM 行**,不是入单附件本身：
      入单的图都会被压到长边 ≤1280(原图不入仓),照附件量会**张张误伤**。
    ★只盖命令行这条路：网页上传拿不到同目录的出处 txt,
      而且网页上传多是随手贴图,判卷人自己开原图核即可。
    ★闸在客户端：出处小文件在交板人那台机器上,远程模式下服务端根本看不到它。
    ★★默认关：没有取图链的话，任何手工截图都会被拒——
      那时候它不是一道闸，是一条走不通的路。
    """
    if not SHOT_GATE_ON:
        return
    if args.command == "attach":
        if getattr(args, "origin", "") != "world":
            return
        image = getattr(args, "image", "")
    elif args.command == "live":
        image = getattr(args, "image", "") or ""
        if not image:  # 内部单免图、免独图、批量共用图那几条路不走这里
            return
        # ★--shot 还没填时让位:那是更基础的一条(「这张图是同图还是独图」),
        # 由 service 统一报。两条一起堆上去,人只会看见后加的这条,
        # 反而找不到真正该先补的那一格(本闸曾把那句话整个盖掉)。
        if not str(getattr(args, "shot", "") or "").strip():
            return
    else:
        return
    if not str(image).strip():
        return

    source = Path(str(image)).expanduser()
    provenance = Path(provenance_path_for(str(source)))
    if not provenance.is_file():
        raise TicketError(
            f"不能收这张 world 判据图:找不到它的出处小文件({provenance})。\n"
            "判据图必须是取图链正常跑出来的那一张——出处只在取图成功那一趟才写,"
            "没有出处就说明这张图不是那条链的产物(宪法八章 12)。\n"
            "请用取图链重取一趟;确实是特殊情形,请该单所属总监位或复检席代为处置。"
        )
    dimension = read_provenance_dimension(provenance.read_text(encoding="utf-8", errors="replace"))
    if not dimension:
        raise TicketError(
            f"不能收这张 world 判据图:出处小文件里没有 {PROVENANCE_DIM_KEY} 那一行({provenance})。\n"
            "取图链每张都会写这一行;没有多半是出处被手工改过或只写了一半,请重取一趟。"
        )
    if dimension == JUDGING_RESOLUTION:
        return
    if getattr(args, "layout_extra", False) and dimension == LAYOUT_EXTRA_RESOLUTION:
        return
    raise TicketError(
        f"判据图须 {JUDGING_RESOLUTION.replace('x', '×')}(见 config「判据图闸·尺寸」),本图 {dimension};"
        f"布局类第二张 {LAYOUT_EXTRA_RESOLUTION.replace('x', '×')} 请用 --origin world --layout-extra。\n"
        f"★取图工具的默认输出本来就该是 {JUDGING_RESOLUTION},什么都不传就对;"
        "要是你的工具默认不是这个尺寸,改 config「判据图闸·尺寸」,别去改任务书。\n"
        f"出处小文件:{provenance}"
    )


def _submitting_repo_root() -> Path:
    """交板这台机器上的仓根:从当前目录往上找到带 .git 的那一级；找不到就用当前目录。

    工作树的 .git 是个文件不是目录，所以用 exists() 不用 is_dir()。
    """
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def _print_both(lines: list[str], json_output: bool, payload: dict[str, Any]) -> None:
    """回落提示必须两个流都看得见：stdout 给人看，stderr 给脚本和日志看。"""
    for line in lines:
        print(line, file=sys.stderr)
    if json_output:
        if payload:
            print(json.dumps(dict(payload, 警告=lines), ensure_ascii=False))
        return
    for line in lines:
        print(line)


def _snapshot_time(root: Path) -> str:
    """本机只读快照最后一次被写动的时间；说不清就明说说不清，不许糊过去。"""
    stamps: list[float] = []
    for name in ("log.jsonl", "counter.json", "staff.json"):
        candidate = root / name
        if candidate.is_file():
            stamps.append(candidate.stat().st_mtime)
    items = root / "items"
    if items.is_dir():
        stamps.extend(path.stat().st_mtime for path in items.glob("T-*.json"))
    if not stamps:
        return "无——本机根本没有快照"
    return datetime.fromtimestamp(max(stamps)).astimezone().isoformat(timespec="seconds")


def _reconciliation_text(report: dict[str, Any]) -> str:
    return (
        "迁移对账表\n"
        f"工单 {report['工单']} · 图片 {report['图片']} · 对话 {report['对话']}\n"
        f"工单字段全同 {'是' if report['工单字段全同'] else '否'} · 图片 SHA 全同 {'是' if report['图片SHA全同'] else '否'}"
    )


def _dump_text(report: dict[str, Any], target: str) -> str:
    return f"已导回 {Path(target).resolve()} · 工单 {report['工单']} · 图片 {report['图片']} · 对话 {report['对话']}"


def _is_mutating(args: argparse.Namespace) -> bool:
    command = args.command
    if command in {"new", "set", "void", "claim", "attach", "submit", "judge", "verify", "deploy-record", "evidence-ticket", "merge", "live", "close", "block", "unblock", "transfer", "ask", "answer", "say", "export", "build", "demo", "taskbook-check"}:
        return True
    if command == "inbox":
        return bool(args.mark_read)
    if command == "staff":
        return args.staff_command != "list"
    if command == "state":
        return args.state_command == "set"
    return False


if __name__ == "__main__":
    raise SystemExit(main())
