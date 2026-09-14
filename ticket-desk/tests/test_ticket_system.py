from __future__ import annotations

import atexit
import base64
import contextlib
import gzip
import hashlib
import http.client
import io
import inspect
import ipaddress
import json
import os
import random
import socket
import sqlite3
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from PIL import Image, ImageFilter

# ★★用例一律跑**内置默认配置**，不许读到运行者机器上的任何配置。
#
#   症状很吓人：用户照 2-中型.md 建好 ticket_desk/config.json（三个位），
#   再跑 pytest 就是 454 failed / 59 passed——而 README 写着「519 passed」。
#   **一份会随运行者配置变结果的用例，不是用例。**
#
#   ★光弹掉环境变量不够：config 还会回落到**包目录下的 config.json**，
#     而那一份正是文档让用户建的。所以这里反过来做——
#     写一份空对象（{} = 一切走内置默认），把 TICKET_CONFIG 显式指过去，
#     它的优先级高于包目录那份，于是两条路都堵住了。
#   ★必须在 import ticket_desk 之前做：config 是在 import 那一刻读的。
_TEST_CONFIG_DIR = tempfile.mkdtemp(prefix="ticketdesk-testcfg-")
_TEST_CONFIG_FILE = os.path.join(_TEST_CONFIG_DIR, "config.json")
with open(_TEST_CONFIG_FILE, "w", encoding="utf-8") as _config_handle:
    _config_handle.write("{}")
os.environ["TICKET_CONFIG"] = _TEST_CONFIG_FILE
atexit.register(shutil.rmtree, _TEST_CONFIG_DIR, True)

from ticket_desk import http_server as http_server_module, model, service as service_module, store as store_module
from ticket_desk.model import (
    TicketError, provenance_path_for, read_provenance_dimension,
)
from ticket_desk.auth import AccountManager
from ticket_desk.http_server import TicketHTTPServer, TicketRequestHandler
from ticket_desk.service import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE, TicketService
from ticket_desk.store import SqliteStore, TicketStore
from ticket_desk.remote import RemoteClient
from ticket_desk import channel as channel_config, config as config_module


ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, str(ROOT / "ticket_desk" / "ticket.py")]
SLOT = "前端·界面与交互"
OTHER_SLOT = "后端·服务与接口"
VALID_DECISION_BODY = "一、这是什么\n需要决定界面配色。\n二、选了会怎样\n会统一后续视觉实现。\n三、推荐\n推荐暖色方案。"
PASS_VERDICT = "用户怎么打开它：双击启动工单台后进入对应卡片。功能与验收均通过。"
REWORK_VERDICT = "模型责任：功能未达到工单验收要求，按返工原因修正后重交。"
QUESTION_REWORK_VERDICT = "出题责任：任务书或判据本身写错，执行方照做无误。"
# 任务书路径的用例钉的是「绝对路径 + {ticket} 占位符能一路活到 show」，不是某个盘符。
# 提交端会走 Path(value).expanduser().resolve()：POSIX 上反斜杠不是分隔符，
# Path(r"D:\a\b.md") 整串只是一个相对文件名，resolve() 会把它接到 cwd 后面，
# 于是写死 D: 的断言只在 Windows 上成立——服务器上就是这么红的（T-000790）。
# 按平台各取一条本平台真绝对的路径，两边都照常跑，不是跳过。
TASKBOOK_DIRECTORY = (
    r"C:\ticket-desk-workspace\_office\平台·工单系统\任务书" if os.name == "nt"
    else "/tmp/ticket-desk-workspace/_office/平台·工单系统/任务书"
)

# 只要这几个变量之一漏进测试子进程,CLI 就可能连上真服务器(T-000108 方向二:六张假单写进生产库)。
# ★TICKET_CONFIG **不**在清空之列:子进程要继承上面那份「空对象」夹具,
#  否则它会回落到包目录下的 config.json——也就是运行者自己的名册。
#  要给某组用例换配置(例如把判据图闸打开),走 extra 显式传，extra 后覆盖。
CHANNEL_VARIABLES = ("TICKET_REMOTE", "TICKET_TOKEN_FILE", "TICKET_CA_SHA256",
                     "TICKET_ALLOW_STALE", "TICKET_ENV")
REMOTE_GUARD_MESSAGE = "测试不得连真服务器,请先 unset TICKET_REMOTE"


def clean_environment(tickets_root: Path | str, **extra: str) -> dict[str, str]:
    """测试子进程唯一允许的环境来源:从当前环境派生,但通道变量一律清空,本机库指到用例自己的临时目录。

    需要远程模式的用例,把 TICKET_REMOTE 等作为 extra 显式传进来——那只能是用例自己起的 127.0.0.1 服务。
    """
    environment = {key: value for key, value in os.environ.items() if key not in CHANNEL_VARIABLES}
    environment["TICKET_ROOT"] = str(tickets_root)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(extra)
    return environment


def run_local_cli(arguments: list[str], tickets_root: Path | str, **extra: str) -> subprocess.CompletedProcess:
    """本机模式跑一次 ticket.py:干净环境 + 显式 --local,双保险。

    errors="replace":子进程 stderr 现在是守门用例的失败消息(T-000089 R1),
    真出事那次不许因为一个解码不了的字节把整条原因吃掉。
    """
    return subprocess.run(
        [*CLI, *arguments, "--local"], cwd=ROOT, env=clean_environment(tickets_root, **extra),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def created_ticket_id(result: subprocess.CompletedProcess) -> str:
    return result.stdout.splitlines()[0]


# 上服包(deploy/update.sh 的 --source)不含整棵仓树,而这套用例里有一批是**钉网页台面源码文本**的:
# 它们在包树上没有前提,从前直接红,把每一次上服都拦死。这里给它们一个统一的、写清人话理由的干净跳过。
# ★这不是放水:有文件就照跑照有效,没文件才跳,由 PackageTreeGateTests 两条一起钉住。
#
# ★★ 开源版另有一层含义:网页台面(web/)**不随本包发布**——它在原项目里是另一个目录,
#    不在本次移植的范围内。所以这一批用例在开源版上默认全部跳过,skip 理由里会写清是哪个文件。
#    等你把自己的网页台面放进 web/ 之后,它们会自动重新生效——★但里面钉的是**原版网页的
#    函数名与字符串**,你的页面长得不一样,该改的是用例不是页面。
PACKAGE_TREE_SKIP_PREFIX = "本包不含网页台面(web/)"
WEB_ROOT = ROOT / "web"


def repository_file_or_skip(test: unittest.TestCase, *parts: str) -> Path:
    """要读本包之外的仓内文件时走这里:文件在就返回真路径,不在就干净跳过。"""
    path = ROOT.joinpath(*parts)
    if not path.is_file():
        test.skipTest(
            f"{PACKAGE_TREE_SKIP_PREFIX},这条用例要读仓内的 {'/'.join(parts)},没有它:{path}"
        )
    return path


def web_file_or_skip(test: unittest.TestCase, name: str) -> Path:
    """读网页台面下的某个文件;不在就跳过(开源版默认就是不在)。"""
    return repository_file_or_skip(test, "web", name)


def _web_text_or_skip(name: str) -> str:
    """setUpClass 里用:那里没有 self,只能直接抛 SkipTest。"""
    path = WEB_ROOT / name
    if not path.is_file():
        raise unittest.SkipTest(f"{PACKAGE_TREE_SKIP_PREFIX},这组用例要读 {path}")
    return path.read_text(encoding="utf-8")


class TicketTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = TicketService(TicketStore(self.root / "tickets"))
        self.worker = self.service.staff_new(SLOT, "model-a")["员工名"]
        self.deliverable = self.root / "main-scene"
        self.deliverable.write_text("[gd_scene]\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def dispatch(self, title: str = "测试派单", assign: str | None = None):
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker if assign is None else assign,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
        )

    def picture(self, name: str = "world.png", size: tuple[int, int] = (1600, 900), mode: str = "RGB") -> Path:
        path = self.root / name
        color = (55, 90, 125, 180) if mode == "RGBA" else (55, 90, 125)
        Image.new(mode, size, color).save(path)
        return path

    def to_judging(self):
        ticket = self.dispatch()
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        return self.service.submit(ticket["编号"], "登录后界面已出现")

    def verified(self, ticket_id: str, actor: str = "独立复检"):
        """把复验这一道记成「过」（D9-460 ①）。

        并线前置是「判过 ∧ 复验过」两道齐,所以凡是**造数据到已合并**
        的用例都要先走这一步。写成夹具而不是在每处内联,是为了让
        「这一步是造数据、不是被测行为」一眼看得出来——
        真正钉并线前置本身的是 ReviewInParallelTests,那里逐条显式调。
        """
        return self.service.verify(ticket_id, actor, "过", gates="夹具:六项闸摘要")[0]

    def merged_ticket(self, ticket_id: str, actor: str = "独立复检", verifier: str = "独立复检"):
        """造数据用:补上复验再并线。被测的是 merge 之后的事,不是这两道闸本身。"""
        self.verified(ticket_id, verifier)
        return self.service.merge(ticket_id, actor)

    def keep_window_open(self) -> None:
        """让 self.worker 回到在岗（T-001081）。

        非固定工位到终态会自动收窗，可这些用例是「一个窗连着做好几张单」的老写法：
        第一张 live 过之后窗就收了，第二张 create_dispatch 立刻被 require_active_staff 拦下。
        它们要钉的是同图/独图分类、批量 live、免独图——不该被名册规则牵着走，
        所以造数据时把窗按回在岗；**规则本身**由 AutoRetireAtTerminalTests 十条专门钉。
        """
        if self.service.find_staff(self.worker)[1]["状态"] != "在岗":
            self.service.staff_reopen(self.worker)

    def to_merged(self, title: str = "待实机复验", internal: bool = False):
        self.keep_window_open()
        ticket = self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台" if internal else "主场景/UiRoot", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=internal,
        )
        self.service.claim(ticket["编号"], self.worker)
        if internal:
            ticket = self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        else:
            self.service.attach(ticket["编号"], str(self.picture(f"{ticket['编号']}-before.png")), "world", self.worker)
            ticket = self.service.submit(ticket["编号"], "登录后界面已出现")
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        return self.merged_ticket(ticket["编号"], "独立复检")


class JudgeBlameTests(TicketTestCase):
    def test_r1_1_rework_without_blame_is_rejected_with_plain_guidance(self):
        """--blame 缺省时先照判语首行推导(网页判退不带 --blame);首行也没标才拒。"""
        ticket = self.to_judging()
        # 首行有「模型责任」:推导成 blame=模型,放行
        derived = run_local_cli([
            "judge", ticket["编号"], "--rework", "入口仍会闪一下", "--by", "UI总监",
            "--verdict", REWORK_VERDICT,
        ], self.service.store.root)
        self.assertEqual(0, derived.returncode, derived.stderr)
        self.assertEqual("模型", self.service.store.load_ticket(ticket["编号"])["返工原因列表"][-1]["责任"])
        # 首行什么都没标:拒,并给人话
        blank = self.to_judging()
        result = run_local_cli([
            "judge", blank["编号"], "--rework", "入口仍会闪一下", "--by", "UI总监",
            "--verdict", "功能未达到工单验收要求，按返工原因修正后重交。",
        ], self.service.store.root)
        self.assertEqual(2, result.returncode)
        self.assertIn(
            "判退必须写清责任归属:--blame 模型(执行方做错) 或 --blame 出题(任务书/判据本身写错)。",
            result.stderr,
        )
        self.assertIn("出题责任不计入模型判退累计,但会记进该总监的出题账。", result.stderr)

    def test_r1_2_pass_with_blame_is_rejected(self):
        ticket = self.to_judging()
        result = run_local_cli([
            "judge", ticket["编号"], "--pass", "--blame", "模型", "--by", "UI总监",
            "--verdict", PASS_VERDICT,
        ], self.service.store.root)
        self.assertEqual(2, result.returncode)
        self.assertIn("判过不需要责任归属", result.stderr)

    def test_r1_3_question_blame_does_not_add_any_model_score(self):
        ticket = self.to_judging()
        before = json.loads(json.dumps(self.service.store.load_staff()["模型记分"], ensure_ascii=False))
        self.service.judge(
            ticket["编号"], False, "UI总监", "任务书把入口写错了", QUESTION_REWORK_VERDICT, "出题",
        )
        after = self.service.store.load_staff()["模型记分"]
        self.assertEqual(before, after)
        self.assertNotIn("model-a", after)

    def test_r1_4_model_blame_adds_the_existing_model_score(self):
        ticket = self.to_judging()
        self.service.judge(ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型")
        score = self.service.store.load_staff()["模型记分"]["model-a-未标"]
        self.assertEqual((1, 1), (score[SLOT], score["合计"]))

    def test_r1_5_verdict_first_line_must_match_blame(self):
        ticket = self.to_judging()
        with self.assertRaises(TicketError) as caught:
            self.service.judge(ticket["编号"], False, "UI总监", "执行结果不符", QUESTION_REWORK_VERDICT, "模型")
        message = str(caught.exception)
        self.assertIn('--blame 写的是“模型”', message)
        self.assertIn('判语首行写的是“出题责任”', message)
        self.assertEqual("待判", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_r1_6_blame_is_saved_on_ticket_reason_and_event_for_both_values(self):
        for index, (blame, verdict) in enumerate((("模型", REWORK_VERDICT), ("出题", QUESTION_REWORK_VERDICT)), 1):
            with self.subTest(blame=blame):
                ticket = self.dispatch(f"责任字段 {index}")
                self.service.claim(ticket["编号"], self.worker)
                self.service.attach(ticket["编号"], str(self.picture(f"blame-{index}.png")), "world", self.worker)
                self.service.submit(ticket["编号"], "登录后仍有问题")
                result = run_local_cli([
                    "judge", ticket["编号"], "--rework", "按责任返工", "--blame", blame,
                    "--by", "UI总监", "--verdict", verdict,
                ], self.service.store.root)
                self.assertEqual(0, result.returncode, result.stderr)
                judged = self.service.store.load_ticket(ticket["编号"])
                self.assertEqual(blame, judged["判退责任"])
                self.assertEqual(blame, judged["返工原因列表"][-1]["判退责任"])
                event = self.service.store.read_jsonl(self.service.store.log_path)[-1]
                self.assertEqual(blame, event["判退责任"])
                self.assertIn(f"判退责任：{blame}", event["说明"])


class DemandAnswerPermissionTests(TicketTestCase):
    """需求单答复权交给所属总监位，外加三种前缀闸（T-000770，答 T-000409 / D9-414 ②）。

    以前需求只有总编排能答：接收位把活干完了也答不动，只能另建一张疑问单回话，
    原单永远挂在「待答」——设计者队列上看着是总编排卡了 30 小时，其实活早做完了
    （T-000367 / T-000370）。总工单的口径一个字没动，那一条由
    BlockedAnswerPermissionTests.test_general_ticket_still_refuses_the_owner_slot 钉着。
    """

    def demand(self, title: str = "要一套新图标", slot: str = SLOT):
        return self.service.create_question("需求", slot, title, "请排一下期。")

    def test_r1_1_owner_slot_can_answer_with_accepted_prefix(self):
        ticket = self.service.answer(self.demand()["编号"], "受理，本周内给排期。", SLOT)
        self.assertEqual("已答", ticket["状态"])
        self.assertEqual("受理，本周内给排期。", ticket["答复"])
        # 答完还要关得掉：答复权交出去了、关闭权还捏在总编排手里的话，单子照样落不了地。
        self.assertEqual("关闭", self.service.close(ticket["编号"], SLOT)["状态"])

    def test_r1_2_other_slot_is_refused_and_the_error_names_the_owner(self):
        ticket = self.demand()
        with self.assertRaises(TicketError) as caught:
            self.service.answer(ticket["编号"], "受理，我来排。", OTHER_SLOT)
        message = str(caught.exception)
        for expected in (ticket["编号"], SLOT, OTHER_SLOT):
            self.assertIn(expected, message)
        self.assertEqual("待答", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_r1_3_orchestrator_can_still_answer(self):
        ticket = self.service.answer(self.demand()["编号"], "已排期→T-000123", "总编排")
        self.assertEqual("已答", ticket["状态"])

    def test_r1_4_bad_first_word_is_refused_and_lists_all_three_forms(self):
        ticket = self.demand()
        result = run_local_cli(
            ["answer", ticket["编号"], "好的，知道了。", "--by", SLOT], self.service.store.root,
        )
        self.assertEqual(2, result.returncode)
        for form in ("受理(后面可跟预计)", "已排期→T-xxxxxx(派单号)", "已完成→T-xxxxxx(交板单号)"):
            self.assertIn(form, result.stderr)
        self.assertEqual("待答", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_r1_5_malformed_ticket_id_after_the_arrow_is_refused(self):
        ticket = self.demand()
        with self.assertRaises(TicketError) as caught:
            self.service.answer(ticket["编号"], "已排期→T-12", SLOT)
        self.assertIn("T- 加六位数字的单号", str(caught.exception))
        self.assertEqual("待答", self.service.store.load_ticket(ticket["编号"])["状态"])
        # 形状对的同一句照样过，证明拒的是单号形状不是箭头本身。
        self.assertEqual("已答", self.service.answer(ticket["编号"], "已完成→T-000770 已交板", SLOT)["状态"])


class BlameCorrectionTests(TicketTestCase):
    """判卷人自纠判退归属 set --blame（T-000770，答 T-000747）。

    judge 只认「待判」态，一判完就再也进不去，而模型判退是会累计到停用线的硬账：
    曾有工位在「再判退 1 次就到停用线」的提示下回头复核，
    发现自己把出题责任记成了模型责任，却没有任何路径改回来。
    """

    def reworked(self):
        ticket = self.to_judging()
        ticket, _ = self.service.judge(
            ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型",
        )
        return ticket

    def test_r2_6_model_to_question_moves_the_field_and_both_ledgers(self):
        ticket = self.reworked()
        self.assertEqual({SLOT: 1, "合计": 1}, self.service.store.load_staff()["模型记分"]["model-a-未标"])
        result = run_local_cli([
            "set", ticket["编号"], "--blame", "出题",
            "--reason", "复核后确认是任务书判据写错，执行方照做无误", "--by", SLOT,
        ], self.service.store.root)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("已改判退责任：模型 → 出题", result.stdout)
        changed = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("出题", changed["判退责任"])
        self.assertEqual("出题", changed["返工原因列表"][-1]["判退责任"])
        self.assertEqual("出题", changed["返工原因列表"][-1]["责任"])
        staff = self.service.store.load_staff()
        self.assertEqual({SLOT: 0, "合计": 0}, staff["模型记分"]["model-a-未标"])
        self.assertEqual({"合计": 1, SLOT: 1}, staff["出题记分"][SLOT])
        event = self.service.store.read_jsonl(self.service.store.log_path)[-1]
        self.assertEqual(("set-blame", "模型", "出题"), (event["事件"], event["旧值"], event["新值"]))
        self.assertEqual("复核后确认是任务书判据写错，执行方照做无误", event["理由"])

    def test_r2_7_judging_state_is_refused_and_points_back_at_judge(self):
        ticket = self.to_judging()
        with self.assertRaises(TicketError) as caught:
            self.service.set_blame(ticket["编号"], "出题", "记反了", SLOT)
        message = str(caught.exception)
        self.assertIn("现在是「待判」", message)
        self.assertIn("judge --blame", message)
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["判退责任"])

    def test_r2_8_other_slot_is_refused(self):
        ticket = self.reworked()
        with self.assertRaises(TicketError) as caught:
            self.service.set_blame(ticket["编号"], "出题", "我替他改一下", OTHER_SLOT)
        message = str(caught.exception)
        self.assertIn(OTHER_SLOT, message)
        self.assertIn(SLOT, message)
        self.assertEqual("模型", self.service.store.load_ticket(ticket["编号"])["判退责任"])
        self.assertEqual(1, self.service.store.load_staff()["模型记分"]["model-a-未标"]["合计"])

    def test_r2_9_empty_reason_is_refused(self):
        ticket = self.reworked()
        result = run_local_cli(
            ["set", ticket["编号"], "--blame", "出题", "--by", SLOT], self.service.store.root,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--reason 不能为空", result.stderr)
        self.assertEqual("模型", self.service.store.load_ticket(ticket["编号"])["判退责任"])


class ModelAccountingTests(TicketTestCase):
    def test_r2_1_model_spelling_variants_normalize_to_one_key(self):
        values = [service_module.normalize_model_name(value) for value in ("Model-A  High", "model-a_high", "model-a high")]
        self.assertEqual(["model-a-high", "model-a-high", "model-a-high"], values)

    def test_r2_2_three_models_stay_separate_through_real_judgements(self):
        for index, actual_model in enumerate(("model-a", "model-a-high", "model-a-xhigh"), 1):
            ticket = self.dispatch(f"模型分开 {actual_model}")
            self.service.open_window(ticket["编号"], "设计者", actual_model)
            self.service.claim(ticket["编号"], self.worker)
            self.service.attach(ticket["编号"], str(self.picture(f"separate-{index}.png")), "world", self.worker)
            self.service.submit(ticket["编号"], "登录后仍有问题")
            self.service.judge(ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型")
        scores = self.service.store.load_staff()["模型记分"]
        self.assertEqual(1, scores["model-a"]["合计"])
        self.assertEqual(1, scores["model-a-high"]["合计"])
        self.assertEqual(1, scores["model-a-xhigh"]["合计"])

    def test_r2_3_missing_actual_model_uses_a_separate_unmarked_key(self):
        ticket = self.to_judging()
        _, notice = self.service.judge(
            ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型",
        )
        scores = self.service.store.load_staff()["模型记分"]
        self.assertEqual(1, scores["model-a-未标"]["合计"])
        self.assertNotIn("model-a", scores)
        self.assertIn("这张单没填实际模型，已按 model-a-未标 单独记账", notice)

    def test_r2_4_empty_roster_warns_but_does_not_block_rework(self):
        slots = self.service.store.read_json(self.service.store.slots_path)
        slots["模型名册"] = []
        self.service.store.atomic_json(self.service.store.slots_path, slots)
        ticket = self.dispatch("空名册照常判退")
        self.service.open_window(ticket["编号"], "设计者", "Mystery__Ultra")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("empty-roster.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "登录后仍有问题")
        judged, notice = self.service.judge(
            ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型",
        )
        self.assertEqual("返工", judged["状态"])
        self.assertEqual(1, self.service.store.load_staff()["模型记分"]["mystery-ultra"]["合计"])
        self.assertIn("模型名 mystery-ultra 不在名册里，已按字面记账", notice)

    def test_r2_5_digest_splits_the_same_model_by_task_tier(self):
        for index, task_tier in enumerate(("甲", "乙"), 1):
            ticket = self.service.create_dispatch(
                SLOT, f"同模型不同档 {task_tier}", ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
                task_tier=task_tier, deliverables=[str(self.deliverable)], internal=False,
            )
            self.service.open_window(ticket["编号"], "设计者", "Model-A High")
            self.service.claim(ticket["编号"], self.worker)
            self.service.attach(ticket["编号"], str(self.picture(f"tier-{index}.png")), "world", self.worker)
            self.service.submit(ticket["编号"], "按任务档统计")
            self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        rows = [row for row in self.service.model_statistics() if row["模型"] == "model-a-high"]
        self.assertEqual(["乙", "甲"], sorted(row["任务档"] for row in rows))
        digest = self.service.digest()
        self.assertIn("各模型合格率：模型 | 任务档 | 交板 | 判过 | 判退 | 合格率 | 状态", digest)
        self.assertTrue(any(line.startswith("[模型] model-a-high | 甲 |") for line in digest))
        self.assertTrue(any(line.startswith("[模型] model-a-high | 乙 |") for line in digest))


class QuestionScoreAndBanNoticeTests(TicketTestCase):
    def _judge_model_rework(
        self, index: int, slot: str = SLOT, worker: str | None = None, actual_model: str = "model-a",
    ) -> tuple[dict, str]:
        worker = worker or self.worker
        ticket = self.service.create_dispatch(
            slot, f"停用计数 {slot} {index}", ["DECISIONS.md:测试"], "主场景/UiRoot", worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
        )
        self.service.open_window(ticket["编号"], "设计者", actual_model)
        self.service.claim(ticket["编号"], worker)
        self.service.attach(ticket["编号"], str(self.picture(f"ban-{slot[-2:]}-{index}.png")), "world", worker)
        self.service.submit(ticket["编号"], "登录后仍有问题")
        return self.service.judge(
            ticket["编号"], False, "UI总监", f"第 {index} 次执行错误", REWORK_VERDICT, "模型",
        )

    def test_r3_1_question_blame_adds_owner_score_without_model_score(self):
        ticket = self.to_judging()
        before = json.loads(json.dumps(self.service.store.load_staff()["模型记分"], ensure_ascii=False))
        self.service.judge(
            ticket["编号"], False, "UI总监", "任务书把入口写错了", QUESTION_REWORK_VERDICT, "出题",
        )
        staff = self.service.store.load_staff()
        self.assertEqual(before, staff["模型记分"])
        self.assertEqual({"合计": 1, SLOT: 1}, staff["出题记分"][SLOT])

    def test_r3_2_digest_contains_director_question_score(self):
        ticket = self.to_judging()
        self.service.judge(
            ticket["编号"], False, "UI总监", "任务书把入口写错了", QUESTION_REWORK_VERDICT, "出题",
        )
        digest = self.service.digest()
        self.assertIn("总监出题账:总监位 | 出题判退次数", digest)
        self.assertIn(f"[出题] {SLOT} | 1", digest)

    def test_r3_3_threshold_minus_one_really_notifies_conductor_thread(self):
        # 用非主力模型 model-c:R1.5 之后主力模型(model-a/model-b/model-e)到线只作质量提示,不再有「停用线」措辞。
        self._judge_model_rework(1, actual_model="model-c")
        self._judge_model_rework(2, actual_model="model-c")
        rows = self.service.store.read_jsonl(self.service.store.thread_path(service_module.CONDUCTOR_SLOT))
        texts = [row["文字"] for row in rows]
        self.assertTrue(any("累计判退 2 次" in x and "再判退 1 次就到停用线" in x for x in texts), texts)

    def test_r3_4_reaching_threshold_notifies_conductor_but_writes_no_ban(self):
        """T-000529 答「乙」:到停用线只通知,不自动停用(两次误停主力模型之后定的)。

        用非主力模型 model-c 钉「原样措辞」:主力模型的到线措辞在 R1.5 改成了质量提示,
        由 BanLineCountTests 单独钉。
        """
        for index in range(1, 4):
            self._judge_model_rework(index, actual_model="model-c")
        rows = self.service.store.read_jsonl(self.service.store.thread_path(service_module.CONDUCTOR_SLOT))
        text = next(row["文字"] for row in rows if "已到停用线" in row["文字"])
        self.assertIn("模型 model-c", text)
        self.assertIn("累计判退 3 次", text)
        self.assertIn("自动停用已关", text)
        bans = self.service.store.load_staff().get("模型停用", {})
        self.assertNotIn("model-c", bans.get("按位", {}).get(SLOT, []))

    def test_r3_5_thresholds_unchanged_but_neither_local_nor_global_auto_ban_is_written(self):
        other_worker = self.service.staff_new(OTHER_SLOT, "model-a")["员工名"]
        for index in range(1, 4):
            self._judge_model_rework(index)
        for index in range(4, 6):
            self._judge_model_rework(index, OTHER_SLOT, other_worker)
        slots = self.service.store.read_json(self.service.store.slots_path)
        staff = self.service.store.load_staff()
        self.assertEqual({"同位": 3, "全项目": 5}, slots["停用阈值"])
        self.assertEqual(5, staff["模型记分"]["model-a"]["合计"])
        self.assertNotIn("model-a", staff["模型停用"]["按位"][SLOT])
        self.assertNotIn("model-a", staff["模型停用"]["全项目"])


class BanLineCountTests(TicketTestCase):
    """停用线只数「判退责任=模型」的条目,按模型基名+任务档归并(T-000794,答 T-000793 / D9-388 ③)。

    工单台把「模型 model-b(乙档)跨位累计 5 次,已到停用线」报给了总编排:
    逐张核 315 张派单后,model-b 且有返工的 9 张里判退责任=模型的只有 3 张,两张明写出题、
    一张返工次数为 0——旧实现拿「模型记分」账本的累计直接对阈值,把出题责任也数了进去。
    现在账本照旧记(给合格率与 digest 用),停用线单独从工单现算。
    """

    def _rework(
        self, index: int, slot: str = SLOT, worker: str | None = None,
        actual_model: str = "model-c", task_tier: str = "乙", blame: str = "模型",
    ) -> tuple[dict, str]:
        worker = worker or self.worker
        ticket = self.service.create_dispatch(
            slot, f"停用线归并 {slot} {index}", ["DECISIONS.md:测试"], "主场景/UiRoot", worker,
            task_tier=task_tier, deliverables=[str(self.deliverable)], internal=False,
        )
        self.service.open_window(ticket["编号"], "设计者", actual_model)
        self.service.claim(ticket["编号"], worker)
        self.service.attach(ticket["编号"], str(self.picture(f"banline-{index}.png")), "world", worker)
        self.service.submit(ticket["编号"], "登录后仍有问题")
        verdict = REWORK_VERDICT if blame == "模型" else QUESTION_REWORK_VERDICT
        return self.service.judge(ticket["编号"], False, "UI总监", f"第 {index} 次执行错误", verdict, blame)

    def test_r1_1_ban_line_counts_only_model_blame(self):
        """三张判退责任=模型 + 两张=出题:停用线的数是 3,不是 5。"""
        model_ids = []
        for index in (1, 2):
            ticket, _ = self._rework(index)
            model_ids.append(ticket["编号"])
        question_ids = []
        for index in (3, 4):
            ticket, _ = self._rework(index, blame="出题")
            question_ids.append(ticket["编号"])
        third, warning = self._rework(5)
        self.assertIn("累计判退 3 次", warning, warning)
        self.assertIn("已到停用线", warning)
        for ticket_id in model_ids:
            self.assertIn(ticket_id, warning)
        for ticket_id in question_ids:
            self.assertNotIn(ticket_id, warning)

    def test_r1_2_blank_blame_entry_is_not_counted(self):
        """返工原因列表里责任字段为空的条目(旧库迁移常见)不计入停用线。"""
        first, _ = self._rework(1)
        stored = self.service.store.load_ticket(first["编号"])
        stored.setdefault("返工原因列表", []).append({
            "时间": "2026-09-05 00:00:00", "判卷人": "UI总监", "原因": "旧库迁移来的条目,没有责任字段",
        })
        self.service.store.save_ticket(stored, "set", "测试", "补一条无责任字段的旧条目")
        _, warning = self._rework(2)
        self.assertIn("累计判退 2 次", warning, warning)
        self.assertIn("再判退 1 次就到停用线", warning)
        self.assertNotIn("已到停用线", warning)

    def test_r1_3_variant_models_merge_into_one_base_cell(self):
        """opus 与 model-b-high 两张同档单归并成同一个基名:数是 2。"""
        self._rework(1, actual_model="model-b")
        _, warning = self._rework(2, actual_model="model-b-high")
        self.assertIn("模型 model-b(乙档)", warning, warning)
        self.assertIn("记模型责任判退 2 次", warning, warning)

    def test_r1_4_task_tiers_never_merge(self):
        """甲档一张 + 乙档一张各算各的:乙档第二张时是 2(差一到线),不是 3(已到线)。D9-389。"""
        self._rework(1, task_tier="甲")
        self._rework(2)
        _, warning = self._rework(3)
        self.assertIn("累计判退 2 次", warning, warning)
        self.assertIn("再判退 1 次就到停用线", warning)
        self.assertNotIn("已到停用线", warning)

    def test_r1_5_notice_lists_every_counted_entry(self):
        """到线时通知正文列出计入的每一笔:单号 · 所属位 · 任务档 · 责任字段。"""
        ids = []
        for index in (1, 2, 3):
            ticket, _ = self._rework(index)
            ids.append(ticket["编号"])
        rows = self.service.store.read_jsonl(self.service.store.thread_path(service_module.CONDUCTOR_SLOT))
        text = next(row["文字"] for row in rows if "已到停用线" in row["文字"])
        self.assertIn("计入的每一笔(单号 · 所属位 · 任务档 · 责任字段):", text)
        for ticket_id in ids:
            self.assertIn(f"{ticket_id} · {SLOT} · 乙 · 模型", text)

    def test_r1_6_ledger_untouched_by_ban_line_recount(self):
        """同一场景跑完,模型记分与改动前的记账口径一致:出题责任进不了模型账。"""
        for index, blame in ((1, "模型"), (2, "出题"), (3, "出题"), (4, "模型"), (5, "模型")):
            self._rework(index, blame=blame)
        staff = self.service.store.load_staff()
        self.assertEqual({SLOT: 3, "合计": 3}, staff["模型记分"]["model-c"])
        self.assertEqual({"合计": 2, SLOT: 2}, staff["出题记分"][SLOT])
        bans = staff["模型停用"]
        self.assertEqual([], bans["全项目"])
        self.assertFalse(any(bans["按位"].values()))

    def test_r2_1_main_model_never_says_ban_line(self):
        """R1.5:主力模型到线只作质量提示,不再出现「已到停用线」这类吓人的措辞。"""
        ids = []
        for index in (1, 2, 3):
            ticket, warning = self._rework(index, actual_model="model-b")
            ids.append(ticket["编号"])
        self.assertIn("记模型责任判退 3 次", warning, warning)
        self.assertIn("主力模型不停用", warning)
        self.assertIn("质量提示", warning)
        self.assertNotIn("已到停用线", warning)
        self.assertNotIn("停不停", warning)
        for ticket_id in ids:
            self.assertIn(ticket_id, warning)

    def test_r2_2_non_main_model_keeps_original_wording(self):
        """非主力模型到线措辞保持原样:仍是「已到停用线」+「自动停用已关」,只通知不落停用名单。"""
        _, warning = self._rework(1, actual_model="model-c")
        self.assertNotIn("停用线", warning)
        for index in (2, 3):
            _, warning = self._rework(index, actual_model="model-c")
        self.assertIn("已到停用线", warning)
        self.assertIn("自动停用已关", warning)
        self.assertIn("累计判退 3 次", warning)
        self.assertNotIn("主力模型不停用", warning)
        self.assertEqual([], self.service.store.load_staff()["模型停用"]["全项目"])

    def test_r3_1_main_model_ban_is_designer_only(self):
        """R1.5 的闸:staff ban 主力模型只有设计者能落笔,总编排署名拒并带出口径;非主力不变。"""
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("model-b", "总编排", reason="核过三次判退确属模型责任")
        message = str(caught.exception)
        self.assertIn("只有设计者", message)
        self.assertIn("绝对不能停用", message)
        # 变体也算主力:换个写法(model-b-high)同样过不了这道闸。
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("Model-B High", "总编排", reason="换个写法试试")
        self.assertIn("只有设计者", str(caught.exception))
        detail = self.service.staff_ban("model-b", "设计者", reason="设计者本人拍板,长期算力不足")
        self.assertIn("model-b", detail)
        self.assertIn("model-b", self.service.store.load_staff()["模型停用"]["全项目"])
        self.service.staff_ban("model-c", "总编排", reason="核过责任归属,确属模型责任")
        self.assertIn("model-c", self.service.store.load_staff()["模型停用"]["全项目"])


class ChineseOutputSurvivesNonUtf8ConsoleTests(unittest.TestCase):
    """本工具通篇中文:控制台不是 UTF-8 时,输出也必须是看得懂的中文。

    ★这个坏法在 Windows 上对**每一个新用户**都成立:控制台默认按 locale 编码
      (简中机器是 cp936/GBK),于是 README 里「30 秒跑起来」那三条命令打出来是
      一片乱码——功能全对、退出码是 0、一个字看不懂。
      不报错也不退非零的坏法最不划算:人只会以为这软件坏了。

    ★这一组为什么必须**自己造环境**、不能用 clean_environment():
      那个帮手里写着 PYTHONIOENCODING="utf-8"——测试子进程一直被喂着这个变量,
      于是这个 bug 在全绿的用例里一直没露头。
      **一个把自己要测的条件预先设好的用例,测不到那个条件不成立时的行为。**
      这里显式把它设成 gbk,去逼出真实形态。
    """

    @staticmethod
    def run_with_console_encoding(encoding: str, tickets_root: Path) -> subprocess.CompletedProcess:
        environment = {k: v for k, v in os.environ.items() if k not in CHANNEL_VARIABLES}
        environment["TICKET_ROOT"] = str(tickets_root)
        environment["PYTHONIOENCODING"] = encoding      # ★故意不是 utf-8
        environment.pop("PYTHONUTF8", None)             # ★也不许靠它兜底
        return subprocess.run(
            [*CLI, "state", "get", "--local"], cwd=ROOT, env=environment,
            capture_output=True, encoding="utf-8", errors="replace", text=True,
        )

    def test_chinese_is_readable_even_when_the_console_is_gbk(self):
        with tempfile.TemporaryDirectory() as temporary:
            done = self.run_with_console_encoding("gbk", Path(temporary) / "tickets")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        text = done.stdout + done.stderr
        # 断言的是**真的中文**,不是「没报错」:乱码时这里全是解不开的字节
        self.assertIn("值", text, f"GBK 控制台下中文成了乱码:{text[:120]}")
        self.assertNotIn("�", text, f"输出里有解码不了的字节(乱码):{text[:120]}")

    def test_utf8_console_is_left_alone(self):
        """★反面:本来就是 UTF-8 的(Linux / macOS / 设过 PYTHONUTF8 的 Windows)行为不变。"""
        with tempfile.TemporaryDirectory() as temporary:
            done = self.run_with_console_encoding("utf-8", Path(temporary) / "tickets")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("值", done.stdout + done.stderr)


class SlotRosterFollowsConfigTests(unittest.TestCase):
    """位名成员必须跟着 config 走;手改项(主力模型集合 / 模型名册 / 停用阈值)一个字不许被回写。

    ★这个 bug 是**静默**的:slots.json 的位名是建库那一刻落下来的,之后再不校正,
      而另一半代码(argparse 的 choices、say / transfer / create 的判据)全程直读 config.SLOTS。
      两边不一致时,网页按 slots.json 渲染出来的位服务端当场拒收;更难查的是
      「要你去唤醒的窗口」——它也按 slots.json 逐位查未读,于是**真实位的对话线压根没被遍历**,
      say 写进去多少条都不冒出来,连一个字的报错都没有。
    ★触发它不需要谁做错事:装机脚本「先起服务、后写 TICKET_CONFIG」就够了。

    ★写这一组时值得记住的一跤:`_migrate_metadata()` 是 **`ensure()`** 调的,不是 `__init__`。
      光 `TicketStore(root)` 什么都不会发生——那样测的是一个根本没跑过的迁移。
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "tickets"

    def build(self, roster: list[dict[str, Any]]) -> dict[str, Any]:
        """先把一份**陈旧**名册摆进库,再跑一次 ensure(),看它校正成什么样。"""
        store = TicketStore(self.root)
        store.ensure()                                   # ★先建库,再覆盖成陈旧的那份
        slots = store.read_json(store.slots_path, {})
        slots["总监位"] = roster
        store.atomic_json(store.slots_path, slots)
        TicketStore(self.root).ensure()                  # ★迁移挂在 ensure() 上,不是 __init__
        return TicketStore(self.root).read_json(store.slots_path, {})

    def test_stale_roster_is_corrected_to_config(self):
        after = self.build([{"名字": "示例总监位", "启用": True, "主力模型": True}])
        names = [row["名字"] for row in after["总监位"] if row.get("启用") is not False]
        self.assertEqual(list(model.SLOTS), names, "在册位名必须与 config.SLOTS 一致")

    def test_slot_dropped_from_config_is_disabled_not_deleted(self):
        """★停用而不是删掉:历史单的「所属总监位」还引用着它,删了那些单就成了孤儿。"""
        after = self.build(
            [{"名字": name, "启用": True, "主力模型": True} for name in model.SLOTS]
            + [{"名字": "已经撤掉的位", "启用": True, "主力模型": True}]
        )
        row = next(r for r in after["总监位"] if r["名字"] == "已经撤掉的位")
        self.assertFalse(row["启用"], "撤掉的位要停用")
        self.assertIn("已经撤掉的位", [r["名字"] for r in after["总监位"]], "但不许从库里抹掉")

    def test_hand_edited_fields_are_never_written_back(self):
        """★反面,也是这一组最要紧的一条:手改项一个字都不许被 config 冲掉。

        「按 config 回写会把手改冲掉」正是原来不校正位名的理由,那个顾虑对这些字段完全成立。
        位名跟着 config 走,不等于整份名册跟着 config 走。
        """
        store = TicketStore(self.root)
        store.ensure()
        slots = store.read_json(store.slots_path, {})
        slots["主力模型集合"] = ["只剩这一个"]
        slots["停用阈值"] = {"同位": 99, "全项目": 98}
        slots["模型名册"] = [{"模型": "手改进去的", "状态": "在用", "可选档位": ["high"]}]
        slots["总监位"] = [{"名字": "示例总监位", "启用": True, "主力模型": True}]
        store.atomic_json(store.slots_path, slots)
        TicketStore(self.root).ensure()
        after = TicketStore(self.root).read_json(store.slots_path, {})
        self.assertEqual(["只剩这一个"], after["主力模型集合"])
        self.assertEqual({"同位": 99, "全项目": 98}, after["停用阈值"])
        self.assertEqual([{"模型": "手改进去的", "状态": "在用", "可选档位": ["high"]}], after["模型名册"])

    def test_hand_edits_on_a_row_that_stays_are_kept(self):
        """在册位那一行上的手改要留住:校正的是**成员**,不是把每一行推倒重建。"""
        first = model.SLOTS[0]
        after = self.build(
            [{"名字": first, "启用": True, "主力模型": False, "备注": "手写的一句"}]
        )
        row = next(r for r in after["总监位"] if r["名字"] == first)
        self.assertFalse(row["主力模型"], "行内手改项要留住")
        self.assertEqual("手写的一句", row.get("备注"))
        self.assertTrue(row["启用"])


class SqliteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = TicketStore(self.root / "files")
        self.service = TicketService(self.source)
        worker = self.service.staff_new(SLOT, "model-a")["员工名"]
        deliverable = self.root / "main-scene"
        deliverable.write_text("[gd_scene]\n", encoding="utf-8")
        ticket = self.service.create_dispatch(
            SLOT, "SQLite 对账", ["DECISIONS.md:SQLite"], "主场景/UiRoot", worker,
            task_tier="乙", deliverables=[str(deliverable)], internal=False,
        )
        self.service.claim(ticket["编号"], worker)
        image = self.root / "world.png"
        Image.new("RGB", (80, 60), (10, 20, 30)).save(image)
        self.service.attach(ticket["编号"], str(image), "world", worker)
        self.service.say(SLOT, SLOT, "SQLite 对话", reference=ticket["编号"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_migrate_is_idempotent_and_dump_round_trips(self):
        database = self.root / "db" / "tickets.sqlite"
        sqlite_store = SqliteStore(database)
        first = sqlite_store.import_files(self.source.root)
        second = sqlite_store.import_files(self.source.root)
        self.assertEqual(first, second)
        self.assertTrue(first["工单字段全同"])
        self.assertTrue(first["图片SHA全同"])

        dumped = self.root / "dumped"
        sqlite_store.dump_files(dumped)
        roundtrip = SqliteStore(self.root / "roundtrip" / "tickets.sqlite").import_files(dumped)
        self.assertTrue(roundtrip["工单字段全同"])
        self.assertTrue(roundtrip["图片SHA全同"])
        self.assertEqual(self.source.list_tickets(), TicketStore(dumped).list_tickets())

    def test_r2_5_migrate_and_dump_preserve_source_image_sha256(self):
        source_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.source.images_dir.iterdir()
            if path.is_file()
        }
        sqlite_store = SqliteStore(self.root / "sha-db" / "tickets.sqlite")
        sqlite_store.import_files(self.source.root)
        self.assertEqual(source_hashes, sqlite_store.image_hashes())

        dumped = self.root / "sha-dump"
        sqlite_store.dump_files(dumped)
        dumped_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in TicketStore(dumped).images_dir.iterdir()
            if path.is_file()
        }
        self.assertEqual(source_hashes, dumped_hashes)

    def test_sqlite_store_runs_service_without_business_changes(self):
        sqlite_store = SqliteStore(self.root / "db" / "tickets.sqlite")
        sqlite_store.import_files(self.source.root)
        service = TicketService(sqlite_store)
        ticket = service.list_tickets(SLOT)[0]
        self.assertEqual("已认领", ticket["状态"])
        # 本例要证的是「say 的那一行能原样穿过 SQLite 导入」,不是它排第几。
        # T-000269 起建单会先往该位对话线落一行唤醒通知,原来的下标 0 断言会被挤掉,故改按内容找。
        rows = service.store.read_jsonl(service.store.thread_path(SLOT))
        self.assertIn("SQLite 对话", [row["文字"] for row in rows])

    def test_sqlite_store_preserves_state_entry_time_on_same_state_save(self):
        sqlite_store = SqliteStore(self.root / "fresh" / "tickets.sqlite")
        service = TicketService(sqlite_store)
        worker = service.staff_new(SLOT, "model-a")["员工名"]
        deliverable = self.root / "fresh-main-scene"
        deliverable.write_text("[gd_scene]\n", encoding="utf-8")
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T01:00:00+00:00"):
            ticket = service.create_dispatch(
                SLOT, "SQLite 状态时间", ["DECISIONS.md:SQLite"], "主场景/UiRoot", worker,
                task_tier="乙", deliverables=[str(deliverable)], internal=False,
            )
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T02:00:00+00:00"):
            ticket = service.claim(ticket["编号"], worker)
        entered = ticket["状态进入时间"]
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T03:00:00+00:00"):
            ticket, _ = service.edit(ticket["编号"], SLOT, deliverables=[str(deliverable), "review/report.md"])
        self.assertEqual("2026-09-01T03:00:00+00:00", ticket["最后更新时间"])
        self.assertEqual(entered, ticket["状态进入时间"])


class StateMachineTests(TicketTestCase):
    def test_stale_thresholds_are_per_state(self):
        now = datetime.now().astimezone()
        claimed = self.dispatch("已认领九小时")
        claimed["状态"] = "已认领"
        claimed["状态进入时间"] = (now - timedelta(hours=9)).isoformat()
        judging = self.dispatch("待判三小时")
        judging["状态"] = "待判"
        judging["状态进入时间"] = (now - timedelta(hours=3)).isoformat()
        self.assertEqual(8, self.service.stale_info(claimed, now)["阈值小时"])
        self.assertIsNone(self.service.stale_info(judging, now))

    def test_stale_check_missing_state_time_falls_back_to_last_update(self):
        now = datetime.now().astimezone()
        ticket = self.dispatch("存量字段回落")
        ticket["状态"] = "已认领"
        ticket.pop("状态进入时间")
        ticket["最后更新时间"] = (now - timedelta(hours=9)).isoformat()
        info = self.service.stale_info(ticket, now)
        self.assertEqual((8, 9), (info["阈值小时"], info["卡住小时"]))

    def test_blocked_and_terminal_tickets_are_never_stale(self):
        now = datetime.now().astimezone()
        for state in ("阻塞", "关闭", "作废", "实机复验过"):
            with self.subTest(state=state):
                ticket = self.dispatch(state)
                ticket["状态"] = state
                ticket["状态进入时间"] = (now - timedelta(days=30)).isoformat()
                self.assertIsNone(self.service.stale_info(ticket, now))
        blocker = self.service.create_question("阻塞", SLOT, "阻塞类型", "等待协调")
        blocker["状态进入时间"] = (now - timedelta(days=30)).isoformat()
        self.assertIsNone(self.service.stale_info(blocker, now))

    def test_digest_summarizes_stale_tickets_by_state_and_turn(self):
        now = datetime.now().astimezone()
        claimed_rows = [self.dispatch(f"已认领停滞 {index}") for index in range(2)]
        for row in claimed_rows:
            row["状态"] = "已认领"
            row["状态进入时间"] = (now - timedelta(hours=9)).isoformat()
            self.service.store.atomic_json(self.service.store.item_path(row["编号"]), row)
        judging = self.dispatch("待判停滞")
        judging["状态"] = "待判"
        judging["状态进入时间"] = (now - timedelta(hours=5)).isoformat()
        self.service.store.atomic_json(self.service.store.item_path(judging["编号"]), judging)
        fresh = self.dispatch("待判未超线")
        fresh["状态"] = "待判"
        fresh["状态进入时间"] = (now - timedelta(hours=3)).isoformat()
        self.service.store.atomic_json(self.service.store.item_path(fresh["编号"]), fresh)

        digest = self.service.digest()
        self.assertIn("停滞 3 张(已认领 2 · 待判 1)", digest[1])
        stale_lines = [line for line in digest if line.startswith("[停滞]")]
        self.assertEqual(3, len(stale_lines))
        self.assertTrue(any(f"轮到 {self.worker}" in line for line in stale_lines))
        self.assertTrue(any(f"轮到 {SLOT}" in line for line in stale_lines))

    def test_digest_never_truncates_stale_section(self):
        now = datetime.now().astimezone()
        old = (now - timedelta(hours=9)).isoformat()
        rows = [
            {
                "编号": f"T-{index:06d}", "标题": f"停滞单 {index}", "类型": "派单", "状态": "已认领",
                "状态进入时间": old, "最后更新时间": old, "发起时间": old, "所属总监位": SLOT,
                "指派给": self.worker, "发起位": "", "转交历史": [],
            }
            for index in range(1, 62)
        ]
        with mock.patch.object(self.service.store, "list_tickets", return_value=rows), \
             mock.patch.object(self.service.store, "read_jsonl", return_value=[]), \
             mock.patch.object(self.service, "model_statistics", return_value=[]):
            digest = self.service.digest()
        self.assertTrue(any(line.startswith("[停滞] T-000061") for line in digest))
        self.assertEqual("48 小时", self.service._stale_duration_text(48))
        self.assertEqual("2 天", self.service._stale_duration_text(49))

    def test_state_entry_time_changes_only_when_state_changes(self):
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T01:00:00+00:00"):
            ticket = self.dispatch()
        self.assertEqual("2026-09-01T01:00:00+00:00", ticket["状态进入时间"])
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T02:00:00+00:00"):
            ticket = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual("2026-09-01T02:00:00+00:00", ticket["状态进入时间"])
        old_state_time = ticket["状态进入时间"]
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T03:00:00+00:00"):
            ticket, _ = self.service.edit(ticket["编号"], SLOT, deliverables=[str(self.deliverable), "review/report.md"])
        self.assertEqual("2026-09-01T03:00:00+00:00", ticket["最后更新时间"])
        self.assertEqual(old_state_time, ticket["状态进入时间"])

    def test_missing_state_entry_time_falls_back_to_previous_update(self):
        ticket = self.dispatch()
        path = self.service.store.item_path(ticket["编号"])
        stored = self.service.store.load_ticket(ticket["编号"])
        previous_update = stored["最后更新时间"]
        stored.pop("状态进入时间")
        self.service.store.atomic_json(path, stored)
        ticket, _ = self.service.edit(ticket["编号"], SLOT, deliverables=[str(self.deliverable), "review/report.md"])
        self.assertEqual(previous_update, ticket["状态进入时间"])

    def test_backfill_state_time_cli_is_idempotent(self):
        ticket = self.dispatch()
        ticket = self.service.claim(ticket["编号"], self.worker)
        expected = ticket["状态进入时间"]
        path = self.service.store.item_path(ticket["编号"])
        stored = self.service.store.load_ticket(ticket["编号"])
        stored.pop("状态进入时间")
        self.service.store.atomic_json(path, stored)

        first = run_local_cli(["migrate", "--backfill-state-time"], self.service.store.root)
        second = run_local_cli(["migrate", "--backfill-state-time"], self.service.store.root)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("已补齐 1", first.stdout)
        self.assertIn("已跳过 1", second.stdout)
        self.assertEqual(expected, self.service.store.load_ticket(ticket["编号"])["状态进入时间"])

    def test_full_legal_dispatch_path(self):
        ticket = self.dispatch()
        ticket = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual("已认领", ticket["状态"])
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        ticket = self.service.submit(ticket["编号"], "登录后可见")
        self.assertEqual("待判", ticket["状态"])
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("待复检", ticket["状态"])
        ticket = self.merged_ticket(ticket["编号"], "独立复检")
        self.assertEqual("已合并", ticket["状态"])
        ticket = self.service.live(ticket["编号"], str(self.picture("live.png")), "独立复检", "独图")
        self.assertEqual("实机复验过", ticket["状态"])
        ticket = self.service.close(ticket["编号"], "总编排")
        self.assertEqual("关闭", ticket["状态"])

    def test_rework_returns_to_claim(self):
        ticket = self.to_judging()
        ticket, _ = self.service.judge(
            ticket["编号"], False, "UI总监", "入口仍会闪一下", REWORK_VERDICT, "模型",
        )
        self.assertEqual("返工", ticket["状态"])
        self.assertEqual(1, ticket["返工次数"])
        ticket = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual("已认领", ticket["状态"])

    def test_block_and_unblock_lands_on_rework(self):
        """阻塞照旧能解开,只是落点从「阻塞前状态」改成一律「返工」(T-000891 R2)。

        这条守的仍是老命题「阻塞解得开、流程提示会换」,断言里变的只有那一个落点。
        """
        ticket = self.dispatch()
        self.service.claim(ticket["编号"], self.worker)
        ticket = self.service.block(ticket["编号"], "缺一张登录图")
        self.assertEqual("阻塞", ticket["状态"])
        self.assertEqual("先把不依赖它的部分做完并交板,再收窗", ticket["流程提示"])
        ticket = self.service.unblock(ticket["编号"])
        self.assertEqual("返工", ticket["状态"])
        self.assertIn("发续单", ticket["流程提示"])

    def test_answer_path_and_close(self):
        ticket = self.service.create_question("拍板", SLOT, "颜色选择", VALID_DECISION_BODY, sources=["DECISIONS.md:颜色"])
        ticket = self.service.answer(ticket["编号"], "采用暖色", "设计者")
        self.assertEqual("已答", ticket["状态"])
        self.assertEqual("关闭", self.service.close(ticket["编号"], "设计者")["状态"])

    def test_blocker_is_a_distinct_ticket_type(self):
        ticket = self.service.create_question("阻塞", SLOT, "缺登录凭据", "请总编排协调")
        self.assertEqual("阻塞", ticket["类型"])
        self.assertEqual("待答", ticket["状态"])
        with self.assertRaisesRegex(TicketError, "只能由总编排"):
            self.service.answer(ticket["编号"], "已协调", "设计者")
        self.assertEqual("已答", self.service.answer(ticket["编号"], "已协调", "总编排")["状态"])

    def test_each_wrong_state_is_rejected_with_plain_reason(self):
        ticket = self.dispatch()
        image = str(self.picture())
        calls = [
            lambda: self.service.submit(ticket["编号"], "说明"),
            lambda: self.service.judge(ticket["编号"], True, "判卷人"),
            lambda: self.service.merge(ticket["编号"], "复检人"),
            lambda: self.service.live(ticket["编号"], image, "复检人", "独图"),
            lambda: self.service.close(ticket["编号"], "总编排"),
            lambda: self.service.unblock(ticket["编号"]),
        ]
        for call in calls:
            with self.subTest(call=call), self.assertRaisesRegex(TicketError, "只有|必须"):
                call()


class BlockedAnswerPermissionTests(TicketTestCase):
    """阻塞单放行「所属总监位 + 总编排」两方（T-000683，答 T-000680 / D9-405 ①）。

    设计者仍然答不动阻塞，那一条由 StateMachineTests.test_blocker_is_a_distinct_ticket_type
    钉着，本类不重复；需求已在 T-000770 另行放开（见 DemandAnswerPermissionTests），
    总工单的口径一个字没动，这里只留一条回归。
    """

    def blocked(self, title: str = "缺登录凭据", slot: str = SLOT):
        return self.service.create_question("阻塞", slot, title, "请协调一下登录凭据。")

    def test_owner_slot_can_answer_blocked_including_after_transfer(self):
        ticket = self.service.answer(self.blocked()["编号"], "已协调，凭据放在 server-keys。", SLOT)
        self.assertEqual("已答", ticket["状态"])
        self.assertEqual("已协调，凭据放在 server-keys。", ticket["答复"])
        # transfer 把「所属总监位」改成接收位，所以放行的是转交后的接收位，不是最初挂的那一位；
        # 这也正是 T-000575 的场景：总编排把阻塞转回所属位，所属位要按得动。
        moved = self.service.transfer(self.blocked("转给别位的阻塞")["编号"], OTHER_SLOT, "归他管", "总编排")
        self.assertEqual(OTHER_SLOT, moved["所属总监位"])
        self.assertEqual("已答", self.service.answer(moved["编号"], "已协调。", OTHER_SLOT)["状态"])

    def test_other_slot_is_refused_and_the_error_names_the_owner(self):
        ticket = self.blocked()
        with self.assertRaises(TicketError) as caught:
            self.service.answer(ticket["编号"], "我来代答。", OTHER_SLOT)
        message = str(caught.exception)
        for expected in (ticket["编号"], SLOT, OTHER_SLOT):
            self.assertIn(expected, message)
        self.assertEqual("待答", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_orchestrator_can_still_answer_blocked(self):
        self.assertEqual("已答", self.service.answer(self.blocked()["编号"], "已协调。", "总编排")["状态"])

    def test_general_ticket_still_refuses_the_owner_slot(self):
        """总工单是总编排自己的账本，T-000770 放开需求时它一个字没动。"""
        ticket = self.service.create_question("总工单", SLOT, "本周总账", "请总编排汇总。")
        with self.assertRaisesRegex(TicketError, "只能由总编排答复"):
            self.service.answer(ticket["编号"], "已汇总。", SLOT)
        self.assertEqual("已答", self.service.answer(ticket["编号"], "已汇总。", "总编排")["状态"])

class HardGateTests(TicketTestCase):
    def test_gate_1_source_and_consumer_required_before_claim(self):
        missing_source = self.service.create_dispatch(SLOT, "缺依据", [], "主场景", self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=False)
        with self.assertRaisesRegex(TicketError, "真源指针"):
            self.service.claim(missing_source["编号"], self.worker)
        missing_consumer = self.service.create_dispatch(SLOT, "缺消费者", ["DECISIONS.md:1"], "", self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=False)
        with self.assertRaisesRegex(TicketError, "实机消费者"):
            self.service.claim(missing_consumer["编号"], self.worker)

    def test_gate_2_isolated_picture_does_not_allow_submit(self):
        ticket = self.dispatch(); self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "isolated", self.worker)
        with self.assertRaisesRegex(TicketError, "主场景真登录"):
            self.service.submit(ticket["编号"], "隔离场景看到了")

    def test_gate_3_judge_must_differ_from_worker(self):
        ticket = self.to_judging()
        with self.assertRaisesRegex(TicketError, "不能与执行员工"):
            self.service.judge(ticket["编号"], True, self.worker)

    def test_gate_4_reviewer_must_differ_from_both(self):
        ticket = self.to_judging(); ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        # 先把复验那一道补上(D9-460 ①),否则先撞的是「还没复验」,测不到这里要钉的三方互斥闸。
        self.verified(ticket["编号"])
        for actor in (self.worker, "UI总监"):
            with self.subTest(actor=actor), self.assertRaisesRegex(TicketError, "都不同"):
                self.service.merge(ticket["编号"], actor)

    def test_gate_5_live_adds_a_second_world_picture(self):
        ticket = self.to_judging(); ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        ticket = self.merged_ticket(ticket["编号"], "独立复检")
        before = len(ticket["接线证据"]["图片列表"])
        ticket = self.service.live(ticket["编号"], str(self.picture("second.png")), "独立复检", "独图")
        self.assertEqual(before + 1, len(ticket["接线证据"]["图片列表"]))
        self.assertEqual("主场景真登录", ticket["接线证据"]["图片列表"][-1]["来源标注"])

    def test_gate_6_answer_roles(self):
        decision = self.service.create_question("拍板", SLOT, "拍板", VALID_DECISION_BODY)
        with self.assertRaisesRegex(TicketError, "设计者或总编排"):
            self.service.answer(decision["编号"], "乱答", "普通员工")
        # 需求的答复权 T-000770 放开给了所属位与设计者，但三种前缀对谁都一样：「乱答」照样拒。
        requirement = self.service.create_question("需求", SLOT, "需求", "请改")
        with self.assertRaisesRegex(TicketError, "首词必须是这三种之一"):
            self.service.answer(requirement["编号"], "乱答", "设计者")
        general = self.service.create_question("总工单", SLOT, "总工单", "请汇总")
        with self.assertRaisesRegex(TicketError, "只能由总编排"):
            self.service.answer(general["编号"], "乱答", "设计者")

    def test_gate_7_staff_registry_and_format(self):
        for actor in ("前端·界面与交互-99", "格式错误"):
            ticket = self.service.create_dispatch(SLOT, actor, ["DECISIONS.md:1"], "主场景", task_tier="乙", deliverables=[str(self.deliverable)], internal=False)
            with self.subTest(actor=actor), self.assertRaisesRegex(TicketError, "员工名格式|名册"):
                self.service.claim(ticket["编号"], actor)

    def test_rework_to_retired_worker_prints_reassign_warning(self):
        ticket = self.to_judging(); self.service.staff_retire(self.worker)
        ticket, warning = self.service.judge(
            ticket["编号"], False, "UI总监", "入口不稳", REWORK_VERDICT, "模型",
        )
        self.assertEqual(self.worker, ticket["指派给"])
        self.assertIn("已收窗", warning)
        self.assertIn("改派", warning)


class LiveShotTests(TicketTestCase):
    def test_live_requires_shot_with_exact_plain_message(self):
        ticket = self.to_merged()
        result = run_local_cli([
            "live", ticket["编号"], str(self.picture("missing-shot.png")), "--by", "独立复检",
        ], self.service.store.root)
        self.assertEqual(2, result.returncode)
        self.assertIn(
            "live 必须说明这张图是同图(全批共用一张)还是独图(专为这张单拍):--shot 同图 或 --shot 独图。",
            result.stderr,
        )

    def test_shot_classifies_unique_shared_internal_and_old_rows(self):
        unique = self.service.live(
            self.to_merged("独图单")["编号"], str(self.picture("unique.png")), "独立复检", "独图",
        )
        shared = self.service.live(
            self.to_merged("同图可感知")["编号"], str(self.picture("shared.png")), "独立复检", "同图",
        )
        internal = self.service.live(self.to_merged("同图内部", internal=True)["编号"], "", "独立复检", "同图")
        self.assertEqual("独图", unique["实机图标记"])
        self.assertEqual("待独图", shared["实机图标记"])
        self.assertEqual("同图", internal["实机图标记"])
        self.assertEqual([], internal["图片列表"])
        old = dict(unique)
        old.pop("实机图标记")
        from ticket_desk.ticket import compact_ticket
        self.assertTrue(compact_ticket(old).endswith(" · 乙档"))

    def test_pending_ticket_can_add_unique_picture_without_state_change(self):
        ticket = self.to_merged("补独图")
        ticket = self.service.live(ticket["编号"], str(self.picture("shared-first.png")), "独立复检", "同图")
        before = len(ticket["接线证据"]["图片列表"])
        entered = ticket["状态进入时间"]
        ticket = self.service.live(ticket["编号"], str(self.picture("unique-later.png")), "独立复检", "独图")
        self.assertEqual("实机复验过", ticket["状态"])
        self.assertEqual("独图", ticket["实机图标记"])
        self.assertEqual(before + 1, len(ticket["接线证据"]["图片列表"]))
        self.assertEqual(entered, ticket["状态进入时间"])
        events = self.service.store.read_jsonl(self.service.store.log_path)
        self.assertTrue(any(row.get("工单号") == ticket["编号"] and "补独图" in row.get("说明", "") for row in events))

    def test_only_pending_ticket_can_be_lived_again(self):
        ticket = self.to_merged("不可重复")
        ticket = self.service.live(ticket["编号"], str(self.picture("already-unique.png")), "独立复检", "独图")
        with self.assertRaisesRegex(TicketError, "只有实机图标记为.*待独图"):
            self.service.live(ticket["编号"], str(self.picture("repeat.png")), "独立复检", "独图")


class InternalLiveByDeployRecordTests(TicketTestCase):
    """内部单并线并上服之后,可以拿那笔上服记录当实机证据**显式收口**。

    ★这一组补的不是「解死锁」:内部单并线本来就已经是终态(is_terminal 为真),
      不进老化、不占执行方的手。它补的是**出口**——想把这类单显式关掉时,
      close 要「实机复验过」、live 要一张它根本截不出来的登录图、
      旧的免独图闸只认「已经复验过」,而 close --not-deployed 又会拒绝它,
      理由是「代码在线上跑着,欠的只是一笔 live 记账」——那句话准,却指向一条走不通的路。
    ★三条判据全是机器能核的事实,任一不成立,老闸与署名闸都当场收回原样(见下面两条反面用例)。
    """

    REASON = "内部工具单,无用户可见界面;实机证据是那笔上服记录"
    HEAD = "abc1234de"

    def deployed_internal(self, title: str = "内部单已上服", *, internal: bool = True,
                          record_head: str | None = HEAD):
        ticket = self.to_merged(title, internal=internal)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["判语"] = f"判过。判的是提交 {self.HEAD}。"
        self.service.store.atomic_json(self.service.store.item_path(stored["编号"]), stored)
        if record_head is not None:
            self.service.state_set("deploy_head", record_head, service_module.REVIEW_SLOT)
        return self.service.store.load_ticket(stored["编号"])

    def test_three_facts_all_hold_so_it_closes_through_the_deploy_record(self):
        ticket = self.deployed_internal()
        self.assertTrue(self.service.internal_live_by_deploy_record(ticket))
        done = self.service.shot_exempt(ticket["编号"], "复检·合并与部署-01", self.REASON)
        self.assertEqual("实机复验过", done["状态"])
        self.assertEqual("免独图", done["实机图标记"])
        self.assertEqual(self.REASON, done["免独图原因"])
        # 收到「实机复验过」之后,close 那条路才真的通——这才是本组的目的
        closed = self.service.close(done["编号"], "总编排")
        self.assertEqual("关闭", closed["状态"])

    def test_the_owning_director_may_sign_only_on_this_edge(self):
        """署名放宽只在这条边上:走它时是**核对**(是不是内部单、上没上服),不是判断。"""
        ticket = self.deployed_internal("本位总监也能签")
        done = self.service.shot_exempt(ticket["编号"], SLOT, self.REASON)
        self.assertEqual("实机复验过", done["状态"])

    def test_user_facing_ticket_does_not_take_this_edge(self):
        """★反面:用户可感知单照旧要真登录图,一个字没放松。"""
        ticket = self.deployed_internal("用户可感知单", internal=False)
        self.assertFalse(self.service.internal_live_by_deploy_record(ticket))
        with self.assertRaisesRegex(TicketError, "现在是「已合并」"):
            self.service.shot_exempt(ticket["编号"], "复检·合并与部署-01", self.REASON)

    def test_internal_ticket_not_yet_deployed_does_not_take_this_edge(self):
        """★反面:没上服的内部单不走这条边——实机证据是那笔上服记录,没记录就没证据。"""
        ticket = self.deployed_internal("没上服的内部单", record_head="99999999")
        self.assertFalse(self.service.internal_live_by_deploy_record(ticket))
        with self.assertRaisesRegex(TicketError, "现在是「已合并」"):
            self.service.shot_exempt(ticket["编号"], "复检·合并与部署-01", self.REASON)

    def test_signature_gate_snaps_back_when_the_edge_does_not_hold(self):
        """★反面中最要紧的一条:边不成立时,本位总监**立刻**签不动。

        放宽署名与放宽状态是同一个 by_deploy_record 控制的。要是哪天有人把这两件事拆开,
        就会出现「边不成立、署名却还开着」的口子——那等于本位总监能给任何单打免独图。
        """
        ticket = self.deployed_internal("没上服所以签不动", record_head="99999999")
        with self.assertRaises(TicketError) as caught:
            self.service.shot_exempt(ticket["编号"], SLOT, self.REASON)
        self.assertIn(SLOT, str(caught.exception))
        self.assertIn("只有", str(caught.exception))
        self.assertEqual("已合并", self.service.store.load_ticket(ticket["编号"])["状态"])


class ShotExemptTests(TicketTestCase):
    """D9-402 ②:诊断类单与验证对象已退役的单可以免掉那张独图,但只有复检席与总编排能打,且必须写原因。"""

    REASON = "判语已写明无用户可见产出,属诊断类单"

    def pending(self, title: str = "待独图豁免"):
        ticket = self.to_merged(title)
        return self.service.live(
            ticket["编号"], str(self.picture(f"{ticket['编号']}-shared.png")), "独立复检", "同图",
        )

    def test_review_slot_marks_exempt_and_writes_reason(self):
        ticket = self.pending()
        ticket = self.service.shot_exempt(ticket["编号"], "复检·合并与部署-01", self.REASON)
        self.assertEqual("免独图", ticket["实机图标记"])
        self.assertEqual(self.REASON, ticket["免独图原因"])
        self.assertEqual("实机复验过", ticket["状态"])
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("免独图", stored["实机图标记"])
        self.assertEqual(self.REASON, stored["免独图原因"])
        events = self.service.store.read_jsonl(self.service.store.log_path)
        self.assertTrue(any(
            row.get("工单号") == ticket["编号"] and row.get("事件") == "live"
            and f"免独图 · {self.REASON}" == row.get("说明") for row in events
        ))

    def test_conductor_may_also_mark_exempt(self):
        ticket = self.pending("总编排也能打")
        ticket = self.service.shot_exempt(ticket["编号"], "总编排", "验证对象已退役,原命题不再成立")
        self.assertEqual("免独图", ticket["实机图标记"])
        self.assertEqual("验证对象已退役,原命题不再成立", ticket["免独图原因"])

    def test_other_slot_is_rejected_with_its_signature_quoted(self):
        ticket = self.pending("别位总监不能打")
        with self.assertRaises(TicketError) as caught:
            self.service.shot_exempt(ticket["编号"], SLOT, self.REASON)
        self.assertIn(SLOT, str(caught.exception))
        self.assertIn(ticket["编号"], str(caught.exception))
        self.assertEqual("待独图", self.service.store.load_ticket(ticket["编号"])["实机图标记"])

    def test_wrong_mark_or_wrong_state_is_rejected(self):
        unique = self.service.live(
            self.to_merged("已经是独图")["编号"], str(self.picture("exempt-unique.png")), "独立复检", "独图",
        )
        with self.assertRaisesRegex(TicketError, "实机图标记现在是「独图」"):
            self.service.shot_exempt(unique["编号"], "复检·合并与部署-01", self.REASON)
        merged = self.to_merged("还没复验过")
        with self.assertRaisesRegex(TicketError, "现在是「已合并」"):
            self.service.shot_exempt(merged["编号"], "复检·合并与部署-01", self.REASON)
        self.assertEqual("独图", self.service.store.load_ticket(unique["编号"])["实机图标记"])
        self.assertEqual("", self.service.store.load_ticket(merged["编号"])["实机图标记"])

    def test_empty_reason_is_rejected(self):
        ticket = self.pending("缺原因")
        for reason in ("", "   "):
            with self.subTest(reason=reason), self.assertRaisesRegex(TicketError, "--reason 不能为空"):
                self.service.shot_exempt(ticket["编号"], "复检·合并与部署-01", reason)
        self.assertEqual("待独图", self.service.store.load_ticket(ticket["编号"])["实机图标记"])

    def test_exempt_drops_out_of_pending_list_and_digest_count(self):
        from ticket_desk.ticket import execute, parser

        target = self.pending("要被豁免的")
        other = self.pending("仍然待独图")
        before_rows, _ = execute(parser().parse_args(["list", "--shot-pending"]), self.service)
        before_count = self._digest_pending(self.service.digest())
        self.assertEqual(2, before_count)
        self.assertEqual(
            {target["编号"], other["编号"]}, {row["编号"] for row in before_rows},
        )
        self.service.shot_exempt(target["编号"], "复检·合并与部署-01", self.REASON)
        after_rows, _ = execute(parser().parse_args(["list", "--shot-pending"]), self.service)
        after_count = self._digest_pending(self.service.digest())
        self.assertEqual([other["编号"]], [row["编号"] for row in after_rows])
        self.assertEqual(before_count - 1, after_count)

    @staticmethod
    def _digest_pending(lines: list[str]) -> int:
        row = next(line for line in lines if line.startswith("待独图 "))
        return int(re.fullmatch(r"待独图 (\d+) 张", row).group(1))

    def test_cli_rejects_batch_and_picture_and_marks_through_service(self):
        ticket = self.pending("命令行豁免")
        batched = run_local_cli([
            "live", ticket["编号"], "--batch", "T-000001", "--shot", "免独图",
            "--reason", self.REASON, "--by", "复检·合并与部署-01",
        ], self.service.store.root)
        self.assertEqual(2, batched.returncode)
        self.assertIn("不能和 --batch 一起用", batched.stderr)
        withpic = run_local_cli([
            "live", ticket["编号"], str(self.picture("exempt-with-pic.png")), "--shot", "免独图",
            "--reason", self.REASON, "--by", "复检·合并与部署-01",
        ], self.service.store.root)
        self.assertEqual(2, withpic.returncode)
        self.assertIn("不要图", withpic.stderr)
        self.assertEqual("待独图", self.service.store.load_ticket(ticket["编号"])["实机图标记"])
        good = run_local_cli([
            "live", ticket["编号"], "--shot", "免独图",
            "--reason", self.REASON, "--by", "复检·合并与部署-01",
        ], self.service.store.root)
        self.assertEqual(0, good.returncode, good.stderr)
        self.assertIn(f"免独图 · {self.REASON}", good.stdout)
        self.assertEqual("免独图", self.service.store.load_ticket(ticket["编号"])["实机图标记"])

    def test_first_live_cannot_take_the_exempt_value(self):
        """已合并态的首次 live 不许免图:SHOT_EXEMPT 不在 SHOT_VALUES,豁免只走 shot_exempt。"""
        from ticket_desk.service import SHOT_EXEMPT, SHOT_VALUES

        self.assertNotIn(SHOT_EXEMPT, SHOT_VALUES)
        merged = self.to_merged("首次 live 不能免图")
        with self.assertRaisesRegex(TicketError, "live 必须说明这张图"):
            self.service.live(merged["编号"], str(self.picture("first-live.png")), "总编排", SHOT_EXEMPT)
        self.assertEqual("已合并", self.service.store.load_ticket(merged["编号"])["状态"])

class LiveBatchTests(TicketTestCase):
    def five_merged(self) -> list[dict[str, object]]:
        return [
            self.to_merged("可感知一"), self.to_merged("内部一", internal=True),
            self.to_merged("可感知二"), self.to_merged("内部二", internal=True),
            self.to_merged("可感知三"),
        ]

    def test_one_picture_batch_marks_two_shared_and_three_pending(self):
        tickets = self.five_merged()
        rows = self.service.live_batch(
            [row["编号"] for row in tickets], str(self.picture("batch.png")), "独立复检", "同图",
        )
        self.assertEqual(["已复验"] * 5, [row["结果"] for row in rows])
        stored = [self.service.store.load_ticket(row["编号"]) for row in tickets]
        self.assertEqual(["实机复验过"] * 5, [row["状态"] for row in stored])
        self.assertEqual(2, sum(row["实机图标记"] == "同图" for row in stored))
        self.assertEqual(3, sum(row["实机图标记"] == "待独图" for row in stored))

    def test_wrong_state_is_skipped_without_dragging_down_other_five(self):
        tickets = self.five_merged()
        wrong = self.to_judging()
        rows = self.service.live_batch(
            [wrong["编号"], *[row["编号"] for row in tickets]],
            str(self.picture("mixed-batch.png")), "独立复检", "同图",
        )
        self.assertEqual("跳过", rows[0]["结果"])
        self.assertIn("当前状态是“待判”", rows[0]["原因"])
        self.assertEqual(["已复验"] * 5, [row["结果"] for row in rows[1:]])
        self.assertEqual("待判", self.service.store.load_ticket(wrong["编号"])["状态"])
        self.assertTrue(all(
            self.service.store.load_ticket(row["编号"])["状态"] == "实机复验过" for row in tickets
        ))

    def test_pending_filter_digest_and_compact_output(self):
        pending = self.service.live(
            self.to_merged("筛选目标")["编号"], str(self.picture("pending-filter.png")), "独立复检", "同图",
        )
        self.service.live(
            self.to_merged("不是目标")["编号"], str(self.picture("unique-filter.png")), "独立复检", "独图",
        )
        from ticket_desk.ticket import execute, parser
        rows, text = execute(parser().parse_args(["list", "--shot-pending"]), self.service)
        self.assertEqual([pending["编号"]], [row["编号"] for row in rows])
        self.assertIn("· 待独图", text)
        self.assertNotIn("不是目标", text)
        self.assertIn("待独图 1 张", self.service.digest())

class StaffAndConversationTests(TicketTestCase):
    def test_staff_numbers_never_reuse_and_are_independent_per_slot(self):
        second = self.service.staff_new(SLOT, "codex")["员工名"]
        third = self.service.staff_new(SLOT, "claude")["员工名"]
        other = self.service.staff_new(OTHER_SLOT, "model-a")["员工名"]
        self.assertEqual(["前端·界面与交互-01", "前端·界面与交互-02", "前端·界面与交互-03"], [self.worker, second, third])
        self.assertEqual("后端·服务与接口-01", other)
        self.service.staff_retire(second)
        reopened = self.service.staff_reopen(second)
        self.assertEqual(second, reopened["员工名"])
        self.assertEqual("在岗", reopened["状态"])

    def test_history_lists_owned_tickets(self):
        ticket = self.dispatch()
        history = self.service.history(self.worker)
        self.assertEqual([ticket["编号"]], [row["编号"] for row in history["工单"]])

    def test_say_inbox_mark_read_and_slot_isolation(self):
        self.service.say(SLOT, "设计者", "请检查", reference="")
        self.service.say(OTHER_SLOT, "设计者", "另一个位")
        rows = self.service.inbox(SLOT, "总编排")
        self.assertEqual(["请检查"], [row["文字"] for row in rows])
        self.assertEqual(1, len(self.service.inbox(SLOT, "总编排", True)))
        self.assertEqual([], self.service.inbox(SLOT, "总编排"))


class TransferAndHumanGateTests(TicketTestCase):
    def test_transfer_four_targets_keeps_id_and_state_and_updates_views_log_digest(self):
        targets = ("总编排", "复检·合并与部署", OTHER_SLOT, "设计者")
        ticket_ids = []
        for target in targets:
            original = self.dispatch(f"转给{target}")
            ticket_ids.append(original["编号"])
            transferred = self.service.transfer(original["编号"], target, f"需要{target}接手", SLOT)
            self.assertEqual(original["编号"], transferred["编号"])
            self.assertEqual("新建", transferred["状态"])
            self.assertEqual(target, transferred["指派给"])
            self.assertEqual(target, transferred["转交历史"][-1]["到"])
            self.assertEqual(1, sum(row["编号"] == original["编号"] for row in self.service.list_tickets(SLOT)))
            if target in ("总编排", "复检·合并与部署", OTHER_SLOT):
                self.assertEqual(1, sum(row["编号"] == original["编号"] for row in self.service.list_tickets(target)))

        transfer_logs = [row for row in self.service.store.read_jsonl(self.service.store.log_path) if row.get("op") == "transfer"]
        self.assertEqual(4, len(transfer_logs))
        self.assertEqual({"from", "to", "reason", "by"}, {key for key in transfer_logs[0] if key in {"from", "to", "reason", "by"}})
        digest = "\n".join(self.service.digest())
        self.assertIn("今日转交", digest)
        for ticket_id in ticket_ids:
            self.assertIn(ticket_id, digest)
        source_notices = [row for row in self.service.inbox(SLOT, "总编排") if row.get("引用工单号") in ticket_ids]
        self.assertEqual(4, len(source_notices))
        incoming = [row for row in self.service.inbox(OTHER_SLOT, "总编排") if row.get("引用工单号") == ticket_ids[2]]
        self.assertEqual(1, len(incoming))

    def test_empty_transfer_reason_is_rejected(self):
        ticket = self.dispatch()
        with self.assertRaisesRegex(TicketError, "请用一句话写明原因"):
            self.service.transfer(ticket["编号"], "总编排", "  ", SLOT)

    def test_submitted_dispatch_transfer_keeps_original_worker(self):
        ticket = self.to_judging()
        transferred = self.service.transfer(ticket["编号"], "总编排", "交给总编排判卷", SLOT)
        self.assertEqual("待判", transferred["状态"])
        self.assertEqual(self.worker, transferred["指派给"])
        self.assertEqual("总编排", transferred["所属总监位"])

    def test_fresh_dispatch_transfer_still_changes_assignee(self):
        ticket = self.dispatch("新建态换人接手")
        transferred = self.service.transfer(ticket["编号"], "总编排", "换人接手", SLOT)
        self.assertEqual("新建", transferred["状态"])
        self.assertEqual("总编排", transferred["指派给"])

    def test_decision_requires_three_human_sections_and_limits_bare_ids(self):
        with self.assertRaisesRegex(TicketError, "拍板单要写成三段人话"):
            self.service.create_question("拍板", SLOT, "缺段", "一、这是什么\n一个问题\n二、选了会怎样\n会改变界面")
        too_many = VALID_DECISION_BODY + "\nD9-1 DA-2 PV-3 BE-4"
        with self.assertRaisesRegex(TicketError, "拍板单要写成三段人话"):
            self.service.create_question("拍板", SLOT, "术语过多", too_many)
        parenthesized = VALID_DECISION_BODY + "\n（D9-1 DA-2 PV-3 BE-4）"
        self.assertEqual("待答", self.service.create_question("拍板", SLOT, "括号说明", parenthesized)["状态"])
        legacy = self.service.create_question("拍板", SLOT, "旧专业单", VALID_DECISION_BODY)
        legacy["正文"] = "只有专业编号 D9-1 DA-2 PV-3 BE-4，没有三段人话"
        self.service.store.atomic_json(self.service.store.item_path(legacy["编号"]), legacy)
        with self.assertRaisesRegex(TicketError, "拍板单要写成三段人话"):
            self.service.transfer(legacy["编号"], "设计者", "送设计者拍板", SLOT)

    def test_non_dispatch_transfer_returns_to_waiting_answer(self):
        ticket = self.service.create_question("拍板", SLOT, "重新转交", VALID_DECISION_BODY)
        ticket = self.service.answer(ticket["编号"], "先按推荐", "设计者")
        self.assertEqual("已答", ticket["状态"])
        ticket = self.service.transfer(ticket["编号"], "总编排", "请总编排复核", "设计者")
        self.assertEqual("待答", ticket["状态"])

    def test_say_takes_any_slot_and_rejects_unregistered_signatures(self):
        # 原来这一条钉的是「三方之外一律拒」，连总监之间传一句话也拒；
        # 那条路被真用起来之后才发现它堵死的是「传话」本身——一句话的事只剩「建一张疑问单」，
        # 而疑问单是要人答的、会进对方待答队列。现在位名一侧放开，方向翻过来钉：
        # 在册位名放行、不在册的署名照拒、**员工那条窄缝一个字没放松**。
        expected = TicketService.SAY_REFUSED
        self.assertIn("总监之间可互相对话", expected)
        # ① 别位总监现在能说，而且那一行的发言人就是他本人——串门的顾虑由留痕兜着
        row = self.service.say(SLOT, OTHER_SLOT, "别位总监跨位说一句")
        self.assertEqual(OTHER_SLOT, row["发言人"])
        self.assertNotIn("【员工留言】", row["文字"])   # 总监不是员工,不该被打上员工留言前缀
        # ② 员工不带 --ref:照旧拒
        with self.assertRaises(TicketError) as caught:
            self.service.say(SLOT, self.worker, "员工不带 --ref 仍然进不去")
        self.assertEqual(expected, str(caught.exception))
        # ③ 压根不在名册里的署名:拒
        with self.assertRaises(TicketError) as caught:
            self.service.say(SLOT, "路过的谁", "不在册的署名进不去")
        self.assertEqual(expected, str(caught.exception))

    def test_say_and_say_uploaded_share_one_gate(self):
        """两条入口共用 _slot_may_say——同一个事实两处各拼一份必然漂，这里钉住它只有一份。"""
        source = inspect.getsource(TicketService)
        self.assertEqual(2, source.count("self._slot_may_say(actor) or by_staff"))
        self.assertNotIn("{OWNER_ROLE, CONDUCTOR_SLOT, slot}", source)
        row = self.service.say_uploaded(SLOT, OTHER_SLOT, "别位总监走网页台面也能说")
        self.assertEqual(OTHER_SLOT, row["发言人"])

    def test_cross_slot_ticket_records_origin_and_is_copied_to_digest(self):
        ticket = self.service.create_question("疑问", OTHER_SLOT, "跨位求证", "请后端总监答复", initiator=self.worker)
        self.assertEqual(SLOT, ticket["发起位"])
        self.assertEqual(OTHER_SLOT, ticket["指派给"])
        digest = "\n".join(self.service.digest())
        self.assertIn(f"[跨位单] {ticket['编号']} · {SLOT}→{OTHER_SLOT} · 抄送总编排", digest)

    def test_missing_deliverable_blocks_whole_submit_and_lists_the_line(self):
        missing = self.root / "missing-output.bin"
        ticket = self.service.create_dispatch(
            SLOT, "缺交付件", ["DECISIONS.md:1"], "主场景/UiRoot", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable), str(missing)], internal=False,
        )
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        with self.assertRaisesRegex(TicketError, re.escape(str(missing))):
            self.service.submit(ticket["编号"], "已有一部分")
        self.assertEqual("已认领", self.service.store.load_ticket(ticket["编号"])["状态"])


class InternalToolAndVerdictTests(TicketTestCase):
    def internal_dispatch(self):
        return self.service.create_dispatch(
            SLOT, "内部工具改造", ["ticket_desk/service.py"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )

    def test_internal_submit_requires_command_and_raw_output_but_not_world_picture(self):
        ticket = self.internal_dispatch()
        self.assertTrue(ticket["非用户可感知"])
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaisesRegex(TicketError, "验证命令与原样输出"):
            self.service.submit(ticket["编号"], "内部验证", "python -m pytest", "")
        ticket = self.service.submit(ticket["编号"], "内部验证", "python -m pytest", "44 passed")
        self.assertEqual("待判", ticket["状态"])
        self.assertEqual("python -m pytest", ticket["接线证据"]["验证命令"])
        self.assertEqual("44 passed", ticket["接线证据"]["原样输出"])
        self.assertEqual([], ticket["图片列表"])

    def test_internal_live_bypasses_second_world_picture_gate(self):
        ticket = self.internal_dispatch()
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "44 passed")
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        ticket = self.merged_ticket(ticket["编号"], "独立复检")
        ticket = self.service.live(ticket["编号"], "", "独立复检", "同图")
        self.assertEqual("实机复验过", ticket["状态"])
        self.assertEqual([], ticket["图片列表"])

    def test_judge_requires_verdict_and_pass_requires_opening_sentence(self):
        ticket = self.to_judging()
        with self.assertRaisesRegex(TicketError, "判语不能为空"):
            self.service.judge(ticket["编号"], True, "UI总监")
        with self.assertRaisesRegex(TicketError, "用户怎么打开它"):
            self.service.judge(ticket["编号"], True, "UI总监", verdict="功能通过。")
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual(PASS_VERDICT, ticket["判语"])

    def test_internal_judge_accepts_user_or_designer_opening_sentence(self):
        verdicts = (
            "用户怎么打开它：双击启动工单台。功能通过。",
            "设计者怎么打开它：双击启动工单台。功能通过。",
        )
        for verdict in verdicts:
            with self.subTest(verdict=verdict):
                ticket = self.internal_dispatch()
                self.service.claim(ticket["编号"], self.worker)
                self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
                judged, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=verdict)
                self.assertEqual("待复检", judged["状态"])

    def test_judge_takes_either_opening_sentence_on_internal_and_user_visible_alike(self):
        """两句写哪一句都放行（T-001499 ①，在 T-001470 上撞到）。

        原来「设计者怎么打开它」只对内部单放行。可取证/事实核查那一类单要真登录图、
        因此标不了 --internal，产出却是屏上那一眼——用户没有「打开它」这回事，于是判不过去，
        而 judge -h 又写着可以那么写。放宽的是**措辞**，用户可感知那条纪律一个字没动：
        它靠 --consumer、交板的 主场景真登录图、独图与实机复验守，那几道都比一句措辞硬。
        """
        internal = self.internal_dispatch()
        self.service.claim(internal["编号"], self.worker)
        self.service.submit(internal["编号"], "验证完成", "python -m pytest", "all passed")
        judged, _ = self.service.judge(
            internal["编号"], True, "UI总监", verdict="设计者怎么打开它：双击启动工单台。通过。")
        self.assertEqual("待复检", judged["状态"])

        visible = self.to_judging()
        judged, _ = self.service.judge(
            visible["编号"], True, "UI总监", verdict="设计者怎么打开它：打开工单台。通过。")
        self.assertEqual("待复检", judged["状态"], "用户可感知单也要认「设计者怎么打开它」")

    def test_judge_still_refuses_a_verdict_with_neither_opening_sentence(self):
        """放宽的只是二选一，不是把这道闸拆了:一句都不写照样拦，且报错要把两句都摆出来。"""
        internal = self.internal_dispatch()
        self.service.claim(internal["编号"], self.worker)
        self.service.submit(internal["编号"], "验证完成", "python -m pytest", "all passed")
        for ticket in (internal, self.to_judging()):
            with self.subTest(ticket=ticket["编号"]):
                with self.assertRaisesRegex(TicketError, "用户怎么打开它.*设计者怎么打开它"):
                    self.service.judge(ticket["编号"], True, "UI总监", verdict="功能通过。")

    def test_digest_always_has_zero_transfer_section(self):
        self.assertIn("今日转交 0", self.service.digest())


class ImageExportAndBuildTests(TicketTestCase):
    def test_r2_4_save_image_replace_failure_never_exposes_final_path(self):
        image_dir = self.root / "atomic-images"
        target = image_dir / "evidence.jpg"
        with mock.patch.object(store_module.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.service.store.save_image(image_dir, target.name, b"partial-or-complete-bytes")
        self.assertFalse(target.exists())
        leftovers = list(image_dir.iterdir())
        self.assertTrue(all(path.name == "evidence.jpg.tmp" for path in leftovers))

    def test_r1_1_noisy_1280_png_compresses_under_limit(self):
        size = (1280, 720)
        pixels = random.Random(38).randbytes(size[0] * size[1] * 3)
        source = self.root / "noisy-1280.png"
        Image.frombytes("RGB", size, pixels).filter(ImageFilter.GaussianBlur(0.7)).save(source)
        self.assertGreater(source.stat().st_size, 1.5 * 1024 * 1024)

        ticket = self.dispatch("R1-1 质量循环")
        _, record = self.service.attach(ticket["编号"], str(source), "other", self.worker)
        target = self.service.store.images_dir / record["文件名"]
        self.assertLessEqual(target.stat().st_size, MAX_IMAGE_BYTES)

    def test_r1_2_path_bytes_and_remote_compression_have_identical_sha256(self):
        source = self.picture("same-input.png", (1600, 900))
        path_ticket = self.dispatch("R1-2 路径入口")
        bytes_ticket = self.dispatch("R1-2 字节入口")
        _, path_record = self.service.attach(path_ticket["编号"], str(source), "other", self.worker)
        _, bytes_record = self.service.attach_bytes(
            bytes_ticket["编号"], source.read_bytes(), source.name, "other", self.worker
        )
        _, remote_data = RemoteClient._compress(source)
        outputs = (
            (self.service.store.images_dir / path_record["文件名"]).read_bytes(),
            (self.service.store.images_dir / bytes_record["文件名"]).read_bytes(),
            remote_data,
        )
        digests = tuple(hashlib.sha256(data).hexdigest() for data in outputs)
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(digests[0], digests[2])

    def test_r1_3_uncompressible_error_reports_quality_edge_and_size(self):
        source = self.picture("too-large-for-one-byte.png", (128, 96))
        with mock.patch.object(service_module, "MAX_IMAGE_BYTES", 1):
            with self.assertRaisesRegex(
                TicketError, r"已降到质量 50 / 长边 \d+,仍 \d+(?:\.\d+)?KB"
            ):
                service_module.compress_image(source)

    def test_exif_orientation_is_transposed_before_encoding(self):
        source = self.root / "rotated-by-exif.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (40, 80), (15, 30, 45)).save(source, exif=exif)
        _, data = service_module.compress_image(source)
        with Image.open(io.BytesIO(data)) as compressed:
            self.assertEqual((80, 40), compressed.size)

    def test_large_png_is_resized_and_under_limit(self):
        source = self.picture("large.png", (4000, 3000))
        ticket = self.dispatch()
        _, record = self.service.attach(ticket["编号"], str(source), "world", self.worker)
        target = self.service.store.images_dir / record["文件名"]
        self.assertTrue(source.exists())
        self.assertLessEqual(target.stat().st_size, MAX_IMAGE_BYTES)
        with Image.open(target) as image:
            self.assertLessEqual(max(image.size), MAX_IMAGE_EDGE)
        self.assertEqual(".jpg", target.suffix)

    def test_transparent_picture_uses_webp(self):
        source = self.picture("alpha.png", (500, 400), "RGBA")
        ticket = self.dispatch()
        _, record = self.service.attach(ticket["编号"], str(source), "other", self.worker)
        target = self.service.store.images_dir / record["文件名"]
        self.assertEqual(".webp", target.suffix)
        self.assertLessEqual(target.stat().st_size, MAX_IMAGE_BYTES)

    def test_export_writes_to_the_explicit_out_path(self):
        ticket = self.dispatch("导出测试")
        out = self.root / "自定目录" / "导出的任务书.md"
        path = self.service.export(ticket["编号"], out)
        self.assertEqual(out.resolve(), path)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual("# 导出测试", lines[0])
        self.assertTrue(any(ticket["编号"] in line for line in lines[:4]))
        self.assertTrue(any("主场景/UiRoot" in line for line in lines))

    def test_export_default_path_uses_slot_ticket_and_sanitized_title(self):
        ticket = self.dispatch('标题有 / : * 非法字符')
        path = self.service.export(ticket["编号"], workspace_root=self.root)
        self.assertEqual(
            self.root / "_office" / SLOT / "任务书" / f"{ticket['编号']}_标题有 - - - 非法字符.md",
            path,
        )
        self.assertTrue(path.is_file())

    def test_c_tier_export_uses_restricted_template(self):
        ticket = self.service.create_dispatch(SLOT, "丙档导出", ["D:/source.txt:1-20"], r"D:\output.csv", self.worker, notes=r"把 D:\client\input.csv 转成一张 CSV", task_tier="丙", context_lines=20, deliverables=[str(self.deliverable)], internal=False)
        path = self.service.export(ticket["编号"], self.root / "丙档.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("任务档:丙", text)
        self.assertIn("## §2 只读这些文件", text)
        self.assertIn("D:/source.txt:1-20", text)
        self.assertIn(r"D:\client\input.csv", text)
        self.assertIn(r"D:\output.csv", text)
        self.assertIn("# 合计预算：20 行", text)
        self.assertIn("主场景真登录", text)
        self.assertIn("做完 §4 全部步骤再交,不中途停下等审", text)
        self.assertNotIn("/tmp/ticket-desk-workspace/模板", text)

    def test_export_rejects_the_retired_inbox_names_with_a_clear_message(self):
        result = run_local_cli(["export", "T-000001", "--inbox", "codex"], self.root / "cli-export")
        self.assertEqual(2, result.returncode)
        self.assertIn("--inbox 已废弃，改用 --out", result.stderr)

    def test_bundle_is_byte_identical_on_second_build(self):
        self.dispatch(); self.service.say(SLOT, "设计者", "幂等测试")
        output = self.root / "tickets-bundle.js"
        self.service.build_bundle(output); first = output.read_bytes()
        self.service.build_bundle(output); second = output.read_bytes()
        self.assertEqual(first, second)

    def test_receipt_is_exactly_one_line(self):
        ticket = self.dispatch("一行回执")
        receipt = self.service.receipt(ticket)
        self.assertEqual(f"已进入工单 {ticket['编号']} · 一行回执 · 新建", receipt)
        self.assertNotIn("\n", receipt)


# 判语全文特意写成多行:R1 要的是「原样,不截断、不改写」,单行判语钉不住这一条。
MULTILINE_REWORK_VERDICT = (
    "模型责任：四条守门用例一条都没写,交板证据也只写了「验证完成」。\n"
    "按任务书 R4 补齐五条用例后重交；每条要说清钉的是哪一句。\n"
    "另:基线绿数与完工绿数都要写进 --raw-output,不许只贴一句话。"
)


class VerdictReceiptTests(TicketTestCase):
    """判语随 receipt 打到员工窗(T-000891 R1,答 T-000870 / D9-424 ⑩)。

    开窗指令只有三行、只带任务书路径,判退的判语只留在卡片上;新窗跑 receipt
    看到的也只有标题与状态。T-000282 就是这么空转两轮的:判语要求「补四条守门用例」,
    新窗看不见,照着旧任务书又交了一模一样的板。
    """

    def to_rework(self, reason: str = "五条用例一条都没写", verdict: str = MULTILINE_REWORK_VERDICT):
        ticket = self.to_judging()
        ticket, _ = self.service.judge(ticket["编号"], False, "UI总监", reason, verdict, "模型")
        return ticket

    def test_rework_receipt_carries_the_whole_verdict_and_the_rework_count(self):
        """第 1 条:判退之后跑 receipt → 判语全文、首行责任归属、返工次数都在。"""
        ticket = self.to_rework()
        receipt = self.service.receipt(ticket)
        # 首行仍是原来那一行,员工一眼看得出单号与状态
        self.assertTrue(receipt.startswith(f"已进入工单 {ticket['编号']} · 测试派单 · 返工"))
        # ①判语原样:整段逐字在,连中间那两行都不许掉
        self.assertIn(MULTILINE_REWORK_VERDICT, receipt)
        for line in MULTILINE_REWORK_VERDICT.splitlines():
            self.assertIn(line, receipt)
        # D9-388:首行责任归属要看得见——员工得知道这一次是不是他的错
        self.assertIn("模型责任：", receipt)
        # ②一眼看出这是「上一轮为什么被退」
        self.assertIn("上一轮为什么被退", receipt)
        self.assertIn("第 1 次判退", receipt)
        self.assertIn("判卷:UI总监", receipt)
        # 最近一条返工原因也要带上
        self.assertIn("五条用例一条都没写", receipt)

    def test_rework_receipt_counts_up_on_the_second_bounce(self):
        """返工次数是累计的:第二次判退,receipt 上写的是「第 2 次」、判语换成新那份。"""
        ticket = self.to_rework()
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("second.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "改完再交")
        second = "模型责任：第二次仍然缺那条变异检验。"
        ticket, _ = self.service.judge(ticket["编号"], False, "UI总监", "变异检验没做", second, "模型")
        receipt = self.service.receipt(ticket)
        self.assertIn("第 2 次判退", receipt)
        self.assertIn(second, receipt)
        self.assertNotIn(MULTILINE_REWORK_VERDICT, receipt)

    def test_other_states_keep_the_receipt_at_exactly_one_line(self):
        """第 2 条:非返工态一个字都不加——receipt 每次开工都要跑,不能变成一堵墙。

        故意拿一张**判退过又被重新认领**的单:它的「判语」字段还留着上一轮那份,
        所以这条钉的是「按状态判」,不是「按判语字段空不空判」。
        """
        ticket = self.to_rework()
        # 已认领
        claimed = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual(MULTILINE_REWORK_VERDICT, claimed["判语"])
        receipt = self.service.receipt(claimed)
        self.assertEqual(f"已进入工单 {claimed['编号']} · 测试派单 · 已认领", receipt)
        self.assertNotIn("\n", receipt)
        self.assertNotIn("模型责任", receipt)
        self.assertNotIn("上一轮为什么被退", receipt)
        # 待判
        self.service.attach(ticket["编号"], str(self.picture("again.png")), "world", self.worker)
        judging = self.service.submit(ticket["编号"], "改完再交")
        receipt = self.service.receipt(judging)
        self.assertEqual(f"已进入工单 {judging['编号']} · 测试派单 · 待判", receipt)
        self.assertNotIn("\n", receipt)
        self.assertNotIn("模型责任", receipt)

    def test_cli_prints_the_verdict_and_keeps_the_protocol_tag_on_the_first_line(self):
        """网页与 CLI 都消费服务端同一处生成的文本:CLI 打出来的就是 service.receipt 那一份。

        协议尾巴只贴第一行——落到判语末尾会让人以为那句提示也是判语的一部分。
        """
        ticket = self.to_rework()
        tagged = channel_config.receipt_with_protocol(self.service.receipt(ticket), 3, 3)
        first, _, rest = tagged.partition("\n")
        self.assertTrue(first.endswith("· 客户端协议 3 · 服务端协议 3"), first)
        self.assertIn("返工", first)
        self.assertIn(MULTILINE_REWORK_VERDICT, rest)
        # 单行回执的输出与从前逐字相同
        plain = self.service.receipt(self.dispatch("一行回执"))
        self.assertEqual(
            f"{plain} · 客户端协议 3 · 服务端协议 3",
            channel_config.receipt_with_protocol(plain, 3, 3),
        )


class UnblockLandsOnReworkTests(TicketTestCase):
    """unblock 改落「返工」态,换书不换号(T-000891 R2/R3)。

    单子会被阻塞,多半正说明任务书要改;而以前 unblock 把单退回「已认领」,
    任务书换不了,总监只能 void 掉再建新号——断点、返工次数、模型账全丢。
    """

    def test_unblock_lands_on_rework_and_still_remembers_the_blocked_state(self):
        """第 3 条:unblock 之后状态是「返工」,「阻塞前状态」仍记得住原来那一档。"""
        for blocked_at, prepare in (
            ("已认领", lambda t: self.service.claim(t["编号"], self.worker)),
            ("新建", lambda t: t),
        ):
            with self.subTest(阻塞前状态=blocked_at):
                ticket = self.dispatch(f"阻塞落点-{blocked_at}")
                prepare(ticket)
                ticket = self.service.block(ticket["编号"], "缺一张登录图")
                self.assertEqual(blocked_at, ticket["阻塞前状态"])
                ticket = self.service.unblock(ticket["编号"])
                self.assertEqual("返工", ticket["状态"])
                self.assertEqual(blocked_at, ticket["阻塞前状态"])
                self.assertEqual("", ticket["阻塞原因"])
                # 落库的也是同一份,不是只在返回值上好看
                self.assertEqual("返工", self.service.store.load_ticket(ticket["编号"])["状态"])
                self.assertEqual(blocked_at, self.service.store.load_ticket(ticket["编号"])["阻塞前状态"])

    def test_unblock_clears_the_opened_stamp_so_the_queue_shows_it_again(self):
        """★T-001122:解阻塞必须把「已开窗」戳记一起清掉,否则设计者队列里根本看不到它。

        前端 isOpened() 判的是「已开窗.轮次 == 返工次数」,wantsDispatch() 又要求 !isOpened。
        unblock 从前两个数都不动,0 == 0 恒成立——这张单**不进**「要你传达的」,
        要熬过返工那 8 小时线才从折叠着的「卡住了」段冒出来。
        撞到过:T-001057 解阻塞后,重新派发的工单在队列里看不到。
        judge --rework 早就清了这个戳记,unblock 漏了——这里按同一条口径钉死。
        """
        ticket = self.dispatch("解阻塞要回到要你传达的")
        self.service.claim(ticket["编号"], self.worker)
        opened, _ = self.service.open_window(ticket["编号"], "设计者", "model-b")
        self.assertEqual(0, int(opened["已开窗"]["轮次"]))
        self.service.block(ticket["编号"], "等别位先答")
        unblocked = self.service.unblock(ticket["编号"])
        self.assertEqual("返工", unblocked["状态"])
        # 戳记清了 → 前端那条 轮次==返工次数 再也成立不了 → 单子回到「要你传达的」
        self.assertIsNone(unblocked["已开窗"])
        self.assertIsNone(self.service.store.load_ticket(ticket["编号"])["已开窗"])
        # 返工次数一分不动:它是模型账,不能借着解阻塞悄悄加一次(判退才算判退)
        self.assertEqual(0, self.service.store.load_ticket(ticket["编号"])["返工次数"])
        # 回执要说出来,别让人以为只是换了个状态
        rows = [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("事件") == "unblock" and row.get("工单号") == ticket["编号"]
        ]
        self.assertIn("已开窗标记已清", rows[-1]["说明"])

    def test_unblocking_a_ticket_blocked_while_in_rework_stays_rework(self):
        """已经在返工态的单被阻塞后再 unblock,行为不变(仍然是返工),不报错。"""
        ticket = self.to_judging()
        ticket, _ = self.service.judge(ticket["编号"], False, "UI总监", "入口仍会闪一下", REWORK_VERDICT, "模型")
        ticket = self.service.block(ticket["编号"], "等设计者拍板配色")
        self.assertEqual("返工", ticket["阻塞前状态"])
        ticket = self.service.unblock(ticket["编号"])
        self.assertEqual("返工", ticket["状态"])
        self.assertEqual("返工", ticket["阻塞前状态"])

    def test_rework_state_swaps_the_taskbook_without_changing_the_id(self):
        """第 4 条:返工态 set --taskbook 过、单号不变;待判态仍拒。

        白名单里本来就有返工(EDITABLE_STATES),这条钉死它,防止以后有人收紧——
        收紧了「换书不换号」就没了,总监又只能 void 掉重建。
        """
        ticket = self.dispatch("换书不换号")
        self.service.claim(ticket["编号"], self.worker)
        blocked = self.service.block(ticket["编号"], "任务书判据本身写错了")
        reworking = self.service.unblock(blocked["编号"])
        self.assertEqual("返工", reworking["状态"])
        new_path = str(Path(TASKBOOK_DIRECTORY) / f"{ticket['编号']}_改过判据的任务书.md")
        updated, _ = self.service.edit(reworking["编号"], SLOT, taskbook=new_path)
        self.assertEqual(ticket["编号"], updated["编号"])          # 单号不变
        self.assertEqual("返工", updated["状态"])
        self.assertEqual(new_path, updated["任务书路径"])
        # --body 同样在返工态可改
        updated, _ = self.service.edit(reworking["编号"], SLOT, body="改过判据的正文")
        self.assertEqual("改过判据的正文", updated["正文"])
        # 员工 claim 一次即回「已认领」,单号仍旧不变
        claimed = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual("已认领", claimed["状态"])
        self.assertEqual(ticket["编号"], claimed["编号"])
        self.assertEqual(new_path, claimed["任务书路径"])
        # 待判态仍然只准改指派给
        judging = self.to_judging()
        with self.assertRaisesRegex(TicketError, "待判态只允许改指派给"):
            self.service.edit(judging["编号"], SLOT, taskbook=new_path)


class FixtureAndInterfaceTests(unittest.TestCase):
    def test_the_minimal_fallback_page_is_self_contained_and_read_only(self):
        """兜底台面(web/minimal)必须**自带全部静态件、不连外网**,而且只读。

        它的定位是「主台面不在时顶上」:看单、筛单、搜索,仅此而已。
        ★只读是有意的:写操作全都有署名闸与状态闸,做成网页按钮只会让人绕过它们;
          要写就去主台面或命令行。
        ★不连外网:工单台常装在内网/离线机器上,一个 CDN 链接就能让页面白屏,
          而白屏不会报错,人只会以为服务挂了。
        """
        page = repository_file_or_skip(self, "web", "minimal", "index.html").read_text(encoding="utf-8")
        script = repository_file_or_skip(self, "web", "minimal", "tickets.js").read_text(encoding="utf-8")
        self.assertNotRegex(page, r"https?://")
        self.assertIn('src="tickets.js"', page)
        self.assertIn('href="tickets.css"', page)
        for forbidden in ("/api/action", "/api/say", "/api/upload", "method: 'POST'", 'method: "POST"'):
            self.assertNotIn(forbidden, script)
        self.assertNotIn("searchBox').addEventListener('input'", script)
        self.assertIn("searchButton", script)

    def test_cli_json_output_can_appear_after_subcommand(self):
        with tempfile.TemporaryDirectory() as root:
            result = run_local_cli(["staff", "list", "--json"], Path(root) / "tickets")
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual([], payload["result"])

    def test_new_transfer_ask_and_judge_missing_parameters_use_plain_errors(self):
        with tempfile.TemporaryDirectory() as root:
            commands = (["new"], ["transfer", "T-000001"], ["ask"], ["judge", "T-000001", "--pass", "--by", "总编排"])
            for command in commands:
                with self.subTest(command=command):
                    result = run_local_cli(command, Path(root) / "tickets")
                    self.assertEqual(2, result.returncode)
                    self.assertTrue(result.stderr.startswith("拦下:"), result.stderr)
                    self.assertNotIn("usage:", result.stderr)
                    self.assertNotIn("required", result.stderr)


class TaskTierAndModelScoreTests(TicketTestCase):
    def test_dispatch_requires_explicit_tier(self):
        with self.assertRaisesRegex(TicketError, "任务档必填"):
            self.service.create_dispatch(SLOT, "缺任务档", ["DECISIONS.md:1"], "主场景", self.worker)

    def test_c_tier_requires_budget_at_most_2000(self):
        before = self.service.store.read_json(self.service.store.counter_path)["最后编号"]
        with self.assertRaisesRegex(TicketError, "必须填写上下文预算"):
            self.service.create_dispatch(SLOT, "缺预算", ["DECISIONS.md:1"], "主场景", self.worker, task_tier="丙", deliverables=[str(self.deliverable)])
        with self.assertRaisesRegex(TicketError, "超过 2000"):
            self.service.create_dispatch(SLOT, "预算过大", ["DECISIONS.md:1"], "主场景", self.worker, task_tier="丙", context_lines=2001, deliverables=[str(self.deliverable)])
        self.assertEqual(before, self.service.store.read_json(self.service.store.counter_path)["最后编号"])
        ticket = self.service.create_dispatch(SLOT, "预算合规", ["DECISIONS.md:1"], "主场景", self.worker, task_tier="丙", context_lines=2000, deliverables=[str(self.deliverable)], internal=False)
        self.assertEqual(("丙", 2000), (ticket["任务档"], ticket["上下文预算"]))

    def test_slots_have_editable_model_policy(self):
        slots = self.service.store.read_json(self.service.store.slots_path)
        self.assertEqual(["model-a", "model-b", "model-e"], slots["主力模型集合"])
        self.assertEqual({"同位": 3, "全项目": 5}, slots["停用阈值"])
        self.assertTrue(all(row["主力模型"] for row in slots["总监位"]))

    def test_r4_actual_model_is_not_required_when_dispatch_is_created(self):
        ticket = self.dispatch("建单不替设计者选模型")
        self.assertEqual("乙", ticket["任务档"])
        self.assertEqual("", ticket["实际模型"])

    def test_r4_roster_has_all_model_a_levels_and_retired_model_cannot_be_new(self):
        roster = self.service.store.read_json(self.service.store.slots_path)["模型名册"]
        main = next(row for row in roster if row["模型"] == "model-a")
        spark = next(row for row in roster if row["模型"] == "model-legacy")
        self.assertEqual(["high", "middle", "low"], main["可选档位"])
        self.assertEqual("退役", spark["状态"])
        self.assertEqual("", self.service.staff_new(OTHER_SLOT, "model-a low")["提示"])

        staff = self.service.store.load_staff()
        historical = staff["总监位"][SLOT]["员工"][0]
        historical["工具/窗类型"] = "model-legacy"
        self.service.store.save_staff(staff)
        with self.assertRaisesRegex(TicketError, "已退役"):
            self.service.staff_new(SLOT, "model-legacy")
        self.assertEqual("model-legacy", self.service.find_staff(self.worker)[1]["工具/窗类型"])

    def test_r4_model_a_low_warns_for_an_a_tier_ticket_but_does_not_block(self):
        ticket = self.service.create_dispatch(
            SLOT, "甲档用低档模型提醒", ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
            task_tier="甲", deliverables=[str(self.deliverable)], internal=False,
        )
        updated, warning = self.service.open_window(ticket["编号"], "设计者", "model-a low")
        self.assertEqual("model-a low", updated["实际模型"])
        self.assertIn("低于本单甲档", warning)

    def test_non_main_model_warns_but_is_registered(self):
        member = self.service.staff_new(OTHER_SLOT, "codex")
        self.assertIn("不在主力模型名册里", member["提示"])
        self.assertEqual("在岗", self.service.find_staff(member["员工名"])[1]["状态"])

    def test_pending_tool_registers_without_warning_or_model_rate_and_ban_score(self):
        member = self.service.staff_new(OTHER_SLOT, "待定")
        self.assertEqual("", member["提示"])
        self.assertEqual("待定", self.service.find_staff(member["员工名"])[1]["工具/窗类型"])
        ticket = self.service.create_dispatch(
            OTHER_SLOT, "待定模型不计分", ["DECISIONS.md:测试"], "主场景/UiRoot",
            member["员工名"], task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
        )
        self.service.claim(ticket["编号"], member["员工名"])
        self.service.attach(ticket["编号"], str(self.picture("pending-world.png")), "world", member["员工名"])
        self.service.submit(ticket["编号"], "登录后仍有问题")
        self.service.judge(ticket["编号"], False, "UI总监", "判退", REWORK_VERDICT, "模型")
        self.assertFalse(any(row["模型"] == "待定" for row in self.service.model_statistics()))
        self.assertNotIn("待定", self.service.store.load_staff()["模型记分"])

    def test_three_same_slot_reworks_warn_but_no_longer_ban_the_model(self):
        """D9-388 / T-000529 乙:到停用线只通知,不自动停用;停不停由总编排落 D9。

        改之前这里断言第 3 次判退后 staff_new 报「已停用」——那正是两次误停 model-a 的机制。
        用非主力模型 model-c:R1.5(T-000794)之后主力模型到线只作质量提示,不再有「已到停用线」措辞。
        """
        last_warning = ""
        for index in range(3):
            ticket = self.dispatch(f"判退{index + 1}")
            self.service.open_window(ticket["编号"], "设计者", "model-c")
            self.service.claim(ticket["编号"], self.worker)
            self.service.attach(ticket["编号"], str(self.picture(f"world-{index}.png")), "world", self.worker)
            self.service.submit(ticket["编号"], "登录后仍有问题")
            _, last_warning = self.service.judge(
                ticket["编号"], False, "UI总监", f"第{index + 1}次判退", REWORK_VERDICT, "模型",
            )
        self.assertIn("累计判退 3 次", last_warning)
        self.assertIn("已到停用线", last_warning)
        self.assertIn("自动停用已关", last_warning)
        bans = self.service.store.load_staff().get("模型停用", {})
        self.assertEqual([], bans.get("全项目", []))
        self.assertEqual([], bans.get("按位", {}).get(SLOT, []))
        # 不再被拦:新窗照开
        self.assertEqual("前端·界面与交互-02", self.service.staff_new(SLOT, "model-c")["员工名"])

    def test_first_review_notice_and_model_rate(self):
        ticket = self.to_judging()
        _, notice = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertIn("首检从严", notice)
        stats = self.service.model_statistics()
        main = next(row for row in stats if row["模型"] == "model-a-未标")
        self.assertEqual((1, 1, 0, "100.0%"), (main["交板数"], main["判过"], main["判退"], main["合格率"]))

    def test_open_window_writes_actual_model_to_staff_and_ticket_then_stats_use_it(self):
        ticket = self.service.create_dispatch(
            SLOT, "已开窗写回真实模型", ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
            task_tier="甲", deliverables=[str(self.deliverable)], internal=False,
        )
        updated, warning = self.service.open_window(ticket["编号"], "设计者", "model-c")
        self.assertEqual("model-c", updated["实际模型"])
        self.assertEqual("model-c", self.service.find_staff(self.worker)[1]["工具/窗类型"])
        self.assertIn("低于本单甲档", warning)
        _, repeated_warning = self.service.open_window(ticket["编号"], "设计者", "model-c")
        self.assertEqual("", repeated_warning)

        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("actual-model.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "实际模型统计")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        stats = self.service.model_statistics()
        actual = next(row for row in stats if row["模型"] == "model-c")
        self.assertEqual((1, 1, "100.0%"), (actual["交板数"], actual["判过"], actual["合格率"]))
        self.assertFalse(any(row["模型"] == "model-a" for row in stats))

    def test_actual_model_is_snapshotted_per_rework_round(self):
        ticket = self.dispatch("返工换模型不改写旧成绩")
        self.service.open_window(ticket["编号"], "设计者", "model-c")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("round-one.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "第一轮")
        self.service.judge(ticket["编号"], False, "UI总监", "第一轮判退", REWORK_VERDICT, "模型")

        self.service.open_window(ticket["编号"], "设计者", "model-a high")
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "第二轮")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        stats = {row["模型"]: row for row in self.service.model_statistics()}
        self.assertEqual((1, 0, 1), (stats["model-c"]["交板数"], stats["model-c"]["判过"], stats["model-c"]["判退"]))
        self.assertEqual((1, 1, 0), (stats["model-a-high"]["交板数"], stats["model-a-high"]["判过"], stats["model-a-high"]["判退"]))


class DeskOpenedServerFieldTests(TicketTestCase):
    def opened_to_judging(self):
        ticket = self.dispatch("服务端已开窗字段")
        opened, _ = self.service.open_window(ticket["编号"], "设计者", "model-a high")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("opened-world.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "服务端已开窗字段验收")
        return opened

    def test_open_window_saves_current_round_time_and_actual_model(self):
        ticket = self.dispatch("开窗写服务端")
        updated, _ = self.service.open_window(ticket["编号"], "设计者", "model-a high")
        marker = updated["已开窗"]
        self.assertEqual(ticket["返工次数"], marker["轮次"])
        self.assertEqual("model-a high", marker["实际模型"])
        self.assertEqual(marker, self.service.store.load_ticket(ticket["编号"])["已开窗"])
        datetime.fromisoformat(marker["时间"])

    def test_rework_clears_opened_marker(self):
        opened = self.opened_to_judging()
        self.assertIsNotNone(opened["已开窗"])
        reworked, _ = self.service.judge(opened["编号"], False, "UI总监", "字段未清", REWORK_VERDICT)
        self.assertIsNone(reworked["已开窗"])
        self.assertIsNone(self.service.store.load_ticket(opened["编号"])["已开窗"])

    def test_legacy_ticket_without_opened_key_loads_as_null_without_migration(self):
        for name, store in (
            ("files", TicketStore(self.root / "legacy-files")),
            ("sqlite", SqliteStore(self.root / "legacy-sqlite" / "tickets.sqlite")),
        ):
            with self.subTest(store=name):
                store.ensure()
                legacy = model.new_ticket_record(
                    "T-000001", "派单", SLOT, "旧单", "总编排", assign=self.worker,
                    sources=["DECISIONS.md:旧单"], consumer="主场景/UiRoot", task_tier="乙",
                )
                legacy.pop("已开窗")
                store.save_ticket(legacy, "new", "总编排", "旧格式写入")
                loaded = store.load_ticket(legacy["编号"])
                listed = store.list_tickets()[0]
                self.assertIn("已开窗", loaded)
                self.assertIsNone(loaded["已开窗"])
                self.assertIsNone(listed["已开窗"])

    def test_open_and_rework_event_details_explain_marker_changes(self):
        ticket = self.opened_to_judging()
        self.service.judge(ticket["编号"], False, "UI总监", "重做", REWORK_VERDICT)
        rows = [row for row in self.service.store.read_jsonl(self.service.store.log_path) if row["工单号"] == ticket["编号"]]
        opened = next(row for row in rows if row["事件"] == "window-opened")
        reworked = next(row for row in rows if row["事件"] == "judge-rework")
        self.assertIn("已开窗·第 0 轮", opened["说明"])
        self.assertIn("已开窗标记已清,等设计者重新开窗", reworked["说明"])

def _self_signed_cert(directory: Path) -> tuple[str, str]:
    """给 TLS 用例造一张自签证书。造不出来(缺 cryptography)就让调用方跳过。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return str(cert_path), str(key_path)


class TlsAcceptLoopTests(TicketTestCase):
    """T-000471:TLS 握手不许在 accept 主循环里做,否则一个不握手的客户端能钉死整台服务。

    线上真事故:服务 systemd 显示 active、进程还在,但端口全超时。
    ss 显示 Recv-Q 6 / Send-Q 5(accept 队列满),Tasks 只剩 1(一个工作线程都没起)。
    根因是 serve() 把监听套接字整个 wrap_socket 成了 SSLSocket,于是握手在 accept 那一步、
    主循环线程里做,而且没有超时;ThreadingHTTPServer 的多线程要等 accept 返回才轮得到。
    """

    def setUp(self) -> None:
        super().setUp()
        try:
            self.cert, self.key = _self_signed_cert(self.root)
        except Exception as exc:  # pragma: no cover - 只在缺 cryptography 的机器上走到
            self.skipTest(f"造不出自签证书,跳过 TLS 用例:{exc}")
        handler = partial(TicketRequestHandler, directory=str(WEB_ROOT))
        self.server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        # 和 serve() 里的写法保持一致:挂 ssl_context，不包监听套接字。
        self.server.ssl_context = context
        self.server.connection_timeout = 5
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.dead: list[socket.socket] = []

    def tearDown(self) -> None:
        for sock in self.dead:
            with contextlib.suppress(OSError):
                sock.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def https_get(self, path: str, timeout: float = 8.0) -> tuple[int, bytes]:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=timeout, context=context)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_tls_serves_normally(self):
        status, _ = self.https_get("/api/tickets")
        self.assertEqual(200, status)

    def test_clients_that_never_finish_the_handshake_do_not_wedge_the_accept_loop(self):
        """连上来就不吭声的客户端,开满旧 backlog 的两倍,正常请求仍要能过。

        改之前这一条必挂:那些连接会卡在 accept 里的握手上,accept 队列排满之后
        新连接的 SYN 被内核丢掉,https_get 只会超时。
        """
        for _ in range(12):  # 旧 backlog 是 5,这里给它两倍多
            sock = socket.socket()
            sock.settimeout(5)
            sock.connect(("127.0.0.1", self.port))
            self.dead.append(sock)  # 连上就不发任何字节，握手永远开不了头

        status, _ = self.https_get("/api/tickets")
        self.assertEqual(200, status)

    def test_plain_http_probe_on_the_tls_port_does_not_kill_the_service(self):
        """拿明文 HTTP 去捅 HTTPS 端口(扫描器天天干),服务要照常活着。"""
        probe = socket.socket()
        probe.settimeout(5)
        probe.connect(("127.0.0.1", self.port))
        probe.sendall(b"GET / HTTP/1.0\r\n\r\n")
        with contextlib.suppress(OSError):
            probe.recv(64)
        probe.close()

        status, _ = self.https_get("/api/tickets")
        self.assertEqual(200, status)

    def test_backlog_is_big_enough_for_every_director_window(self):
        """默认 5 太小:11 个总监窗 + 网页轮询,排满之后客户端看到的是超时,不是拒绝连接。"""
        self.assertGreaterEqual(TicketHTTPServer.request_queue_size, 128)

class HttpServiceTests(TicketTestCase):
    def setUp(self) -> None:
        super().setUp()
        handler = partial(TicketRequestHandler, directory=str(WEB_ROOT))
        self.server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        sent_headers = dict(headers or {})
        if body is not None:
            sent_headers["Content-Type"] = "application/json; charset=utf-8"
        connection.request(method, path, body=body, headers=sent_headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def test_get_tickets_over_temporary_port(self):
        ticket = self.dispatch("HTTP 列表")
        status, payload = self.request("GET", "/api/tickets")
        self.assertEqual(200, status)
        self.assertEqual(channel_config.PROTOCOL_VERSION, payload["server_protocol"])
        self.assertEqual(ticket["编号"], payload["result"][0]["编号"])
        self.assertEqual(ticket["状态进入时间"], payload["result"][0]["状态进入时间"])
        self.assertEqual([], payload["result"][0]["开窗指令"])

    def test_http_open_window_writes_actual_model_before_browser_marks_it(self):
        ticket = self.dispatch("HTTP 已开窗")
        status, payload = self.request("POST", "/api/action", {
            "op": "open-window", "ticket": ticket["编号"], "by": "设计者", "actual_model": "sol high",
        })
        self.assertEqual(200, status)
        self.assertEqual("sol high", payload["result"]["工单"]["实际模型"])
        self.assertEqual("sol high", self.service.find_staff(self.worker)[1]["工具/窗类型"])

    def test_open_window_rework_then_ticket_api_returns_cleared_marker_end_to_end(self):
        ticket = self.dispatch("端到端清已开窗")
        self.service.claim(ticket["编号"], self.worker)
        status, opened = self.request("POST", "/api/action", {
            "op": "open-window", "ticket": ticket["编号"], "by": "设计者", "actual_model": "sol high",
        })
        self.assertEqual(200, status)
        self.assertIsNotNone(opened["result"]["工单"]["已开窗"])
        self.service.attach(ticket["编号"], str(self.picture("end-to-end-opened.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "端到端判退前交板")
        status, _ = self.request("POST", "/api/action", {
            "op": "judge", "ticket": ticket["编号"], "by": "UI总监", "passed": False,
            "reason": "端到端判退", "verdict": REWORK_VERDICT,
        })
        self.assertEqual(200, status)
        status, listing = self.request("GET", "/api/tickets")
        self.assertEqual(200, status)
        shown = next(row for row in listing["result"] if row["编号"] == ticket["编号"])
        self.assertIsNone(shown["已开窗"])

    def test_http_set_taskbook_writes_the_server_ticket(self):
        ticket = self.dispatch("浏览器补任务书")
        path = rf"C:\ticket-desk-workspace\_office\{SLOT}\任务书\{ticket['编号']}_补路径.md"
        status, payload = self.request("POST", "/api/action", {
            "op": "set", "ticket": ticket["编号"], "taskbook": path, "by": SLOT,
        })
        self.assertEqual(200, status)
        self.assertEqual(path, payload["result"]["工单"]["任务书路径"])
        self.assertEqual(path, self.service.store.load_ticket(ticket["编号"])["任务书路径"])
        _, listing = self.request("GET", "/api/tickets")
        shown = next(row for row in listing["result"] if row["编号"] == ticket["编号"])
        self.assertEqual(3, len(shown["开窗指令"]))
        self.assertIn(f" claim {ticket['编号']} --by {self.worker}", shown["开窗指令"][0])

    def test_http_internal_dispatch_submits_with_command_output_and_no_picture(self):
        status, payload = self.request("POST", "/api/action", {
            "op": "new", "slot": SLOT, "title": "HTTP 内部单", "source": ["service.py"],
            "consumer": "工单台", "deliverables": [str(self.deliverable)], "assign": self.worker,
            "tier": "乙", "internal": True, "by": SLOT,
        })
        self.assertEqual(200, status)
        ticket_id = payload["result"]["编号"]
        self.assertTrue(payload["result"]["非用户可感知"])
        self.assertEqual(200, self.request("POST", "/api/action", {"op": "claim", "ticket": ticket_id, "by": self.worker})[0])
        status, payload = self.request("POST", "/api/action", {
            "op": "submit", "ticket": ticket_id, "verify_command": "python -m pytest",
            "raw_output": "48 passed", "evidence": "接口验证完成",
        })
        self.assertEqual(200, status)
        self.assertEqual("待判", payload["result"]["状态"])

    def test_post_action_hard_gate_returns_plain_reason(self):
        ticket = self.service.create_dispatch(SLOT, "缺真源", [], "主场景/UiRoot", self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=False)
        status, payload = self.request("POST", "/api/action", {"op": "claim", "ticket": ticket["编号"], "by": self.worker})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("真源指针还没填", payload["reason"])

    def test_unknown_option_from_newer_client_reports_both_real_protocols(self):
        status, payload = self.request("POST", "/api/cli", {
            "argv": ["list", "--server-does-not-know"], "client_protocol": 99,
        })
        self.assertEqual(400, status)
        self.assertEqual(channel_config.PROTOCOL_VERSION, payload["server_protocol"])
        self.assertEqual(
            "你的客户端比服务器新,服务器还没上这一版:"
            f"客户端 99 / 服务端 {channel_config.PROTOCOL_VERSION};"
            "请等平台位上服,或改用并线前的客户端。",
            payload["reason"],
        )

    def test_unknown_option_without_client_protocol_keeps_old_argparse_message(self):
        status, payload = self.request("POST", "/api/cli", {
            "argv": ["list", "--server-does-not-know"],
        })
        self.assertEqual(400, status)
        # T-001560 ②:不认识的 --选项,报错头部照旧,尾部多一行版本差提示
        # (新参数常先上服后并 main,别让人当成自己写错)。
        reason = payload["reason"]
        self.assertTrue(reason.startswith("ticket.py 参数不对，请检查命令写法。"), reason)
        self.assertIn("git pull 主检出", reason)

    def test_token_requires_header(self):
        self.server.token = "one-run-secret"
        status, payload = self.request("GET", "/api/tickets")
        self.assertEqual(401, status)
        self.assertEqual("未获授权：请提供正确的 X-Ticket-Token。", payload["reason"])
        status, payload = self.request("GET", "/api/tickets", headers={"X-Ticket-Token": "one-run-secret"})
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

    def test_upload_4000_by_3000_is_bounded(self):
        ticket = self.dispatch("大图上传")
        stream = io.BytesIO()
        Image.new("RGB", (4000, 3000), (50, 100, 150)).save(stream, "PNG")
        status, payload = self.request(
            "POST",
            "/api/upload",
            {"ticket": ticket["编号"], "filename": "4000x3000.png", "origin": "other", "by": self.worker, "base64": base64.b64encode(stream.getvalue()).decode("ascii")},
        )
        self.assertEqual(200, status)
        target = self.service.store.images_dir / payload["result"]["图片"]["文件名"]
        self.assertLessEqual(target.stat().st_size, MAX_IMAGE_BYTES)
        with Image.open(target) as image:
            self.assertLessEqual(max(image.size), MAX_IMAGE_EDGE)

    def test_cli_and_http_write_same_ticket_without_overwrite(self):
        ticket = self.dispatch("并发同单")
        source = self.picture("concurrent.png", (900, 600))
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def cli_attach() -> None:
            barrier.wait()
            results["cli"] = run_local_cli(
                ["attach", ticket["编号"], str(source), "--origin", "other", "--by", self.worker], self.service.store.root,
            )

        def http_block() -> None:
            barrier.wait()
            results["http"] = self.request("POST", "/api/action", {"op": "block", "ticket": ticket["编号"], "reason": "并发验证", "by": "总编排"})

        threads = [threading.Thread(target=cli_attach), threading.Thread(target=http_block)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        cli = results["cli"]
        status, _ = results["http"]
        final = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual(0, cli.returncode, cli.stderr)
        self.assertEqual(200, status)
        self.assertEqual("阻塞", final["状态"])
        self.assertEqual(1, len(final["图片列表"]))
        self.assertEqual(3, final["事件序号"])
        print("CONCURRENT ORIGINAL " + json.dumps({"http_status": status, "cli_exit": cli.returncode, "cli_stdout": cli.stdout.strip(), "final_state": final["状态"], "image_count": len(final["图片列表"]), "event_sequence": final["事件序号"]}, ensure_ascii=False))


class AuthHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SqliteStore(self.root / "db" / "tickets.sqlite")
        self.service = TicketService(self.store)
        self.auth = AccountManager(self.store.database)
        self.username = "owner"
        self.auth.init_admin(self.username)
        handler = partial(TicketRequestHandler, directory=str(WEB_ROOT))
        self.server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "", self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        sent = dict(headers or {})
        if body is not None:
            sent["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), raw)
        connection.close()
        return result

    def test_setup_login_cookie_and_personal_token(self):
        status, _, _ = self.request("GET", "/setup")
        self.assertEqual(200, status)
        password = secrets.token_urlsafe(24)
        status, headers, _ = self.request("POST", "/auth/setup", {"username": self.username, "password": password})
        self.assertEqual(200, status)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        session_cookie = cookie.split(";", 1)[0]
        self.assertEqual(404, self.request("GET", "/setup")[0])
        self.assertEqual(200, self.request("GET", "/api/tickets", headers={"Cookie": session_cookie})[0])
        status, _, raw = self.request("POST", "/api/token", {"op": "generate"}, {"Cookie": session_cookie})
        token = json.loads(raw.decode("utf-8"))["result"]["token"]
        self.assertEqual(200, self.request("GET", "/api/tickets", headers={"X-Ticket-Token": token})[0])
        status, token_headers, _ = self.request("POST", "/auth/token-login", {"token": token})
        self.assertEqual(200, status)
        self.assertIn("HttpOnly", token_headers["Set-Cookie"])
        self.request("POST", "/api/token", {"op": "revoke"}, {"Cookie": session_cookie})
        self.assertEqual(401, self.request("GET", "/api/tickets", headers={"X-Ticket-Token": token})[0])

    def test_no_token_wrong_token_and_twenty_failures_ban(self):
        self.assertEqual(401, self.request("GET", "/api/tickets")[0])
        for _ in range(19):
            self.assertEqual(401, self.request("GET", "/api/tickets", headers={"X-Ticket-Token": secrets.token_urlsafe(8)})[0])
        self.assertEqual(429, self.request("GET", "/api/tickets")[0])

    def test_ten_login_failures_ban_for_ten_minutes(self):
        for _ in range(10):
            status, _, _ = self.request(
                "POST", "/auth/login", {"username": self.username, "password": secrets.token_urlsafe(12)}
            )
            self.assertEqual(401, status)
        self.assertEqual(
            429,
            self.request("POST", "/auth/login", {"username": self.username, "password": secrets.token_urlsafe(12)})[0],
        )


class RemoteClientTests(TicketTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dispatch("远程模式")
        self.sqlite = SqliteStore(self.root / "remote" / "db" / "tickets.sqlite")
        self.sqlite.import_files(self.service.store.root)
        self.remote_service = TicketService(self.sqlite)
        self.service_token = secrets.token_urlsafe(32)
        handler = partial(TicketRequestHandler, directory=str(WEB_ROOT))
        self.server = TicketHTTPServer(
            ("127.0.0.1", 0), handler, self.remote_service, self.service_token, AccountManager(self.sqlite.database)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token_file = self.root / "remote.token"
        self.token_file.write_text(self.service_token, encoding="utf-8")
        self.client = RemoteClient(
            f"http://127.0.0.1:{self.server.server_address[1]}", str(self.token_file)
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    @contextlib.contextmanager
    def protocol_zero_server(self):
        class ProtocolZeroHandler(TicketRequestHandler):
            def _cli(handler, data):
                from ticket_desk.ticket import execute, parser

                argv = data.get("argv")
                handler.server.cli_requests.append(list(argv) if isinstance(argv, list) else argv)
                if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
                    raise TicketError("远程命令参数必须是字符串列表。")
                # 模拟协议 0：不读取 client_protocol，旧 parser 也不认识这个新开关。
                if "--taskbook-client-checked" in argv:
                    raise TicketError("ticket.py 参数不对，请检查命令写法。")
                args = parser().parse_args(argv)
                payload, text = execute(args, handler.server.service)
                return {"payload": payload, "text": text}

        handler = partial(ProtocolZeroHandler, directory=str(WEB_ROOT))
        server = TicketHTTPServer(("127.0.0.1", 0), handler, self.remote_service, self.service_token)
        server.cli_requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = RemoteClient(f"http://127.0.0.1:{server.server_address[1]}", str(self.token_file))
        try:
            with mock.patch.object(http_server_module, "PROTOCOL_VERSION", 0):
                yield client, server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_remote_read_output_matches_service_output(self):
        from ticket_desk.ticket import execute, parser

        _, local_text = execute(parser().parse_args(["list"]), self.remote_service)
        _, remote_text = self.client.execute(["list"])
        self.assertEqual(local_text, remote_text)

    def test_env_probe_and_receipt_report_the_same_live_server_protocol(self):
        ticket = self.remote_service.list_tickets()[0]
        environment = clean_environment(
            self.service.store.root,
            TICKET_REMOTE=f"http://127.0.0.1:{self.server.server_address[1]}",
            TICKET_TOKEN_FILE=str(self.token_file),
        )
        probed = subprocess.run(
            [*CLI, "env", "--probe"], cwd=ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8",
        )
        receipt = subprocess.run(
            [*CLI, "receipt", ticket["编号"]], cwd=ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(0, probed.returncode, probed.stderr)
        self.assertEqual(0, receipt.returncode, receipt.stderr)
        expected = f"服务端协议 {channel_config.PROTOCOL_VERSION}"
        self.assertIn(expected, probed.stdout)
        self.assertIn(expected, receipt.stdout)

    def test_protocol_handshake_uses_real_http_request_and_response_envelope(self):
        seen: dict[str, object] = {}
        original = TicketRequestHandler._cli

        def recording_cli(handler, data):
            seen.update(data)
            return original(handler, data)

        with mock.patch.object(TicketRequestHandler, "_cli", recording_cli):
            self.client.execute(["list"])
        self.assertEqual(channel_config.PROTOCOL_VERSION, seen["client_protocol"])
        self.assertEqual(channel_config.PROTOCOL_VERSION, self.client.server_protocol)
        self.client.server_protocol = 0
        with self.assertRaises(TicketError):
            self.client.execute(["list", "--server-does-not-know"])
        self.assertEqual(channel_config.PROTOCOL_VERSION, self.client.server_protocol)

    def test_response_without_server_protocol_is_cached_as_protocol_zero(self):
        class Response:
            status = 200

            @staticmethod
            def read():
                return b'{"ok":true,"result":{"payload":[],"text":""}}'

        class Connection:
            def request(self, method, path, body=None, headers=None):
                pass

            @staticmethod
            def getresponse():
                return Response()

            @staticmethod
            def close():
                pass

        with mock.patch.object(self.client, "_connection", return_value=Connection()):
            self.client.execute(["list"])
        self.assertEqual(0, self.client.server_protocol)

    def test_protocol_zero_server_downgrades_taskbook_check_once_with_full_warning(self):
        ticket = self.remote_service.list_tickets()[0]
        taskbook = self.root / "协议零任务书.md"
        taskbook.write_text("# 已在客户端核过\n", encoding="utf-8")
        argv = [
            "set", ticket["编号"], "--taskbook", str(taskbook), "--by", SLOT,
            "--taskbook-client-checked",
        ]
        stderr = io.StringIO()
        with self.protocol_zero_server() as (client, server), contextlib.redirect_stderr(stderr):
            payload, _ = client.execute(argv)
            stored = self.remote_service.store.load_ticket(ticket["编号"])
            self.assertIn("--taskbook-client-checked", server.cli_requests[0])
            self.assertNotIn("--taskbook-client-checked", server.cli_requests[1])
            self.assertEqual(2, len(server.cli_requests))
            self.assertEqual("待回核", stored["任务书校验"])
            self.assertEqual("待回核", payload["工单"]["任务书校验"])
        warning = stderr.getvalue().strip()
        self.assertIn("已自动省掉 --taskbook-client-checked 重发", warning)
        # ★T-001256 ⑤(D9-453 / v2.31 八章 6「非业务闸不停车」)改了这段话的口径:
        # 版本差是账面事,不是活没做好。旧文案把「它不会进设计者队列」写成后果,读起来像出了大事,
        # 员工据此停车等平台上服;而 ③ 之后「待回核」已经不再挡队列,那条后果本身也不成立了。
        # 现在必须明说三件事:这次操作已经生效、待回核不挡任何动作、不要为此停车。
        self.assertIn("不影响这次操作", warning)
        self.assertIn("命令已经生效", warning)
        self.assertIn("不挡开窗、不挡认领、不挡交板", warning)
        self.assertIn("不要为此停车等谁", warning)
        self.assertIn("set --taskbook <同一个路径>", warning)
        self.assertNotIn("它不会进设计者队列", warning)

    def test_new_client_keeps_new_set_list_and_receipt_working_against_protocol_zero(self):
        taskbook = self.root / "旧服兼容任务书.md"
        taskbook.write_text("# 已在客户端核过\n", encoding="utf-8")
        replacement = self.root / "旧服兼容任务书-改.md"
        replacement.write_text("# 客户端再次核过\n", encoding="utf-8")
        new_argv = [
            "new", "--slot", SLOT, "--title", "新客户端打旧服", "--source", "DECISIONS.md:兼容",
            "--consumer", "主场景/UiRoot", "--assign", self.worker, "--tier", "乙",
            "--deliverable", str(self.deliverable), "--taskbook", str(taskbook),
            "--taskbook-client-checked", "--user-facing",
        ]
        with self.protocol_zero_server() as (client, _), contextlib.redirect_stderr(io.StringIO()):
            created, _ = client.execute(new_argv)
            changed, _ = client.execute([
                "set", created["编号"], "--taskbook", str(replacement), "--by", SLOT,
                "--taskbook-client-checked",
            ])
            listed, _ = client.execute(["list"])
            receipt_payload, receipt_text = client.execute(["receipt", created["编号"]])
        self.assertEqual("待回核", created["任务书校验"])
        self.assertEqual("待回核", changed["工单"]["任务书校验"])
        self.assertTrue(any(row["编号"] == created["编号"] for row in listed))
        self.assertEqual(0, client.server_protocol)
        self.assertIn(f"客户端协议 {channel_config.PROTOCOL_VERSION} · 服务端协议 0", receipt_text)
        self.assertIn("客户端比服务端新", receipt_payload["receipt"])

    def test_remote_attach_compresses_locally_and_returns_same_copy(self):
        ticket = self.remote_service.list_tickets()[0]
        source = self.picture("remote-large.png", (1600, 900))
        _, text = self.client.execute([
            "attach", ticket["编号"], str(source), "--origin", "other", "--by", self.worker,
        ])
        self.assertRegex(text, r"^已附图 T-\d{6}-\d{2}\.jpg · 其他$")
        record = self.remote_service.store.load_ticket(ticket["编号"])["图片列表"][0]
        self.assertLessEqual((self.sqlite.images_dir / record["文件名"]).stat().st_size, MAX_IMAGE_BYTES)

    def test_remote_live_batch_with_picture_after_batch_options_still_uploads_bytes(self):
        """T-000620:复检席实际敲的是 live <单号> --batch … <图> --shot 同图 --by …(图在选项后面)。

        旧判据只看 argv[2] 是不是图,于是整条 argv 被转发到服务端,服务端拿客户端本机路径去开图,
        16 张用户可感知单全部回显「找不到图片:<本机路径>」。凡带图片位置参数的 live 都必须在客户端读图上传。
        """
        picture = self.picture("after-options.png", (900, 600))
        first = self.remote_service.list_tickets()[0]
        argv = ["live", first["编号"], "--batch", "T-999999", str(picture), "--shot", "同图", "--by", "复检·合并与部署"]
        self.assertEqual([first["编号"], str(picture)], self.client._live_positionals(argv))
        with mock.patch.object(self.client, "request", wraps=self.client.request) as requested:
            try:
                self.client.execute(argv)
            except TicketError:
                pass  # 单子状态不对会被服务端拦,这里只看是不是先上传了字节
        paths = [call.args[1] for call in requested.call_args_list]
        self.assertEqual("/api/upload", paths[0], paths)
        self.assertNotIn("/api/cli", paths)

    def test_remote_live_batch_uploads_picture_exactly_once(self):
        service = self.remote_service

        def merged(title: str, internal: bool) -> dict[str, object]:
            ticket = service.create_dispatch(
                SLOT, title, ["DECISIONS.md:remote-batch"], "工单台" if internal else "主场景/UiRoot",
                self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=internal,
            )
            service.claim(ticket["编号"], self.worker)
            if internal:
                ticket = service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
            else:
                service.attach(ticket["编号"], str(self.picture(f"{ticket['编号']}-remote-before.png")), "world", self.worker)
                ticket = service.submit(ticket["编号"], "登录后界面已出现")
            ticket, _ = service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
            service.verify(ticket["编号"], "独立复检", "过", gates="夹具:六项闸摘要")
            return service.merge(ticket["编号"], "独立复检")

        tickets = [merged("远程批量内部首单", True)] + [
            merged(f"远程批量 {index}", index >= 4) for index in range(1, 5)
        ]
        argv = ["live", tickets[0]["编号"], str(self.picture("remote-batch.png"))]
        for ticket in tickets[1:]:
            argv.extend(["--batch", ticket["编号"]])
        argv.extend(["--shot", "同图", "--by", "独立复检"])
        with mock.patch.object(self.client, "request", wraps=self.client.request) as requested:
            rows, text = self.client.execute(argv)
        upload_count = sum(call.args[1] == "/api/upload" for call in requested.call_args_list)
        self.assertEqual(1, upload_count)
        self.assertEqual(["已复验"] * 5, [row["结果"] for row in rows])
        self.assertEqual(5, text.count("· 已复验 ·"))
        self.assertEqual([], service.store.load_ticket(tickets[0]["编号"])["图片列表"])

    def test_full_remote_flow_output_matches_file_mode(self):
        from ticket_desk.ticket import execute, parser

        image = self.picture("parity.png", (900, 600))
        new_args = [
            "new", "--slot", SLOT, "--title", "远程逐字节一致", "--source", "DECISIONS.md:remote",
            "--consumer", "主场景/UiRoot", "--assign", self.worker, "--tier", "乙",
            "--deliverable", str(self.deliverable), "--user-facing",
        ]

        def both(argv: list[str]) -> tuple[object, object]:
            local_payload, local_text = execute(parser().parse_args(argv), self.service)
            remote_payload, remote_text = self.client.execute(argv)
            self.assertEqual(local_text, remote_text)
            return local_payload, remote_payload

        local_ticket, remote_ticket = both(new_args)
        ticket_id = local_ticket["编号"]
        self.assertEqual(ticket_id, remote_ticket["编号"])
        both(["claim", ticket_id, "--by", self.worker])
        both(["attach", ticket_id, str(image), "--origin", "world", "--by", self.worker])
        both(["submit", ticket_id, "--evidence", "远程路径完成"])
        both(["judge", ticket_id, "--pass", "--by", "UI总监", "--verdict", PASS_VERDICT])
        both(["transfer", ticket_id, "--to", OTHER_SLOT, "--reason", "交给后端复检", "--by", "UI总监"])
        both(["digest"])

    def test_unavailable_remote_reads_snapshot_but_never_writes_local(self):
        environment = clean_environment(
            self.service.store.root, TICKET_REMOTE="http://127.0.0.1:1", TICKET_TOKEN_FILE=str(self.token_file),
        )
        command = CLI
        readable = subprocess.run(command + ["list"], cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(3, readable.returncode)
        self.assertIn("远程不可达", readable.stderr)
        self.assertNotIn("T-0000", readable.stdout)
        stale = subprocess.run(
            command + ["list"], cwd=ROOT, env=dict(environment, TICKET_ALLOW_STALE="1"),
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(3, stale.returncode)
        self.assertIn("本机只读快照", stale.stderr)
        self.assertIn("T-0000", stale.stdout)
        ticket = self.service.list_tickets()[0]
        before = ticket["状态"]
        blocked = subprocess.run(
            command + ["block", ticket["编号"], "断网不应落本机"],
            cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(2, blocked.returncode)
        self.assertIn("没有落回本机", blocked.stderr)
        self.assertEqual(before, self.service.store.load_ticket(ticket["编号"])["状态"])


class TestsNeverTouchProductionTests(unittest.TestCase):
    """T-000108 R1:测试进程、测试子进程,都不许有任何一条路通到真服务器。"""

    def test_r1_test_process_itself_has_no_ticket_remote(self):
        # 环境里留着 TICKET_REMOTE 跑测试,曾在生产库里建出六张假单(T-000080~86)。这里直接报红,不替人擦屁股。
        value = os.environ.get("TICKET_REMOTE", "")
        self.assertEqual("", value, f"{REMOTE_GUARD_MESSAGE}(当前 TICKET_REMOTE={value!r})")

    def test_r1_clean_environment_strips_every_channel_variable(self):
        polluted = {name: f"污染-{name}" for name in CHANNEL_VARIABLES}
        with mock.patch.dict(os.environ, polluted):
            environment = clean_environment(Path(tempfile.gettempdir()) / "用例库")
        for name in CHANNEL_VARIABLES:
            self.assertNotIn(name, environment, name)
        self.assertEqual(str(Path(tempfile.gettempdir()) / "用例库"), environment["TICKET_ROOT"])
        self.assertEqual("utf-8", environment["PYTHONIOENCODING"])
        self.assertIn("PATH", environment, "派生环境仍要带系统变量,子进程才起得来")

    def test_r1_clean_environment_only_lets_a_test_pass_its_own_remote_in(self):
        with mock.patch.dict(os.environ, {"TICKET_REMOTE": "https://生产"}):
            environment = clean_environment("D:/tmp", TICKET_REMOTE="http://127.0.0.1:1")
        self.assertEqual("http://127.0.0.1:1", environment["TICKET_REMOTE"])

    def test_r1_local_flag_beats_a_polluted_parent_environment(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"TICKET_REMOTE": "http://127.0.0.1:1", "TICKET_TOKEN_FILE": str(Path(root) / "无")}):
                result = run_local_cli(["staff", "list"], Path(root) / "tickets")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("远程", result.stderr + result.stdout)
        self.assertEqual("名册为空。", result.stdout.strip())

    def test_r1_every_cli_subprocess_in_this_file_derives_from_clean_environment(self):
        source = Path(__file__).read_text(encoding="utf-8")
        # mock.patch.dict(os.environ, ...) 是往测试进程里放污染,允许;直接拿 os.environ 给子进程,不允许。
        self.assertNotRegex(source, r"(?<!\.)dict\(os\.environ", "起子进程只准用 clean_environment / run_local_cli")
        self.assertNotRegex(source, r"env\s*=\s*os\.environ\b")


class ChannelConfigTests(unittest.TestCase):
    """T-000108 R2:取配置三级顺序 + --local;remote.env 缺什么要说人话。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # 模拟一台别的机器:仓库放在 E:/别的盘/proj/ticket-desk,worktree 在 proj/_work/wt-x。
        self.project = self.root / "proj"
        self.repo = self.project / "ticket-desk"
        self.worktree = self.project / "_work" / "wt-x"
        for path in (self.repo, self.worktree, self.project / "tickets"):
            path.mkdir(parents=True)
        self.token = self.project / "server-keys" / "token.txt"
        self.token.parent.mkdir()
        self.token.write_text("秘密令牌\n", encoding="utf-8")
        self.env_file = self.project / "tickets" / "remote.env"
        self.pointer_file = self.root / "elsewhere.env"
        self.local_store = str(self.root / "本机库")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_env(self, path: Path, remote: str = "https://file.example:8443", token: str | None = None, extra: str = "") -> Path:
        lines = [f"# 注释\nTICKET_REMOTE={remote}"]
        if token is not None:
            lines.append(f"TICKET_TOKEN_FILE={token}")
        if extra:
            lines.append(extra)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def environ(self, **values: str) -> dict[str, str]:
        return {"TICKET_ROOT": self.local_store, **values}

    def test_r2_level_one_environment_wins_over_pointer_and_derived(self):
        self.write_env(self.env_file, token=str(self.token))
        self.write_env(self.pointer_file, remote="https://pointer.example", token=str(self.token))
        channel = channel_config.resolve(False, self.environ(
            TICKET_REMOTE="https://env.example:1", TICKET_TOKEN_FILE=str(self.token), TICKET_ENV=str(self.pointer_file),
        ), search_from=self.repo)
        self.assertTrue(channel.is_remote)
        self.assertEqual(channel_config.LEVEL_ENVIRONMENT, channel.level)
        self.assertEqual("env.example:1", channel.host)
        self.assertEqual("", channel.problem)

    def test_r2_level_two_pointer_wins_over_derived_and_reads_backslash_paths(self):
        self.write_env(self.env_file, token=str(self.token))
        self.write_env(self.pointer_file, remote="https://pointer.example", token=str(self.token).replace("/", "\\"))
        channel = channel_config.resolve(False, self.environ(TICKET_ENV=str(self.pointer_file).replace("/", "\\")), search_from=self.repo)
        self.assertEqual(channel_config.LEVEL_POINTER, channel.level)
        self.assertEqual("pointer.example", channel.host)
        self.assertNotIn("\\", channel.token_file)
        self.assertTrue(channel.token_file_exists)
        self.assertEqual("", channel.problem)

    def test_r2_level_three_is_derived_from_repo_position_including_worktrees(self):
        self.write_env(self.env_file, token=str(self.token))
        for start in (self.repo, self.worktree):
            with self.subTest(start=start):
                channel = channel_config.resolve(False, self.environ(), search_from=start)
                self.assertEqual(channel_config.LEVEL_DERIVED, channel.level)
                self.assertEqual("file.example:8443", channel.host)
                self.assertEqual(channel_config.forward_slashes(str(self.env_file)), channel.source)
                self.assertEqual("", channel.problem)

    def test_r2_level_three_relative_token_path_resolves_next_to_the_env_file(self):
        (self.env_file.parent / "token.txt").write_text("x\n", encoding="utf-8")
        self.write_env(self.env_file, token="token.txt")
        channel = channel_config.resolve(False, self.environ(), search_from=self.worktree)
        self.assertEqual(channel_config.forward_slashes(str(self.env_file.parent / "token.txt")), channel.token_file)
        self.assertTrue(channel.token_file_exists)

    def test_r2_level_four_is_local_and_says_where_the_env_file_should_go(self):
        channel = channel_config.resolve(False, self.environ(), search_from=self.worktree)
        self.assertFalse(channel.is_remote)
        self.assertEqual(channel_config.LEVEL_NONE, channel.level)
        self.assertEqual(Path(self.local_store).resolve(), channel.local_root)
        expected = channel_config.forward_slashes(str(self.env_file))
        self.assertIn(expected, channel.source)
        self.assertIn(expected, channel_config.connect_hint(channel, search_from=self.worktree))
        self.assertIn("TICKET_ENV", channel_config.connect_hint(channel, search_from=self.worktree))

    def test_r2_local_flag_ignores_all_three_levels(self):
        self.write_env(self.env_file, token=str(self.token))
        self.write_env(self.pointer_file, token=str(self.token))
        channel = channel_config.resolve(True, self.environ(
            TICKET_REMOTE="https://env.example", TICKET_TOKEN_FILE=str(self.token), TICKET_ENV=str(self.pointer_file),
        ), search_from=self.repo)
        self.assertFalse(channel.is_remote)
        self.assertEqual(channel_config.LEVEL_FORCED, channel.level)
        self.assertEqual("去掉 --local 再跑", channel_config.connect_hint(channel))

    def test_r2_env_file_missing_token_line_is_explained_in_plain_words(self):
        self.write_env(self.env_file)
        channel = channel_config.resolve(False, self.environ(), search_from=self.repo)
        self.assertTrue(channel.is_remote, "配置找到了就该算远程,哪怕它有问题")
        self.assertIn("缺 TICKET_TOKEN_FILE", channel.problem)
        self.assertIn(channel_config.forward_slashes(str(self.env_file)), channel.problem)
        self.assertIn("TICKET_TOKEN_FILE=<令牌文件的正斜杠路径>", channel.problem)

    def test_r2_env_file_pointing_at_a_missing_token_file_names_the_path(self):
        ghost = self.project / "server-keys" / "没有的令牌.txt"
        self.write_env(self.env_file, token=str(ghost))
        channel = channel_config.resolve(False, self.environ(), search_from=self.repo)
        self.assertIn("不存在", channel.problem)
        self.assertIn(channel_config.forward_slashes(str(ghost)), channel.problem)
        self.assertIn("向本位总监领", channel.problem)
        self.assertFalse(channel.token_file_exists)

    def test_r2_env_file_without_remote_line_and_pointer_to_missing_file_are_both_explained(self):
        self.env_file.write_text("# 只有注释\nTICKET_TOKEN_FILE=x\n", encoding="utf-8")
        channel = channel_config.resolve(False, self.environ(), search_from=self.repo)
        self.assertIn("没有 TICKET_REMOTE 这一行", channel.problem)
        missing = channel_config.resolve(False, self.environ(TICKET_ENV=str(self.root / "无.env")), search_from=self.repo)
        self.assertTrue(missing.is_remote)
        self.assertIn("TICKET_ENV 指向的配置文件不存在", missing.problem)

    def test_r2_env_file_parser_tolerates_export_quotes_and_comments(self):
        self.env_file.write_text(
            "export TICKET_REMOTE='https://q.example'\n"
            "  # 注释\n\n"
            'TICKET_TOKEN_FILE="%s"\n' % str(self.token).replace("\\", "/") +
            "TICKET_CA_SHA256=AB:CD\n",
            encoding="utf-8",
        )
        values = channel_config.parse_env_file(self.env_file)
        self.assertEqual("https://q.example", values["TICKET_REMOTE"])
        self.assertEqual("AB:CD", values["TICKET_CA_SHA256"])
        channel = channel_config.resolve(False, self.environ(), search_from=self.repo)
        self.assertEqual("AB:CD", channel.ca_sha256)
        self.assertTrue(channel.token_file_exists)

    def test_r2_cli_with_broken_env_file_refuses_instead_of_falling_back_to_local(self):
        self.write_env(self.pointer_file)
        result = subprocess.run(
            [*CLI, "list"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            env=clean_environment(self.local_store, TICKET_ENV=str(self.pointer_file)),
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("缺 TICKET_TOKEN_FILE", result.stderr)
        self.assertFalse((Path(self.local_store) / "items").exists(), "配置有误时一个字都不许落到本机库")

    def test_r2_level_four_prints_one_local_mode_line_before_the_data(self):
        from ticket_desk.ticket import main

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self.environ(), clear=False), \
                mock.patch.object(channel_config, "REPO_ROOT", self.repo), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(stderr):
            for name in CHANNEL_VARIABLES:
                os.environ.pop(name, None)
            code = main(["staff", "list"])
        self.assertEqual(0, code)
        self.assertEqual("名册为空。", stdout.getvalue().strip())
        notice = stderr.getvalue().strip()
        self.assertTrue(notice.startswith("本机模式(未接通道):本机库 "), notice)
        self.assertIn(channel_config.forward_slashes(str(Path(self.local_store).resolve())), notice)
        self.assertIn(channel_config.forward_slashes(str(self.env_file)), notice)

    def test_r3_missing_ticket_in_unconfigured_local_mode_says_two_exact_sentences(self):
        from ticket_desk.ticket import main

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self.environ(), clear=False), \
                mock.patch.object(channel_config, "REPO_ROOT", self.repo), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            for name in CHANNEL_VARIABLES:
                os.environ.pop(name, None)
            code = main(["submit", "T-000069"])
        self.assertEqual(2, code)
        lines = [line for line in stderr.getvalue().splitlines() if line.startswith("拦下:")]
        self.assertEqual(1, len(lines), stderr.getvalue())
        root = channel_config.forward_slashes(str(Path(self.local_store).resolve()))
        expected_first = f"拦下:本机模式(未接通道):本机库 {root} 里没有 T-000069。"
        self.assertEqual(expected_first, lines[0])
        second = stderr.getvalue().splitlines()[stderr.getvalue().splitlines().index(lines[0]) + 1]
        self.assertTrue(second.startswith("如果这张单在服务器上,先接通道:"), second)
        self.assertIn(channel_config.forward_slashes(str(self.env_file)), second)
        self.assertIn("TICKET_ENV", second)

    def test_r3_missing_ticket_under_local_flag_tells_you_to_drop_the_flag(self):
        result = run_local_cli(["show", "T-000069"], self.local_store)
        self.assertEqual(2, result.returncode)
        self.assertIn("拦下:本机模式(--local 强制):本机库 ", result.stderr)
        self.assertIn(" 里没有 T-000069。", result.stderr)
        self.assertIn("如果这张单在服务器上,先接通道:去掉 --local 再跑。", result.stderr)
        self.assertNotIn("找不到工单", result.stderr)

    def env_cli(self, *arguments: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*CLI, "env", *arguments], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            env=clean_environment(self.local_store, **extra),
        )

    def test_r4_env_in_remote_mode_prints_host_token_path_existence_and_level_but_never_the_token(self):
        secret = "S3cr3t-" + secrets.token_urlsafe(24)
        self.token.write_text(secret + "\n", encoding="utf-8")
        self.write_env(self.pointer_file, remote="https://desk.example:8443", token=str(self.token))
        for arguments in ((), ("--json",)):
            with self.subTest(arguments=arguments):
                result = self.env_cli(*arguments, TICKET_ENV=str(self.pointer_file))
                self.assertEqual(0, result.returncode, result.stderr)
                output = result.stdout + result.stderr
                self.assertNotIn(secret, output, "令牌本身绝不能被打出来")
                self.assertNotIn(secret[:12], output)
                self.assertIn("desk.example:8443", output)
                self.assertIn(channel_config.forward_slashes(str(self.token)), output)
                self.assertIn(channel_config.forward_slashes(str(Path(self.local_store).resolve())), output)
                self.assertIn("②TICKET_ENV", output)
                self.assertEqual("", result.stderr)
        text = self.env_cli(TICKET_ENV=str(self.pointer_file)).stdout.strip()
        self.assertEqual(1, len(text.splitlines()), text)
        self.assertTrue(text.startswith("远程模式 · 服务器 desk.example:8443 · 令牌文件 "), text)
        self.assertIn("（存在）", text)
        payload = json.loads(self.env_cli("--json", TICKET_ENV=str(self.pointer_file)).stdout)
        self.assertEqual("远程模式", payload["模式"])
        self.assertTrue(payload["令牌文件存在"])
        self.assertEqual("", payload["问题"])
        self.assertNotIn("令牌", json.dumps({k: v for k, v in payload.items() if k not in {"令牌文件", "令牌文件存在"}}, ensure_ascii=False))

    def test_env_without_probe_on_a_real_dead_port_stays_offline_and_returns_zero(self):
        self.token.write_text("dead-port-token\n", encoding="utf-8")
        result = self.env_cli(
            TICKET_REMOTE="http://127.0.0.1:1", TICKET_TOKEN_FILE=str(self.token),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"客户端协议 {channel_config.PROTOCOL_VERSION}", result.stdout)
        self.assertNotIn("服务端协议", result.stdout + result.stderr)
        self.assertNotIn("服务端版本没查到", result.stdout + result.stderr)

    def test_env_without_probe_sends_no_packet_to_a_real_listener(self):
        self.token.write_text("listener-token\n", encoding="utf-8")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(1)
        connections: list[object] = []

        def trap() -> None:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                return
            connections.append(connection)
            connection.close()

        thread = threading.Thread(target=trap, daemon=True)
        thread.start()
        try:
            result = self.env_cli(
                TICKET_REMOTE=f"http://127.0.0.1:{listener.getsockname()[1]}",
                TICKET_TOKEN_FILE=str(self.token),
            )
            thread.join(timeout=2)
        finally:
            listener.close()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], connections, "光跑 env 不得连接远程地址")

    def test_env_probe_on_a_real_dead_port_explains_failure_but_returns_zero(self):
        self.token.write_text("dead-port-token\n", encoding="utf-8")
        result = self.env_cli(
            "--probe", TICKET_REMOTE="http://127.0.0.1:1", TICKET_TOKEN_FILE=str(self.token),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("服务端版本没查到:", result.stdout)
        self.assertIn("远程工单台不可达", result.stdout)

    def test_r4_env_reports_a_missing_token_file_and_a_broken_config_without_failing(self):
        ghost = self.project / "server-keys" / "没有的令牌.txt"
        self.write_env(self.pointer_file, token=str(ghost))
        result = self.env_cli(TICKET_ENV=str(self.pointer_file))
        self.assertEqual(0, result.returncode, "自检命令自己不该炸,要把问题报出来")
        self.assertIn("远程模式(配置有误)", result.stdout)
        self.assertIn("（不存在）", result.stdout)
        self.assertIn("问题 ", result.stdout)
        self.assertIn(channel_config.forward_slashes(str(ghost)), result.stdout)

    def test_r4_env_in_local_mode_names_the_local_store_and_the_level(self):
        forced = self.env_cli("--local")
        self.assertEqual(0, forced.returncode, forced.stderr)
        self.assertTrue(forced.stdout.startswith("本机模式(--local 强制) · 服务器 无 · 令牌文件 无 · 本机库 "), forced.stdout)
        self.assertIn(channel_config.forward_slashes(str(Path(self.local_store).resolve())), forced.stdout)
        self.assertEqual("", forced.stderr, "env 自己不该再打一遍本机模式提示")

        from ticket_desk.ticket import main

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.environ(), clear=False), \
                mock.patch.object(channel_config, "REPO_ROOT", self.repo), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            for name in CHANNEL_VARIABLES:
                os.environ.pop(name, None)
            self.assertEqual(0, main(["env"]))
        self.assertIn("本机模式(未接通道) · 服务器 无 · 令牌文件 无 · 本机库 ", stdout.getvalue())
        self.assertIn("④无配置", stdout.getvalue())
        self.assertIn(channel_config.forward_slashes(str(self.env_file)), stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_r4_env_never_reads_the_token_file_contents(self):
        self.write_env(self.pointer_file, token=str(self.token))
        real_read_text = Path.read_text

        def guarded(path: Path, *args, **kwargs):
            if path.resolve() == self.token.resolve():
                raise AssertionError("env 自检读了令牌文件的内容")
            return real_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded):
            channel = channel_config.resolve(False, self.environ(TICKET_ENV=str(self.pointer_file)), search_from=self.repo)
            channel_config.describe(channel)
            channel_config.describe_payload(channel)
        self.assertTrue(channel.token_file_exists)

    def test_r5_readme_has_the_three_line_channel_section_with_env_first(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## 员工窗怎么接通道", readme)
        section = readme.split("## 员工窗怎么接通道", 1)[1].split("\n## ", 1)[0]
        steps = [line for line in section.splitlines() if re.match(r"^\d\. ", line)]
        self.assertEqual(3, len(steps), steps)
        self.assertIn("ticket.py env", steps[0])
        self.assertIn("remote.env", steps[1])
        self.assertIn("TICKET_ENV", steps[1])
        self.assertIn("receipt", steps[2])
        self.assertIn("--local", section)

    def test_protocol_readme_documents_versions_probe_whitelist_and_recovery(self):
        from ticket_desk.ticket import DEGRADABLE_OPTIONS

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## 协议版本、探测与安全降级", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`0` 是没有版本握手的旧版", section)
        self.assertIn("`1` 是认识 `--taskbook-client-checked` 的版本", section)
        self.assertIn("`2` 是加入握手、版本回显和安全降级后的版本", section)
        self.assertIn("普通 `env` 永远不出网", section)
        self.assertIn("env --probe", section)
        self.assertIn("安全降级白名单", section)
        self.assertIn("set <工单号> --taskbook <同一个路径>", section)
        self.assertEqual({"--taskbook-client-checked": 1}, DEGRADABLE_OPTIONS)

    def test_r3_other_errors_are_left_untouched(self):
        from ticket_desk.ticket import _explain_missing_ticket

        channel = channel_config.resolve(True, self.environ())
        self.assertEqual("工单号格式不对：abc，应为 T-000001。", _explain_missing_ticket("工单号格式不对：abc，应为 T-000001。", channel))
        self.assertEqual("找不到工单 T-000001。", _explain_missing_ticket("找不到工单 T-000001。", None))


class DeployScriptTests(unittest.TestCase):
    def test_install_is_scoped_and_keeps_state_outside_app(self):
        script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("绝对路径", script)
        self.assertIn('"$SERVICE_NAME.service"', script)
        self.assertIn("--reopen-setup", script)
        self.assertIn("--rotate-token", script)
        self.assertIn("rsa:2048", script)
        self.assertIn('systemctl restart "$SERVICE_NAME.service"', script)
        self.assertNotIn("默认安装目录", script)
        self.assertNotIn("3306", script)
        self.assertNotIn("8080", script)

    def test_update_only_replaces_app_and_backup_keeps_fourteen_days(self):
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        backup = (ROOT / "deploy" / "backup.sh").read_text(encoding="utf-8")
        self.assertNotIn("$INSTALL_DIR/db", update)
        self.assertNotIn("$INSTALL_DIR/img", update)
        self.assertIn("0 3 * * *", backup)
        self.assertIn("-mtime +13", backup)
        self.assertIn("ticket.py\" dump", backup)


@unittest.skipUnless(shutil.which("bash"), "本机没有 bash,跑不了 update.sh 的闸")
class DeployGateTests(unittest.TestCase):
    """T-000637 R2:update.sh 的三道前置闸,真跑脚本验,不是 grep 源码字符串。

    两次事故都是「上服前没有硬闸」:线上 python 没装 Pillow(探针只 grep 源码,
    没探过运行环境);pytest 退出码被 `| tail -1` 吞成绿,带红上服两回。
    这些闸只有真被拦过一次才算数,所以这里用打桩的 python3/node 把每一道单独按红。
    """

    UPDATE = ROOT / "deploy" / "update.sh"

    PYTHON_STUB = """#!/usr/bin/env bash
# 打桩的 python3:各条探针的退出码由环境变量控制,好让三道闸能被单独按红。
if [[ "${1:-}" == "-" ]]; then cat >/dev/null; exit 0; fi   # 版本检查那段 heredoc
if [[ "${1:-}" == "-c" ]]; then
  case "${2:-}" in
    *"from PIL"*) exit "${STUB_PIL_RC:-0}" ;;
    *"import pytest"*) exit "${STUB_PYTEST_INSTALLED_RC:-0}" ;;
  esac
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "pytest" ]]; then
  # 闸②到底拿哪些参数跑的 pytest,记下来给 test_6b4 当证据(grep 源码证明不了真传了)。
  if [[ -n "${STUB_ARGV_LOG:-}" ]]; then printf '%s\n' "$@" > "$STUB_ARGV_LOG"; fi
  echo "${STUB_PYTEST_OUTPUT:-243 passed in 1.00s}"
  exit "${STUB_PYTEST_RC:-0}"
fi
exit 0
"""

    NODE_STUB = """#!/usr/bin/env bash
if [[ "${1:-}" == "--check" ]]; then
  if [[ "${STUB_NODE_RC:-0}" != "0" ]]; then
    echo "SyntaxError: Unexpected token in $2" >&2
  fi
  exit "${STUB_NODE_RC:-0}"
fi
exit 0
"""

    @staticmethod
    def posix(path: Path) -> str:
        return str(path).replace("\\", "/")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "src"
        (source / "ticket_desk").mkdir(parents=True)
        (source / "tests").mkdir(parents=True)
        (source / "web").mkdir(parents=True)
        (source / "web" / "tickets.js").write_text("const a = 1;\n", encoding="utf-8")
        self.source = source
        stubs = self.root / "stubs"
        stubs.mkdir()
        self.python_stub = stubs / "python3"
        self.python_stub.write_text(self.PYTHON_STUB, encoding="utf-8", newline="\n")
        self.node_stub = stubs / "node"
        self.node_stub.write_text(self.NODE_STUB, encoding="utf-8", newline="\n")
        for stub in (self.python_stub, self.node_stub):
            stub.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_update(self, *arguments: str, **stub: str) -> subprocess.CompletedProcess:
        # 起子进程一律从 clean_environment 派生(T-000108):这条链一路上不该有任何通向真服务器的变量。
        overrides = {key: str(value) for key, value in stub.items()}
        overrides.setdefault("PYTHON_BIN", self.posix(self.python_stub))
        overrides.setdefault("NODE_BIN", self.posix(self.node_stub))
        # ★SERVICE_USER 必须指向一个一定不存在的账号(T-000790)。
        #   闸① 在 `command -v sudo` 且 `id "$SERVICE_USER"` 都成立时,会拿服务账号再 import 一次 PIL;
        #   服务器上 sudo 和 ticket-desk 都真在,于是它去 sudo 跑本用例摆在临时目录里的打桩解释器,
        #   服务账号读不到那个临时目录 → 闸① 直接拦 → stdout 全空、退 2,
        #   下面这五条(6b/6b2/6c/6c2/7a)全部红在「本该走到闸②③」的地方。本机没有 sudo,所以从没露过。
        #   固定成不存在的账号,这几条在哪台机器上都只验它们各自那道闸;
        #   服务账号那一支由 test_6a2/test_6a3 用打桩的 sudo/id 单独钉。
        overrides.setdefault("SERVICE_USER", f"没有这个服务账号-{os.getpid()}")
        # ★安装目录也必须给:update.sh 故意不设默认值(见脚本里那段护栏)。
        #   这些用例只跑到闸那一层,真替换那一步在本机跑不通,所以给一个一定不会被动到的路径就够。
        overrides.setdefault("INSTALL_DIR", f"/tmp/ticket-desk-gate-{os.getpid()}")
        environment = clean_environment(self.root / "tickets", **overrides)
        return subprocess.run(
            ["bash", self.posix(self.UPDATE), "--source", self.posix(self.source), *arguments],
            capture_output=True, text=True, encoding="utf-8", env=environment,
        )

    def test_6a_missing_pillow_is_blocked_before_anything_is_touched(self):
        """⑥-① 服务端 import 不到 Pillow 就拦:这一条就是 16 张带图 live 全被拦的那次。"""
        done = self.run_update("--check-only", STUB_PIL_RC="1")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 拦", done.stderr)
        self.assertIn("apt install python3-pil", done.stderr)
        self.assertNotIn("前置闸 ②", done.stdout)      # 第一道就停,不往下走
        self.assertNotIn("开始替换", done.stdout)

    def service_account_overrides(self, sudo_rc: int) -> dict[str, str]:
        """造一棵「服务器上的样子」:sudo 在、服务账号也在,sudo -n 那一趟的退出码由参数定。

        本机(开发机)既没有 sudo 也没有 ticket-desk 账号,闸① 永远走「那一次没跑」的分支;
        服务器上两样都真在,走的是另一支——服务端 5 条红就出在这个分支差异上。
        用打桩的 sudo/id 把这一支在任何机器上都钉住。
        """
        stubs = self.root / f"pathstubs-{sudo_rc}"
        stubs.mkdir()
        (stubs / "sudo").write_text(f"#!/usr/bin/env bash\nexit {sudo_rc}\n", encoding="utf-8", newline="\n")
        (stubs / "id").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
        for stub in (stubs / "sudo", stubs / "id"):
            stub.chmod(0o755)
        return {
            "PATH": self.posix(stubs) + os.pathsep + os.environ.get("PATH", ""),
            "SERVICE_USER": "ticket-desk",
        }

    def test_6a2_service_account_that_cannot_import_pillow_is_blocked(self):
        """⑥-① 当前用户能 import、服务账号不能,照样拦——线上跑服务的是服务账号。"""
        done = self.run_update("--check-only", **self.service_account_overrides(1))
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 拦", done.stderr)
        self.assertIn("服务账号 ticket-desk 不能", done.stderr)
        self.assertEqual("", done.stdout.strip())     # 拦在第一道,后两道一个字都不打

    def test_6a3_service_account_that_can_import_pillow_passes_and_is_named(self):
        """服务账号那一趟过了,过语里要点出它真跑过——否则没人分得清是过了还是没跑。"""
        done = self.run_update("--check-only", **self.service_account_overrides(0))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 过", done.stdout)
        self.assertIn("服务账号 ticket-desk 也可 import", done.stdout)
        self.assertIn("三闸全过；--check-only", done.stdout)

    def test_6b_red_pytest_is_blocked_with_the_real_exit_code(self):
        """⑥-② pytest 红就拦,而且拦语里带的是 pytest 自己的退出码,不是管道末节的 0。"""
        done = self.run_update("--check-only", STUB_PYTEST_RC="1", STUB_PYTEST_OUTPUT="1 failed, 242 passed")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 过", done.stdout)
        self.assertIn("前置闸 ② pytest … 拦（pytest 退出码 1）", done.stderr)
        self.assertIn("1 failed, 242 passed", done.stderr)  # 现场也打出来,不用再跑一遍
        self.assertNotIn("开始替换", done.stdout)

    def test_6b2_missing_pytest_is_named_not_silently_skipped(self):
        """没装 pytest 时必须明说,并指出 --skip-tests 才能跳——静默跳过正是带红上服的温床。"""
        done = self.run_update("--check-only", STUB_PYTEST_INSTALLED_RC="1")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("服务器没装 pytest,--skip-tests 才能跳过", done.stderr)
        skipped = self.run_update("--check-only", "--skip-tests", STUB_PYTEST_INSTALLED_RC="1")
        self.assertEqual(0, skipped.returncode, skipped.stdout + skipped.stderr)
        self.assertIn("前置闸 ② pytest … 跳过", skipped.stdout)

    def test_6b3_too_many_skips_is_treated_as_not_run_and_blocked(self):
        """跳过条数超过上限就按拦处理:pytest 自己退 0,闸照样不放——跳这么多等于没跑。

        R1 把「读 tools/ 以外文件」的用例改成了干净跳过;要是没有这道上限,
        哪天跳光了闸还是一路绿灯,今天这个坑(闸看起来在,其实从没真跑过)就换个形状再来一次。
        """
        listing = (
            "SKIPPED [1] tests/test_ticket_system.py:1: "
            "本包不含网页台面(web/),读不到 review/ticket-system/MOVE-VERIFY.md\n"
            "SKIPPED [15] tests/test_ticket_system.py:2: 另外十五条同理\n"
            "264 passed, 16 skipped in 1.00s"
        )
        done = self.run_update("--check-only", STUB_PYTEST_OUTPUT=listing)
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ② pytest … 拦（跳过 16 条,超过上限 15", done.stderr)
        self.assertIn("MOVE-VERIFY.md", done.stderr)          # 清单要真列出来,不能只报个数
        self.assertIn("另外十五条同理", done.stderr)
        self.assertNotIn("前置闸 ③", done.stdout)             # 拦在闸②,不往下走
        self.assertNotIn("开始替换", done.stdout)

    def test_6b3b_skips_at_the_limit_still_pass_and_the_count_is_printed(self):
        """上限之内照常放行,而且过语里必须报出跳了几条——不报数就等于没人看得见跳过。"""
        done = self.run_update("--check-only", STUB_PYTEST_OUTPUT="265 passed, 15 skipped in 1.00s")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ② pytest … 过（265 passed, 15 skipped in 1.00s;跳过 15 条,上限 15）", done.stdout)

    def test_6b4_gate_two_keeps_its_pytest_cache_out_of_the_package(self):
        """闸②跑 pytest 时缓存 provider 直接关掉,不在包目录里落 .pytest_cache。

        用 sudo 跑过一次之后,包里会留下 root 属主的 .pytest_cache,下一个普通账号写不进去,
        pytest 只打一条 PytestCacheWarning 就过去了——闸看着在跑,其实已经被自己上一趟的产物半瞎。
        这里不 grep 源码,而是把闸真传给 pytest 的 argv 记下来看。
        """
        log = self.root / "pytest-argv.txt"
        done = self.run_update("--check-only", STUB_ARGV_LOG=self.posix(log))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        argv = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(["-m", "pytest", "tests", "-q", "-rs", "-p", "no:cacheprovider"], argv)
        self.assertFalse((self.source / ".pytest_cache").exists(), "闸②不许在包目录里留缓存")
        self.assertNotIn("cache_dir", self.UPDATE.read_text(encoding="utf-8"))

    def test_6c_broken_tickets_js_is_blocked(self):
        """⑥-③ tickets.js 语法坏了就拦:浏览器直接读它,没人替它编译。"""
        done = self.run_update("--check-only", STUB_NODE_RC="1")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ② pytest … 过", done.stdout)
        self.assertIn("前置闸 ③ node … 拦", done.stderr)
        self.assertNotIn("开始替换", done.stdout)

    def test_6c2_missing_node_is_named_and_skippable(self):
        done = self.run_update("--check-only", NODE_BIN=self.posix(self.root / "没有这个 node"))
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("服务器没装 node,--skip-node-check 才能跳过", done.stderr)
        skipped = self.run_update(
            "--check-only", "--skip-node-check", NODE_BIN=self.posix(self.root / "没有这个 node"),
        )
        self.assertEqual(0, skipped.returncode, skipped.stdout + skipped.stderr)
        self.assertIn("前置闸 ③ node … 跳过", skipped.stdout)

    def test_7a_all_three_pass_then_check_only_stops_before_replacing(self):
        """⑦ 三闸全过才有「三闸全过」这一行;--check-only 到此为止,一个字节都不动线上。"""
        done = self.run_update("--check-only")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("前置闸 ① PIL … 过", done.stdout)
        self.assertIn("前置闸 ② pytest … 过（243 passed in 1.00s;跳过 0 条,上限 15）", done.stdout)
        self.assertIn("前置闸 ③ node … 过", done.stdout)
        self.assertIn("三闸全过；--check-only", done.stdout)
        self.assertNotIn("开始替换", done.stdout)

    @unittest.skipIf(getattr(os, "geteuid", lambda: 1)() == 0, "root 下会真去动系统目录,不跑这一条")
    def test_7b_replacement_phase_is_only_reached_after_all_three_gates(self):
        """⑦ 不加 --check-only 时,三闸全过之后才走到替换段——到那里因为不是 root 而失败,
        正好证明「先过闸、后 cp」这个次序是真的,而不是只写在注释里。"""
        install_dir = f"/tmp/ticket-desk-install-{os.getpid()}"
        done = self.run_update("--install-dir", install_dir)
        self.assertIn("三闸全过,开始替换。", done.stdout)
        self.assertNotEqual(0, done.returncode)          # 替换段本身在本机跑不通,这是预期的
        self.assertNotIn("前置闸", done.stderr)          # 失败不在任何一道闸上
        subprocess.run(["bash", "-c", f"rm -rf '{install_dir}'"], capture_output=True)

    def test_7c_bad_source_is_still_refused_before_the_gates(self):
        environment = clean_environment(
            self.root / "tickets", PYTHON_BIN=self.posix(self.python_stub),
            INSTALL_DIR=f"/tmp/ticket-desk-gate-{os.getpid()}")
        done = subprocess.run(
            ["bash", self.posix(self.UPDATE), "--source", self.posix(self.root / "不存在"), "--check-only"],
            capture_output=True, text=True, encoding="utf-8", env=environment,
        )
        self.assertEqual(2, done.returncode)
        self.assertIn("必须用 --source 指向工单台源码根目录", done.stderr)
        self.assertNotIn("前置闸", done.stdout)

    def test_7d_post_check_watches_the_port_not_a_log_line(self):
        """后置闸判的是「端口现在在听」;journal 是追加的,旧 READY 行照样会被 grep 到。"""
        script = self.UPDATE.read_text(encoding="utf-8")
        self.assertIn("ss -ltn", script)
        self.assertIn(':$DESK_PORT ', script)
        self.assertIn('journalctl -u "$SERVICE_NAME.service" -n 20 --no-pager', script)
        self.assertIn("exit 3", script)
        self.assertNotIn("grep -q READY", script)
        # 退出码必须直接接在 pytest 那条命令后面,不能经过管道
        self.assertIn("TEST_RC=$?", script)
        self.assertIn("set -euo pipefail", script)
        self.assertNotIn("pytest tests -q | tail", script)


class ReleaseVersionTests(unittest.TestCase):
    """发布版本号有两处(代码里一处、CHANGELOG 顶上一处),钉住它们不许漂。

    ★两处各写一份的东西必然漂——发版那天漏改一处,以后谁也说不清线上跑的是哪一版。
    """

    @staticmethod
    def changelog() -> Path | None:
        """CHANGELOG 在仓根。ticket-desk 可能是仓根,也可能是别的仓里的子目录。"""
        for candidate in (ROOT / "CHANGELOG.md", ROOT.parent / "CHANGELOG.md"):
            if candidate.is_file():
                return candidate
        return None

    def test_the_version_in_code_matches_the_top_of_the_changelog(self):
        from ticket_desk import __version__

        self.assertRegex(__version__, r"^\d+\.\d+$")
        changelog = self.changelog()
        if changelog is None:
            self.skipTest(f"{PACKAGE_TREE_SKIP_PREFIX},上服包里没有仓根的 CHANGELOG.md。")
        versions = re.findall(r"^## (\d+\.\d+)$", changelog.read_text(encoding="utf-8"), re.MULTILINE)
        self.assertTrue(versions, "CHANGELOG 里一条版本记录都没有")
        self.assertEqual(versions[0], __version__, "代码里的版本号与 CHANGELOG 最上面那条对不上")

    def test_the_release_version_is_not_the_protocol_version(self):
        """两个号管的是两件事,别哪天被人对齐成一个。"""
        from ticket_desk import __version__

        self.assertNotEqual(str(channel_config.PROTOCOL_VERSION), __version__)


@unittest.skipUnless(shutil.which("git") and shutil.which("tar"), "本机没有 git 或 tar,打不了包")
class PackedTreeIsSelfSufficientTests(unittest.TestCase):
    """★真打一次包、解出来、在**解包目录**里核它自带了跑闸链要用的东西。

    这一组堵的是「单机跑得通」与「上服跑得通」之间那条缝。那条缝一次次以同一个形状出现:
    打包少打一个文件、install 少拷一个目录、配置放在会被上服删掉的位置——
    共同点是**在仓树上永远看不见**,只有真打一次包才露头。
    最贵的一次:pack.sh 不打 README.md,而闸②里有两条用例直接读它,
    于是一个照文档操作的新使用方,**第一次上服就被自己的闸拦死**,
    报出来还长得像「你的测试坏了」,不像「包少打了一个文件」。
    ★所以这里不 grep pack.sh 的源码——源码里写着要打什么不算数,包里真有才算数。
    """

    @classmethod
    def setUpClass(cls) -> None:
        """把**当前这棵树**捧进一个临时 git 仓再打包,不打真仓的 HEAD。

        ★两个理由,都要紧:
          ① pack.sh 有一道「工作树脏就拦」的闸(它是对的:打包打的是提交,
             没提交的东西进不了包)。可是开发期工作树**总是**脏的——
             直接对真仓打包,这一组就永远走跳过分支,等于没有这道闸,
             而「跳过」正是本仓一再写的那个静默漏洞的形状。
          ② 打当前树才能立刻验到**这次的改动**:改了 pack.sh 要当场看见效果,
             而不是提交完才知道打漏了文件。
        真仓一个字节都不碰:只读地拷出来,commit 落在临时目录里。
        """
        cls.temporary = tempfile.TemporaryDirectory()
        repository = Path(cls.temporary.name) / "repo"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tickets", "config.json")
        for name in ("ticket_desk", "tests", "deploy", "web"):
            if (ROOT / name).is_dir():
                shutil.copytree(ROOT / name, repository / name, ignore=ignore)
        repository.mkdir(parents=True, exist_ok=True)
        if (ROOT / "README.md").is_file():
            shutil.copy2(ROOT / "README.md", repository / "README.md")
        git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
               "-c", "commit.gpgsign=false"]
        for argv in ([*git, "init", "-q"], [*git, "add", "-A"], [*git, "commit", "-qm", "pack"]):
            done = subprocess.run(argv, cwd=str(repository), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if done.returncode != 0:
                raise unittest.SkipTest(f"临时仓建不起来,跳过:{(done.stderr or done.stdout).strip()[:200]}")
        # ★输出用相对路径:pack.sh 判绝对路径用的是 `= /*`,而 Git Bash 下的
        #   `C:/...` 不以 / 开头,会被当成相对路径再拼一次 $PWD。服务器上是 posix 路径,
        #   那条判断在真实场景没问题,所以这里绕开它,不改脚本。
        packed = subprocess.run(
            ["bash", str(repository / "deploy" / "pack.sh").replace("\\", "/"), "../desk.tar"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(repository),
        )
        assert packed.returncode == 0, f"pack.sh 打包失败：\n{packed.stdout}\n{packed.stderr}"
        cls.unpacked = Path(cls.temporary.name) / "unpacked"
        cls.unpacked.mkdir()
        subprocess.run(["tar", "xf", "../desk.tar"], cwd=str(cls.unpacked), check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def test_the_package_carries_everything_the_gate_chain_reads(self):
        """闸链要读的每一样都得在包里。缺一样,闸② 就会在服务器上**每次都拦**。"""
        for relative in ("ticket_desk", "tests", "deploy", "deploy/DEPLOY_HEAD", "README.md"):
            with self.subTest(relative=relative):
                self.assertTrue((self.unpacked / relative).exists(), f"上服包里缺 {relative}")

    def test_the_two_readme_tests_really_run_in_the_unpacked_tree(self):
        """★不是「README 在包里」就完了——要在**解包目录**里把那两条真跑一遍。

        「文件在包里」与「用例在包里读得到它」是两件事:包根取错一层,文件也在包里,
        用例照样 FileNotFoundError。所以这里跑的是真 pytest,不是查文件在不在。
        """
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_ticket_system.py", "-q", "-rs",
             "-p", "no:cacheprovider", "-k", "readme_has_the_three_line or protocol_readme_documents"],
            cwd=str(self.unpacked), env=clean_environment(self.unpacked / "tickets"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("2 passed", done.stdout)
        self.assertNotIn("skipped", done.stdout)      # 补包,不是给用例加跳过
        self.assertNotIn("FileNotFoundError", done.stdout + done.stderr)


class PackageTreeGateTests(unittest.TestCase):
    """T-000790:闸②在服务器上跑的是「只有 ticket_desk 与 web」的上服包树,不是完整仓树。

    包树里读不到 tools/ 以外的文件,那几条用例必须**干净跳过而不是红**——否则闸把每一次上服都拦死。
    但只钉这一半,等于给自己开了个「跳光了也算绿」的口子,所以下面第二条反过来钉:
    同样那几条在仓树上必须**真跑**。两条缺一不可,少哪条都能把 R1 变成放水。
    """

    REPOSITORY_ONLY_TEST = (
        "tests/test_ticket_system.py"
        "::FixtureAndInterfaceTests::test_the_minimal_fallback_page_is_self_contained_and_read_only"
    )

    @staticmethod
    def run_pytest(working_directory: Path, node_id: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pytest", node_id, "-q", "-rs", "-p", "no:cacheprovider"],
            cwd=working_directory, env=clean_environment(working_directory / "tickets"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def package_tree(self) -> Path:
        """照 deploy/update.sh 的口径造一棵上服包树:只有 ticket_desk,不含 web。

        ★故意不含 web:网页台面是可选件。包树里没有它,读它的用例就该**干净跳过**——
          这正是本组要钉的那件事。
        """
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        package = Path(temporary.name) / "pkg"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(ROOT / "ticket_desk", package / "ticket_desk", ignore=ignore)
        shutil.copytree(ROOT / "tests", package / "tests", ignore=ignore)
        self.assertFalse((package / "web").exists(), "包树里不该有网页台面")
        return package

    def test_repository_only_tests_are_skipped_not_failed_in_the_package_tree(self):
        done = self.run_pytest(self.package_tree(), self.REPOSITORY_ONLY_TEST)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("1 skipped", done.stdout)
        self.assertNotIn("failed", done.stdout)
        self.assertIn(PACKAGE_TREE_SKIP_PREFIX, done.stdout)     # -rs 把人话理由打出来了

    def test_the_same_tests_really_run_in_the_repository_tree(self):
        """★这一条是防放水的那一条:R1 的跳过条件一旦变成「永远跳过」,这里立刻红。

        它自己也只在仓树上成立——包树里根本没有仓树可跑。但这里**不能**走 repository_file_or_skip:
        把那个跳过条件改成「永远跳过」的那次变异,会连这条守门用例一起跳掉,守门就白守了。
        所以它自己直接看文件在不在,不经过被守的那段代码。
        真正把这条守住的是仓树上的每一次全量(交板闸、本机全量),不是服务器上那一趟。
        """
        if not (ROOT / "web" / "minimal" / "index.html").is_file():
            self.skipTest(f"{PACKAGE_TREE_SKIP_PREFIX},这一条要在带 web/minimal 的仓树上真跑一次。")
        done = self.run_pytest(ROOT, self.REPOSITORY_ONLY_TEST)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("1 passed", done.stdout)
        self.assertNotIn("skipped", done.stdout)
        self.assertNotIn(PACKAGE_TREE_SKIP_PREFIX, done.stdout)


class TicketSeventeenRegressionTests(unittest.TestCase):
    """T-000018 的六条回归：一条一条来，坏了要能一眼看出坏在哪一条。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SqliteStore(self.root / "db" / "tickets.sqlite")
        self.service = TicketService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def rows_in_threads_table(self, slot: str) -> list[tuple]:
        connection = sqlite3.connect(self.store.database)
        try:
            return connection.execute(
                "SELECT position, payload FROM threads WHERE slot=? ORDER BY position", (slot,)
            ).fetchall()
        finally:
            connection.close()

    def test_r1_mark_read_lands_in_the_threads_table_not_on_disk(self):
        # 服务器上的 threads/ 目录根本不存在，这里也刻意不建。
        self.assertFalse(self.store.threads_dir.exists())
        self.service.say(SLOT, "总编排", "第一条")
        self.service.say(SLOT, "设计者", "第二条")
        self.assertEqual(2, len(self.service.inbox(SLOT, SLOT)))

        self.assertEqual(2, len(self.service.inbox(SLOT, SLOT, mark_read=True)))

        self.assertEqual([], self.service.inbox(SLOT, SLOT))
        rows = self.rows_in_threads_table(SLOT)
        self.assertEqual([1, 2], [row[0] for row in rows])
        for _, payload in rows:
            self.assertIn(SLOT, json.loads(payload)["已读标记"])
        self.assertFalse(self.store.threads_dir.exists(), "标已读不许偷偷落磁盘")

    def test_r1_say_and_inbox_work_on_a_slot_with_no_thread_yet(self):
        fresh = "内容·文案与真源"
        self.assertEqual([], self.rows_in_threads_table(fresh))
        self.service.say(fresh, "总编排", "新位第一句")
        self.assertEqual(1, len(self.service.inbox(fresh, fresh)))
        self.service.inbox(fresh, fresh, mark_read=True)
        self.assertEqual([], self.service.inbox(fresh, fresh))
        self.assertEqual(1, len(self.rows_in_threads_table(fresh)))
        self.assertEqual([], self.service.inbox(SLOT, SLOT), "别位的对话线不该被带出来")

    def test_r2_question_owner_slot_can_answer_but_decision_still_cannot(self):
        question = self.service.create_question("疑问", SLOT, "跨位提事", "远程标已读报错", initiator=OTHER_SLOT)
        self.assertEqual(SLOT, question["所属总监位"])
        answered = self.service.answer(question["编号"], "已确认是服务端写回绕过 store。", SLOT)
        self.assertEqual("已答", answered["状态"])

        decision = self.service.create_question("拍板", SLOT, "要不要统一话术", VALID_DECISION_BODY, initiator=OTHER_SLOT)
        with self.assertRaisesRegex(TicketError, "设计者或总编排"):
            self.service.answer(decision["编号"], "我来拍", SLOT)
        self.assertEqual("待答", self.service.store.load_ticket(decision["编号"])["状态"])

    def test_r4_new_slot_gets_its_staff_bucket_and_nothing_raises_keyerror(self):
        fake = "测试·加位不炸"
        original = model.SLOTS
        try:
            for module in (model, store_module, service_module):
                module.SLOTS = original + (fake,)
            store = SqliteStore(self.store.database)
            service = TicketService(store)
            self.assertIn(fake, store.load_staff()["总监位"])
            self.assertEqual([], service.list_staff(fake))
            self.assertEqual(f"{fake}-01", service.staff_new(fake, "model-b")["员工名"])
            self.assertEqual([f"{fake}-01"], [row["员工名"] for row in service.list_staff(fake)])
        finally:
            for module in (model, store_module, service_module):
                module.SLOTS = original


class TicketSeventeenRemoteTests(TicketTestCase):
    """R3 与 R5：都要真的起一个服务端、真的走一遍命令行才算数。"""

    def setUp(self) -> None:
        super().setUp()
        self.sqlite = SqliteStore(self.root / "server" / "db" / "tickets.sqlite")
        self.remote_service = TicketService(self.sqlite)
        self.remote_worker = self.remote_service.staff_new(SLOT, "model-a")["员工名"]
        self.service_token = secrets.token_urlsafe(32)
        handler = partial(TicketRequestHandler, directory=str(WEB_ROOT))
        self.server = TicketHTTPServer(
            ("127.0.0.1", 0), handler, self.remote_service, self.service_token, AccountManager(self.sqlite.database)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token_file = self.root / "remote.token"
        self.token_file.write_text(self.service_token, encoding="utf-8")
        self.command = CLI

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def environment(self, remote: str, **extra) -> dict[str, str]:
        return clean_environment(
            self.service.store.root, TICKET_REMOTE=remote, TICKET_TOKEN_FILE=str(self.token_file), **extra,
        )

    def run_cli(self, argv: list[str], environment: dict[str, str]):
        return subprocess.run(
            self.command + argv, cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8"
        )

    def claimed_remote_ticket(self, title: str, deliverable: Path) -> str:
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        created = self.run_cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", title, "--source", "AGENTS.md:1",
            "--consumer", "工单台", "--assign", self.remote_worker, "--tier", "乙",
            "--deliverable", str(deliverable), "--internal", "--json",
        ], environment)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = json.loads(created.stdout)["result"]["编号"]
        self.assertEqual(0, self.run_cli(["claim", ticket_id, "--by", self.remote_worker], environment).returncode)
        return ticket_id

    def test_r3_unreachable_remote_exits_non_zero_and_only_shows_stale_data_on_request(self):
        self.dispatch("本机快照里的旧单")
        environment = self.environment("http://127.0.0.1:1")
        default = self.run_cli(["list"], environment)
        self.assertEqual(3, default.returncode)
        self.assertIn("远程不可达", default.stderr)
        self.assertIn("快照时间", default.stderr)
        self.assertNotIn("T-0000", default.stdout)

        stale = self.run_cli(["list"], self.environment("http://127.0.0.1:1", TICKET_ALLOW_STALE="1"))
        self.assertEqual(3, stale.returncode, "给了快照也不许退 0")
        self.assertIn("不是服务器上的数据", stale.stdout)
        self.assertIn("不是服务器上的数据", stale.stderr)
        self.assertIn("T-0000", stale.stdout)

    def test_t108_r2_ticket_env_file_alone_connects_the_cli_to_the_server(self):
        env_file = self.root / "remote.env"
        env_file.write_text(
            f"TICKET_REMOTE=http://127.0.0.1:{self.server.server_address[1]}\n"
            f"TICKET_TOKEN_FILE={str(self.token_file).replace(chr(92), '/')}\n",
            encoding="utf-8",
        )
        environment = clean_environment(self.service.store.root, TICKET_ENV=str(env_file))
        listed = self.run_cli(["list"], environment)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual("", listed.stderr)
        self.assertEqual(self.remote_service.list_tickets(), [], "服务端库里还没有单")
        self.assertIn("没有符合条件的工单", listed.stdout)

    def test_t108_r3_missing_ticket_in_remote_mode_blames_the_server_not_the_local_store(self):
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        result = self.run_cli(["show", "T-000999"], environment)
        self.assertEqual(2, result.returncode)
        self.assertEqual(
            f"拦下:远程模式:服务器 127.0.0.1:{self.server.server_address[1]} 上没有这张单 T-000999。",
            result.stderr.strip(),
        )
        self.assertNotIn("本机", result.stderr)

    def test_remote_export_fetches_the_ticket_then_writes_the_client_out_path(self):
        ticket = self.remote_service.create_dispatch(
            SLOT, "远程丙档导出", ["D:/source.txt:1-20"], "D:/output.csv", self.remote_worker,
            notes="只生成一张 CSV", task_tier="丙", context_lines=20,
            deliverables=[str(self.deliverable)], internal=False,
        )
        out = self.root / "client-only" / "任务书.md"
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        result = self.run_cli(["export", ticket["编号"], "--out", str(out)], environment)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(out.resolve()), result.stdout.strip())
        self.assertTrue(out.is_file())
        self.assertIn("任务档:丙", out.read_text(encoding="utf-8"))

    def test_r5_remote_submit_checks_deliverables_on_the_submitting_machine(self):
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        # 交付项写成一个只有本机有、服务端根目录里绝对没有的路径。
        local_only = self.root / "提交端独有交付物.md"
        local_only.write_text("# 交付物\n", encoding="utf-8")
        self.assertFalse((self.sqlite.root / local_only.name).exists())

        ticket_id = self.claimed_remote_ticket("远程交板·交付项在本机", local_only)
        submitted = self.run_cli([
            "submit", ticket_id, "--evidence", "内部工具单验证完成",
            "--verify-command", "python -m pytest tests -q", "--raw-output", "61 passed",
        ], environment)
        self.assertEqual(0, submitted.returncode, submitted.stderr)
        stored = self.remote_service.store.load_ticket(ticket_id)
        self.assertEqual("待判", stored["状态"])
        self.assertIn("交付项本机核验:1/1 齐", stored["接线证据"]["文字"])

    def test_r5_missing_deliverable_is_refused_on_the_submitting_machine(self):
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        ghost = self.root / "缺失交付物.md"
        ghost.write_text("# 建单时存在\n", encoding="utf-8")
        ticket_id = self.claimed_remote_ticket("远程交板·交付项缺一个", ghost)
        ghost.unlink()
        refused = self.run_cli([
            "submit", ticket_id, "--evidence", "内部工具单验证完成",
            "--verify-command", "python -m pytest tests -q", "--raw-output", "61 passed",
        ], environment)
        self.assertEqual(2, refused.returncode)
        self.assertIn("以下交付项找不到对应文件", refused.stderr)
        self.assertIn(str(ghost), refused.stderr)
        self.assertIn("以提交端的文件系统为准", refused.stderr)
        self.assertEqual("已认领", self.remote_service.store.load_ticket(ticket_id)["状态"])

    def _six_item_submit_failure(self):
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        deliverables = [self.root / f"deliverable-{index}.md" for index in range(1, 7)]
        for path in deliverables:
            path.write_text(f"# {path.stem}\n", encoding="utf-8")
        command = [
            "new", "--type", "派单", "--slot", SLOT, "--title", "六项交付改单提示",
            "--source", "AGENTS.md:1", "--consumer", "工单台", "--assign", self.remote_worker,
            "--tier", "乙", "--internal", "--json",
        ]
        for path in deliverables:
            command.extend(["--deliverable", str(path)])
        created = self.run_cli(command, environment)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = json.loads(created.stdout)["result"]["编号"]
        self.assertEqual(0, self.run_cli(["claim", ticket_id, "--by", self.remote_worker], environment).returncode)
        missing = deliverables[3]
        missing.unlink()
        refused = self.run_cli([
            "submit", ticket_id, "--evidence", "内部工具单验证完成",
            "--verify-command", "python -m pytest tests -q", "--raw-output", "tests passed",
        ], environment)
        self.assertEqual(2, refused.returncode)
        set_command = next(line for line in refused.stderr.splitlines() if line.startswith("python "))
        return refused, set_command, deliverables, missing

    def test_t267_r2_submit_error_has_complete_set_command_with_all_six_items(self):
        refused, set_command, deliverables, _ = self._six_item_submit_failure()
        self.assertIn("交付项只能由本位总监或总编排改", refused.stderr)
        self.assertIn(f"原样发给 {SLOT}", refused.stderr)
        self.assertIn(str((ROOT / "ticket_desk" / "ticket.py").resolve()), set_command)
        self.assertEqual(6, set_command.count("--deliverable"))
        for path in deliverables:
            self.assertIn(str(path), set_command)
        self.assertIn(f"--by '{SLOT}'", set_command)

    def test_t267_r2_only_missing_item_is_listed_below_the_copyable_command(self):
        refused, set_command, _, missing = self._six_item_submit_failure()
        self.assertNotIn("<#", set_command)
        marker = "核不到的交付项（请在上面的命令里改这几条）：\n"
        self.assertIn(marker, refused.stderr)
        missing_section = refused.stderr.rsplit(marker, 1)[1].split("\n（以上是在本机", 1)[0]
        self.assertEqual(f"- {missing}", missing_section.strip())


class TicketSeventyFiveEditableTests(TicketTestCase):
    """T-000075：一张单建完之后还能改什么。"""

    def cli(self, arguments: list[str], root: Path):
        return run_local_cli(arguments, root)

    def test_r1_dispatch_body_is_stored_not_silently_dropped(self):
        ticket = self.service.create_dispatch(
            SLOT, "派单也要收正文", ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], body="abc", internal=False,
        )
        self.assertEqual("abc", ticket["正文"])
        self.assertEqual("abc", self.service.store.load_ticket(ticket["编号"])["正文"])

    def test_r1_cli_new_dispatch_body_survives_show(self):
        root = self.root / "cli-body"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行派单带正文",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--body", "abc", "--user-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        shown = self.cli(["show", ticket_id], root)
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertEqual("abc", json.loads(shown.stdout)["正文"])

    def test_r2_taskbook_is_a_real_field_and_defaults_to_empty(self):
        ticket = self.dispatch("没填任务书路径")
        self.assertEqual("", ticket["任务书路径"])
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["任务书路径"])

    def test_r2_ticket_placeholder_is_replaced_with_the_real_id(self):
        ticket = self.service.create_dispatch(
            SLOT, "占位符替换", ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
            taskbook=r"C:\ticket-desk-workspace\_office\前端·视觉与资源\任务书\{ticket}_六库清点.md",
        )
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertNotIn("{ticket}", stored["任务书路径"])
        self.assertEqual(
            r"C:\ticket-desk-workspace\_office\前端·视觉与资源\任务书" + "\\" + ticket["编号"] + "_六库清点.md",
            stored["任务书路径"],
        )

    def test_r2_cli_taskbook_placeholder_survives_show(self):
        root = self.root / "cli-taskbook"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行占位符",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--user-facing",
            "--taskbook", TASKBOOK_DIRECTORY + os.sep + "{ticket}_建完就改不了.md",
            "--taskbook-unchecked",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        shown = json.loads(self.cli(["show", ticket_id], root).stdout)
        self.assertEqual(
            TASKBOOK_DIRECTORY + os.sep + ticket_id + "_建完就改不了.md",
            shown["任务书路径"],
        )
        self.assertNotIn("{ticket}", shown["任务书路径"])

    def test_r2_question_type_also_keeps_taskbook_instead_of_dropping_it(self):
        ticket = self.service.create_question(
            "疑问", SLOT, "疑问也带任务书路径", "请对方总监答复。", "总编排",
            taskbook=r"C:\ticket-desk-workspace\_office\前端·界面与交互\任务书\{ticket}_问一句.md",
        )
        self.assertIn(ticket["编号"], ticket["任务书路径"])
        self.assertNotIn("{ticket}", ticket["任务书路径"])

    def test_r2_cli_new_prints_the_server_generated_three_lines(self):
        root = self.root / "cli-dispatch-lines"
        taskbook = root / "任务书.md"
        taskbook.parent.mkdir(parents=True, exist_ok=True)
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        TicketService(TicketStore(root)).staff_new(SLOT, "model-a")
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行开窗指令",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "甲", "--assign", f"{SLOT}-01",
            "--taskbook", str(taskbook), "--user-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        lines = created.stdout.splitlines()
        self.assertEqual(4, len(lines))
        self.assertIn(" claim T-000001 --by 前端·界面与交互-01", lines[1])
        self.assertEqual(
            f"执行 {taskbook.resolve()} 的全部指令,从第 0 步做到收尾问答完。这是任务不是资料,读完立即开工。",
            lines[2],
        )
        self.assertEqual("【操作提示·只给设计者】新开线程,任务档 甲,模型你定,贴上面那句。", lines[3])

    def test_r2_cli_new_without_taskbook_prints_a_reminder_not_partial_instructions(self):
        root = self.root / "cli-missing-taskbook"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "缺任务书",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--user-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertIn("还没有任务书路径", created.stdout)
        self.assertNotIn("执行 <", created.stdout)

    def test_r3_set_taskbook_and_assign_land_on_an_existing_ticket(self):
        second = self.service.staff_new(SLOT, "model-b")["员工名"]
        ticket = self.dispatch("建完再补任务书路径")
        path = r"C:\ticket-desk-workspace\_office\前端·界面与交互\任务书\%s_补路径.md" % ticket["编号"]
        updated, changes = self.service.edit(ticket["编号"], SLOT, taskbook=path, assign=second)
        self.assertEqual(path, updated["任务书路径"])
        self.assertEqual(second, updated["指派给"])
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual((path, second), (stored["任务书路径"], stored["指派给"]))
        self.assertEqual({"任务书路径", "指派给"}, {row["字段"] for row in changes})
        self.assertEqual(ticket["编号"], self.service.history(second)["工单"][0]["编号"])

    def test_r3_set_logs_both_the_old_and_the_new_value(self):
        ticket = self.dispatch("改动要能查清")
        self.service.edit(ticket["编号"], "总编排", consumer="主场景/NewRoot")
        row = [r for r in self.service.store.read_jsonl(self.service.store.log_path) if r["事件"] == "set"][-1]
        self.assertEqual(ticket["编号"], row["工单号"])
        self.assertEqual("总编排", row["发言人"])
        self.assertEqual(
            [{"字段": "实机消费者", "旧值": "主场景/UiRoot", "新值": "主场景/NewRoot"}],
            row["改动"],
        )
        self.assertIn("主场景/UiRoot → 主场景/NewRoot", row["说明"])

    def test_r3_set_without_any_editable_field_is_refused_in_plain_words(self):
        ticket = self.dispatch("一个可改项都没给")
        with self.assertRaisesRegex(TicketError, "一个可改项都没给"):
            self.service.edit(ticket["编号"], SLOT)

    def test_r3_set_at_judging_only_allows_assign(self):
        ticket = self.to_judging()
        with self.assertRaises(TicketError) as caught:
            self.service.edit(ticket["编号"], SLOT, taskbook="D:/a.md")
        message = str(caught.exception)
        self.assertIn("现在是「待判」", message)
        self.assertIn("待判态只允许改指派给（--assign）", message)
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["任务书路径"])

        second = self.service.staff_new(SLOT, "model-b")["员工名"]
        updated, changes = self.service.edit(ticket["编号"], SLOT, assign=second)
        self.assertEqual(second, updated["指派给"])
        self.assertEqual(["指派给"], [row["字段"] for row in changes])

    def test_r3_set_at_judging_can_restore_worker_from_previous_slot(self):
        ticket = self.to_judging()
        ticket["指派给"] = "总编排"
        ticket["所属总监位"] = "总编排"
        ticket["转交可见位"] = [SLOT]
        self.service.store.atomic_json(self.service.store.item_path(ticket["编号"]), ticket)

        updated, _ = self.service.edit(ticket["编号"], "总编排", assign=self.worker)
        self.assertEqual(self.worker, updated["指派给"])

    def test_r3_set_by_a_foreign_slot_or_a_worker_window_is_refused(self):
        ticket = self.dispatch("别位不许改")
        for actor in (OTHER_SLOT, self.worker, "设计者"):
            with self.subTest(actor=actor):
                with self.assertRaises(TicketError) as caught:
                    self.service.edit(ticket["编号"], actor, taskbook="D:/a.md")
                self.assertIn("只有该位总监", str(caught.exception))
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["任务书路径"])

    def test_r3_set_assign_still_requires_an_active_staff_of_this_slot(self):
        ticket = self.dispatch("改派也要过名册")
        self.service.staff_retire(self.worker)
        with self.assertRaisesRegex(TicketError, "已收窗"):
            self.service.edit(ticket["编号"], SLOT, assign=self.worker)
        with self.assertRaisesRegex(TicketError, "员工名格式或所属位不对"):
            self.service.edit(ticket["编号"], SLOT, assign="后端·服务与接口-01")

    def test_r3_set_refuses_to_empty_the_source_pointer(self):
        ticket = self.dispatch("真源指针不许清空")
        with self.assertRaisesRegex(TicketError, "一条有内容的都没有"):
            self.service.edit(ticket["编号"], SLOT, sources=["  "])

    def test_r3_cli_set_then_show_carries_the_new_value(self):
        root = self.root / "cli-set"
        worker = TicketService(TicketStore(root)).staff_new(SLOT, "model-a")["员工名"]
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行改单",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--assign", worker, "--user-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        taskbook = root / f"{ticket_id}_命令行改单.md"
        taskbook.write_text("# 已写好\n", encoding="utf-8")
        path = str(taskbook.resolve())
        changed = self.cli(["set", ticket_id, "--taskbook", path, "--by", SLOT], root)
        self.assertEqual(0, changed.returncode, changed.stderr)
        self.assertIn("已改 任务书路径", changed.stdout)
        self.assertIn(f" claim {ticket_id} --by {worker}", changed.stdout)
        self.assertIn("【操作提示·只给设计者】新开线程,任务档 乙,模型你定,贴上面那句。", changed.stdout)
        self.assertEqual(path, json.loads(self.cli(["show", ticket_id], root).stdout)["任务书路径"])

    def test_r1_missing_taskbook_is_blocked_then_the_same_command_passes_after_write(self):
        root = self.root / "taskbook-gate"
        taskbook = root / "任务书" / "待写.md"
        command = [
            "new", "--type", "派单", "--slot", SLOT, "--title", "先写任务书再建单",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--by", SLOT,
            "--taskbook", str(taskbook), "--user-facing",
        ]
        refused = self.cli(command, root)
        self.assertEqual(2, refused.returncode)
        self.assertIn("任务书还没写，先把 md 落盘再建单", refused.stderr)
        self.assertIn(str(taskbook.resolve()), refused.stderr)

        taskbook.parent.mkdir(parents=True)
        taskbook.write_text("# 已写好\n", encoding="utf-8")
        created = self.cli(command, root)
        self.assertEqual(0, created.returncode, created.stderr)
        stored = json.loads(self.cli(["show", created_ticket_id(created)], root).stdout)
        self.assertEqual("已核存在", stored["任务书校验"])

    def test_r1_unchecked_escape_passes_and_logs_actor_time_command_and_path(self):
        root = self.root / "taskbook-unchecked"
        missing = root / "根本不存在.md"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "冒烟跳过任务书",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--by", SLOT,
            "--taskbook", str(missing), "--taskbook-unchecked", "--user-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        rows = [json.loads(line) for line in (root / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        audit = next(row for row in rows if row["事件"] == "taskbook-unchecked")
        self.assertTrue(audit["时间"])
        self.assertEqual(SLOT, audit["发言人"])
        self.assertEqual("new", audit["命令"])
        self.assertEqual(str(missing), audit["任务书绝对路径"])

    def test_r1_placeholder_existing_at_the_final_id_is_checked_after_creation(self):
        root = self.root / "taskbook-placeholder-existing"
        taskbook_dir = root / "任务书"
        taskbook_dir.mkdir(parents=True)
        (taskbook_dir / "T-000001_已经写好.md").write_text("# 已写好\n", encoding="utf-8")
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "拿号后文件已在",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--by", SLOT,
            "--taskbook", str(taskbook_dir / "{ticket}_已经写好.md"), "--user-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertEqual("T-000001", created_ticket_id(created))
        stored = json.loads(self.cli(["show", "T-000001"], root).stdout)
        self.assertEqual("已核存在", stored["任务书校验"])

    def test_r1_set_replaces_placeholder_before_checking_the_existing_file(self):
        root = self.root / "set-placeholder"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "改单替换占位符",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--by", SLOT, "--user-facing",
        ], root)
        ticket_id = created_ticket_id(created)
        taskbook_dir = root / "任务书"
        taskbook_dir.mkdir(parents=True)
        final_path = taskbook_dir / f"{ticket_id}_改单.md"
        final_path.write_text("# 已写好\n", encoding="utf-8")
        changed = self.cli([
            "set", ticket_id, "--taskbook", str(taskbook_dir / "{ticket}_改单.md"), "--by", SLOT,
        ], root)
        self.assertEqual(0, changed.returncode, changed.stderr)
        stored = json.loads(self.cli(["show", ticket_id], root).stdout)
        self.assertEqual(str(final_path), stored["任务书路径"])
        self.assertEqual("已核存在", stored["任务书校验"])
    def test_r4_a_fresh_ticket_can_be_voided_with_a_reason(self):
        ticket = self.dispatch("建错了的单")
        voided = self.service.void(ticket["编号"], "单号猜错了，重开一张", SLOT)
        self.assertEqual("作废", voided["状态"])
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("作废", stored["状态"])
        self.assertIn("单号猜错了", stored["流程提示"])
        row = [r for r in self.service.store.read_jsonl(self.service.store.log_path) if r["事件"] == "void"][-1]
        self.assertEqual((ticket["编号"], SLOT, "单号猜错了，重开一张"), (row["工单号"], row["发言人"], row["说明"]))
        self.assertIn("作废", model.DISPATCH_STATES)

    def test_r4_a_blocked_ticket_can_finally_be_voided_instead_of_hanging_forever(self):
        ticket = self.dispatch("挂着的阻塞单")
        self.service.block(ticket["编号"], "依赖的上游单也建错了")
        self.assertEqual("阻塞", self.service.store.load_ticket(ticket["编号"])["状态"])
        self.assertEqual("作废", self.service.void(ticket["编号"], "上游改了口径，这张不要了", "总编排")["状态"])

    def test_r4_a_submitted_ticket_cannot_be_voided(self):
        ticket = self.to_judging()
        with self.assertRaises(TicketError) as caught:
            self.service.void(ticket["编号"], "不想要了", SLOT)
        message = str(caught.exception)
        self.assertIn("现在是「待判」", message)
        self.assertIn("新建 / 已认领 / 阻塞", message)
        self.assertIn("交了板的活不许一笔勾销", message)
        self.assertEqual("待判", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_r4_void_needs_a_reason_and_the_owning_slot_or_the_conductor(self):
        ticket = self.dispatch("作废也有闸")
        with self.assertRaisesRegex(TicketError, "原因必填"):
            self.service.void(ticket["编号"], "   ", SLOT)
        with self.assertRaises(TicketError) as caught:
            self.service.void(ticket["编号"], "别位想废掉", OTHER_SLOT)
        self.assertIn("只有该位总监", str(caught.exception))
        self.assertEqual("新建", self.service.store.load_ticket(ticket["编号"])["状态"])
        self.service.void(ticket["编号"], "本位总监废掉", SLOT)
        with self.assertRaisesRegex(TicketError, "已经是「作废」"):
            self.service.void(ticket["编号"], "再废一次", SLOT)

    def test_r4_a_voided_ticket_cannot_be_edited_any_more(self):
        ticket = self.dispatch("作废之后不许再改")
        self.service.void(ticket["编号"], "建错了", SLOT)
        with self.assertRaisesRegex(TicketError, "现在是「作废」"):
            self.service.edit(ticket["编号"], SLOT, taskbook="D:/a.md")

    def test_r4_cli_void_then_show_and_list(self):
        root = self.root / "cli-void"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行作废",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--user-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        voided = self.cli(["void", ticket_id, "--reason", "建错了", "--by", SLOT], root)
        self.assertEqual(0, voided.returncode, voided.stderr)
        self.assertIn("作废", voided.stdout)
        self.assertEqual("作废", json.loads(self.cli(["show", ticket_id], root).stdout)["状态"])

    def test_r5_default_main_model_set_follows_the_v25_roster(self):
        slots = self.service.store.read_json(self.service.store.slots_path)
        self.assertEqual(["model-a", "model-b", "model-e"], slots["主力模型集合"])
        self.assertEqual("", self.service.staff_new(SLOT, "model-e")["提示"])

    def test_r5_an_old_store_missing_the_key_entirely_is_topped_up(self):
        """老库整格缺失才补默认值;**已有值一个字都不追改**。

        ★这是刻意的口径:名册建出来之后真源就是 slots.json,总监会在上面手改主力集合。
          若按配置回写,那些手改会在下一次任何命令跑起来时被悄悄冲掉——
          「配置文件改了一个字,线上名册全被重置」是这类迁移最典型的事故形态。
        """
        root = self.root / "老库"
        store = TicketStore(root)
        store.ensure()
        slots = store.read_json(store.slots_path)
        del slots["主力模型集合"]
        store.atomic_json(store.slots_path, slots)
        TicketService(TicketStore(root))
        self.assertEqual(list(config_module.MAIN_MODELS), store.read_json(store.slots_path)["主力模型集合"])

    def test_r5_a_hand_edited_main_model_set_is_left_alone(self):
        root = self.root / "手改过的库"
        store = TicketStore(root)
        store.ensure()
        slots = store.read_json(store.slots_path)
        slots["主力模型集合"] = ["model-b"]
        store.atomic_json(store.slots_path, slots)
        TicketService(TicketStore(root))
        self.assertEqual(["model-b"], store.read_json(store.slots_path)["主力模型集合"])

    def test_r5_non_main_model_notice_says_which_tiers_it_may_take(self):
        notice = self.service.staff_new(OTHER_SLOT, "model-c")["提示"]
        self.assertIn("不在主力模型名册里", notice)
        self.assertIn("当前主力是 model-a、model-b、model-e", notice)
        self.assertIn("只吃丙档", notice)
        self.assertIn("2000 行", notice)
        self.assertNotIn("['model-b', 'model-a']", notice)
        self.assertEqual("在岗", self.service.find_staff(f"{OTHER_SLOT}-01")[1]["状态"])
    def old_ticket_without_taskbook(self, title: str = "老单没有任务书路径"):
        """造一张 T-000075 之前建的单：文件里根本没有「任务书路径」这个键。"""
        ticket = self.dispatch(title)
        path = self.service.store.item_path(ticket["编号"])
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored.pop("任务书路径")
        path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        return ticket["编号"]

    def test_r6_an_old_ticket_without_the_taskbook_key_still_reads_and_writes(self):
        ticket_id = self.old_ticket_without_taskbook()
        loaded = self.service.store.load_ticket(ticket_id)
        self.assertNotIn("任务书路径", loaded)
        self.assertEqual("", loaded.get("任务书路径", ""))
        self.assertEqual(ticket_id, self.service.claim(ticket_id, self.worker)["编号"])
        path = r"C:\ticket-desk-workspace\_office\前端·界面与交互\任务书\%s_补路径.md" % ticket_id
        updated, changes = self.service.edit(ticket_id, SLOT, taskbook=path)
        self.assertEqual(path, updated["任务书路径"])
        self.assertEqual([{"字段": "任务书路径", "旧值": "", "新值": path}], changes)
        self.assertEqual(path, self.service.store.load_ticket(ticket_id)["任务书路径"])

    def test_r6_an_old_ticket_without_the_taskbook_key_can_still_be_voided_and_bundled(self):
        ticket_id = self.old_ticket_without_taskbook("老单也要能作废")
        self.assertEqual("作废", self.service.void(ticket_id, "老单建错了", "总编排")["状态"])
        bundle = self.service.build_bundle(self.root / "bundle.js")
        payload = json.loads(bundle.read_text(encoding="utf-8").split(" = ", 1)[1].rstrip(";\n"))
        row = next(item for item in payload["items"] if item["编号"] == ticket_id)
        self.assertEqual("作废", row["状态"])
        self.assertEqual("", row.get("任务书路径", ""))


class UserFacingFlagEditTests(TicketTestCase):
    """T-000514:建单标错「用户可感知」之后,总监要能就地改回来。

    T-000489:一张只改 UiP0Tests.cs 两行断言的内部单,建单时漏了 --internal,
    员工把活全干完、提交也推了,交板却被「必须附 主场景真登录图」的闸拦死。
    这个标记以前只能在 new 时定,set 改不了 —— 于是只剩两条路:作废重建(活白干一轮),
    或者拿一张无关截图糊弄闸(比卡住还坏)。两条都不该走。
    """

    def test_marking_internal_lets_the_same_submit_through_without_any_picture(self):
        ticket = self.dispatch("标错用户可感知的内部单")
        number = ticket["编号"]
        self.service.claim(number, self.worker)

        # 改之前:一张图都没有,交板被闸拦死 —— 这就是员工卡住的地方。
        with self.assertRaises(TicketError) as refused:
            self.service.submit(number, "只改测试断言", "dotnet test", "49/49 绿")
        self.assertIn("主场景", str(refused.exception))

        _, changes = self.service.edit(number, SLOT, internal=True)
        self.assertEqual(
            [{"字段": "非用户可感知", "旧值": False, "新值": True}], changes
        )

        # 改之后:同一条交板,不补任何截图,直接过。
        submitted = self.service.submit(number, "只改测试断言", "dotnet test", "49/49 绿")
        self.assertEqual("待判", submitted["状态"])
        self.assertEqual([], submitted["图片列表"])

    def test_marking_user_facing_again_puts_the_picture_gate_back(self):
        ticket = self.dispatch("改回用户可感知")
        number = ticket["编号"]
        self.service.claim(number, self.worker)
        self.service.edit(number, SLOT, internal=True)
        self.service.edit(number, SLOT, internal=False)

        with self.assertRaises(TicketError) as refused:
            self.service.submit(number, "改回来了", "dotnet test", "49/49 绿")
        self.assertIn("主场景", str(refused.exception))

    def test_only_the_owning_director_or_conductor_can_flip_the_flag(self):
        ticket = self.dispatch("员工不许自己改标记")
        number = ticket["编号"]
        self.service.claim(number, self.worker)
        with self.assertRaises(TicketError) as refused:
            self.service.edit(number, self.worker, internal=True)
        self.assertIn("只有该位总监", str(refused.exception))
        # 总编排可以。
        _, changes = self.service.edit(number, "总编排", internal=True)
        self.assertTrue(changes)

    def test_flipping_the_flag_is_written_to_the_event_line_with_both_values(self):
        """总编排点名要的:谁、何时、从什么改成什么,一条都不能少。"""
        ticket = self.dispatch("事件线要记全")
        number = ticket["编号"]
        self.service.edit(number, SLOT, internal=True)
        rows = [
            json.loads(line)
            for line in (self.service.store.root / "log.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        entry = next(
            row for row in rows
            if row.get("工单号") == number and "非用户可感知" in str(row.get("说明", ""))
        )
        self.assertEqual("set", entry["事件"])
        self.assertEqual(SLOT, entry["发言人"])
        self.assertIn("False → True", entry["说明"])
        self.assertTrue(entry["时间"])

    def test_the_cli_rejects_both_switches_at_once(self):
        root = self.root / "facing-cli"
        deliverable = root / "a.cs"
        deliverable.parent.mkdir(parents=True, exist_ok=True)
        deliverable.write_text("// x\n", encoding="utf-8")
        run_local_cli(["staff", "new", "--slot", SLOT, "--tool", "待定"], root)
        created = run_local_cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "互斥验证",
            "--assign", f"{SLOT}-01", "--consumer", "主场景/UiRoot",
            "--source", "DECISIONS.md:测试", "--tier", "乙",
            "--deliverable", str(deliverable), "--by", SLOT, "--user-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        number = created_ticket_id(created)
        both = run_local_cli(["set", number, "--internal", "--user-facing", "--by", SLOT], root)
        self.assertEqual(2, both.returncode)

        only_internal = run_local_cli(["set", number, "--internal", "--by", SLOT], root)
        self.assertEqual(0, only_internal.returncode, only_internal.stderr)
        self.assertIn("非用户可感知", only_internal.stdout)


class SetTierEditTests(TicketTestCase):
    """set --tier 甲|乙|丙(T-001560,T-001449 丙升乙)。

    任务书重写换了档,档位字段却改不了——卡片与开窗指令还按旧档走,
    设计者照旧档选模型。档位是总监的判断,允许就地改,记进改动日志。
    """

    def test_1_owner_changes_tier_and_it_lands_with_a_change_row(self):
        ticket = self.dispatch("任务书从丙重写成乙的单")
        self.service.claim(ticket["编号"], self.worker)
        # 建单夹具本来就是乙档;先把档真的改一次到丙,再验证能改回来,顺带拿到非平凡的旧值。
        updated, changes = self.service.edit(ticket["编号"], SLOT, task_tier="丙")
        self.assertEqual([{"字段": "任务档", "旧值": "乙", "新值": "丙"}], changes)
        updated, changes = self.service.edit(ticket["编号"], "总编排", task_tier="乙")
        self.assertEqual([{"字段": "任务档", "旧值": "丙", "新值": "乙"}], changes)
        self.assertEqual("乙", self.service.store.load_ticket(ticket["编号"])["任务档"])

    def test_2_other_slots_and_bad_values_are_refused(self):
        ticket = self.dispatch()
        with self.assertRaisesRegex(TicketError, "任务档只能是"):
            self.service.edit(ticket["编号"], SLOT, task_tier="丁")
        with self.assertRaises(TicketError):
            self.service.edit(ticket["编号"], OTHER_SLOT, task_tier="甲")
        # 被拦下时档位原样。
        self.assertEqual("乙", self.service.store.load_ticket(ticket["编号"])["任务档"])

    def test_3_judging_state_still_only_allows_assign(self):
        ticket = self.to_judging()
        with self.assertRaisesRegex(TicketError, "待判"):
            self.service.edit(ticket["编号"], SLOT, task_tier="甲")

    def test_4_no_editable_item_message_lists_tier(self):
        ticket = self.dispatch()
        with self.assertRaisesRegex(TicketError, "--tier 任务档"):
            self.service.edit(ticket["编号"], SLOT)


class UnrecognizedOptionHintTests(TicketTestCase):
    """「参数不对」多一行版本差提示(T-001560 ②,T-001124 上撞到)。"""

    def test_1_unknown_option_error_carries_the_pull_hint(self):
        from ticket_desk.ticket import parser as ticket_parser
        with self.assertRaisesRegex(TicketError, "git pull 主检出"):
            ticket_parser().parse_args(["set", "T-000001", "--no-such-option", "--by", "x"])

    def test_2_missing_required_argument_error_does_not_carry_the_hint(self):
        from ticket_desk.ticket import parser as ticket_parser
        with self.assertRaises(TicketError) as ctx:
            ticket_parser().parse_args(["show"])
        self.assertIn("缺少必填参数", str(ctx.exception))
        self.assertNotIn("git pull", str(ctx.exception))

    def test_3_degradation_matcher_accepts_both_old_and_new_error_formats(self):
        from ticket_desk.remote import RemoteClient
        old_format = "ticket.py 参数不对，请检查命令写法。"
        new_format = (
            "ticket.py 参数不对，请检查命令写法。"
            "★若是新参数而本机检出较旧：工单台参数常先上服后并 main，服务端可能已认"
            "(env --probe 可核服务端协议)，git pull 主检出后再试。"
        )
        self.assertTrue(RemoteClient._is_unrecognized_option_error(old_format))
        self.assertTrue(RemoteClient._is_unrecognized_option_error(new_format))
        self.assertFalse(RemoteClient._is_unrecognized_option_error("ticket.py 缺少必填参数：ticket。"))

class ModelBanNotifyOnlyTests(TicketTestCase):
    """D9-388 / T-000529 乙:自动停用关掉,到阈值只通知。

    今天工具两次把 sol 全项目停掉:8 次判退里 2 次判语明写出题责任、3 次是 sol-high/sol-ultra
    变体被归并——停用是误判,而一停就是所有 sol 员工窗全停。总编排答:宪法八章 10 的「停用」是
    「总编排落笔」,工具自动写 bans 是实现时加的,关掉不必修宪。
    """

    def rework_once(self, title: str, verdict: str = "模型责任:验收不过") -> dict:
        ticket = self.dispatch(title)
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture(f"{title}.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "登录后界面已出现")
        judged, _ = self.service.judge(ticket["编号"], False, SLOT, "返工", verdict)
        return judged

    def thread_lines(self, slot: str) -> str:
        path = self.service.store.root / "threads" / f"{slot}.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_reaching_threshold_writes_no_ban_and_notifies_conductor_and_owner(self):
        # R1.5(T-000794)之后主力模型(model-a/model-b/model-e)到线只作质量提示;这里用非主力模型钉「原样措辞」。
        worker = self.service.staff_new(SLOT, "model-c")["员工名"]
        for index in range(3):  # 同位阈值 3
            ticket = self.dispatch(f"判退{index}", assign=worker)
            self.service.claim(ticket["编号"], worker)
            self.service.attach(ticket["编号"], str(self.picture(f"判退{index}.png")), "world", worker)
            self.service.submit(ticket["编号"], "登录后界面已出现")
            self.service.judge(ticket["编号"], False, SLOT, "返工", "模型责任:验收不过")
        staff = self.service.store.load_staff()
        bans = staff.get("模型停用", {})
        self.assertEqual([], bans.get("全项目", []))
        self.assertEqual([], bans.get("按位", {}).get(SLOT, []))
        # 记分照记,只是不再写停用
        # 实际模型没填 → 记成 <工具>-未标,不并进工具名(T-000528 R2③)
        self.assertEqual(3, staff["模型记分"]["model-c-未标"][SLOT])
        for slot in ("总编排", SLOT):
            text = self.thread_lines(slot)
            self.assertIn("再判退 1 次就到停用线", text)
            self.assertIn("已到停用线", text)
            self.assertIn("自动停用已关", text)
        # 开窗仍然不被拦
        ticket = self.dispatch("停用线之后仍能开窗", assign=worker)
        self.service.open_window(ticket["编号"], "设计者", "model-c")

    def test_verdict_headed_by_authoring_fault_is_not_counted_against_the_model(self):
        judged = self.rework_once("出题责任的判退", "出题责任:任务书把键名写错了,执行方照做无误")
        staff = self.service.store.load_staff()
        model = self.service.find_staff(self.worker)[1]["工具/窗类型"].strip().lower()
        self.assertNotIn(model, staff.get("模型记分", {}))
        self.assertEqual("出题", judged["返工原因列表"][-1]["责任"])

    def test_void_on_rework_state_needs_the_follow_up_ticket_number(self):
        judged = self.rework_once("母单")
        with self.assertRaises(TicketError):
            self.service.void(judged["编号"], "不要了", SLOT)
        voided = self.service.void(judged["编号"], "窗已关,续单 T-000999 接手(宪法十章 6)", SLOT)
        self.assertEqual("作废", voided["状态"])
        self.assertEqual(1, voided["返工次数"])  # 母单返工次数保留,计入模型合格率

class ManualStaffBanTests(TicketTestCase):
    """T-000637 R1:D9-388 乙口径缺的后半截——到停用线只通知,停不停由人手工落 staff ban。

    自动停用关掉之后，bans 里只会有人手工写的项；没有这条命令，「停用」这一半就只是嘴上说说。
    R1.5(T-000794)之后主力模型(model-a/model-b/model-e)只有设计者能 ban,所以本类里总编排落笔的
    场景一律改用非主力模型 model-c;主力模型的闸本身由 BanLineCountTests.test_r3_1 钉。
    """

    def thread_lines(self, slot: str) -> str:
        path = self.service.store.root / "threads" / f"{slot}.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def events(self, name: str) -> list[dict]:
        return [row for row in self.service.store.read_jsonl(self.service.store.log_path) if row.get("事件") == name]

    def test_1_conductor_ban_blocks_open_window_and_staff_new(self):
        """①总编排 ban 非主力模型之后,两条开窗路径都被拦。"""
        self.service.staff_ban("model-c", "总编排", reason="D9-388 核过责任归属,确属模型责任")
        ticket = self.dispatch("停用后不该能开窗")
        with self.assertRaises(TicketError) as caught:
            self.service.open_window(ticket["编号"], "设计者", "model-c")
        self.assertIn("已被手工停用（全项目）", str(caught.exception))
        self.assertIn("staff unban --tool model-c", str(caught.exception))
        with self.assertRaises(TicketError) as caught_new:
            self.service.staff_new(SLOT, "model-c")
        self.assertIn("已被手工停用（全项目）", str(caught_new.exception))

    def test_1b_slot_scoped_ban_only_blocks_that_slot(self):
        self.service.staff_ban("model-a", "设计者", SLOT, "这一位连续判退,先只停这一位")
        with self.assertRaises(TicketError) as caught:
            self.service.staff_new(SLOT, "model-a")
        self.assertIn(f"已被手工停用（限“{SLOT}”）", str(caught.exception))
        # 别位不受影响
        self.assertTrue(self.service.staff_new(OTHER_SLOT, "model-a")["员工名"])

    def test_2_staff_and_other_directors_cannot_ban(self):
        """②员工/别位总监 ban 被拒——停用是人的动作,而且只有那两个人能落笔。"""
        for actor in (self.worker, SLOT, "前端·视觉与资源", "工单台"):
            with self.assertRaises(TicketError) as caught:
                self.service.staff_ban("model-a", actor, reason="我觉得该停")
            self.assertIn("只有设计者或总编排可以停用模型", str(caught.exception))
        self.assertEqual([], self.service.store.load_staff()["模型停用"]["全项目"])

    def test_3_ban_writes_one_event_line_and_one_conductor_line(self):
        """③事件线与总编排线各一条,原因与解禁命令都写在里面。"""
        detail = self.service.staff_ban("Model  C Middle", "总编排", reason="三次判退全是模型责任")
        self.assertIn("模型 model-c-middle 已手工停用（全项目）", detail)
        self.assertIn("原因：三次判退全是模型责任", detail)
        self.assertIn("解禁 staff unban --tool model-c-middle --by 总编排", detail)
        rows = self.events("staff-ban")
        self.assertEqual(1, len(rows))
        self.assertEqual("总编排", rows[0]["发言人"])
        self.assertEqual(detail, rows[0]["说明"])
        conductor = [line for line in self.thread_lines("总编排").splitlines() if "已手工停用" in line]
        self.assertEqual(1, len(conductor))
        # 模型名先 normalize:写进 bans 的是记账键,不是用户敲的原样
        self.assertEqual(["model-c-middle"], self.service.store.load_staff()["模型停用"]["全项目"])

    def test_3b_ban_without_reason_is_rejected(self):
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("model-c", "总编排", reason="   ")
        self.assertIn("必须写原因", str(caught.exception))
        self.assertEqual([], self.events("staff-ban"))

    def test_4_banning_twice_is_rejected(self):
        """④重复 ban 被拒,且不写第二条事件线。"""
        self.service.staff_ban("model-c", "总编排", reason="第一次")
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("MODEL-C", "总编排", reason="第二次")
        self.assertIn("已在停用中（全项目）", str(caught.exception))
        # 全项目已停时,再按位停也拦下:范围更大的那一条已经生效
        with self.assertRaises(TicketError) as narrower:
            self.service.staff_ban("model-c", "总编排", SLOT, "再按位停一次")
        self.assertIn("已在停用中（全项目）", str(narrower.exception))
        self.assertEqual(["model-c"], self.service.store.load_staff()["模型停用"]["全项目"])
        self.assertEqual(1, len(self.events("staff-ban")))

    def test_5_unban_restores_both_paths(self):
        """⑤unban 之后 staff new 与 open_window 都恢复。主力模型由设计者落 ban(R1.5)。"""
        self.service.staff_ban("model-a", "设计者", reason="先停")
        self.service.staff_unban("model-a", "总编排")
        self.assertTrue(self.service.staff_new(SLOT, "model-a")["员工名"])
        ticket = self.dispatch("解禁后应能开窗")
        self.service.open_window(ticket["编号"], "设计者", "model-a")
        self.assertEqual("model-a", self.service.store.load_ticket(ticket["编号"])["实际模型"])

    def test_6_unknown_slot_is_rejected(self):
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("model-c", "总编排", "不存在的位", "理由")
        self.assertIn("总监位不在名册里", str(caught.exception))

    def test_7_cli_exposes_ban_with_the_same_permission_gate(self):
        """命令行这一层也要通:总编排能停非主力模型,员工被拒,--reason 必填。"""
        root = self.root / "cli-ban"
        ok = run_local_cli(
            ["staff", "ban", "--tool", "model-c", "--by", "总编排", "--reason", "核过责任归属,确属模型责任"], root,
        )
        self.assertEqual(0, ok.returncode, ok.stderr)
        self.assertIn("已手工停用（全项目）", ok.stdout)
        refused = run_local_cli(["staff", "ban", "--tool", "model-c", "--by", "UI总监", "--reason", "我要停"], root)
        self.assertNotEqual(0, refused.returncode)
        missing = run_local_cli(["staff", "ban", "--tool", "model-c", "--by", "总编排"], root)
        self.assertNotEqual(0, missing.returncode)
        self.assertIn("--reason", missing.stderr + missing.stdout)


class TicketDeliverableCreationGateTests(TicketTestCase):
    """T-000267 R1：建单/改单时就在操作人的检出里核交付项。"""

    def cli(self, arguments: list[str], root: Path):
        return run_local_cli(arguments, root)

    def new_command(self, deliverables: list[str], *extra: str) -> list[str]:
        command = [
            "new", "--type", "派单", "--slot", SLOT, "--title", "交付项前置闸",
            "--source", "AGENTS.md:工单制", "--consumer", "工单台", "--tier", "乙", "--by", SLOT,
            "--user-facing",
        ]
        for row in deliverables:
            command.extend(["--deliverable", row])
        return [*command, *extra]

    def test_r1_narrative_deliverable_is_blocked(self):
        root = self.root / "narrative-deliverable"
        refused = self.cli(self.new_command(["交付报告已经写完了"]), root)
        self.assertEqual(2, refused.returncode)
        self.assertIn("交付项要写成仓内相对路径或真实文件路径,叙述句永远交不了板", refused.stderr)
        self.assertIn("这一条:交付报告已经写完了", refused.stderr)

    def test_r2_absent_path_is_only_a_reminder_and_the_ticket_still_gets_built(self):
        """T-000447/T-000458:派单的交付项按定义就是还没产出的东西,建单时不许拦。

        旧行为是退 2 拦死,于是各位只能造空占位文件绕过;而占位件一存在,
        submit 那条真闸就永远核得过——闸被绕成了摆设,副作用比闸本身还坏。
        """
        root = self.root / "branch-only-deliverable"
        branch_only = root / "deploy" / "branch-only.sh"
        created = self.cli(self.new_command([str(branch_only)]), root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertTrue(created_ticket_id(created))
        self.assertIn("以下交付项现在还不存在", created.stderr)
        self.assertIn(str(branch_only.resolve()), created.stderr)
        # 只在分支上的部署件那半句要留着：T-000215 就是这么栽的，提醒里必须还说得出。
        self.assertIn("如果它只会存在于某个分支上(比如只推 hk 的部署件)", created.stderr)

    def test_r2_absent_chinese_path_with_narrative_marker_is_not_called_a_narrative(self):
        """T-000458 缺陷B:带「的」的合法中文路径,在派单场景 100% 被误判成叙述句。

        旧写法里「文件存在就跳过叙述词启发式」的保护对派单永远不成立——
        派单交付项本来就还没产出。两条路径只差一个「的」字，报错话术却完全不同：
        「…还没做出来的表.csv」报叙述句(错方向)，「…尚未产出.csv」报路径不存在(对方向)。
        各位办公目录清一色中文，这条是人人会撞。
        """
        root = self.root / "absent-chinese-deliverable"
        with_marker = root / "资产清单" / "还没做出来的表.csv"
        without_marker = root / "资产清单" / "尚未产出.csv"

        created = self.cli(self.new_command([str(with_marker)]), root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertNotIn("叙述句永远交不了板", created.stderr)
        self.assertIn("以下交付项现在还不存在", created.stderr)

        # 只差一个「的」字的对照组，两条必须走同一档。
        sibling = self.cli(self.new_command([str(without_marker)]), root)
        self.assertEqual(0, sibling.returncode, sibling.stderr)
        self.assertNotIn("叙述句永远交不了板", sibling.stderr)

    def test_r1_narrative_with_extension_but_no_separator_is_still_blocked(self):
        """放宽只到「带分隔符的当路径」为止;没有分隔符的仍按叙述标记硬拦。"""
        root = self.root / "narrative-with-extension"
        refused = self.cli(self.new_command(["做出对照表,并核对.csv"]), root)
        self.assertEqual(2, refused.returncode)
        self.assertIn("交付项要写成仓内相对路径或真实文件路径,叙述句永远交不了板", refused.stderr)

    def test_r2_existing_deliverable_prints_no_reminder_at_all(self):
        root = self.root / "existing-deliverable-quiet"
        deliverable = root / "review" / "result.md"
        deliverable.parent.mkdir(parents=True)
        deliverable.write_text("# result\n", encoding="utf-8")
        created = self.cli(self.new_command([str(deliverable)]), root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertNotIn("以下交付项现在还不存在", created.stderr)

    def test_r1_same_command_keeps_working_before_and_after_the_file_appears(self):
        root = self.root / "real-deliverable"
        deliverable = root / "review" / "result.md"
        command = self.new_command([str(deliverable)])
        # 做出来之前：建得出来，但要有提醒。
        before = self.cli(command, root)
        self.assertEqual(0, before.returncode, before.stderr)
        self.assertIn("以下交付项现在还不存在", before.stderr)

        deliverable.parent.mkdir(parents=True)
        deliverable.write_text("# result\n", encoding="utf-8")
        created = self.cli(command, root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertNotIn("以下交付项现在还不存在", created.stderr)

        replacement = root / "review" / "replacement.md"
        replacement.write_text("# replacement\n", encoding="utf-8")
        changed = self.cli([
            "set", created_ticket_id(created), "--deliverable", str(replacement), "--by", SLOT,
        ], root)
        self.assertEqual(0, changed.returncode, changed.stderr)

    def test_r1_real_path_with_narrative_marker_is_not_misclassified(self):
        root = self.root / "real-chinese-deliverable"
        deliverable = root / "真源" / "设计者的意见.md"
        deliverable.parent.mkdir(parents=True)
        deliverable.write_text("# 真文件\n", encoding="utf-8")

        created = self.cli(self.new_command([str(deliverable)]), root)
        self.assertEqual(0, created.returncode, created.stderr)

    def test_r1_unchecked_escape_passes_and_logs_every_skipped_row(self):
        root = self.root / "deliverable-unchecked"
        rows = ["叙述句交付项", str(root / "missing" / "ghost.md")]
        created = self.cli(self.new_command(rows, "--deliverable-unchecked"), root)
        self.assertEqual(0, created.returncode, created.stderr)
        log_rows = [json.loads(line) for line in (root / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        audit = next(row for row in log_rows if row["事件"] == "deliverable-unchecked")
        self.assertTrue(audit["时间"])
        self.assertEqual(SLOT, audit["发言人"])
        self.assertEqual("new", audit["命令"])
        self.assertEqual(rows, audit["跳过的交付项"])
        self.assertEqual(2, len(audit["核验绝对路径"]))


class WakeNotifyTests(TicketTestCase):
    """T-000269:跨位动作要往对方对话线写一行,否则设计者队列的唤醒段不亮。

    设计者只看那一段;不亮 = 那扇窗永远不知道有事等它(总监位的窗口不会自己醒)。
    """

    OTHER = "平台·工单系统"
    REVIEW = "复检·合并与部署"

    def thread(self, slot):
        return self.service.store.read_jsonl(self.service.store.thread_path(slot))

    def test_new_dispatch_writes_one_unread_line_into_the_owner_thread(self):
        before = len(self.thread(SLOT))
        ticket = self.dispatch("建单要能唤醒")
        rows = self.thread(SLOT)
        self.assertEqual(before + 1, len(rows))
        self.assertIn(ticket["编号"], rows[-1]["文字"])
        self.assertIn("建单要能唤醒", rows[-1]["文字"])
        self.assertEqual([], rows[-1]["已读标记"])
        self.assertEqual(ticket["编号"], rows[-1]["引用工单号"])

    def test_every_ask_type_writes_a_line_into_the_owner_thread(self):
        for kind in ("疑问", "需求", "阻塞", "拍板"):
            with self.subTest(kind=kind):
                before = len(self.thread(SLOT))
                body = VALID_DECISION_BODY if kind == "拍板" else f"{kind}正文"
                ticket = self.service.create_question(kind, SLOT, f"{kind}要能唤醒", body, self.OTHER)
                rows = self.thread(SLOT)
                self.assertEqual(before + 1, len(rows))
                self.assertIn(ticket["编号"], rows[-1]["文字"])
                self.assertEqual([], rows[-1]["已读标记"])

    def test_submit_wakes_the_owner_slot_to_judge(self):
        """员工交板 = 球传给总监。撞到过:3 张待判、0 个唤醒提示。"""
        ticket = self.dispatch("交板要能唤醒总监")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        before = len(self.thread(SLOT))
        self.service.submit(ticket["编号"], "登录后可见")
        rows = self.thread(SLOT)
        self.assertEqual(before + 1, len(rows))
        self.assertIn("交板", rows[-1]["文字"])
        self.assertIn("等你判卷", rows[-1]["文字"])
        self.assertEqual(self.worker, rows[-1]["发言人"])
        self.assertNotEqual(SLOT, rows[-1]["发言人"])

    def test_judge_pass_wakes_the_review_slot(self):
        ticket = self.to_judging()
        before = len(self.thread(self.REVIEW))
        self.service.judge(ticket["编号"], True, SLOT, verdict="用户怎么打开它:登录后在主城能看到")
        rows = self.thread(self.REVIEW)
        self.assertEqual(before + 1, len(rows))
        self.assertIn("判过", rows[-1]["文字"])
        self.assertIn("等你复验", rows[-1]["文字"])

    def test_merge_wakes_whoever_deploys(self):
        ticket = self.to_judging()
        self.service.judge(ticket["编号"], True, SLOT, verdict="用户怎么打开它:登录后在主城能看到")
        before_owner, before_review = len(self.thread(SLOT)), len(self.thread(self.REVIEW))
        self.merged_ticket(ticket["编号"], self.REVIEW)
        self.assertEqual(before_owner + 1, len(self.thread(SLOT)))
        self.assertEqual(before_review + 1, len(self.thread(self.REVIEW)))
        self.assertIn("等上服", self.thread(SLOT)[-1]["文字"])

    def test_block_wakes_the_conductor(self):
        ticket = self.dispatch("阻塞要能唤醒总编排")
        before = len(self.thread("总编排"))
        self.service.block(ticket["编号"], "等素材", SLOT)
        rows = self.thread("总编排")
        self.assertEqual(before + 1, len(rows))
        self.assertIn("挂起", rows[-1]["文字"])

    def test_answer_writes_a_line_back_to_the_slot_that_asked(self):
        ticket = self.service.create_question("疑问", SLOT, "问一句", "正文", self.OTHER)
        before = len(self.thread(SLOT))
        self.service.answer(ticket["编号"], "答一句", SLOT)
        rows = self.thread(SLOT)
        self.assertEqual(before + 1, len(rows))
        self.assertIn("答了", rows[-1]["文字"])
        self.assertIn(ticket["编号"], rows[-1]["文字"])

    def test_transfer_still_writes_exactly_one_line_per_side(self):
        ticket = self.dispatch("转交回归")
        before_here, before_there = len(self.thread(SLOT)), len(self.thread(self.OTHER))
        self.service.transfer(ticket["编号"], self.OTHER, "转过去", SLOT)
        self.assertEqual(before_here + 1, len(self.thread(SLOT)))
        self.assertEqual(before_there + 1, len(self.thread(self.OTHER)))
        self.assertIn("转交", self.thread(SLOT)[-1]["文字"])

    def test_transfer_to_the_same_slot_is_not_written_twice(self):
        ticket = self.dispatch("转给自己")
        before = len(self.thread(SLOT))
        self.service.transfer(ticket["编号"], SLOT, "原地转交", SLOT)
        self.assertEqual(before + 1, len(self.thread(SLOT)))

    def test_the_speaker_is_the_actor_so_the_target_slot_counts_it_as_unread(self):
        """前端 slotUnreadForOwner 的过滤条件是「发言人 !== 该位」。

        通知的发言人若写成目标位自己,这一行永远不算它的未读,等于没写。
        """
        ticket = self.service.create_question("疑问", SLOT, "别位来的单", "正文", self.OTHER)
        row = self.thread(SLOT)[-1]
        self.assertEqual(self.OTHER, row["发言人"])
        self.assertNotEqual(SLOT, row["发言人"])
        self.assertEqual(ticket["所属总监位"], SLOT)
        # 自己给自己位建单:发言人本来就等于该位,那一行不计未读是对的,不需要唤醒自己。
        self.service.create_dispatch(
            SLOT, "自己给自己建", ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
            initiator=SLOT, task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
        )
        self.assertEqual(SLOT, self.thread(SLOT)[-1]["发言人"])



class SubmitEvidenceTests(TicketTestCase):
    """T-000315:用户可感知派单的验证命令与原样输出以前被静默丢弃。

    在 T-000201 上撞到:传了两个参数、submit 成功、状态到待判,
    show 出来两字段却是空的——判卷人看不到执行方跑了什么。
    """

    def test_user_facing_ticket_keeps_verify_command_and_raw_output(self):
        ticket = self.dispatch("用户可感知也要留住证据")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        ticket = self.service.submit(
            ticket["编号"], "登录后主城可见",
            verify_command="python -m pytest -q", raw_output="3 passed",
        )
        self.assertEqual("python -m pytest -q", ticket["接线证据"]["验证命令"])
        self.assertEqual("3 passed", ticket["接线证据"]["原样输出"])
        self.assertEqual("待判", ticket["状态"])

    def test_user_facing_ticket_still_needs_a_world_image(self):
        """用户可感知单的硬闸是图片,不是文字证据——放宽保存不等于放宽闸。"""
        ticket = self.dispatch("缺图仍要拦")
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError):
            self.service.submit(ticket["编号"], "登录后主城可见", verify_command="x", raw_output="y")

    def test_user_facing_ticket_without_the_two_fields_still_passes(self):
        """不传两字段不该报错:那是内部工具单的闸,不是用户可感知单的。"""
        ticket = self.dispatch("不传两字段也能交板")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        ticket = self.service.submit(ticket["编号"], "登录后主城可见")
        self.assertEqual("待判", ticket["状态"])
        self.assertEqual("", ticket["接线证据"]["验证命令"])

    def test_internal_ticket_still_requires_both_fields(self):
        ticket = self.service.create_dispatch(
            SLOT, "内部工具单闸不变", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError):
            self.service.submit(ticket["编号"], "验证完成", verify_command="only-one")


class WindowHintTests(TicketTestCase):
    """D9-408 / 宪法 v2.18:开窗工具建议写在派单标题开头的【claude】【codex】【vscode】【zcode】里。

    起因:图标位有一张单在 claude 窗认领之后才发现那个窗生不了图,整轮白跑。
    ★建议不是硬闸——填错才拒,留空、写不认识的标签一律照常建单;
    ★真源只有标题一处,单上的「建议窗口」是解析出来的派生值,读一次就按标题重算一次;
    ★开窗指令三行一个平台名都不许出现(v2.18 把往操作提示那一行注入建议的做法撤了)。
    """

    # 开窗指令头两行里可能天生带平台名:CLI 路径跟着本仓走,仓目录名里就可能含「claude」这类字样。
    # 所以逐名 assertNotIn 之前必须先把这两截与建议窗口无关的固定文本换成占位符,否则用例永远假红。
    CLI_LITERAL = config_module.cli_path()

    def dispatch_with_taskbook(self, title: str, window: str = "", internal: bool = True):
        # 任务书文件名故意不跟标题走:标题里带平台名时,路径会原样进开窗指令第二行,
        # 把「三行不许出现平台名」的用例判成假红。
        self.serial = getattr(self, "serial", 0) + 1
        taskbook = self.root / f"tb-{self.serial}.md"
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)],
            taskbook=str(taskbook), window=window, internal=internal,
        )

    def scrubbed_lines(self, ticket) -> list[str]:
        """把与建议窗口无关的固定文本换成占位符,剩下的才是这三行自己写的字。"""
        lines = self.service.dispatch_instructions(ticket)
        taskbook = str(ticket["任务书路径"])
        return [line.replace(self.CLI_LITERAL, "<CLI>").replace(taskbook, "<任务书>") for line in lines]

    def test_r4_1_each_of_the_four_tags_is_parsed_off_the_title(self):
        """四个合法标签各建一张单,解析出来的建议窗口都对。"""
        for platform in model.WINDOW_PLATFORMS:
            with self.subTest(platform=platform):
                ticket = self.dispatch_with_taskbook(f"【{platform}】接入角色面板")
                self.assertEqual(f"【{platform}】接入角色面板", ticket["标题"])
                self.assertEqual(platform, ticket["建议窗口"])
                self.assertEqual(platform, self.service.store.load_ticket(ticket["编号"])["建议窗口"])
        # 「两处存同一件事必然会漂」的那道防线:盘上的「建议窗口」只是派生缓存,
        # 手工塞一个跟标题对不上的值进去,读一次就被标题重新算平——真源永远只有标题。
        ticket = self.dispatch_with_taskbook("【zcode】只认标题")
        poisoned = self.service.store.load_ticket(ticket["编号"])
        poisoned["建议窗口"] = "codex"
        self.service.store.save_ticket(poisoned, "set", SLOT, "手工塞一个漂掉的派生值")
        self.assertEqual("zcode", self.service.store.load_ticket(ticket["编号"])["建议窗口"])

    def test_r4_2_notepad_and_cursor_are_refused_and_the_message_lists_all_four(self):
        """--window 给不认识的值要拒,报错里列得出四个合法值;cursor 单独钉一遍。"""
        for bad in ("notepad", "cursor"):
            with self.subTest(bad=bad):
                with self.assertRaises(TicketError) as caught:
                    self.dispatch_with_taskbook(f"拒非法值-{bad}", bad)
                message = str(caught.exception)
                self.assertIn(bad, message)
                for platform in model.WINDOW_PLATFORMS:
                    self.assertIn(platform, message)
        # v2.18 把 cursor 换成了 vscode:取值表本身也钉住,免得哪天又被悄悄加回去。
        self.assertNotIn("cursor", model.WINDOW_PLATFORMS)
        self.assertIn("vscode", model.WINDOW_PLATFORMS)
        # 标题里手写【cursor】已经不是合法标签,但建议不是硬闸:解析不出来,也不许拦住建单。
        legacy = self.dispatch_with_taskbook("【cursor】老写法照样建得出来")
        self.assertEqual("", legacy["建议窗口"])
        self.assertEqual("新建", legacy["状态"])

    def test_r4_3_a_title_without_a_tag_creates_fine_with_an_empty_hint(self):
        """标题不带前缀的单,标签为空且照常建得出来。"""
        for title in ("没有前缀的普通标题", "【notepad】不认识的标签也不拦"):
            with self.subTest(title=title):
                ticket = self.dispatch_with_taskbook(title)
                self.assertEqual(title, ticket["标题"])
                self.assertEqual("", ticket["建议窗口"])
                self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["建议窗口"])
                self.assertEqual("新建", ticket["状态"])

    def test_r4_4_window_writes_the_prefix_into_the_title_and_replaces_it(self):
        """--window 是「替你把前缀写进标题」的快捷方式;已有前缀是替换不是叠加。"""
        ticket = self.dispatch_with_taskbook("接入角色面板", "codex")
        self.assertEqual("【codex】接入角色面板", ticket["标题"])
        self.assertEqual("codex", ticket["建议窗口"])
        updated, _ = self.service.edit(ticket["编号"], SLOT, window="zcode")
        self.assertEqual("【zcode】接入角色面板", updated["标题"])
        self.assertEqual(1, updated["标题"].count("【"))
        self.assertEqual("zcode", updated["建议窗口"])
        # 建单时标题里已经带了一个前缀,--window 又给一个:仍然只留一个,以 --window 为准。
        both = self.dispatch_with_taskbook("【claude】两处都给了", "vscode")
        self.assertEqual("【vscode】两处都给了", both["标题"])
        self.assertEqual(1, both["标题"].count("【"))
        # 给空串就是撤回建议:前缀连标签一起去掉,标题回到没有标签的样子。
        cleared, _ = self.service.edit(ticket["编号"], SLOT, window="")
        self.assertEqual("接入角色面板", cleared["标题"])
        self.assertEqual("", cleared["建议窗口"])

    def test_r4_5_the_three_lines_never_carry_a_platform_name(self):
        """开窗指令三行一个字都不含工具名——工具名只在标题开头这一处(v2.18)。"""
        for platform in model.WINDOW_PLATFORMS:
            with self.subTest(platform=platform):
                ticket = self.dispatch_with_taskbook(f"【{platform}】三行不许带平台名")
                scrubbed = self.scrubbed_lines(ticket)
                self.assertEqual(3, len(scrubbed))
                for line in scrubbed:
                    for name in model.WINDOW_PLATFORMS:
                        self.assertNotIn(name, line, line)
                    self.assertNotIn("建议窗口", line, line)
                # 对照组:同一张单把标签清成空再生成一遍,三行必须逐字相等。
                # 标题前缀和派生字段两头都清掉,注入不管从哪一头读都会在这里露馅;
                # 单号、员工名、任务档、任务书路径全都不动,差异只可能来自建议窗口。
                cleared = dict(ticket, 标题=model.strip_window_prefix(ticket["标题"]), 建议窗口="")
                self.assertEqual(
                    self.service.dispatch_instructions(cleared),
                    self.service.dispatch_instructions(ticket),
                )

    def test_r4_6_set_window_changes_the_tag_and_stays_shut_while_judging(self):
        """set --window 能改已建单的标签;待判态被现有可改态闸拦下。"""
        ticket = self.dispatch_with_taskbook("【claude】改标签")
        updated, changes = self.service.edit(ticket["编号"], SLOT, window="zcode")
        self.assertEqual("zcode", updated["建议窗口"])
        self.assertEqual("zcode", self.service.store.load_ticket(ticket["编号"])["建议窗口"])
        # 真源是标题,所以改动清单上写的也是标题——不再有第二个字段各记一笔。
        self.assertEqual(
            [{"字段": "标题", "旧值": "【claude】改标签", "新值": "【zcode】改标签"}], changes,
        )
        # 待判态照旧只放行 --assign:判卷人正看着这张单,别替别人改口径。
        pending = self.dispatch_with_taskbook("【claude】待判态不放开", internal=False)
        self.service.claim(pending["编号"], self.worker)
        self.service.attach(pending["编号"], str(self.picture()), "world", self.worker)
        self.assertEqual("待判", self.service.submit(pending["编号"], "登录后主城可见")["状态"])
        with self.assertRaises(TicketError) as blocked:
            self.service.edit(pending["编号"], SLOT, window="codex")
        self.assertIn("待判", str(blocked.exception))
        # 命令行那一头也要真的认这个开关:argparse 少写一行,服务端做对了也用不上。
        root = self.root / "cli-window"
        worker = TicketService(TicketStore(root)).staff_new(SLOT, "model-a")["员工名"]
        created = run_local_cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行建议窗口",
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--assign", worker,
            "--window", "codex", "--internal",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        changed = run_local_cli(["set", ticket_id, "--window", "vscode", "--by", SLOT], root)
        self.assertEqual(0, changed.returncode, changed.stderr)
        shown = json.loads(run_local_cli(["show", ticket_id], root).stdout)
        self.assertEqual("【vscode】命令行建议窗口", shown["标题"])
        self.assertEqual("vscode", shown["建议窗口"])
        refused = run_local_cli(["set", ticket_id, "--window", "cursor", "--by", SLOT], root)
        self.assertEqual(2, refused.returncode)
        for platform in model.WINDOW_PLATFORMS:
            self.assertIn(platform, refused.stdout + refused.stderr)

class DispatchFacingRequiredTests(TicketTestCase):
    """T-000831:建派单必须二选一标可感知;交板被图片闸拦下时报错给出解法;设计者可单独翻这一个开关。"""

    def new_dispatch_argv(self, title: str, *extra: str) -> list[str]:
        return [
            "new", "--type", "派单", "--slot", SLOT, "--title", title,
            "--source", "DECISIONS.md:测试", "--consumer", "主场景/UiRoot", "--tier", "乙",
            "--deliverable", str(self.deliverable), *extra,
        ]

    def test_r1_dispatch_without_facing_flag_is_rejected_explaining_both(self):
        """两个开关都不给 → 拒,报错里两个开关的含义都要出现。"""
        result = run_local_cli(self.new_dispatch_argv("可感知漏标"), self.root / "facing-missing")
        self.assertEqual(2, result.returncode)
        self.assertIn("内部工具单", result.stderr)
        self.assertIn("验证命令与原样输出", result.stderr)
        self.assertIn("用户可感知单", result.stderr)
        self.assertIn(f"{model.LIVE_ORIGIN}图", result.stderr)
        self.assertIn("二选一", result.stderr)

    def test_r1_dispatch_with_both_facing_flags_is_rejected(self):
        """两个都给 → 拒(argparse 互斥组当场拦)。"""
        result = run_local_cli(
            self.new_dispatch_argv("可感知都给", "--internal", "--user-facing"), self.root / "facing-both",
        )
        self.assertEqual(2, result.returncode)

    def test_r1_single_facing_flag_creates_dispatch_with_matching_flag(self):
        """只给 --internal → 非用户可感知=True;只给 --user-facing → False。"""
        root = self.root / "facing-single"
        created = run_local_cli(self.new_dispatch_argv("内部单", "--internal"), root)
        self.assertEqual(0, created.returncode, created.stderr)
        internal_id = created_ticket_id(created)
        self.assertTrue(json.loads(run_local_cli(["show", internal_id], root).stdout)["非用户可感知"])
        created = run_local_cli(self.new_dispatch_argv("用户单", "--user-facing"), root)
        self.assertEqual(0, created.returncode, created.stderr)
        user_id = created_ticket_id(created)
        self.assertFalse(json.loads(run_local_cli(["show", user_id], root).stdout)["非用户可感知"])

    def test_r1_question_and_request_types_are_not_gated(self):
        """非派单类型(疑问/需求)不受这道闸影响,照常建。"""
        question = self.service.create_question("疑问", SLOT, "疑问不设闸", "请对方总监答复。", "总编排")
        request = self.service.create_question("需求", SLOT, "需求不设闸", "请总编排排期。", "总编排")
        self.assertEqual(("疑问", "需求"), (question["类型"], request["类型"]))

    def test_r2_world_picture_gate_error_offers_the_internal_shortcut(self):
        """用户可感知单交板缺图 → 报错里有「set <真实单号> --internal」解法与「员工不要自己署总监名」提醒。"""
        ticket = self.dispatch("缺图被拦")
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError) as ctx:
            self.service.submit(ticket["编号"], "登录后界面已出现")
        message = str(ctx.exception)
        self.assertIn(f"set {ticket['编号']} --internal", message)
        self.assertIn("员工不要自己署总监名", message)
        self.assertIn("设计者", message)

    def test_r3_designer_can_flip_facing_but_other_slot_director_cannot(self):
        """set --internal --by 设计者 → 过;set --internal --by 别位总监 → 拒。"""
        ticket = self.dispatch("设计者解卡")
        flipped = self.service.edit(ticket["编号"], "设计者", internal=True)[0]
        self.assertTrue(flipped["非用户可感知"])
        with self.assertRaisesRegex(TicketError, "可感知标记"):
            self.service.edit(ticket["编号"], OTHER_SLOT, internal=False)

    def test_r3_designer_cannot_set_other_fields(self):
        """set --taskbook --by 设计者 → 仍然拒:只放开了可感知开关这一项。"""
        ticket = self.dispatch("设计者越权改任务书")
        with self.assertRaisesRegex(TicketError, "本人或总编排能改"):
            self.service.edit(ticket["编号"], "设计者", taskbook=r"D:\office\任务书.md")
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["任务书路径"])

class StaffSayOnOwnTicketTests(TicketTestCase):
    """T-000837:员工在自己经手的单上留一句话,不该被对话线那道闸卡住。

    在 T-000814 上撞到:想留一行说明,被「对话线只有三方」拒,
    而 block 会改状态、submit 要等活干完——于是停在半路等人来问。设计者当天定的口径是
    「不能因为其他客观原因阻塞员工」,所以开了一条很窄的缝,这四条守住那条缝的边界。
    """

    def _own_ticket(self):
        ticket = self.service.create_dispatch(
            SLOT, "员工留言用单", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        return ticket

    def test_staff_can_say_on_the_ticket_assigned_to_them(self):
        ticket = self._own_ticket()
        row = self.service.say(SLOT, self.worker, "第 3 步卡住:缺权限", reference=ticket["编号"])
        self.assertEqual(self.worker, row["发言人"])
        self.assertTrue(row["文字"].startswith("【员工留言】"))
        self.assertEqual(ticket["编号"], row["引用工单号"])

    def test_staff_without_ref_is_still_refused(self):
        with self.assertRaises(TicketError):
            self.service.say(SLOT, self.worker, "没带 ref 就不许进")

    def test_staff_cannot_say_on_someone_elses_ticket(self):
        ticket = self.service.create_dispatch(
            SLOT, "别人的单", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        other = self.service.staff_new(SLOT, "model-a")["员工名"]
        self.service.edit(ticket["编号"], SLOT, assign=other)
        with self.assertRaises(TicketError):
            self.service.say(SLOT, self.worker, "不是我的单", reference=ticket["编号"])

    def test_staff_cannot_say_on_another_slot_thread(self):
        ticket = self._own_ticket()
        with self.assertRaises(TicketError):
            self.service.say(OTHER_SLOT, self.worker, "别位的线仍然进不去", reference=ticket["编号"])


class ThreadSummaryTests(TicketTestCase):
    """对话线摘要(T-001313)。现象是刷新还要 5 秒。

    量出来:那 5 秒里 **4.93 秒是对话线**(13 条线全文,压后约 1.01 MB),
    工单那半在增量之后只剩 0.29 秒 / 100 字节。
    页面真正要从对话线上算的只有几个数,全都能在服务端现算。
    ★摘要**故意不缓存**:已读标记是被回头改的(inbox --mark-read 改已有行),
      缓存它就会出现「标了已读、角标还亮着」的旧数据。现算就没这问题。
    """

    def test_1_the_summary_carries_exactly_what_the_page_needs(self):
        """未读条数(按人分)、最新一条未读的摘要、总行数——页面只用得到这些。"""
        self.service.say(SLOT, "设计者", "设计者说的第一句")
        self.service.say(SLOT, "总编排", "总编排说的第二句")
        summary = self.service.thread_summaries()[SLOT]
        self.assertEqual(2, summary["总行数"])
        # 设计者自己说的那句不算他未读;总编排那句算
        self.assertEqual(1, summary["未读"]["设计者"])
        # 本位两句都没读过
        self.assertEqual(2, summary["未读"][SLOT])
        self.assertEqual("总编排", summary["最新未读"]["发言人"])
        self.assertIn("第二句", summary["最新未读"]["摘要"])

    def test_2_marking_read_shows_up_immediately(self):
        """★现算的意义:标完已读,下一次拿摘要就该降下来——这正是缓存做不到的那一点。"""
        self.service.say(SLOT, "总编排", "等你查收")
        self.assertEqual(1, self.service.thread_summaries()[SLOT]["未读"][SLOT])
        self.service.inbox(SLOT, SLOT, True)
        self.assertEqual(0, self.service.thread_summaries()[SLOT]["未读"][SLOT])
        # ★注意行数一个没变——所以任何「按行数切片」的缓存都发现不了这次改动
        self.assertEqual(1, self.service.thread_summaries()[SLOT]["总行数"])

    def test_3_every_slot_is_present_even_when_silent(self):
        """13 位都要有一行,空线也要有——页面按位取值,缺一位就是 undefined。"""
        summary = self.service.thread_summaries()
        self.assertEqual(set(model.SLOTS), set(summary))
        quiet = summary["前端·视觉与资源"]
        self.assertEqual(0, quiet["总行数"])
        self.assertIsNone(quiet["最新未读"])

    def test_4_the_summary_is_tiny_compared_with_the_full_threads(self):
        """摘要必须比全文小一个量级,否则这一单白做。"""
        for index in range(40):
            self.service.say(SLOT, "总编排", f"第 {index} 句 " + "很长的一段话" * 40)
        summary_bytes = len(json.dumps(self.service.thread_summaries(), ensure_ascii=False).encode("utf-8"))
        full_bytes = sum(
            len(json.dumps(self.service.store.read_jsonl(self.service.store.thread_path(s)), ensure_ascii=False).encode("utf-8"))
            for s in model.SLOTS
        )
        self.assertLess(summary_bytes * 5, full_bytes, f"摘要 {summary_bytes} 相对全文 {full_bytes} 省得不够多")

class IncrementalRefreshTests(TicketTestCase):
    """增量刷新(T-001308)。关闭的工单没必要每次都全量请求。

    查实过:进过「关闭」的 254 张里,**0 张**再离开过关闭态。所以这个判断成立。
    但实现**不按「是不是已关闭」缓存**——那条规矩是靠约定成立的,不是靠机制:
    工具里没有任何东西拦着一张关闭单被改(今天刚加的 rework 就是判过之后还能退回)。
    改用服务端流水行号当书签:关闭的单不变 ⇒ 永远不出现在增量里 ⇒ 自动不重传,
    **同样的省流,少赌一件事**;哪天真有人重开了一张关闭单,它会自己回来。
    """

    def internal(self, title: str):
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )

    def test_1_a_quiet_refresh_carries_no_tickets_at_all(self):
        """没动静时增量是空的——这就是省下来的那 2.45 MB。"""
        self.internal("先建一张")
        first = self.service.changes_since(0)
        self.assertEqual(1, len(first["工单"]))
        quiet = self.service.changes_since(first["游标"])
        self.assertEqual([], quiet["工单"])
        self.assertEqual(first["游标"], quiet["游标"])
        # 空增量必须小到可以忽略
        self.assertLess(len(json.dumps(quiet, ensure_ascii=False).encode("utf-8")), 200)

    def test_2_only_the_touched_ticket_comes_back(self):
        """动过哪张就只回哪张;没动的(含已关闭的)一张都不回。"""
        kept = self.internal("不会再动的那张")
        self.service.claim(kept["编号"], self.worker)
        self.service.submit(kept["编号"], "内部验证", "python -m pytest", "44 passed")
        cursor = self.service.changes_since(0)["游标"]
        moved = self.internal("待会儿要动的那张")
        delta = self.service.changes_since(cursor)
        self.assertEqual([moved["编号"]], [t["编号"] for t in delta["工单"]])
        self.assertNotIn(kept["编号"], [t["编号"] for t in delta["工单"]])

    def test_3_a_closed_ticket_that_does_change_still_comes_back(self):
        """★这正是不写死「关闭的不读」的理由:真变了,增量会把它带回来。

        按状态缓存的话,这一张会在页面上安静地停在旧状态——而且不报错。
        """
        ticket = self.internal("关闭之后又被动过")
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "内部验证", "python -m pytest", "44 passed")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        closed = self.service.close(ticket["编号"], SLOT, not_merged=True, reason="判过但不并线")
        self.assertEqual("关闭", closed["状态"])
        cursor = self.service.changes_since(0)["游标"]
        self.assertEqual([], self.service.changes_since(cursor)["工单"])  # 不动就不传
        # 有人回头动了它(这里用改备注模拟任何一种「关闭后仍被改」)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["备注"] = "关闭之后又补了一句"
        self.service.store.save_ticket(stored, "note", SLOT, "补记")
        back = self.service.changes_since(cursor)
        self.assertEqual([ticket["编号"]], [t["编号"] for t in back["工单"]])
        # 这一条要的是「关闭之后仍被改,增量照样把它传出来」——上面那张单确实回来了,已经验到了。
        # 不再断言备注的**内容**:T-001322 之后备注属于列表页不下发的键(网页零消费端),
        # 增量与整份都不带它。改在这里顺手钉一下,免得哪天有人把它偷偷加回下发集又没人发现。
        self.assertNotIn("备注", back["工单"][0], "备注是列表页不下发的键,增量也不该带")
        self.assertIn("备注", self.service.store.load_ticket(ticket["编号"]),
                      "不下发不等于不落库——盘上那份必须还在")

    def test_3b_a_delta_row_is_shaped_exactly_like_a_full_list_row(self):
        """★★增量与整份必须回**完全一样形状**的行,差一个键都不行。

        撞到过:增量少过了一道 ticket_view,于是「开窗指令」这个**派生字段**
        在增量来的单上没有——而**新建的单必然走增量**,结果设计者队列里那张卡的
        「开窗指令」整栏是空的(T-001314),屏上一眼可见。
        单测某个字段没用,这里比**整个键集合**:以后 ticket_view 再加派生字段,
        只要增量那条路忘了跟上,这条立刻红。
        """
        # 开窗指令要真生成得出来,才谈得上比内容:派单 + 指派到合法员工 + 有任务书路径。
        taskbook = self.root / "形状用例任务书.md"
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        ticket = self.service.create_dispatch(
            SLOT, "形状要一致", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
            taskbook=str(taskbook),
        )
        # ★整份那一趟是 /api/tickets → list_cards(T-001322 之后不再是 list_tickets):
        #   要比的是**网页真正拿到的那两条路**,拿别的路来比等于没比。
        full = {row["编号"]: row for row in self.service.list_cards()}[ticket["编号"]]
        delta = {row["编号"]: row for row in self.service.changes_since(0)["工单"]}[ticket["编号"]]
        self.assertEqual(sorted(full), sorted(delta), "增量行与整份行的键集合不一致")
        # 派生字段的**内容**也要一样,不能只是键在
        self.assertEqual(full["开窗指令"], delta["开窗指令"])
        self.assertTrue(delta["开窗指令"], "派单必须有开窗指令,空的说明没过 ticket_view")
        # ★两道派生各有一个专属键,两个都要钉:
        #   「开窗指令」只有 ticket_view 会加,「未发送字段」只有 card_view 会加。
        #   增量少过任何一道,这里立刻红——这正是那次生产事故的形状。
        self.assertEqual(full["未发送字段"], delta["未发送字段"])
        # ★而全文那条路(CLI list / build_bundle 离线包 / 服务端搜索)必须**照旧带全**。
        #   两条路各司其职:少了这一条,哪天有人把精简做进 list_tickets,
        #   离线包和搜索会一起哑掉,而上面那些断言全是绿的。
        whole = {row["编号"]: row for row in self.service.list_tickets()}[ticket["编号"]]
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, whole, f"全文那条路不该摘掉 {key}")
            self.assertNotIn(key, full, f"列表那一趟不该下发 {key}")

    def test_4_a_bad_cursor_falls_back_to_everything(self):
        """书签越界(换过库、回滚过)一律整份重取,绝不能把客户端永远停在旧数据上。"""
        self.internal("甲"); self.internal("乙")
        total = self.service.changes_since(0)["总数"]
        for bad in (999999999, -5):
            with self.subTest(书签=bad):
                self.assertEqual(total, len(self.service.changes_since(bad)["工单"]))

    def test_6_the_maintenance_command_forces_a_full_reload(self):
        """★唯一绕过 save_ticket 的那条维护命令,必须让缓存整份重取,否则它对页面隐形。"""
        self.internal("维护前建的")
        cursor = self.service.changes_since(0)["游标"]
        self.assertFalse(self.service.changes_since(cursor)["整份重取"])
        self.service.store.backfill_state_times(force=True)
        after = self.service.changes_since(cursor)
        self.assertTrue(after["整份重取"], "维护命令改了盘却没让客户端重取,缓存会一直显示旧的进入时间")

    def test_9_the_same_rules_hold_on_the_sqlite_backend(self):
        """★线上跑的是 SQLite,不是文件后端——增量的两条判据必须在**它**身上也成立。

        这条是变异检验逼出来的:第一版只测文件后端,把 SQLite 那边的
        「坏书签整份回退」改坏了,八条用例**一条都没红**。而线上正是 SQLite,
        一旦书签越界,客户端会永远收到空增量、永远停在旧数据上——最难查的那种。
        """
        store = SqliteStore(self.root / "增量-db" / "tickets.sqlite")
        service = TicketService(store)
        worker = service.staff_new(SLOT, "model-a")["员工名"]
        first = service.create_dispatch(
            SLOT, "库里第一张", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        cursor = service.changes_since(0)["游标"]
        self.assertEqual([], service.changes_since(cursor)["工单"])          # 不动就不传
        service.claim(first["编号"], worker)
        self.assertEqual(                                                    # 动了就回来
            [first["编号"]], [t["编号"] for t in service.changes_since(cursor)["工单"]],
        )
        # ★坏书签必须整份回退,不许夹到边界后回空
        self.assertEqual(1, len(service.changes_since(999999999)["工单"]))
        self.assertEqual(1, len(service.changes_since(-5)["工单"]))
        self.assertEqual(1, service.changes_since(0)["总数"])
        # 维护命令那条信号在 SQLite 上同样要发得出来
        after = service.changes_since(service.changes_since(0)["游标"])
        self.assertFalse(after["整份重取"])
        store.backfill_state_times(force=True)
        self.assertTrue(service.changes_since(after["游标"])["整份重取"])

class ReviewInParallelTests(TicketTestCase):
    """复检简化三条(D9-460 / 宪法 v2.36)。口径:必须简化复检,没必要复检的地方直接通过。

    ① 判卷与复验**并行**:交板即可复验,不再等总监判过;并线 = 判过 ∧ 复验过。
    ② 内部单六项机器闸全绿即视为复验过,复检只看闸输出。
    ③ 上服与取证分离:上服记录由脚本自动建、免判;取证另开单,取不到图不挡上服。
    ④ 日览加「待复验」「可并」两队列。
    """

    def internal(self, title: str = "内部单"):
        self.keep_window_open()
        ticket = self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        return ticket

    def submitted(self, title: str = "内部单", gate_report: str = ""):
        ticket = self.internal(title)
        return self.service.submit(
            ticket["编号"], "验证完成", "python -m pytest", "444 passed", gate_report=gate_report,
        )

    # ── ① 判卷与复验并行 ──────────────────────────────────────────────
    def test_1_verify_works_before_the_judge_has_even_looked(self):
        """★并行的本体:交板之后、判卷之前就能复验。这一条红 = 复检席又被串在总监后面。"""
        ticket = self.submitted("先复验后判卷")
        self.assertEqual("待判", ticket["状态"])
        verified, hint = self.service.verify(ticket["编号"], "独立复检", "过", gates="四工程全绿")
        self.assertEqual("过", verified["复验"]["结论"])
        self.assertEqual("独立复检", verified["复验"]["复验人"])
        # 复验**不改状态**:状态归 judge 管,两件事各走各的
        self.assertEqual("待判", verified["状态"])
        self.assertIn("还没判卷", hint)
        # 判过之后两道齐,直接可并
        judged, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("待复检", judged["状态"])
        self.assertEqual("已合并", self.service.merge(ticket["编号"], "独立复检")["状态"])

    def test_2_merge_needs_both_gates_not_just_the_judge(self):
        """★并线前置 = 判过 ∧ 复验过。少了复验那一道要拒,且报错要说清怎么补。"""
        ticket = self.submitted("只判过没复验")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        with self.assertRaises(TicketError) as blocked:
            self.service.merge(ticket["编号"], "独立复检")
        message = str(blocked.exception)
        self.assertIn("还没复验", message)
        self.assertIn("verify", message)          # 报错要给出下一条命令,不能只说不行
        self.assertIn("--gate-report", message)   # 也要提内部单那条捷径
        self.service.verify(ticket["编号"], "独立复检", "过", gates="六项全绿")
        self.assertEqual("已合并", self.service.merge(ticket["编号"], "独立复检")["状态"])

    def test_2b_verified_alone_is_not_enough_either(self):
        """★另一半:复验过了、总监还没判,同样不能并。

        并线是「判过 ∧ 复验过」,两个合取项都要有闸守着。
        只钉「缺复验要拒」是钉了一半——把「∧ 判过」那一半拿掉,上一条照样绿。
        """
        ticket = self.submitted("只复验没判过")
        self.service.verify(ticket["编号"], "独立复检", "过", gates="六项全绿")
        self.assertEqual("待判", self.service.store.load_ticket(ticket["编号"])["状态"])
        with self.assertRaises(TicketError) as blocked:
            self.service.merge(ticket["编号"], "独立复检")
        self.assertIn("待复检", str(blocked.exception))
        # 机器闸那条路也一样:全绿只抵复验那一道,抵不了判卷
        green = self.submitted("机器闸绿但没判", gate_report=self.GREEN)
        self.assertEqual("过", green["复验"]["结论"])
        with self.assertRaisesRegex(TicketError, "待复检"):
            self.service.merge(green["编号"], "独立复检")

    def test_3_the_two_gates_may_arrive_in_either_order(self):
        """判过→复验 与 复验→判过,两条顺序都要能走到并线。"""
        first = self.submitted("先判后验")
        self.service.judge(first["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.service.verify(first["编号"], "独立复检", "过", gates="闸绿")
        self.assertEqual("已合并", self.service.merge(first["编号"], "独立复检")["状态"])
        second = self.submitted("先验后判")
        self.service.verify(second["编号"], "独立复检", "过", gates="闸绿")
        self.service.judge(second["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("已合并", self.service.merge(second["编号"], "独立复检")["状态"])

    def test_4_a_verify_rejection_leaves_the_state_alone_and_says_where_to_go(self):
        """复验判退**不自己改状态**:退回照旧走 rework/退回单,那两条路才记责任与返工次数。"""
        ticket = self.submitted("复验退回")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        rejected, hint = self.service.verify(ticket["编号"], "独立复检", "退", gates="体积闸不过")
        self.assertEqual("待复检", rejected["状态"])   # 状态没动
        self.assertEqual("退", rejected["复验"]["结论"])
        self.assertIn("rework", hint)
        # 退了就并不了
        with self.assertRaisesRegex(TicketError, "还没复验"):
            self.service.merge(ticket["编号"], "独立复检")

    def test_5_verify_refuses_the_worker_and_the_wrong_states(self):
        """执行员工不能复验自己的活;没交板/已并线的单也不收。"""
        ticket = self.internal("状态闸")
        with self.assertRaisesRegex(TicketError, "待判.*待复检|只有"):
            self.service.verify(ticket["编号"], "独立复检", "过", gates="x")
        self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "444 passed")
        with self.assertRaisesRegex(TicketError, "不能与执行员工"):
            self.service.verify(ticket["编号"], self.worker, "过", gates="x")
        with self.assertRaisesRegex(TicketError, "只可填"):
            self.service.verify(ticket["编号"], "独立复检", "也许", gates="x")
        with self.assertRaisesRegex(TicketError, "必须写清哪一条不过"):
            self.service.verify(ticket["编号"], "独立复检", "退")

    def test_6_a_self_owned_ticket_is_still_exempt(self):
        """★工单台自有单免复检(D9-460 边界)。

        不留这个例外,平台位与复检席自己的单会**永久**卡在待复检:
        它们的复验人也只能是本位总监,「另一双眼睛」在这里数学上无解——
        与三方互斥闸那条例外是同一个道理(T-000899 上撞到过一次)。
        """
        self.keep_window_open()
        ticket = self.service.create_dispatch(
            SLOT, "本位自有单", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "444 passed")
        self.service.judge(ticket["编号"], True, SLOT, verdict=PASS_VERDICT)
        merged = self.service.merge(ticket["编号"], SLOT)   # 没跑过 verify,照样能并
        self.assertEqual("已合并", merged["状态"])
        self.assertIn("自记", merged["复检人"])

    # ── ② 内部单机器闸全绿即并 ────────────────────────────────────────
    GREEN = "\n".join(f"{item}: 过" for item in service_module.GATE_REPORT_ITEMS)

    def test_7_six_green_gates_count_as_verified(self):
        """六项全绿 ⇒ 自动记复验过,判过之后不必再跑 verify。"""
        ticket = self.submitted("六项全绿", gate_report=self.GREEN)
        self.assertTrue(ticket["机器闸"]["全绿"])
        self.assertEqual("过", ticket["复验"]["结论"])
        self.assertEqual("机器闸", ticket["复验"]["复验人"])
        self.assertIn("机器闸全绿", ticket["机器闸提示"])
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("已合并", self.service.merge(ticket["编号"], "独立复检")["状态"])

    def test_8_one_bad_or_missing_gate_is_not_green(self):
        """★任一不过、或缺一项,都不置标——这是这一条最要紧的地方。

        置错标的后果比没有闸更坏:台面上写着「机器闸绿」,复检席就不会再去看,
        而那张单其实没过闸。
        """
        first = service_module.GATE_REPORT_ITEMS[0]
        one_red = self.GREEN.replace(f"{first}: 过", f"{first}: 不过")
        red = self.submitted("一项不过", gate_report=one_red)
        self.assertFalse(red["机器闸"]["全绿"])
        self.assertEqual({}, red["复验"])
        self.assertIn(f"{first}不过", red["机器闸提示"])
        # 缺项 ≠ 全绿:只写五行也不行
        short = "\n".join(f"{item}: 过" for item in service_module.GATE_REPORT_ITEMS[:-1])
        missing = self.submitted("缺一项", gate_report=short)
        self.assertFalse(missing["机器闸"]["全绿"])
        self.assertEqual({}, missing["复验"])
        self.assertIn("缺", missing["机器闸提示"])
        # 没置标的单照旧要人复验才能并
        self.service.judge(red["编号"], True, "UI总监", verdict=PASS_VERDICT)
        with self.assertRaisesRegex(TicketError, "还没复验"):
            self.service.merge(red["编号"], "独立复检")

    def test_9_gate_report_is_internal_only(self):
        """用户可感知单不许拿机器闸抵复验:真登录那一眼是 D9-460 明写不简化的。"""
        self.keep_window_open()
        ticket = self.dispatch("用户可感知")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        with self.assertRaisesRegex(TicketError, "只用于内部单"):
            self.service.submit(ticket["编号"], "看得见", gate_report=self.GREEN)

    def test_10_the_parser_only_trusts_lines_it_actually_understands(self):
        """读不懂的行不算过。宽松解析在这里等于把闸拆了。"""
        items = service_module.GATE_REPORT_ITEMS
        muddy = items[1]        # 第二项写成一句读不懂的话
        report = self.service.parse_gate_report(chr(10).join(
            f"{item}: {'大概吧' if item == muddy else '过'}" for item in items
        ))
        self.assertFalse(report["全绿"])
        self.assertEqual("读不懂", {row["项"]: row["结论"] for row in report["报告"]}[muddy])

    # ── ③ 上服与取证分离 ──────────────────────────────────────────────
    def test_11_a_deploy_record_needs_no_window_no_judge_no_picture(self):
        record = self.service.deploy_record("abc1234de", "服务 active;端口在听", ["T-000001", "T-000002"])
        self.assertEqual("上服记录", record["部署类"])
        self.assertEqual("实机复验过", record["状态"])       # 免判免复检,直接终态
        self.assertEqual(service_module.SHOT_EXEMPT, record["实机图标记"])   # 免图
        self.assertEqual("", record["指派给"])                # 免员工窗
        self.assertIn("T-000001", record["接线证据"]["文字"])
        self.assertIn("端口在听", record["接线证据"]["原样输出"])
        # 部署头自动写进当前值面——以前这一格靠人手抄,抄漏全台面读到旧值
        self.assertEqual("abc1234de", self.service.state_board()["值"]["deploy_head"])

    def test_12_an_evidence_ticket_carries_the_picture_duty_instead(self):
        """取证单才是欠图的那一张;上服记录不欠。取不到图不挡上服。"""
        record = self.service.deploy_record("abc1234de", "端口在听")
        evidence = self.service.evidence_ticket("abc1234de", SLOT)
        self.assertEqual("取证", evidence["部署类"])
        self.assertEqual("待独图", evidence["实机图标记"])
        self.assertFalse(evidence["非用户可感知"])           # 取证要的就是屏上那一眼
        pending = [row["编号"] for row in self.service.list_tickets(shot_pending=True)]
        self.assertIn(evidence["编号"], pending)
        self.assertNotIn(record["编号"], pending, "上服记录不该占待独图那一格")

    def test_12b_the_script_only_records_after_the_port_gate(self):
        """★上服记录必须建在**后置闸之后**:它的意思是「线上真跑起来了」,不是「脚本走到这一行」。

        放到闸前面就成了纸糊的记录——端口没起来时脚本 exit 3,本来就走不到这里;
        真把它挪到前面,一次失败的上服也会留下一张「已上服」的单,而值面还会被写上新头。
        ★这一条只能钉源码顺序:update.sh 是 shell,没法在 pytest 里真跑一遍部署。
          局限写在这里,别把它当成行为验证——真判据是上服后线上那几条探针。
        """
        script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        gate = script.index('ss -ltn 2>/dev/null | grep -q ":$DESK_PORT "')
        record = script.index('"op":"deploy-record"')
        self.assertLess(gate, record, "上服记录那段跑到端口后置闸前面去了")
        self.assertIn("后置检查:$DESK_PORT 已在听。", script[:record])
        # 建单失败不能让整条上服失败:代码已替换、服务已起来,这时候退非零会让人以为上服没成
        self.assertIn("不影响本次上服", script[record:])

    def test_12c_the_packer_puts_the_head_into_the_package(self):
        """★第一次真上服撞到的:`git archive` 打的包**没有 .git**,
        于是 update.sh 那句 `git rev-parse` 永远拿不到部署头,
        「READY 之后自动建上服记录」这条路**每次都走兜底、从不生效**。

        根治只有一条:打包那一刻就把提交号放进包里(pack.sh),update.sh 优先读它。
        ★这里连「pack.sh 自己要回验」也一起钉:那个脚本的第一版用了一个
          根本不存在的 git 选项、错误被 2>/dev/null 吞掉,却照样打印「已写进包内」——
          报成功、没做成、没人发现,正是本仓反复栽的那个形状。
        """
        pack = ROOT / "deploy" / "pack.sh"
        update = ROOT / "deploy" / "update.sh"
        if not pack.is_file():
            self.skipTest(f"{PACKAGE_TREE_SKIP_PREFIX},这条要读 {pack}")
        pack_text = pack.read_text(encoding="utf-8")
        update_text = update.read_text(encoding="utf-8")
        # 打包器把头写进包内固定位置
        self.assertIn("--add-virtual-file=", pack_text)
        self.assertIn("deploy/DEPLOY_HEAD", pack_text)
        # ★打完要回验,而且验不过要**删掉半成品**——不能把一个没写进头的包留在盘上让人拿去上服
        self.assertIn('PACKED="$(tar xOf "$OUT" deploy/DEPLOY_HEAD', pack_text)
        self.assertIn('if [[ "$PACKED" != "$HEAD_SHORT" ]]', pack_text)
        self.assertIn('rm -f "$OUT"', pack_text)
        # 工作树脏要拦:打的是提交,未提交的改动进不了包,不拦就会「上服了但没生效」
        self.assertIn("有未提交的改动", pack_text)
        # update.sh 那边优先读包里的文件,git 只是最后兜底
        self.assertIn("$SOURCE/deploy/DEPLOY_HEAD", update_text)
        self.assertLess(update_text.index("$SOURCE/deploy/DEPLOY_HEAD"),
                        update_text.index("git rev-parse --short=9 HEAD"),
                        "包内 DEPLOY_HEAD 必须排在 git rev-parse 之前——后者在上服包里永远拿不到")
        # ★令牌也要自己找,靠调用者传第二个路径 = 每次上服都可能因为记错而少建一张记录
        #   (撞到过两次)。★但只能**从 systemd 的 --token-file 现读**,不许写死路径:
        #   写死就会出现指向数据库目录的字面,而本脚本一个字都不许碰它——
        #   那道闸(下面 test_update_only_replaces_app…)防的是「上服脚本误删数据库」,
        #   为了读个令牌把它放宽是本末倒置。
        self.assertIn("--token-file", update_text)
        self.assertNotIn("$INSTALL_DIR/db", update_text)
        self.assertLess(update_text.index("--token-file"),
                        update_text.index('if [[ -n "${TICKET_TOKEN:-}" ]]; then'),
                        "自己找令牌那一段必须排在用它之前")

    def test_13_a_deploy_record_refuses_to_be_built_without_a_head(self):
        with self.assertRaisesRegex(TicketError, "必须写明部署头"):
            self.service.deploy_record("", "端口在听")
        with self.assertRaisesRegex(TicketError, "上服目标不认识"):
            self.service.deploy_record("abc1234de", "x", repo="别的仓")

    # ── ④ 日览与队列 ─────────────────────────────────────────────────
    def test_14_the_digest_shows_both_new_queues(self):
        waiting = self.submitted("等复验的")
        ready = self.submitted("两道齐的")
        self.service.judge(ready["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.service.verify(ready["编号"], "独立复检", "过", gates="闸绿")
        digest = self.service.digest()
        self.assertTrue(any("待复验 " in line and "可并 " in line for line in digest), digest[:4])
        self.assertTrue(any(line.startswith(f"[可并] {ready['编号']}") for line in digest))
        pending_ids = [row["编号"] for row in self.service.pending_verify()]
        self.assertIn(waiting["编号"], pending_ids)
        self.assertNotIn(ready["编号"], pending_ids)
        self.assertEqual([ready["编号"]], [row["编号"] for row in self.service.ready_to_merge()])

    def test_15_a_ticket_waiting_too_long_for_verify_is_reported(self):
        """老化按「待复验超 24 小时」单独报:它与按状态计时那套口径不同。"""
        ticket = self.submitted("等太久")
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["状态进入时间"] = (datetime.now().astimezone() - timedelta(hours=30)).isoformat()
        # 直写盘:save_ticket 会按本次事件把「状态进入时间」重刷回现在,那样这条永远造不出超时。
        self.service.store.atomic_json(self.service.store.item_path(stored["编号"]), stored)
        digest = self.service.digest()
        self.assertTrue(any(line.startswith(f"[待复验超时] {ticket['编号']}") for line in digest), digest[:6])

    def test_16_the_judge_stops_telling_the_reviewer_to_verify_twice(self):
        """复验先做完了,判过时那句唤醒要改口——否则复检席会白开一次窗。"""
        ticket = self.submitted("先验过了")
        self.service.verify(ticket["编号"], "独立复检", "过", gates="闸绿")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        rows = self.service.store.read_jsonl(
            self.service.store.thread_path(service_module.REVIEW_SLOT))
        self.assertIn("可以并线了", rows[-1]["文字"])
        self.assertNotIn("等你复验", rows[-1]["文字"])


class DeployedMergedIsNotStalledTests(TicketTestCase):
    """已上服的「已合并」单不算卡住,改报「欠 live 记账」(D9-461)。

    ★为什么要分开:「卡住了」那一段的意思是**有人该动手却没动**。
    一张代码早就在线上跑着、只差一笔 live 记账的单报成「卡 24 小时」,
    会让人去催一个根本不存在的活,而真正该做的只是补一笔账。
    """

    def deployed_merged(self, head: str = "abc1234de", record_head: str | None = None):
        # ★必须用**用户可感知**单:内部单一并线就是终态(is_terminal),本来就不进老化告警,
        #   拿它测「不再报卡住」等于什么都没测——真正会卡在「已合并」上的正是这一类。
        ticket = self.to_merged("已上服的用户可感知单", internal=False)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["判语"] = f"判过。判的是提交 {head}。设计者怎么打开它:照旧。"
        # 挪到 25 小时前:不这么做它根本越不过「已合并 24 小时」那条线,测不到本条
        stored["状态进入时间"] = (datetime.now().astimezone() - timedelta(hours=25)).isoformat()
        self.service.store.atomic_json(self.service.store.item_path(stored["编号"]), stored)
        if record_head is not None:
            self.service.state_set("deploy_head", record_head, service_module.REVIEW_SLOT)
        return self.service.store.load_ticket(stored["编号"])

    def test_1_a_merged_ticket_already_on_the_server_is_not_stalled(self):
        ticket = self.deployed_merged(record_head="abc1234de")
        self.assertTrue(self.service.awaiting_live_record(ticket))
        self.assertIsNone(self.service.stale_info(ticket), "已上服的单不该进「卡住了」")
        digest = self.service.digest()
        self.assertTrue(any(line.startswith(f"[欠 live 记账] {ticket['编号']}") for line in digest), digest[:8])
        self.assertFalse(any(line.startswith(f"[停滞] {ticket['编号']}") for line in digest))

    def test_2_a_merged_ticket_not_yet_deployed_is_still_stalled(self):
        """★反面:没上服的照旧按卡住报。放宽成「已合并一律不报」会把真卡住的单藏起来。"""
        ticket = self.deployed_merged(record_head="99999999")   # 值面里是别的头
        self.assertFalse(self.service.awaiting_live_record(ticket))
        self.assertIsNotNone(self.service.stale_info(ticket))
        # 值面一个头都没填时也照旧按卡住报(没有证据说明它上服了)
        blank = TicketService(TicketStore(self.root / "blank"))
        self.assertEqual(set(), blank.deployed_heads())

    def test_3_short_and_long_hashes_both_match(self):
        """值面写 9 位短号、判语写 40 位全号是常态,直接相等几乎永远不成立。"""
        long_hash = "abc1234de" + "0" * 31
        ticket = self.deployed_merged(head=long_hash, record_head="abc1234de")
        self.assertTrue(self.service.awaiting_live_record(ticket), "短号该匹配得上长号")


class CloseNotDeployedTests(TicketTestCase):
    """「已合并」单的未上服结案出口(T-001509,T-001124 上撞到)。

    ★这条边存在的原因:上服失败已回滚、内容随后来的单上服——这样的单并过线,
    却走不到实机复验过;close/免独图/live/rework 四条路全堵,单永远挂在「已合并」。
    它与 close --not-merged 是一对:那边「判过不并线」,这边「并过没上服」。
    """

    def merged_not_deployed(self):
        # 用户可感知单:正是会卡在「已合并」上老化的那一类(内部单一并线就是终态)。
        ticket = self.to_merged("回滚未上服的用户可感知单", internal=False)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["状态进入时间"] = (datetime.now().astimezone() - timedelta(hours=25)).isoformat()
        self.service.store.atomic_json(self.service.store.item_path(stored["编号"]), stored)
        # 值面里放一个与判语无关的头:证明它没上服,不欠 live 记账。
        self.service.state_set("deploy_head", "99999999", service_module.REVIEW_SLOT)
        return self.service.store.load_ticket(stored["编号"])

    def test_1_owner_closes_a_rolled_back_merged_ticket(self):
        ticket = self.merged_not_deployed()
        closed = self.service.close(
            ticket["编号"], SLOT, not_deployed=True, reason="部署单#15 失败已回滚,内容随 #16 上服",
        )
        self.assertEqual("关闭", closed["状态"])
        record = closed["未上服结案"]
        self.assertEqual(SLOT, record["结案人"])
        self.assertIn("部署单#15", record["原因"])
        self.assertIn("未上服结案:", closed["备注"])
        self.assertTrue(service_module.is_terminal(closed), "结案后不该再进老化告警")
        self.assertIsNone(self.service.stale_info(closed))
        from ticket_desk.ticket import compact_ticket
        self.assertIn("已合并·未上服·已结案", compact_ticket(closed))

    def test_2_only_merged_tickets_can_take_this_path(self):
        """没并过的单各有各的出路,不该混进这条路:待复检走 --not-merged,待判走 judge。"""
        ticket = self.to_judging()
        with self.assertRaisesRegex(TicketError, "只给并过线"):
            self.service.close(ticket["编号"], SLOT, not_deployed=True, reason="x")

    def test_3_reason_is_required(self):
        ticket = self.merged_not_deployed()
        with self.assertRaisesRegex(TicketError, "--reason 必填"):
            self.service.close(ticket["编号"], SLOT, not_deployed=True, reason="  ")
        # 拦下时状态不动,还停在「已合并」。
        self.assertEqual("已合并", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_4_actor_gate_owner_conductor_designer_yes_review_no(self):
        ticket = self.merged_not_deployed()
        with self.assertRaisesRegex(TicketError, "只有该单所属位"):
            self.service.close(
                ticket["编号"], service_module.REVIEW_SLOT, not_deployed=True, reason="复检席不该能结这个",
            )
        # 复检席被拦下后单子原样;换成总编排就能结——「确认没上服」由它说得清。
        self.assertEqual("已合并", self.service.store.load_ticket(ticket["编号"])["状态"])
        closed = self.service.close(ticket["编号"], "总编排", not_deployed=True, reason="回滚未上服")
        self.assertEqual("关闭", closed["状态"])
        again = self.to_merged("设计者也可以", internal=False)
        designer_closed = self.service.close(again["编号"], "设计者", not_deployed=True, reason="设计者收口")
        self.assertEqual("关闭", designer_closed["状态"])

    def test_5_already_deployed_ticket_is_refused_and_sent_to_live(self):
        """提交号已在部署头里的单欠的只是记账,结成「未上服」等于把线上跑着的活说成没上。"""
        ticket = self.to_merged("已上服只欠记账", internal=False)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["判语"] = "判过。判的是提交 abc1234de。设计者怎么打开它:照旧。"
        self.service.store.atomic_json(self.service.store.item_path(stored["编号"]), stored)
        self.service.state_set("deploy_head", "abc1234de", service_module.REVIEW_SLOT)
        with self.assertRaisesRegex(TicketError, "补记账"):
            self.service.close(stored["编号"], SLOT, not_deployed=True, reason="不该结这张")

    def test_6_not_deployed_and_not_merged_are_mutually_exclusive(self):
        ticket = self.merged_not_deployed()
        with self.assertRaisesRegex(TicketError, "二选一"):
            self.service.close(
                ticket["编号"], SLOT, not_merged=True, not_deployed=True, reason="两个都给",
            )

    def test_7_digest_lists_it_as_not_deployed_and_not_stalled(self):
        ticket = self.merged_not_deployed()
        digest_before = self.service.digest()
        self.assertTrue(
            any(line.startswith(f"[停滞] {ticket['编号']}") for line in digest_before),
            "未结案前,未上服的已合并单就该按卡住报",
        )
        self.service.close(ticket["编号"], SLOT, not_deployed=True, reason="回滚未上服,原命题由 #16 落地")
        digest = self.service.digest()
        self.assertTrue(any(line.startswith(f"[未上服结案] {ticket['编号']}") for line in digest), digest[:10])
        self.assertIn("未上服结案 1 张", "\n".join(digest))
        self.assertIn("不算上服、不算卡住", "\n".join(digest))
        self.assertFalse(any(line.startswith(f"[停滞] {ticket['编号']}") for line in digest))


class ServerTimeEnvelopeTests(RemoteClientTests):
    """每个响应信封带服务器本地真时刻(T-001508):核时区/_clock 从此一条命令。"""

    def test_1_client_captures_server_time_from_the_envelope(self):
        self.client.execute(["list"])
        self.assertRegex(
            self.client.server_time,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
        )

    def test_2_env_probe_prints_the_server_time(self):
        environment = clean_environment(
            self.service.store.root,
            TICKET_REMOTE=f"http://127.0.0.1:{self.server.server_address[1]}",
            TICKET_TOKEN_FILE=str(self.token_file),
        )
        probed = subprocess.run(
            [*CLI, "env", "--probe"], cwd=ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(0, probed.returncode, probed.stderr)
        self.assertIn("服务器时刻", probed.stdout)
        self.assertRegex(probed.stdout, r"服务器时刻 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")


class RebuildChecklistOnSubmitTests(TicketTestCase):
    """真源位交板时提醒「生成物刷了没」(D9-463 补 ③,总编排答 T-001460)。

    ★只提示、不拦:服务端看不见交板人那台机器的 git,判不了他刷没刷。
      硬闸在 GeneratedArtifactDriftTests(真跑一次 build 逐字比对)。
      这里保证的是「人被提醒过」——两条各管一段,别指望这一条挡住什么。
    """

    def submit_for(self, slot: str):
        worker = self.service.staff_new(slot, "model-a")["员工名"]
        ticket = self.service.create_dispatch(
            slot, "改真源", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], worker)
        return self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "477 passed")

    def test_the_truth_source_slot_is_reminded(self):
        submitted = self.submit_for("内容·文案与真源")
        hint = submitted.get("重刷提示", "")
        self.assertIn("真源表", hint)
        self.assertIn("同一笔提交", hint)
        self.assertIn("派生件", hint)

    def test_other_slots_are_not_nagged(self):
        """别位不改真源,给它们挂这句话只会训练人无视提示。"""
        self.assertEqual("", self.submit_for(SLOT).get("重刷提示", ""))

    def test_the_cli_prints_it(self):
        script = (ROOT / "ticket_desk" / "ticket.py").read_text(encoding="utf-8")
        self.assertIn('rebuild_note = str(ticket.get("重刷提示", ""))', script)
        self.assertIn("(notice, gate_note, rebuild_note)", script)


# 「只分发不派单」的那一位:只发需求/疑问/拍板,不派实现单、不判卷、不并线。
# ★这一位是**可以没有的**:config「只分发不派单位」允许为空清单,
#   而文档推荐给中型档的三位配置正好就是空的。
#   直接取 [0] 会让整份用例在**收集阶段**就崩(IndexError),
#   于是「519 条全绿」这句话在我们自己推荐的配置下根本不成立。
#   —— 空名册时整类跳过,并在跳过理由里说清为什么,不许静默不跑。
DISPATCH_SLOT = model.DISPATCH_FORBIDDEN_SLOTS[0] if model.DISPATCH_FORBIDDEN_SLOTS else ""


@unittest.skipUnless(DISPATCH_SLOT, "本机 config「只分发不派单位」是空的：这一位是可选的，没有就没这组用例")
class DecisionRelaySlotTests(TicketTestCase):
    """第十三位「设计·裁定分发」(宪法 v2.38 / D9-462 补,T-001436)。

    它是设计者在后端/数值/物品/真源类事务上的**唯一对接人**:
    逐字记录裁定 → 送总编排落 D9 号 → 按影响面用需求/疑问单广播。
    **不派实现单、不判卷、不 merge、不 live**——让传话的人给活打分是这条的反面。
    """

    def test_1_the_slot_is_in_the_roster_everywhere(self):
        """位名要真进 SLOTS,而不是只在某一处硬编码。"""
        self.assertIn(DISPATCH_SLOT, model.SLOTS)
        self.assertEqual(len(config_module.SLOTS), len(model.SLOTS), "位名名册只有 config 一处真源")
        # 建单/转交/ask 的位名校验都走同一个 SLOTS,所以进了它就三处都认
        asked = self.service.create_question("需求", DISPATCH_SLOT, "归它的需求", "正文")
        self.assertEqual(DISPATCH_SLOT, asked["所属总监位"])
        moved = self.service.transfer(asked["编号"], DISPATCH_SLOT, "归口对接", "总编排")
        self.assertEqual(DISPATCH_SLOT, moved["所属总监位"])
        # 名册也要能给它开编号
        member = self.service.staff_new(DISPATCH_SLOT, "model-a")
        self.assertTrue(member["员工名"].startswith(f"{DISPATCH_SLOT}-"))

    def test_2_it_may_send_the_three_question_types(self):
        """需求、疑问、拍板三类照发。"""
        for kind, body in (("需求", "要什么"), ("疑问", "问什么"), ("拍板", VALID_DECISION_BODY)):
            with self.subTest(kind=kind):
                ticket = self.service.create_question(
                    kind, SLOT, f"{kind}单", body, initiator=DISPATCH_SLOT)
                self.assertEqual(kind, ticket["类型"])

    def test_3_it_refuses_to_create_a_dispatch(self):
        """★变异点:去掉这道闸须红。

        它不派实现单——实现单由收到广播的那一位自己的总监派。
        报错要给出下一步(ask --type 需求),不能只说不行:拦下不是终点。
        """
        with self.assertRaises(TicketError) as blocked:
            self.service.create_dispatch(
                DISPATCH_SLOT, "它不该能派的活", ["DECISIONS.md:x"], "主场景",
                task_tier="乙", deliverables=[str(self.deliverable)], internal=True)
        message = str(blocked.exception)
        self.assertIn("本位不派实现单", message)
        self.assertIn("ask --type 需求", message)
        # ★拦在取号之前:不能白烧一个单号(T-000070 那一课)
        self.assertEqual("T-000001", self.service.store.next_ticket_id())

    def test_4_it_neither_judges_nor_merges_nor_lives(self):
        """判卷 / 并线 / 实机复验三条都拒,位名与它的员工编号都要拦住。"""
        ticket = self.to_judging()
        for actor in (DISPATCH_SLOT, f"{DISPATCH_SLOT}-01"):
            with self.subTest(actor=actor), self.assertRaisesRegex(TicketError, "不判卷"):
                self.service.judge(ticket["编号"], True, actor, verdict=PASS_VERDICT)
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.verified(ticket["编号"])
        with self.assertRaisesRegex(TicketError, "不并线"):
            self.service.merge(ticket["编号"], DISPATCH_SLOT)
        merged = self.service.merge(ticket["编号"], "独立复检")
        self.assertEqual("已合并", merged["状态"])
        with self.assertRaisesRegex(TicketError, "不做实机复验"):
            self.service.live(ticket["编号"], str(self.picture("relay.png")), DISPATCH_SLOT, "独图")

    def test_5_the_charter_path_is_registered_as_the_window_source(self):
        """章程路径要能读出来,没登记时按办公目录约定推算,不留「未填」。"""
        self.assertEqual(
            config_module.charter_path(DISPATCH_SLOT),
            self.service.slot_charter_path(DISPATCH_SLOT))
        charters = self.service.slot_charters()
        self.assertEqual(set(model.SLOTS), set(charters))
        self.assertTrue(all(charters.values()), "每一位都该有章程路径,不能有空的")
        # 登记了就以登记为准
        slots = self.service.store.read_json(self.service.store.slots_path, {})
        for row in slots.get("总监位", []):
            if row.get("名字") == DISPATCH_SLOT:
                row["章程"] = r"D:\另一处\章程.md"
        self.service.store.atomic_json(self.service.store.slots_path, slots)
        self.assertEqual(r"D:\另一处\章程.md", self.service.slot_charter_path(DISPATCH_SLOT))

    def test_6_the_digest_lists_it_separately(self):
        """日览把它的待答单列:积在这一位手上 = 设计者那一侧的问题没归并。"""
        self.service.create_question("需求", DISPATCH_SLOT, "等它归并的一条", "正文")
        digest = self.service.digest()
        self.assertTrue(any(line.startswith(f"{DISPATCH_SLOT} 待答 1 张") for line in digest), digest[:8])

class ListPayloadSlimmingTests(TicketTestCase):
    """首次进页面那一趟只发网页真读的键(T-001322)。

    背景:稳态刷新已由 T-001308 降到 86 KB,只剩**首次**那一趟 2.62 MB(压后)。
    接管件里原写的下一步是「关闭+作废那 340 张发精简版」,按线上 1309 张真数据量过:
    那样只降 10.7%(2682→2395 KB),首次仍要 2.4 MB——终态单只占压后体积的 15%,
    且那批里仍有卡片必须显示的键(判语全库就 1.05 MB)。⇒ 改按**消费端**裁,不按状态裁。
    实测 2682 KB → 983 KB,降 63.3%。

    摘掉哪四个键是逐个 grep 网页脚本得出的,不是按体积挑的:
    答复/备注在渲染处一次都没出现;接线证据只被 worldImages() 读,而它全仓无调用处;
    正文只有 answerCard() 读。这几条边界就是下面几个用例要钉住的东西。
    """

    def serve(self):
        server = TicketHTTPServer(("127.0.0.1", 0), TicketRequestHandler, self.service, token="t0ken")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # 与 ResponseCompressionTests 同一套顺序:先登记 server_close、后登记 shutdown,
        # addCleanup 后进先出,才能保证先停 serve_forever 再关套接字(Windows 上否则 WinError 10038)。
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address

    def get(self, address, path):
        conn = http.client.HTTPConnection(address[0], address[1], timeout=10)
        conn.request("GET", path, headers={"X-Ticket-Token": "t0ken"})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw.decode("utf-8"))["result"]

    def fatten(self, ticket_id: str, mark: str = "只有全文才找得到的那句"):
        """把一张单填成线上那种样子:四个重键都有真内容。"""
        stored = self.service.store.load_ticket(ticket_id)
        stored["正文"] = f"{mark}·正文 " + "很长的一段交代" * 60
        stored["答复"] = f"{mark}·答复 " + "很长的一段答复" * 60
        stored["接线证据"] = {"文字": f"{mark}·接线 " + "证据" * 60,
                              "验证命令": "python -m pytest", "原样输出": "44 passed", "图片列表": []}
        stored["备注"] = f"{mark}·备注 " + "边界说明" * 30
        self.service.store.save_ticket(stored, "note", SLOT, "填厚")
        return stored

    def test_1_the_list_drops_exactly_the_keys_the_page_never_shows(self):
        """整份那一趟不发这四个键,而且**如实报出**摘掉了哪几个。"""
        ticket = self.dispatch("列表精简")
        self.fatten(ticket["编号"])
        row = {r["编号"]: r for r in self.service.list_cards()}[ticket["编号"]]
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertNotIn(key, row, f"{key} 网页一个字都不显示,不该占首次那一趟的体积")
        # ★空串和「没发过来」必须分得清:不给这个标,人会对着空正文去改单。
        self.assertEqual(["正文", "答复", "接线证据", "备注"], row["未发送字段"])
        # 落库那份一个字都没少——精简只发生在下发这一段。
        stored = self.service.store.load_ticket(ticket["编号"])
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, stored)

    def test_2_the_body_survives_exactly_where_the_page_shows_it(self):
        """正文只在 answerCard() 会渲染的那几张上保留:待答 + 拍板/疑问/需求 + 指派给设计者。

        ★这条是「按消费端裁」的核心。少了它就会出现一种最难查的坏法:
        设计者队列「要你答的」那一段每张卡的正文都是空的,而页面不报任何错。
        """
        asked = self.service.create_question("疑问", SLOT, "要设计者答的", "这一段正文页面上要显示")
        self.assertEqual("待答", asked["状态"])
        self.assertEqual("设计者", asked["指派给"])
        plain = self.dispatch("不显示正文的普通派单")
        self.fatten(plain["编号"])
        rows = {r["编号"]: r for r in self.service.list_cards()}
        self.assertEqual("这一段正文页面上要显示", rows[asked["编号"]]["正文"])
        self.assertNotIn("正文", rows[asked["编号"]]["未发送字段"])
        self.assertNotIn("正文", rows[plain["编号"]])
        # 答完之后就不再显示了,正文也就不必再发
        self.service.answer(asked["编号"], "已排期→T-000001", "设计者")
        answered = {r["编号"]: r for r in self.service.list_cards()}[asked["编号"]]
        self.assertNotIn("正文", answered, "答完的单 answerCard 不再渲染它,正文不该继续下发")

    def test_3_search_still_finds_words_that_only_live_in_dropped_keys(self):
        """★搜索不许静默降级:被摘掉的键里的词,服务端照样要搜得到,并回全文。

        原来 doSearch 是 JSON.stringify(整张单) 的本地索引。列表不发正文之后,
        若搜索还在本地精简行上匹配,搜正文里的词会一条都搜不到,**而且页面不报错**,
        人只会以为「确实没有这张单」——这类安静的少给结果是最坏的一种失败。
        """
        ticket = self.dispatch("搜得到吗")
        self.fatten(ticket["编号"], mark="独角兽暗号")
        address = self.serve()
        status, hits = self.get(address, "/api/tickets?q=" + quote("独角兽暗号"))
        self.assertEqual(200, status)
        self.assertEqual([ticket["编号"]], [row["编号"] for row in hits])
        # 命中的那几张要回**全文**:点进去就能看,不必再多跑一趟
        self.assertIn("独角兽暗号", hits[0]["正文"])
        self.assertIn("独角兽暗号", hits[0]["答复"])
        # 而不带 q 的那一趟仍然是精简的
        status, listing = self.get(address, "/api/tickets")
        self.assertEqual(200, status)
        self.assertNotIn("正文", listing[0])

    def test_4_opening_one_ticket_still_returns_the_whole_thing(self):
        """要看被摘掉的内容,走 /api/ticket/<编号>——那条路照旧回全文。"""
        ticket = self.dispatch("点开看全文")
        self.fatten(ticket["编号"])
        address = self.serve()
        status, row = self.get(address, f"/api/ticket/{ticket['编号']}")
        self.assertEqual(200, status)
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, row, f"点开单张不该缺 {key}")
        self.assertIn("开窗指令", row)

    def test_5_the_cli_and_the_offline_bundle_still_get_everything(self):
        """★精简只做在下发那一段,不能做进 list_tickets。

        离线包(build_bundle)背后没有服务端可以按需取全文,包里少了正文就是**永久**少了,
        而离线模式下的搜索正是靠本地索引——那时候它是唯一的一条路。
        CLI 的 list 与非业务看板也走 list_tickets,一并保持全文。
        """
        ticket = self.dispatch("离线包要全文")
        self.fatten(ticket["编号"])
        whole = {r["编号"]: r for r in self.service.list_tickets()}[ticket["编号"]]
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, whole, f"全文那条路不该摘掉 {key}")
        self.assertNotIn("未发送字段", whole, "全文行没摘过东西,不该挂这个标")
        bundle = json.loads(
            (self.service.build_bundle(self.root / "bundle.js")).read_text(encoding="utf-8")
            .removeprefix(f"window.{config_module.BUNDLE_GLOBAL} = ").rstrip(";\n"))
        packed = {r["编号"]: r for r in bundle["items"]}[ticket["编号"]]
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, packed, f"离线包不该摘掉 {key}——那边没有服务端可以补")

    def test_6_the_payload_really_shrinks(self):
        """行为判据:整份那一趟真的小下去了,不是只把键名改了改。

        钉的是**比值**不是绝对字节:绝对值会随单数涨,比值不会。
        """
        for index in range(12):
            self.keep_window_open()
            ticket = self.dispatch(f"填厚 {index}")
            self.fatten(ticket["编号"])
        whole = len(json.dumps(self.service.list_tickets(), ensure_ascii=False).encode("utf-8"))
        slim = len(json.dumps(self.service.list_cards(), ensure_ascii=False).encode("utf-8"))
        self.assertLess(slim * 2, whole, f"精简后 {slim} 相对全文 {whole} 省得不够多")

class ResponseCompressionTests(TicketTestCase):
    """响应压缩(T-001304)。现象是「刷新工单页要多等十几秒」。

    量下来病根**不在服务端算得慢**:取数 + 序列化只花 1.1 秒;
    而一次刷新下行 11.17 MB(工单 7.48 MB + 13 条对话线 3.59 MB),
    跨洋线路约 480 KB/s——光传就二十多秒。这些全是中文 JSON,gzip 压到 32%。
    ★命令行那条路(remote.py 用 http.client)默认不发 Accept-Encoding,
      所以一个字节都不受影响——这一条必须钉死,不然哪天有人给 CLI 加了这个头
      却没加解压,整条命令行会突然读到一堆二进制。
    """

    def serve(self):
        service = self.service
        server = TicketHTTPServer(("127.0.0.1", 0), TicketRequestHandler, service, token="t0ken")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # addCleanup 是后进先出:先登记 server_close、后登记 shutdown,
        # 才能保证**先** shutdown 停掉 serve_forever、**再** close 套接字。
        # 反过来会让 select() 拿到一个已经关掉的套接字,Windows 上抛 WinError 10038。
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address

    def fetch(self, address, path, accept_gzip: bool):
        headers = {"X-Ticket-Token": "t0ken"}
        if accept_gzip:
            headers["Accept-Encoding"] = "gzip"
        conn = http.client.HTTPConnection(address[0], address[1], timeout=10)
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response, raw

    def bulk(self, count: int = 40):
        for index in range(count):
            self.service.create_dispatch(
                SLOT, f"压缩用例填数据 {index} " + "长判语" * 60, ["DECISIONS.md:测试"],
                "主场景/UiRoot", self.worker, task_tier="乙",
                deliverables=[str(self.deliverable)], internal=False,
            )

    def test_1_a_gzip_client_gets_a_much_smaller_body(self):
        """声明能收 gzip 的客户端(浏览器)拿到压缩包,内容解开后与不压时逐字节相同。"""
        self.bulk()
        address = self.serve()
        plain_response, plain = self.fetch(address, "/api/tickets", accept_gzip=False)
        gzip_response, packed = self.fetch(address, "/api/tickets", accept_gzip=True)
        self.assertEqual("gzip", gzip_response.getheader("Content-Encoding"))
        self.assertEqual("Accept-Encoding", gzip_response.getheader("Vary"))
        self.assertIsNone(plain_response.getheader("Content-Encoding"))
        # 解开必须与不压的那份**一模一样**:压缩只许省带宽,不许改内容
        self.assertEqual(plain, gzip.decompress(packed))
        self.assertLess(len(packed), len(plain) // 2, f"压完 {len(packed)} 没有小于原来 {len(plain)} 的一半")
        # Content-Length 必须报压缩后的真实长度,否则客户端会读少或读挂
        self.assertEqual(len(packed), int(gzip_response.getheader("Content-Length")))

    def test_2_the_command_line_client_is_untouched(self):
        """★命令行那条路不发 Accept-Encoding,所以永远拿明文——这一条钉死。"""
        source = (ROOT / "ticket_desk" / "remote.py").read_text(encoding="utf-8")
        self.assertNotIn("Accept-Encoding", source)
        self.bulk(5)
        address = self.serve()
        response, raw = self.fetch(address, "/api/tickets", accept_gzip=False)
        self.assertIsNone(response.getheader("Content-Encoding"))
        json.loads(raw.decode("utf-8"))  # 明文能直接解析

    def test_3_small_replies_are_not_compressed(self):
        """几百字节的回执不压:压完可能更大,还白费一次 CPU。

        ★阈值看的是**压之前**的正文长度,不是压之后的——这里用一条错误回执
        (几十字节)来钉,别拿 /api/state 那种看着小、其实正文过 1KB 的当样本。
        """
        address = self.serve()
        response, raw = self.fetch(address, "/api/ticket/T-999999", accept_gzip=True)
        self.assertLess(len(raw), http_server_module.GZIP_MIN_BYTES)
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertIn("找不到工单", json.loads(raw.decode("utf-8"))["reason"])

class ConfigIsolationTests(unittest.TestCase):
    """★这一组钉的是「用例跑的是内置默认配置」这件事本身。

    真出过事：用户照 2-中型.md 建好 ticket_desk/config.json（三个位），
    再跑 pytest 是 454 failed / 59 passed，而 README 写着「519 passed」。
    病根是 config 会回落到包目录下的 config.json，而那份正是文档让他建的。

    ★这两条不是形式主义:谁哪天把顶上那段隔离删了,红的是这两条,
      而不是「另外五百条随机红」——后者没人知道该从哪儿查起。
    """

    def test_the_roster_is_the_builtin_default_not_the_runners(self):
        """在跑的名册必须是内置默认,不是这台机器上 config.json 里那份。"""
        self.assertEqual(tuple(config_module.DEFAULTS["位名"]), model.SLOTS)

    def test_a_config_json_in_the_package_folder_cannot_leak_into_a_subprocess(self):
        """★包目录里真放一份别的名册,子进程也必须照样跑默认名册。

        用真文件复现那次事故:写一份只有三个位的 config.json 到包目录,
        再起一个子进程问它名册——名册必须还是默认那十位。
        """
        package_config = Path(config_module.__file__).resolve().parent / "config.json"
        if package_config.exists():          # 运行者自己就有一份的话,别动他的
            self.skipTest("包目录下已有 config.json（运行者自己的），不覆盖它")
        package_config.write_text(json.dumps({
            "位名": ["甲位", "乙位", "丙位"],
            "总编排位": "丙位", "复检位": "乙位", "平台位": "丙位", "内容位": "甲位",
            "只分发不派单位": [],
            "部署目标": [{"名字": "production", "值面键": "deploy_head", "归位": "丙位"},
                         {"名字": "staging", "值面键": "staging_head", "归位": "乙位"}],
        }, ensure_ascii=False), encoding="utf-8")
        try:
            with tempfile.TemporaryDirectory() as room:
                result = run_local_cli(["list"], room)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotIn("甲位", result.stdout + result.stderr)
        finally:
            package_config.unlink(missing_ok=True)


class JudgingResolutionGateTests(TicketTestCase):
    """真实环境判据图交单口的闸（config「判据图闸」，★默认关）。

    ★★本类显式把闸打开(gate_env)。默认关是给没有取图链的人用的——
      但「关着能用」不等于「开着好使」,所以这几条必须在开着的时候跑。

    原口径（尺寸闸）：

    判据图自部署单 #12 起就是 2560×1440,工具默认(shot.py DEFAULT_RESOLUTION)早已是它,
    可仍有窗按 1280×720 交单——根因是任务书抄了过期前提,
    而**交单口一直没有闸**。
    ★★ 闸读的是**出处小文件里的 PNG_DIM 行**,绝不能量入单附件本身:
       工单台的图片管线把每张入单的图压到长边 ≤1280(四章 4 / 九章 8),照附件量**张张误伤**。
    ★ 只盖命令行这条路:网页上传拿不到同目录的出处 txt。
    """

    def shot_with_provenance(self, dimension: str, name: str = "world-shot.png") -> Path:
        """造一张判据图 + 它的出处小文件,出处里的尺寸行由调用方指定。

        ★图片本身故意造成 1600×900:与出处里写的尺寸**不一样**。
        这样一来,任何「改成量附件」的写法都会当场露馅——闸必须只看出处。
        """
        image = self.picture(name, size=(1600, 900))
        provenance = Path(provenance_path_for(str(image)))
        provenance.write_text(
            "取图机 = 用例\n"
            f"PNG = {image}\n"
            f"{model.PROVENANCE_DIM_KEY} = {dimension}\n"
            f"分辨率 = {dimension}\n",
            encoding="utf-8",
        )
        return image

    def gate_env(self) -> dict[str, str]:
        """写一份把「判据图闸」打开的 config，交给子进程。"""
        import json as _json
        path = Path(self.service.store.root) / "gate-on.json"
        path.write_text(_json.dumps(
            {"判据图闸": {"开": True, "尺寸": "2560x1440",
                          "布局例外尺寸": "1920x1080", "出处后缀": ".出处.txt"}},
            ensure_ascii=False), encoding="utf-8")
        return {"TICKET_CONFIG": str(path)}

    def attach_world(self, image: Path, ticket_id: str, *extra: str):
        return run_local_cli(
            ["attach", ticket_id, str(image), "--origin", "world", "--by", self.worker, *extra],
            self.service.store.root, **self.gate_env(),
        )

    def claimed(self):
        ticket = self.dispatch("判据图尺寸闸")
        self.service.claim(ticket["编号"], self.worker)
        return ticket["编号"]

    def test_1_a_2560_shot_goes_through(self):
        """出处写着 2560x1440 就放行——哪怕附件本身是别的尺寸(附件必然被压过)。"""
        ticket_id = self.claimed()
        result = self.attach_world(self.shot_with_provenance("2560x1440"), ticket_id)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("已附图", result.stdout)

    def test_2_a_1280_shot_is_rejected_with_the_exact_wording(self):
        """1280×720 拒收,并原样打出宪法那句话与本图实际尺寸。"""
        ticket_id = self.claimed()
        result = self.attach_world(self.shot_with_provenance("1280x720", "old.png"), ticket_id)
        self.assertNotEqual(0, result.returncode)
        message = result.stdout + result.stderr
        self.assertIn("判据图须 2560×1440", message)
        self.assertIn("config「判据图闸·尺寸」", message)
        self.assertIn("本图 1280x720", message)
        self.assertIn("--layout-extra", message)
        # 还要告诉人怎么做对,不能只说不行
        self.assertIn("默认输出本来就该是 2560x1440", message)

    def test_3_layout_extra_allows_1920_and_only_1920(self):
        """--layout-extra 只放行 1920×1080 这一个例外,别的尺寸照拒。"""
        ticket_id = self.claimed()
        ok = self.attach_world(
            self.shot_with_provenance("1920x1080", "layout.png"), ticket_id, "--layout-extra",
        )
        self.assertEqual(0, ok.returncode, ok.stderr)
        # 带着标记也不能把 1280 蒙混过去
        bad = self.attach_world(
            self.shot_with_provenance("1280x720", "layout-bad.png"), ticket_id, "--layout-extra",
        )
        self.assertNotEqual(0, bad.returncode)
        self.assertIn("判据图须 2560×1440", bad.stdout + bad.stderr)
        # 不带标记时 1920 也不行
        without = self.attach_world(self.shot_with_provenance("1920x1080", "layout2.png"), ticket_id)
        self.assertNotEqual(0, without.returncode)

    def test_4_a_shot_without_provenance_is_refused(self):
        """没有出处小文件 = 这张图不是取图链的产物(八章 12),拒收并说清怎么办。"""
        ticket_id = self.claimed()
        naked = self.picture("没有出处.png", size=(2560, 1440))
        result = self.attach_world(naked, ticket_id)
        self.assertNotEqual(0, result.returncode)
        message = result.stdout + result.stderr
        self.assertIn("找不到它的出处小文件", message)
        self.assertIn("八章 12", message)
        self.assertIn("重取一趟", message)

    def test_5_only_world_origin_is_gated(self):
        """隔离场景图与其他图不受这道闸管——它只管 world 判据图。"""
        ticket_id = self.claimed()
        naked = self.picture("隔离图.png", size=(800, 600))
        for origin in ("isolated", "other"):
            with self.subTest(来源=origin):
                result = run_local_cli(
                    ["attach", ticket_id, str(naked), "--origin", origin, "--by", self.worker],
                    self.service.store.root,
                )
                self.assertEqual(0, result.returncode, result.stderr)

    def test_6_the_gate_reads_the_sidecar_never_the_attachment(self):
        """★把这条口径钉死:闸只看出处,不许量附件。

        本用例造的图**永远是 1600×900**,而出处写 2560×1440——
        谁把实现改成「量附件本身」,test_1 立刻红:1600×900 不是 2560×1440。
        这就是 D9-444 特意写进宪法的那个坑:附件一律被压到 ≤1280,量附件张张误伤。
        """
        image = self.shot_with_provenance("2560x1440", "证明只读出处.png")
        with Image.open(image) as opened:
            self.assertEqual((1600, 900), opened.size)
        self.assertEqual(
            "2560x1440",
            read_provenance_dimension(Path(provenance_path_for(str(image))).read_text(encoding="utf-8")),
        )
        source = (ROOT / "ticket_desk" / "ticket.py").read_text(encoding="utf-8")
        self.assertIn("read_provenance_dimension(provenance.read_text(", source)

    def test_8_the_sidecar_name_follows_the_capture_chain(self):
        """出处文件名的算法必须与取图链一致:去掉扩展名再接 .出处.txt。"""
        self.assertEqual("D:/a/b/T-1-world.出处.txt", provenance_path_for("D:/a/b/T-1-world.png"))
        self.assertEqual(r"D:/a/b/T-1-world.出处.txt", provenance_path_for(r"D:\a\b\T-1-world.png"))
        # 取图链里 PNG 字段有时不带扩展名,那就直接接
        self.assertEqual("D:/a/T-1-world.出处.txt", provenance_path_for("D:/a/T-1-world"))


class NonBusinessGatesTests(TicketTestCase):
    """非业务闸不停车(T-001256,D9-453 / 宪法 v2.31 八章 6)。

    停过的窗**几乎全是账面闸**——
    交付项写 .jpg 台面存 .webp(T-000979)、目录型交付项(T-001191/T-001223)、
    工作树路径 vs 主检出路径(T-000876/877)、任务书待回核、远端镜像滞后(T-001240)、
    本机 CLI 不认新参数。**没有一件是活没做好**,而每一件都停掉了一整扇窗。
    ★这不是把闸拆了,是把闸分两种:拦「活没做好」的一个不动,拦「字没写对」的记一行继续走。
    """

    # ── ① 交付项归一化 ────────────────────────────────────────────────────
    def test_1_extension_differences_do_not_block_submit(self):
        """交付项写 .jpg、台面实际存 .webp:编号对上即同一份产物(T-000979)。

        落哪个扩展名是工单台自己的压缩管线按有没有透明通道决定的,员工建单时根本猜不到。
        """
        ticket = self.service.create_dispatch(
            SLOT, "图片交付项", ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
            task_tier="乙", deliverables=[f"{'T-000001'}-01.jpg"], internal=False,
        )
        self.service.claim(ticket["编号"], self.worker)
        # 台面上真正落盘的是 RGBA→webp
        self.service.attach(
            ticket["编号"], str(self.picture("透明.png", mode="RGBA")), "world", self.worker,
        )
        stored = self.service.store.load_ticket(ticket["编号"])
        names = [row["文件名"] for row in stored["图片列表"]]
        self.assertTrue(any(name.endswith(".webp") for name in names), names)
        # 交付项写的是 .jpg,照样交得了板
        submitted = self.service.submit(ticket["编号"], "登录后界面已出现")
        self.assertEqual("待判", submitted["状态"])

    def test_2_directory_and_worktree_paths_are_folded_to_repo_relative(self):
        """目录型交付项、以及工作树路径 vs 主检出路径,都折回仓相对路径再核。"""
        from ticket_desk.model import resolve_under_root

        # 目录型:exists() 而不是 is_file(),目录算数(T-001191/T-001223)
        (self.root / "产物目录").mkdir()
        self.assertIsNotNone(resolve_under_root("产物目录", self.root))
        self.assertIsNotNone(resolve_under_root("产物目录/", self.root))
        # 前缀不同、尾巴相同:两种绝对前缀都折得回来(T-000876/877)
        (self.root / "tools" / "x").mkdir(parents=True)
        (self.root / "tools" / "x" / "y.py").write_text("x\n", encoding="utf-8")
        for written in (
            "tools/x/y.py",
            "/repo-worktrees/wt-abc/tools/x/y.py",
            r"/repo/tools\x\y.py",
        ):
            with self.subTest(写法=written):
                self.assertIsNotNone(resolve_under_root(written, self.root))
        # 真的不存在的仍然找不到——这条闸只放行写法,不放行「活没做」
        self.assertIsNone(resolve_under_root("tools/x/根本没有.py", self.root))

    # ── ② 员工可改自己单交付项的写法 ──────────────────────────────────────
    def test_3_worker_may_reshape_only_the_path_form_of_their_own_ticket(self):
        """员工能改自己单交付项的**写法**;换成另一份产物、或改别人的单,仍被原闸拦住。"""
        ticket = self.service.create_dispatch(
            SLOT, "路径写法", ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
            task_tier="乙", deliverables=["ticket_desk/service.py"], internal=False,
        )
        self.service.claim(ticket["编号"], self.worker)
        # 同一份产物换个前缀:放行
        changed, _ = self.service.edit(
            ticket["编号"], self.worker,
            deliverables=["/repo-worktrees/wt-abc/ticket_desk/service.py"],
        )
        self.assertIn("wt-abc", changed["交付项"][0])
        rows = [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("事件") == "worker-reshape-deliverable"
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual(self.worker, rows[0]["发言人"])
        # ★换成另一份产物:不是写法问题,落回原来的署名闸
        with self.assertRaises(TicketError) as swapped:
            self.service.edit(ticket["编号"], self.worker, deliverables=["别处/service.py"])
        self.assertIn("改", str(swapped.exception))
        # 别人的单也不行
        other = self.service.create_dispatch(
            SLOT, "别人的单", ["DECISIONS.md:测试"], "主场景/UiRoot", self.worker,
            task_tier="乙", deliverables=["ticket_desk/service.py"], internal=False,
        )
        with self.assertRaises(TicketError):
            self.service.edit(other["编号"], "前端·界面与交互-99", deliverables=["ticket_desk/service.py"])

    # ── ④ 阻塞分业务/非业务 ───────────────────────────────────────────────
    def test_4_a_non_business_block_never_changes_the_state(self):
        """★非业务阻塞一个字都不改状态:员工窗接着做,不等任何人答复。"""
        ticket = self.dispatch("非业务阻塞")
        self.service.claim(ticket["编号"], self.worker)
        before = self.service.store.load_ticket(ticket["编号"])["状态"]
        result = self.service.block(
            ticket["编号"], "交付项后缀写错了", self.worker, service_module.BLOCK_NON_BUSINESS,
        )
        after = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual(before, after["状态"])
        self.assertEqual("已认领", after["状态"])
        self.assertEqual(1, len(after["非业务阻塞"]))
        self.assertIn("接着做", result["流程提示"])
        # 不进老化告警那一段:它根本不是阻塞态
        self.assertNotIn("阻塞", after["状态"])

    def test_5_business_blocks_still_stop_the_car(self):
        """业务阻塞照旧停车——「活没做好」那一类一个都没放松。"""
        ticket = self.dispatch("业务阻塞")
        self.service.claim(ticket["编号"], self.worker)
        blocked = self.service.block(ticket["编号"], "真源指针缺一条", "总编排")
        self.assertEqual("阻塞", blocked["状态"])
        self.assertEqual(service_module.BLOCK_BUSINESS, blocked["阻塞类型"])
        with self.assertRaises(TicketError):
            self.service.block(ticket["编号"], "再挂一次", "总编排")

    def test_6_the_board_and_the_digest_are_the_only_two_exits(self):
        """非业务不占队列,所以 list --nonbiz 与日览那两个数是它唯一的出口。"""
        ticket = self.dispatch("要清的非业务")
        self.service.claim(ticket["编号"], self.worker)
        self.service.block(
            ticket["编号"], "路径前缀不同", self.worker, service_module.BLOCK_NON_BUSINESS,
        )
        board = self.service.non_business_blocked(SLOT)
        self.assertEqual([ticket["编号"]], [row["编号"] for row in board])
        self.assertEqual(1, board[0]["条数"])
        digest = "\n".join(self.service.digest(24))
        self.assertIn("非业务 1", digest)
        self.assertIn("非业务不停车、不占队列", digest)
        # 命令行那张看板
        listed = run_local_cli(["list", "--nonbiz", "--slot", SLOT], self.service.store.root)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIn(ticket["编号"], listed.stdout)
        self.assertIn("路径前缀不同", listed.stdout)

    def test_7_the_kind_must_be_one_of_the_two(self):
        """阻塞类型二选一,填别的当场拒并把两个合法值列出来。"""
        ticket = self.dispatch("类型写错")
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError) as raised:
            self.service.block(ticket["编号"], "原因", "总编排", "随便写的")
        self.assertIn(service_module.BLOCK_BUSINESS, str(raised.exception))
        self.assertIn(service_module.BLOCK_NON_BUSINESS, str(raised.exception))


class ReworkFromReviewTests(TicketTestCase):
    """把「待复检」的单退回原位重做(T-001218,复检席报)。

    缺的是一条**状态边**:judge 只吃「待判」,于是判过之后才发现要重做的单谁都翻不动——
    曾有一批画在拍板环节被否,而当时「待复检态只有复检席能翻」,
    复检席手上也没有这个动作,最后只能 close --not-merged 一刀切成终态,单号作废、另开新单。
    这与 close --not-merged 是同一个缺口的两半:那边「到此为止」(终态),这边「还要接着做」(回返工)。
    """

    def to_review(self, title: str = "判过待复检"):
        ticket = self.to_judging()
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("待复检", ticket["状态"])
        return ticket

    def test_1_review_slot_can_bounce_it_back_to_rework(self):
        """复检席把待复检的单打回「返工」:单号、任务书、断点全留住。"""
        ticket = self.to_review()
        before = ticket["任务书路径"] if ticket.get("任务书路径") else ""
        bounced = self.service.rework_from_review(
            ticket["编号"], "这一批的画被拍板人否了", "复检·合并与部署",
        )
        self.assertEqual("返工", bounced["状态"])
        self.assertEqual(ticket["编号"], bounced["编号"])
        self.assertEqual(before, bounced.get("任务书路径", ""))
        self.assertEqual(1, bounced["返工次数"])
        self.assertEqual("这一批的画被拍板人否了", bounced["返工原因列表"][-1]["原因"])
        # 落库的也是同一份,不是只在返回值上好看
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("返工", stored["状态"])

    def test_2_the_opened_stamp_is_cleared_so_it_lands_back_in_the_queue(self):
        """与 judge --rework、unblock 同口径:戳记清掉,当场回「要你传达的」,不必等 8 小时线。"""
        # 真实时序:设计者在「已认领」那会儿点的已开窗,一路交板判过之后戳记还留着
        #(judge --pass 不清它,留作历史记录),所以到「待复检」时它仍在。
        ticket = self.dispatch("戳记要被清掉")
        self.service.claim(ticket["编号"], self.worker)
        self.service.open_window(ticket["编号"], "设计者", "model-b")
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        self.service.submit(ticket["编号"], "登录后界面已出现")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertIsNotNone(self.service.store.load_ticket(ticket["编号"])["已开窗"])
        self.service.rework_from_review(ticket["编号"], "口径写错了要重出", "复检·合并与部署")
        self.assertIsNone(self.service.store.load_ticket(ticket["编号"])["已开窗"])

    def test_3_blame_defaults_to_the_question_not_the_model(self):
        """★默认记「出题」账,不是「模型」账。

        触发这条边的典型情形正是复检席报的那种:任务书口径被否了、执行方照做没错。
        默认记模型账 = 凭空给执行方记一次判退,模型合格率就脏了。
        """
        ticket = self.to_review()
        bounced = self.service.rework_from_review(ticket["编号"], "口径被否", "复检·合并与部署")
        self.assertEqual("出题", bounced["判退责任"])
        staff = self.service.store.load_staff()
        self.assertEqual({}, staff.get("模型记分") or {})
        self.assertEqual(1, int((staff.get("出题记分") or {})[SLOT]["合计"]))
        # 要记模型账必须显式写
        other = self.to_review("第二张")
        self.service.rework_from_review(other["编号"], "画得不对", "复检·合并与部署", "模型")
        self.assertTrue(self.service.store.load_staff().get("模型记分"))

    def test_4_all_four_signers_pass_and_a_stranger_is_told_who_can(self):
        """四个署名位都放行——今天这一张正是「本位翻不动、复检席也翻不动」卡住的。"""
        for signer in (SLOT, "复检·合并与部署", service_module.CONDUCTOR_SLOT, "设计者"):
            with self.subTest(署名=signer):
                ticket = self.to_review(f"退回-{signer}")
                self.assertEqual(
                    "返工",
                    self.service.rework_from_review(ticket["编号"], "口径被否", signer)["状态"],
                )
        blocked = self.to_review("别位来退")
        with self.assertRaises(TicketError) as raised:
            self.service.rework_from_review(blocked["编号"], "口径被否", "前端·视觉与资源")
        # 拒的时候必须说得出谁可以,只回「没有权限」会把人卡在原地
        self.assertIn(SLOT, str(raised.exception))
        self.assertIn("复检·合并与部署", str(raised.exception))

    def test_5_reason_is_required_and_only_review_state_is_accepted(self):
        """--reason 必填;受理态只有「待复检」,待判的仍走 judge --rework。"""
        ticket = self.to_review()
        with self.assertRaises(TicketError) as blank:
            self.service.rework_from_review(ticket["编号"], "   ", "复检·合并与部署")
        self.assertIn("--reason", str(blank.exception))
        judging = self.to_judging()
        with self.assertRaises(TicketError) as wrong_state:
            self.service.rework_from_review(judging["编号"], "口径被否", "复检·合并与部署")
        self.assertIn("待复检", str(wrong_state.exception))
        self.assertIn("judge --rework", str(wrong_state.exception))

    def test_6_the_command_line_and_the_page_offer_the_same_door(self):
        """命令行端到端;网页那条 op 的 blame 默认值必须与 CLI 同一个,否则两处记出两本账。"""
        ticket = self.to_review()
        result = run_local_cli([
            "rework", ticket["编号"], "--reason", "这一批被拍板人否了", "--by", "复检·合并与部署",
        ], self.service.store.root)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("返工", result.stdout)
        self.assertEqual("返工", self.service.store.load_ticket(ticket["编号"])["状态"])
        source = (ROOT / "ticket_desk" / "http_server.py").read_text(encoding="utf-8")
        self.assertIn('str(data.get("blame", "") or "出题")', source)

    def test_7_the_event_line_says_where_it_came_from(self):
        """事件线要记 rework-from-review,并把责任与「戳记已清」写进说明。"""
        ticket = self.to_review()
        self.service.rework_from_review(ticket["编号"], "口径被否", "复检·合并与部署")
        rows = [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("事件") == "rework-from-review"
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual(ticket["编号"], rows[0]["工单号"])
        self.assertIn("出题", rows[0]["说明"])
        self.assertIn("已开窗标记已清", rows[0]["说明"])


class SelfOwnedMergeAndNotMergedCloseTests(TicketTestCase):
    """T-000908(答复检席 T-000899/T-000902):两道闸原来拦住的是正当动作。

    ①本位自有单(复检席的部署单、平台位的工单台单)执行方是本位员工、判卷人与复检人都只能是本位总监,
      三方互斥在这里数学上无解,单子判过之后永远出不去(T-000849/T-000851 上撞到)。宪法 v2.20 ⑤ 已批。
    ②「判过了但正确的处置就是不并线」原来没有出路:close 要求「实机复验过」,于是烂在待复检里。
    """

    def to_judged(self, judge: str = "UI总监"):
        ticket = self.service.create_dispatch(
            SLOT, "不并线与自记并线", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        ticket, _ = self.service.judge(ticket["编号"], True, judge, verdict=PASS_VERDICT)
        return ticket

    def test_self_owned_merge_is_allowed_and_marked_as_self_recorded(self):
        """所属位 == 判卷人 == 复检人 时放行,但复检人写成「自记·待设计者终验」,不伪装第三方。"""
        ticket = self.to_judged(judge=SLOT)
        merged = self.service.merge(ticket["编号"], SLOT)
        self.assertEqual("已合并", merged["状态"])
        self.assertIn("自记", merged["复检人"])
        self.assertIn("待设计者终验", merged["复检人"])
        self.assertIn(SLOT, merged["复检人"])

    def test_cross_slot_three_way_gate_still_holds(self):
        """别位的单照旧:判卷人自己来复检仍然拒,报错里要说得出本位自有单那条例外。"""
        ticket = self.to_judged(judge="UI总监")
        # 复验先补上(D9-460 ①):别位的单不走 self_owned 例外,不补就先撞「还没复验」,
        # 测不到这里要钉的三方互斥闸。
        self.verified(ticket["编号"])
        with self.assertRaises(TicketError) as blocked:
            self.service.merge(ticket["编号"], "UI总监")
        self.assertIn("复检人必须与执行员工、判卷人都不同", str(blocked.exception))
        self.assertIn("本位自有单", str(blocked.exception))

    def test_not_merged_close_needs_a_reason(self):
        """不并线结案必须写原因——没有原因的不并线,事后与「忘了并」分不清。"""
        ticket = self.to_judged()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT, not_merged=True, reason="   ")
        self.assertIn("--reason 必填", str(blocked.exception))

    def test_not_merged_close_from_pending_review_records_the_reason(self):
        """待复检 → 关闭,原因落库并进备注,事件名与普通关闭区分开。"""
        ticket = self.to_judged()
        closed = self.service.close(
            ticket["编号"], SLOT, not_merged=True, reason="画风作废,设计者 D9-407②",
        )
        self.assertEqual("关闭", closed["状态"])
        self.assertIn("画风作废", closed["备注"])
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("关闭", reloaded["状态"])
        self.assertIn("不并线结案", reloaded["备注"])

    def to_pending_judgement(self):
        """停在「待判」:交板了,但还没有人判。"""
        ticket = self.service.create_dispatch(
            SLOT, "待判不并线", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        return self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")

    def test_not_merged_close_from_pending_judgement_needs_a_verdict(self):
        """T-001499 ②(在 T-001470 上撞到):待判就是还没判过。

        这条路原来对「待复检」与「待判」一视同仁,于是从待判直接关掉的单落成终态,
        判卷人与判语却都是空——事后没人说得出这活到底行不行、是谁看过的。
        """
        ticket = self.to_pending_judgement()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT, not_merged=True, reason="产物是结论不是代码")
        message = str(blocked.exception)
        self.assertIn("还没判过", message)
        self.assertIn("--verdict", message)
        self.assertIn("judge", message, "拦下要给出另一条路,不能只说不行")
        self.assertEqual(
            "待判", self.service.store.load_ticket(ticket["编号"])["状态"], "拦下就不许动状态",
        )

    def test_not_merged_close_from_pending_judgement_records_the_verdict(self):
        """带 --verdict 就一并补判:判卷人与判语都要真落库,不能只落状态。"""
        ticket = self.to_pending_judgement()
        closed = self.service.close(
            ticket["编号"], SLOT, not_merged=True, reason="产物是结论不是代码",
            verdict="判过。判的是提交 abc1234。产物是结论,不并线。",
        )
        self.assertEqual("关闭", closed["状态"])
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual(SLOT, reloaded["判卷人"])
        self.assertIn("abc1234", reloaded["判语"])
        self.assertIn("产物是结论不是代码", reloaded["备注"])

    def test_not_merged_close_refuses_a_verdict_when_the_ticket_was_already_judged(self):
        """待复检那一态判语已经在单上,再带 --verdict 就是覆盖判过的话,拦下。"""
        ticket = self.to_judged()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(
                ticket["编号"], SLOT, not_merged=True, reason="画风作废", verdict="另写一句",
            )
        self.assertIn("判语已经在单上", str(blocked.exception))
        self.assertEqual(PASS_VERDICT, self.service.store.load_ticket(ticket["编号"])["判语"])

    def test_cli_exposes_the_verdict_switch_for_a_pending_judgement_close(self):
        """argparse 少写一行,服务端做对了也用不上——这条和 --reason 那条一样要真跑一遍 CLI。"""
        root = self.root / "cli-not-merged-verdict"
        service = TicketService(TicketStore(root))
        worker = service.staff_new(SLOT, "model-a")["员工名"]
        deliverable = root / "产物.md"
        deliverable.write_text("# 产物\n", encoding="utf-8")
        ticket = service.create_dispatch(
            SLOT, "命令行待判不并线", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(deliverable)], internal=True,
        )
        service.claim(ticket["编号"], worker)
        service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        refused = run_local_cli(
            ["close", ticket["编号"], "--by", SLOT, "--not-merged", "--reason", "产物是结论"], root,
        )
        self.assertEqual(2, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("--verdict", refused.stdout + refused.stderr)
        done = run_local_cli([
            "close", ticket["编号"], "--by", SLOT, "--not-merged",
            "--reason", "产物是结论", "--verdict", "判过。判的是提交 abc1234。不并线。",
        ], root)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        reloaded = service.store.load_ticket(ticket["编号"])
        self.assertEqual("关闭", reloaded["状态"])
        self.assertIn("abc1234", reloaded["判语"])

    def test_not_merged_close_is_refused_from_a_working_state(self):
        """只给判过之后的单:已认领态不许走这条路,免得拿它当作废用。"""
        ticket = self.service.create_dispatch(
            SLOT, "还在做", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT, not_merged=True, reason="不想做了")
        self.assertIn("待复检", str(blocked.exception))

    def test_not_merged_close_is_refused_for_an_unrelated_slot(self):
        """别位总监不能替人结案,报错里要说得出谁可以。"""
        ticket = self.to_judged()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], OTHER_SLOT, not_merged=True, reason="我看不顺眼")
        self.assertIn(SLOT, str(blocked.exception))
        self.assertIn("复检·合并与部署", str(blocked.exception))

    def test_normal_close_path_is_untouched(self):
        """老路一个字没动:实机复验过才能普通关闭,不是实机复验过仍然拒。"""
        ticket = self.to_judged()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT)
        self.assertIn("实机复验过", str(blocked.exception))

    def test_cli_exposes_not_merged_and_reason(self):
        """命令行那一头也要真的认这条路:argparse 少写一行,服务端做对了也用不上。"""
        root = self.root / "cli-not-merged"
        service = TicketService(TicketStore(root))
        worker = service.staff_new(SLOT, "model-a")["员工名"]
        deliverable = root / "产物.md"
        deliverable.write_text("# 产物\n", encoding="utf-8")
        ticket = service.create_dispatch(
            SLOT, "命令行不并线", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(deliverable)], internal=True,
        )
        service.claim(ticket["编号"], worker)
        service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        refused = run_local_cli(["close", ticket["编号"], "--by", SLOT, "--not-merged"], root)
        self.assertEqual(2, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("--reason 必填", refused.stdout + refused.stderr)
        done = run_local_cli(
            ["close", ticket["编号"], "--by", SLOT, "--not-merged", "--reason", "需求被后来的单取代"], root,
        )
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual("关闭", service.store.load_ticket(ticket["编号"])["状态"])


class CurrentStateBoardTests(TicketTestCase):
    """T-000892(答 T-000869,D9-424 ③):当前值落工单台一处机器可读,各位读它不要转抄。

    转抄之所以会错,是因为这几个数每天都在变而抄件不会自己更新——T-000844 就是照着
    抄错的判据图尺寸做的,整张单作废。所以写口子只留给复检席与总编排,别位一律拒。
    """

    WRITER = "复检·合并与部署"
    TOOLS = "取图链=已改未并,在 feat/shot 上；ticket.py=T-000891 待复检"

    def tickets_root(self) -> Path:
        return self.root / "tickets"

    def state_get(self) -> subprocess.CompletedProcess:
        return run_local_cli(["state", "get"], self.tickets_root())

    def test_r4_1_review_slot_and_orchestrator_may_both_write(self):
        """两个合法署名位都要真的能写,写完值落库。"""
        first = self.service.state_set("screenshot_resolution", "1280x720", self.WRITER)
        self.assertEqual("1280x720", first["新值"])
        second = self.service.state_set("staging_head", "C0ED7A843", "总编排")
        self.assertEqual("c0ed7a843", second["新值"])
        board = self.service.state_board()
        self.assertEqual("1280x720", board["值"]["screenshot_resolution"])
        self.assertEqual("c0ed7a843", board["值"]["staging_head"])

    def test_r4_2_other_slots_and_staff_are_refused_with_both_writers_named(self):
        """别位总监与员工一律拒,报错里两个合法署名位都要出现——不然被拒的人不知道该找谁。"""
        for actor in (SLOT, OTHER_SLOT, self.worker, "设计者", ""):
            with self.assertRaises(TicketError) as blocked:
                self.service.state_set("screenshot_resolution", "1280x720", actor)
            message = str(blocked.exception)
            self.assertIn("复检·合并与部署", message)
            self.assertIn("总编排", message)
        self.assertIsNone(self.service.state_board()["值"]["screenshot_resolution"])

    def test_r4_3_unknown_key_is_refused_with_every_legal_key_listed(self):
        """键不认识就拒,并且把合法键原样列全:只说「键不对」等于让人猜。"""
        with self.assertRaises(TicketError) as blocked:
            self.service.state_set("没有这个键", "c0ed7a8", "总编排")
        message = str(blocked.exception)
        for key in ("screenshot_resolution", "staging_head", "deploy_head",
                    "latency_budget", "pending_shared_tools"):
            self.assertIn(key, message)

    def test_r4_4_state_get_prints_machine_readable_json_that_matches_what_was_set(self):
        """机器可读是本单的正题:stdout 必须是干净 JSON,值要对得上。"""
        self.service.state_set("screenshot_resolution", "1280x720", self.WRITER)
        self.service.state_set("latency_budget", "p50=120,p99=480", self.WRITER)
        self.service.state_set("pending_shared_tools", self.TOOLS, "总编排")
        result = self.state_get()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        board = json.loads(result.stdout)
        self.assertEqual("1280x720", board["值"]["screenshot_resolution"])
        self.assertEqual({"p50": 120, "p99": 480}, board["值"]["latency_budget"])
        self.assertEqual(
            [{"名字": "取图链", "状态": "已改未并,在 feat/shot 上"},
             {"名字": "ticket.py", "状态": "T-000891 待复检"}],
            board["值"]["pending_shared_tools"],
        )

    def test_r4_4b_untouched_keys_stay_empty_and_the_cli_says_who_should_fill_them(self):
        """没填过的键就是空;提示走 stderr,stdout 仍是能直接 json.loads 的 JSON。"""
        result = self.state_get()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        board = json.loads(result.stdout)
        self.assertEqual(list(board["值"]), board["未填"])
        self.assertTrue(all(value is None for value in board["值"].values()))
        self.assertIn("未填", result.stderr)
        self.assertIn("state set", result.stderr)
        self.assertIn("复检席", result.stderr)

    def test_r4_5_every_set_writes_who_when_and_old_to_new_into_the_log(self):
        """一条日志要能回答:谁、何时、哪个键、旧值 → 新值。"""
        self.service.state_set("screenshot_resolution", "1280x720", self.WRITER)
        self.service.state_set("screenshot_resolution", "2560x1440", "总编排")
        rows = [row for row in self.service.store.read_jsonl(self.service.store.log_path)
                if row.get("事件") == "state-set"]
        self.assertEqual(2, len(rows))
        self.assertEqual([self.WRITER, "总编排"], [row["发言人"] for row in rows])
        self.assertTrue(all(row["时间"] for row in rows))
        self.assertEqual(["screenshot_resolution", "screenshot_resolution"], [row["值面键"] for row in rows])
        self.assertIsNone(rows[0]["旧值"])
        self.assertEqual("1280x720", rows[1]["旧值"])
        self.assertEqual("2560x1440", rows[1]["新值"])
        self.assertIn("1280x720 → 2560x1440", rows[1]["说明"])
        latest = self.service.state_board()["最近改动"]["screenshot_resolution"]
        self.assertEqual("总编排", latest["改动人"])
        self.assertEqual("1280x720", latest["旧值"])

    def test_r4_6_receipt_ends_with_the_summary_and_shows_未填_for_empty_items(self):
        """员工窗第 0 步跑 receipt 就该看见这四项;摘要必须是最后一行,空的显示「未填」。"""
        ticket = self.dispatch("值面摘要")
        self.service.state_set("screenshot_resolution", "1280x720", self.WRITER)
        result = run_local_cli(["receipt", ticket["编号"]], self.tickets_root())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        summary = lines[-1]
        self.assertIn("当前值面", summary)
        self.assertIn("判据图 1280x720", summary)
        # 没填的三项照样露面,写着「未填」——看不见的项等于逼人回去翻接管件。
        self.assertIn("部署头 未填/未填", summary)
        self.assertIn("延迟预算 未填", summary)
        self.assertIn("已改未并公共工具 未填", summary)
        self.assertNotIn("当前值面", "\n".join(lines[:-1]))

    def test_bad_values_are_refused_with_the_写法_spelled_out(self):
        """形状不对当场拒,并把该怎么写原样说出来;拒掉的不许留半个坏值。"""
        cases = {
            "screenshot_resolution": "1280*720",
            "staging_head": "头一个提交",
            "latency_budget": "p50=120",
            "pending_shared_tools": "取图链",
        }
        for key, value in cases.items():
            with self.assertRaises(TicketError) as blocked:
                self.service.state_set(key, value, self.WRITER)
            self.assertIn(key, str(blocked.exception))
            self.assertIsNone(self.service.state_board()["值"][key])

    def test_values_survive_the_sqlite_backend_and_the_web_bundle(self):
        """值和工单库同一处存:换 SQLite 后端照样读得出,网页包也带上这一份。"""
        store = SqliteStore(self.root / "db" / "tickets.sqlite3")
        service = TicketService(store)
        service.state_set("deploy_head", "d03bdff9f", "总编排")
        self.assertEqual("d03bdff9f", TicketService(SqliteStore(store.database)).state_board()["值"]["deploy_head"])
        bundle = service.build_bundle(self.root / "bundle.js")
        payload = json.loads(bundle.read_text(encoding="utf-8").split("=", 1)[1].strip().rstrip(";"))
        self.assertEqual("d03bdff9f", payload["state"]["值"]["deploy_head"])

class QuestionAssigneeAndPendingTests(TicketTestCase):
    """T-000957(答 T-000953 与设计者「美术总监之间发不了工单」):

    「指派给」是各位扫自己活的那一格。D9-424 ⑥ 把需求/阻塞的答复权交给了所属总监位,
    可建单时还把它们一律指给总编排——收件位按「指派给」扫**看不见本该自己答的单**,
    发的人以为没发出去。实测全台面待答 40 张里 22 张这么错位,最老的单号还在三四百段。
    """

    def test_cross_slot_demand_lands_on_the_recipient_slot(self):
        """跨位发需求:指派给 = 收件位,不再是总编排——这是「发不出去」的真因。"""
        ticket = self.service.create_question(
            "需求", OTHER_SLOT, "地面位发给别位的需求", "请把接口补上", SLOT,
        )
        self.assertEqual(OTHER_SLOT, ticket["所属总监位"])
        self.assertEqual(OTHER_SLOT, ticket["指派给"])

    def test_cross_slot_question_still_lands_on_the_recipient_slot(self):
        """疑问的跨位规则本来就是对的,别改坏了。"""
        ticket = self.service.create_question("疑问", OTHER_SLOT, "跨位疑问", "问一句", SLOT)
        self.assertEqual(OTHER_SLOT, ticket["指派给"])

    def test_self_question_still_goes_to_the_designer(self):
        """自己位上的疑问仍然送设计者,这一档没动。"""
        ticket = self.service.create_question("疑问", SLOT, "本位疑问", "问一句", "设计者")
        self.assertEqual("设计者", ticket["指派给"])

    def test_demand_filed_against_the_conductor_stays_with_the_conductor(self):
        """发给总编排的需求仍归总编排——别把该他答的也推走。"""
        ticket = self.service.create_question("需求", "总编排", "发给总编排的需求", "请裁定", SLOT)
        self.assertEqual("总编排", ticket["指派给"])

    def test_pending_answer_line_lists_the_ids_and_is_empty_when_clean(self):
        """inbox 尾巴那一行:有待答就列出张数与单号;一张都不欠时不打空行。"""
        self.assertEqual("", self.service.pending_answer_line(OTHER_SLOT))
        first = self.service.create_question("需求", OTHER_SLOT, "第一张", "正文", SLOT)
        second = self.service.create_question("疑问", OTHER_SLOT, "第二张", "正文", SLOT)
        line = self.service.pending_answer_line(OTHER_SLOT)
        self.assertIn("待答 2 张", line)
        self.assertIn(first["编号"], line)
        self.assertIn(second["编号"], line)
        # 答掉一张,行里就只剩另一张
        self.service.answer(first["编号"], "受理,明天给", OTHER_SLOT)
        line = self.service.pending_answer_line(OTHER_SLOT)
        self.assertIn("待答 1 张", line)
        self.assertNotIn(first["编号"], line)

    def test_backfill_only_lists_until_apply_is_given(self):
        """回填默认只打清单不写——里面可能真有该总编排答的,先给人看一眼。"""
        ticket = self.service.create_question("需求", OTHER_SLOT, "历史错位单", "正文", SLOT)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["指派给"] = "总编排"          # 造一张老口径的单
        self.service.store.save_ticket(stored, "set", SLOT, "造历史错位")
        dry = self.service.backfill_question_assignees(False)
        self.assertEqual(1, dry["命中"])
        self.assertFalse(dry["已写入"])
        self.assertEqual("总编排", self.service.store.load_ticket(ticket["编号"])["指派给"])
        done = self.service.backfill_question_assignees(True)
        self.assertEqual(1, done["命中"])
        self.assertTrue(done["已写入"])
        self.assertEqual(OTHER_SLOT, self.service.store.load_ticket(ticket["编号"])["指派给"])

    def test_backfill_leaves_the_conductors_own_demands_alone(self):
        """所属位就是总编排的那些,回填一个都不许碰。"""
        ticket = self.service.create_question("需求", "总编排", "该他答的", "正文", SLOT)
        report = self.service.backfill_question_assignees(True)
        self.assertNotIn(ticket["编号"], [row["编号"] for row in report["明细"]])
        self.assertEqual("总编排", self.service.store.load_ticket(ticket["编号"])["指派给"])

    def test_a_decision_ticket_is_not_counted_against_the_slot_that_filed_it(self):
        """总编排判退第一轮点出的真例:拍板单送设计者,发起位不欠它(T-000959 那一类)。"""
        decision = self.service.create_question(
            "拍板", SLOT, "发起位送设计者的拍板", VALID_DECISION_BODY, SLOT,
        )
        self.assertEqual("设计者", decision["指派给"])
        self.assertEqual(SLOT, decision["所属总监位"])
        self.assertEqual([], self.service.pending_answers(SLOT))
        self.assertEqual("", self.service.pending_answer_line(SLOT))

    def test_a_self_slot_question_waiting_on_the_designer_is_not_counted_either(self):
        """本位自己提给设计者的疑问同理:指派给是设计者,本位没欠。"""
        self.service.create_question("疑问", SLOT, "等设计者答的疑问", "问一句", SLOT)
        self.assertEqual([], self.service.pending_answers(SLOT))

    def test_the_line_still_counts_what_this_slot_really_owes(self):
        """反向:真该本位答的仍要数进去,别为了修上面那条把闸修哑。"""
        mine = self.service.create_question("需求", SLOT, "别位发来的需求", "请办", OTHER_SLOT)
        self.assertEqual(SLOT, mine["指派给"])
        self.assertIn(mine["编号"], [row["编号"] for row in self.service.pending_answers(SLOT)])
        self.assertIn(mine["编号"], self.service.pending_answer_line(SLOT))

    def test_cli_inbox_tail_and_pending_mine(self):
        """命令行两头都要真的认:inbox 尾巴带待答行、list --pending-mine 列得出来。"""
        root = self.root / "cli-pending"
        service = TicketService(TicketStore(root))
        service.staff_new(SLOT, "model-a")
        ticket = service.create_question("需求", SLOT, "命令行待答", "正文", OTHER_SLOT)
        shown = run_local_cli(["inbox", "--slot", SLOT, "--for", SLOT], root)
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertIn("你位当前待答", shown.stdout)
        self.assertIn(ticket["编号"], shown.stdout)
        mine = run_local_cli(["list", "--slot", SLOT, "--pending-mine"], root)
        self.assertEqual(0, mine.returncode, mine.stderr)
        self.assertIn(ticket["编号"], mine.stdout)


class InternalMergedIsTerminalTests(TicketTestCase):
    """T-001053(落宪法 v2.20 ④):内部单并线即到头,不该再老化、不该再占「上服」那一格。

    「这几个 live 部署单为什么卡住」——T-000559 卡 38 小时、
    T-000628 卡 37 小时,两张都是已合并的内部单,没有人该动它们,是工具没跟上宪法。
    """

    def merged(self, internal: bool):
        ticket = self.service.create_dispatch(
            SLOT, "并线到头", ["DECISIONS.md:测试"], "工单台" if internal else "主场景/UiRoot",
            self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=internal,
        )
        self.service.claim(ticket["编号"], self.worker)
        if internal:
            self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        else:
            self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
            self.service.submit(ticket["编号"], "登录后界面已出现")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        return self.merged_ticket(ticket["编号"], "独立复检")

    def test_internal_merged_never_goes_stale(self):
        """内部单并线之后,过多久都不算「卡住了」。"""
        ticket = self.merged(True)
        later = datetime.now().astimezone() + timedelta(hours=200)
        self.assertIsNone(self.service.stale_info(ticket, later))
        self.assertTrue(service_module.is_terminal(ticket))

    def test_user_facing_merged_still_goes_stale(self):
        """用户可感知单不适用:它还欠一张真登录图,超 24 小时照旧算卡住。"""
        ticket = self.merged(False)
        later = datetime.now().astimezone() + timedelta(hours=30)
        info = self.service.stale_info(ticket, later)
        self.assertIsNotNone(info)
        self.assertEqual("已合并", info["状态"])
        self.assertFalse(service_module.is_terminal(ticket))

    def test_digest_stall_count_leaves_internal_merged_out(self):
        """日览的停滞段也读同一条判据,别一处改一处不改。"""
        self.merged(True)
        lines = "\n".join(self.service.digest(24))
        self.assertNotIn("并线到头", lines.split("[停滞]", 1)[-1] if "[停滞]" in lines else "")

def unwritable_memory_path(root: Path) -> Path:
    """一条**必然**写不成的记忆件路径，用来钉「重刷失败不许拖垮交板」。

    任务书写的是「指到一个不存在的盘符」。盘符在 Windows 上要现找一个空的（写死 Q:
    可能正好有人挂了盘），在 Linux 上根本不存在这个概念——服务器上跑同一套用例，
    `Q:\\x\\y.md` 只是个相对文件名，会**写成功**，用例就假绿了。
    所以两边都取同一类失败：让父目录是一个已经存在的**普通文件**，mkdir 必抛 OSError。
    """
    blocker = root / "这是个文件不是目录"
    blocker.write_text("x", encoding="utf-8")
    return blocker / "工位记忆" / "记忆.md"


class SlotMemoryTests(TicketTestCase):
    """固定工位的记忆闭环（T-001073）。

    ★三条口径 + 两条硬约束：
    · 名册记「固定工位」与记忆 md 路径，开窗卡自动插「先读记忆」；
    · 记忆件的骨架由工具从**已交板**的单自动生成，每一行都回指到某张单，员工只补一小节；
    · 交板留「给下一窗」，判卷人可以划掉错的行——划掉不是删除。
    ★记忆件是快照：第 0 步「先核分支头与绿数」写死在生成物顶部，不是可选项。
    """

    def setUp(self) -> None:
        super().setUp()
        self.memory = self.root / "工位记忆" / f"{self.worker}.md"

    # ── 造数据 ────────────────────────────────────────────────────────────
    def internal_dispatch(self, title: str = "内部工具单", assign: str | None = None):
        self.serial = getattr(self, "serial", 0) + 1
        taskbook = self.root / f"tb-{self.serial}.md"
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", self.worker if assign is None else assign,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True, taskbook=str(taskbook),
        )

    def submitted(self, title: str = "内部工具单", handoff: str = "", raw: str = "44 passed"):
        ticket = self.internal_dispatch(title)
        self.service.claim(ticket["编号"], self.worker)
        return self.service.submit(
            ticket["编号"], "内部验证", "python -m pytest", raw, handoff=handoff,
        )

    def generated(self, out: Path | None = None, max_lines: int = 400) -> str:
        path = self.service.memory_export(self.worker, out or self.memory, max_lines)
        return Path(path).read_text(encoding="utf-8")

    # ── R5 1 ──────────────────────────────────────────────────────────────
    def test_1_staff_fix_marks_the_slot_and_unfix_keeps_the_path(self):
        """staff fix 打标记与路径；staff unfix 取消标记，但路径**保留**便于回看。"""
        member = self.service.staff_fix(self.worker, SLOT, str(self.memory))
        self.assertTrue(member["固定工位"])
        self.assertEqual(str(self.memory), member["记忆md路径"])
        # 父目录还不在只提醒不拦：记忆件是 memory export 生成的，先有标记才有第一次导出。
        self.assertIn("父目录还不在", member["提示"])
        listed = next(row for row in self.service.list_staff(SLOT) if row["员工名"] == self.worker)
        self.assertTrue(listed["固定工位"])
        released = self.service.staff_unfix(self.worker, SLOT)
        self.assertFalse(released["固定工位"])
        self.assertEqual(str(self.memory), released["记忆md路径"])
        self.assertEqual("", self.service.staff_memory_path(self.worker))
        # 落盘也要是这个样子，不能只在返回值里对。
        again = next(row for row in self.service.list_staff(SLOT) if row["员工名"] == self.worker)
        self.assertFalse(again["固定工位"])
        self.assertEqual(str(self.memory), again["记忆md路径"])

    # ── R5 2 ──────────────────────────────────────────────────────────────
    def test_2_only_the_owning_slot_or_the_conductor_may_fix(self):
        """别位总监与员工窗跑 staff fix 被拒，报错要说得出谁可以。"""
        for actor in (OTHER_SLOT, self.worker, "设计者"):
            with self.subTest(actor=actor):
                with self.assertRaises(TicketError) as caught:
                    self.service.staff_fix(self.worker, actor, str(self.memory))
                message = str(caught.exception)
                self.assertIn(SLOT, message)
                self.assertIn("总编排", message)
                self.assertIn(actor, message)
        # 本位总监与总编排都放行；unfix 的权限与 fix 对称。
        self.assertTrue(self.service.staff_fix(self.worker, SLOT, str(self.memory))["固定工位"])
        self.assertTrue(self.service.staff_fix(self.worker, "总编排", str(self.memory))["固定工位"])
        with self.assertRaises(TicketError):
            self.service.staff_unfix(self.worker, OTHER_SLOT)

    # ── R5 3 ──────────────────────────────────────────────────────────────
    def test_3_dispatch_lines_only_change_for_a_fixed_slot(self):
        """固定工位的单第二行带记忆 md；非固定工位的单三行**逐字不变**。

        ★对照组用的是「打过标记又 unfix、路径仍在名册里」的同一位员工：
        只按「有没有路径」判断的写法会在这里露馅——路径一直在，变的只有固定工位标记。
        """
        ticket = self.internal_dispatch("开窗指令")
        before = self.service.dispatch_instructions(ticket)
        self.assertEqual(3, len(before))
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        fixed = self.service.dispatch_instructions(ticket)
        self.assertEqual(3, len(fixed))
        self.assertEqual(before[0], fixed[0])
        self.assertEqual(before[2], fixed[2])
        self.assertIn(str(self.memory), fixed[1])
        self.assertIn("先核分支头与绿数再干活", fixed[1])
        self.assertNotIn(str(self.memory), fixed[0])
        self.assertNotIn(str(self.memory), fixed[2])
        self.service.staff_unfix(self.worker, SLOT)
        self.assertEqual(before, self.service.dispatch_instructions(ticket))
        # 网页与 CLI 消费的是同一处，两个出口都得跟着变。
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        self.assertEqual(fixed, self.service.ticket_view(ticket)["开窗指令"])
        self.assertIn(str(self.memory), self.service.dispatch_instruction_text(ticket))

    # ── R5 4 ──────────────────────────────────────────────────────────────
    def test_4_only_submitted_tickets_go_into_the_memory_file(self):
        """memory export 只收这位**已交板**的单；没交板的一张都不进。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        done = self.submitted("交过板的")
        fresh = self.internal_dispatch("还没认领的")
        claimed = self.internal_dispatch("认领了没交板的")
        self.service.claim(claimed["编号"], self.worker)
        text = self.generated()
        self.assertIn(done["编号"], text)
        self.assertNotIn(fresh["编号"], text)
        self.assertNotIn(claimed["编号"], text)
        # 判退回「返工」的单交过板，仍然要留在记忆件里——上一窗踩的坑正是这种单最值钱。
        self.service.judge(done["编号"], False, SLOT, "再改一版", REWORK_VERDICT, "模型")
        self.assertIn(done["编号"], self.generated())

    # ── R5 5 ──────────────────────────────────────────────────────────────
    def test_5_step_zero_is_written_into_the_top_of_the_generated_file(self):
        """生成的 md 顶部含「第 0 步」那几行**原文**，且排在第一条正文之前。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        ticket = self.submitted("有第 0 步")
        text = self.generated()
        self.assertIn(service_module.MEMORY_STEP_ZERO, text)
        for line in (
            "## 第 0 步(每次开窗必做,不许跳)",
            "1. `git fetch` 之后核主线短号与本文件记的是否一致;",
            "2. 在自己的工作树上跑一次全量测试,拿到**当下**的绿数;",
            "3. 本文件里的分支头、绿数、行号**只当线索不当事实**——它是快照,写下那一刻起就在过期。",
            "   对不上就以现在跑出来的为准,并在本单里报一行。",
        ):
            self.assertIn(line, text.splitlines(), line)
        self.assertLess(text.index("## 第 0 步"), text.index(ticket["编号"]))
        # 快照会过期这件事在生成物里不止一处：顶部横幅一处、第 0 步第 3 条一处。
        self.assertIn("只当线索不当事实", text)
        self.assertIn("快照", text)

    # ── R5 6 ──────────────────────────────────────────────────────────────
    def test_6_every_entry_points_back_at_a_ticket_with_branch_and_model(self):
        """每一条含单号、分支或「证据里没写」、实际模型或「未标」。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        with_branch = self.submitted("证据里有分支", raw="366 passed 于 feat/desk-slot-memory 265cb53b9")
        without = self.submitted("证据里没分支", raw="全绿")
        # 一张单填上「实际模型」，一张不填：两种都要写得出，不许静默留空。
        stored = self.service.store.load_ticket(with_branch["编号"])
        stored["实际模型"] = "model-b"
        self.service.store.save_ticket(stored, "set", SLOT, "补实际模型")
        text = self.generated()
        self.assertIn(f"### {with_branch['编号']} · 证据里有分支", text)
        self.assertIn("分支 feat/desk-slot-memory", text)
        self.assertIn("265cb53b9", text)
        self.assertIn("本节由模型 model-b 写", text)
        self.assertIn(f"### {without['编号']} · 证据里没分支", text)
        self.assertIn("证据里没写", text)
        self.assertIn("本节由模型 未标 写", text)
        # 每一条都要能回指到某张单：正文里的每个 ### 标题都带一个真单号。
        headings = [line for line in text.splitlines() if line.startswith("### ")]
        self.assertEqual(2, len(headings))
        for heading in headings:
            self.assertRegex(heading, r"^### T-\d{6} · ")

    # ── R5 7 ──────────────────────────────────────────────────────────────
    def test_7_max_lines_archives_the_oldest_entries(self):
        """--max-lines 超限时最旧的条目进 .archive.md，主文件顶部写明归档了几条。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        tickets = [self.submitted(f"第{index}张")["编号"] for index in range(1, 5)]
        full = self.generated()
        self.assertTrue(all(ticket_id in full for ticket_id in tickets))
        archive = service_module.memory_archive_path(self.memory)
        self.assertFalse(archive.exists())
        # 头（含第 0 步）约 15 行，一条约 8 行：给 25 行只装得下最后一条。
        trimmed = self.generated(max_lines=25)
        self.assertIn("更早的 3 条已归档到", trimmed)
        self.assertIn(str(archive), trimmed)
        self.assertIn(tickets[-1], trimmed)
        for ticket_id in tickets[:-1]:
            self.assertNotIn(ticket_id, trimmed)
        archived = archive.read_text(encoding="utf-8")
        for ticket_id in tickets[:-1]:
            self.assertIn(ticket_id, archived)
        # 归档是**追加**不是覆盖：再刷一次，上一轮归档的内容还在。
        self.generated(max_lines=25)
        again = archive.read_text(encoding="utf-8")
        self.assertGreater(len(again), len(archived))
        self.assertEqual(2, again.count(tickets[0]))
        # 第 0 步永远不被挪走——挪走了的记忆件比没有记忆件更坏。
        self.assertIn(service_module.MEMORY_STEP_ZERO, trimmed)

    # ── R5 8 ──────────────────────────────────────────────────────────────
    def test_8_handoff_is_stored_and_a_judge_can_strike_a_line(self):
        """submit --handoff 落库；judge --strike-handoff 划掉而不是删掉；行号越界被拒。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        ticket = self.submitted("留一句给下一窗", handoff="真源在 A\n那个数是错的,别信\n下一窗从 R2 起手")
        rows = service_module.handoff_rows(self.service.store.load_ticket(ticket["编号"]))
        self.assertEqual(["真源在 A", "那个数是错的,别信", "下一窗从 R2 起手"], [row["文字"] for row in rows])
        with self.assertRaises(TicketError) as caught:
            self.service.judge(ticket["编号"], True, SLOT, "", PASS_VERDICT, strike_handoff="4")
            self.fail("行号越界必须被拒")
        self.assertIn("共有 3 行", str(caught.exception))
        _, warning = self.service.judge(ticket["编号"], True, SLOT, "", PASS_VERDICT, strike_handoff="2")
        self.assertIn("已划掉", warning)
        struck = service_module.handoff_rows(self.service.store.load_ticket(ticket["编号"]))
        # ★划掉不是删除：原文一个字都不能少，只多一个「谁在什么时候认为它错了」。
        self.assertEqual(3, len(struck))
        self.assertEqual("那个数是错的,别信", struck[1]["文字"])
        self.assertEqual(SLOT, struck[1]["划掉判卷人"])
        self.assertTrue(struck[1]["划掉时间"])
        self.assertEqual("", struck[0]["划掉判卷人"])
        text = self.generated()
        self.assertIn("~~已划掉~~", text)
        self.assertIn("那个数是错的,别信", text)
        self.assertIn(f"判卷人划掉：{SLOT}", text)

    # ── R5 9 ──────────────────────────────────────────────────────────────
    def test_9_submit_and_judge_refresh_the_memory_file_by_themselves(self):
        """submit / judge 落库之后自动重刷：改一次 handoff 再交，md 内容跟着变。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        ticket = self.submitted("会自动重刷", handoff="第一版底数")
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "完成", ticket["记忆重刷提示"])
        self.assertTrue(self.memory.is_file())
        self.assertIn("第一版底数", self.memory.read_text(encoding="utf-8"))
        # 判退回返工，重新认领、改一句 handoff 再交：文件跟着走，不用有人手工再跑一次导出。
        _, warning = self.service.judge(ticket["编号"], False, SLOT, "再来一版", REWORK_VERDICT, "模型")
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "完成", warning)
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(
            ticket["编号"], "内部验证", "python -m pytest", "44 passed", handoff="第二版底数",
        )
        refreshed = self.memory.read_text(encoding="utf-8")
        self.assertIn("第二版底数", refreshed)
        self.assertNotIn("第一版底数", refreshed)
        # 非固定工位一律不重刷，也就不该多出这一行。
        self.service.staff_unfix(self.worker, SLOT)
        quiet = self.submitted("不重刷")
        self.assertNotIn("记忆重刷提示", quiet)

    # ── R5 10 ─────────────────────────────────────────────────────────────
    def test_10_a_failed_refresh_never_breaks_the_submit(self):
        """★重刷失败不许拖垮交板：路径写到写不进去的地方，submit 照样成功。

        工具的附加动作不能反过来卡住员工交板——这是本单的硬要求，不是「尽量」。
        """
        broken = unwritable_memory_path(self.root)
        self.service.staff_fix(self.worker, SLOT, str(broken))
        ticket = self.submitted("重刷会失败")
        self.assertEqual("待判", self.service.store.load_ticket(ticket["编号"])["状态"])
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "失败", ticket["记忆重刷提示"])
        self.assertFalse(broken.exists())
        # judge 那一头同样不许被拖垮。
        judged, warning = self.service.judge(ticket["编号"], True, SLOT, "", PASS_VERDICT)
        self.assertEqual("待复检", judged["状态"])
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "失败", warning)
        # 手工跑 memory export 时反过来：那是人主动要的动作，失败就要报出来，不能吞。
        with self.assertRaises(OSError):
            self.service.memory_export(self.worker)

    # ── 命令行两头都要真的认 ────────────────────────────────────────────────
    def test_cli_staff_fix_memory_export_and_submit_handoff(self):
        """命令行走一遍：staff fix / staff list 标出固定工位 / memory export / submit --handoff。"""
        root = self.root / "cli-memory"
        service = TicketService(TicketStore(root))
        worker = service.staff_new(SLOT, "model-a")["员工名"]
        taskbook = self.root / "cli-tb.md"
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        memory = self.root / "cli-memory-file" / f"{worker}.md"
        ticket = service.create_dispatch(
            SLOT, "命令行记忆件", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True, taskbook=str(taskbook),
        )
        fixed = run_local_cli(["staff", "fix", worker, "--memory", str(memory), "--by", SLOT], root)
        self.assertEqual(0, fixed.returncode, fixed.stderr)
        self.assertIn("固定工位", fixed.stdout)
        listed = run_local_cli(["staff", "list", "--slot", SLOT], root)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIn("· 固定工位", listed.stdout)
        denied = run_local_cli(["staff", "fix", worker, "--memory", str(memory), "--by", OTHER_SLOT], root)
        self.assertNotEqual(0, denied.returncode)
        self.assertIn(SLOT, denied.stdout + denied.stderr)
        run_local_cli(["claim", ticket["编号"], "--by", worker], root)
        submitted = run_local_cli([
            "submit", ticket["编号"], "--evidence", "内部验证",
            "--verify-command", "python -m pytest", "--raw-output", "44 passed",
            "--handoff", "真源在 ticket_desk\n绿数别信,自己跑",
        ], root)
        self.assertEqual(0, submitted.returncode, submitted.stderr)
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "完成", submitted.stdout)
        self.assertIn("绿数别信,自己跑", memory.read_text(encoding="utf-8"))
        exported = run_local_cli(["memory", "export", "--staff", worker, "--out", str(memory)], root)
        self.assertEqual(0, exported.returncode, exported.stderr)
        self.assertIn(str(memory), exported.stdout)
        judged = run_local_cli([
            "judge", ticket["编号"], "--pass", "--by", SLOT,
            "--verdict", "设计者怎么打开它：开这份工位记忆 md 看。通过。", "--strike-handoff", "2",
        ], root)
        self.assertEqual(0, judged.returncode, judged.stderr)
        self.assertIn("已划掉", judged.stdout)
        self.assertIn("~~已划掉~~", memory.read_text(encoding="utf-8"))


class AutoRetireAtTerminalTests(TicketTestCase):
    """非固定员工到终态自动退役 + 名册默认只显示在岗（T-001081）。

    第四条。为什么非做成规则不可:
    「窗关了顺手跑一次 staff retire」这件事,工单台上线到今天**一次都没人跑过**——
    名册里堆着一批早就不在的窗,总监照派单下拉派过去,单子就那么停在「新建」。
    ★固定工位一律不退:它跨窗复用（D9-438）,退了下一窗连 claim 都进不来,
      正好砸掉 T-001073 那一批的目的。
    """

    def setUp(self) -> None:
        super().setUp()
        self.memory = self.root / "工位记忆" / f"{self.worker}.md"

    # ── 造数据 ────────────────────────────────────────────────────────────
    def internal_ticket(self, assign: str, title: str = "内部工具单"):
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", assign,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )

    def merged(self, assign: str, title: str = "内部工具单"):
        """内部单一路走到「已合并」——它在那里就是终态（T-001053）。"""
        ticket = self.internal_ticket(assign, title)
        self.service.claim(ticket["编号"], assign)
        self.service.submit(ticket["编号"], "内部验证", "python -m pytest", "44 passed")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        return self.merged_ticket(ticket["编号"], "独立复检")

    def state_of(self, name: str) -> str:
        row = next(r for r in self.service.list_staff(SLOT, True) if r["员工名"] == name)
        return str(row["状态"])

    def retire_events(self) -> list[dict]:
        return [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("事件") == "staff-auto-retire"
        ]

    # ── 用例 1 ────────────────────────────────────────────────────────────
    def test_1_a_fixed_slot_is_never_retired(self):
        """固定工位到终态**不退**。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        ticket = self.merged(self.worker)
        # 前提先自己核一遍:这张单确实到了「执行方可以走了」那一步,不退是因为固定工位,
        # 不是因为压根没触发——否则这条用例挖不出任何东西。
        self.assertTrue(service_module.is_done_for_staff(ticket))
        self.assertEqual("在岗", self.state_of(self.worker))
        self.assertEqual("", str(ticket.get("自动退役提示", "")))
        self.assertEqual([], self.retire_events())

    # ── 用例 2 ────────────────────────────────────────────────────────────
    def test_2_another_open_ticket_keeps_the_window_open(self):
        """非固定,但手上还有别的在办单:不退;那张也结了才轮到收窗。"""
        busy = self.internal_ticket(self.worker, "手上另一张")
        self.service.claim(busy["编号"], self.worker)
        done = self.merged(self.worker, "先做完的那张")
        self.assertEqual("在岗", self.state_of(self.worker))
        self.assertEqual("", str(done.get("自动退役提示", "")))
        # 作废也是终态:最后一张一走,窗就该收了。
        voided = self.service.void(busy["编号"], "建重了,并进前一张", SLOT)
        self.assertEqual("已收窗", self.state_of(self.worker))
        self.assertIn(self.worker, str(voided["自动退役提示"]))

    # ── 用例 3 ────────────────────────────────────────────────────────────
    def test_3_the_last_ticket_closes_the_window_and_leaves_a_line(self):
        """非固定且手上没有别的在办单:自动收窗,并在事件线记一行。"""
        ticket = self.merged(self.worker)
        self.assertEqual("已收窗", self.state_of(self.worker))
        notice = str(ticket["自动退役提示"])
        self.assertIn(self.worker, notice)
        self.assertIn("staff reopen", notice)
        events = self.retire_events()
        self.assertEqual(1, len(events))
        self.assertEqual(ticket["编号"], events[0]["工单号"])
        self.assertIn(self.worker, events[0]["说明"])
        # 收窗之后再派给他要被现有那道闸拦下,人话里得说得出怎么救。
        with self.assertRaises(TicketError) as raised:
            self.service.claim(self.internal_ticket(self.worker, "下一张")["编号"], self.worker)
        self.assertIn("staff reopen", str(raised.exception))

    # ── 用例 4 ────────────────────────────────────────────────────────────
    def test_4_the_roster_hides_the_retired_until_all(self):
        """staff list 默认只显示在岗;--all 才看得到退役的。"""
        second = self.service.staff_new(SLOT, "model-a")["员工名"]
        self.merged(self.worker)
        self.assertEqual([second], [row["员工名"] for row in self.service.list_staff(SLOT)])
        self.assertIn(self.worker, [row["员工名"] for row in self.service.list_staff(SLOT, True)])
        # 命令行那一份要跟服务端同一把尺子:服务端改了、命令行照旧,两处就各说一套。
        default = run_local_cli(["staff", "list", "--slot", SLOT], self.service.store.root)
        self.assertEqual(0, default.returncode, default.stderr)
        self.assertNotIn(self.worker, default.stdout)
        self.assertIn(second, default.stdout)
        self.assertIn("--all", default.stdout)
        every = run_local_cli(["staff", "list", "--slot", SLOT, "--all"], self.service.store.root)
        self.assertEqual(0, every.returncode, every.stderr)
        self.assertIn(self.worker, every.stdout)
        self.assertIn("已收窗", every.stdout)

    # ── 用例 5 ────────────────────────────────────────────────────────────
    def test_5_the_accounting_never_notices_the_retirement(self):
        """账不受影响:退役编号仍在册,history 与模型合格率一分不少。

        账按**模型**统计不按编号——把同一位 reopen 回来再算一遍,两份必须逐字相等。
        """
        ticket = self.merged(self.worker)
        self.assertEqual("已收窗", self.state_of(self.worker))
        history = self.service.history(self.worker)
        self.assertEqual([ticket["编号"]], [row["编号"] for row in history["工单"]])
        retired_stats = self.service.model_statistics()
        # 记的是 sol 那一行(名字带着档位后缀),交板 1、判过 1——退役一分没少。
        scored = [row for row in retired_stats if row["模型"].startswith("model-a")]
        self.assertEqual([(1, 1)], [(row["交板数"], row["判过"]) for row in scored])
        self.service.staff_reopen(self.worker)
        self.assertEqual(retired_stats, self.service.model_statistics())

    # ── 闸:阻塞不是终态 ──────────────────────────────────────────────────
    def test_6_blocking_is_not_done_and_still_holds_the_window(self):
        """阻塞既不触发收窗,也仍旧算这位手上的一张在办单。

        ★这就是不能直接复用 is_terminal 的地方:那一条把阻塞算终态(没人该动它、不报老化),
        可阻塞解开之后原员工还得接着做,而 claim 只认在岗——退了他就再也认领不回来。
        """
        blocked = self.internal_ticket(self.worker, "被挂起的那张")
        self.service.claim(blocked["编号"], self.worker)
        held = self.service.block(blocked["编号"], "等别位先答", service_module.CONDUCTOR_SLOT)
        self.assertTrue(service_module.is_terminal(held))
        self.assertFalse(service_module.is_done_for_staff(held))
        self.assertEqual("在岗", self.state_of(self.worker))
        # 再结掉别的单也不许收窗:阻塞那张还在他手上等着解。
        self.merged(self.worker, "同时在做的另一张")
        self.assertEqual("在岗", self.state_of(self.worker))
        self.assertEqual([], self.retire_events())

    # ── 闸:履历不是在办 ──────────────────────────────────────────────────
    def test_7_a_ticket_handed_to_someone_else_no_longer_holds_the_window(self):
        """改派走的单不算他手上的活:「经手工单号列表」是履历,改派之后不会撤回。

        照履历数的话,凡是被改派过一次的员工永远退不掉。
        """
        moved = self.internal_ticket(self.worker, "后来改派走的那张")
        self.service.claim(moved["编号"], self.worker)
        taker = self.service.staff_new(SLOT, "model-a")["员工名"]
        self.service.edit(moved["编号"], SLOT, assign=taker)
        self.assertIn(moved["编号"], self.service.find_staff(self.worker)[1]["经手工单号列表"])
        self.merged(self.worker, "他自己那张")
        self.assertEqual("已收窗", self.state_of(self.worker))
        # 接手的那位手上还有活,不能跟着一起被收。
        self.assertEqual("在岗", self.state_of(taker))

    # ── 闸:附加动作不许弄挂主流程 ────────────────────────────────────────
    def test_8_a_failed_retirement_never_breaks_the_close(self):
        """名册这一步出任何问题,都只多一行字;结案本身照常落库成功。"""
        with mock.patch.object(
            TicketService, "_open_ticket_ids", side_effect=RuntimeError("名册读坏了"),
        ):
            ticket = self.merged(self.worker)
        self.assertEqual("已合并", self.service.store.load_ticket(ticket["编号"])["状态"])
        notice = str(ticket["自动退役提示"])
        self.assertIn("名册读坏了", notice)
        self.assertIn(f"staff retire {self.worker}", notice)
        self.assertEqual("在岗", self.state_of(self.worker))

    # ── 闸:前端与服务端同一条判据 ────────────────────────────────────────
    # ── 闸:命令行端到端 ──────────────────────────────────────────────────
    def test_10_the_command_line_says_it_out_loud(self):
        """结案类命令的回执必须当场说出「名册被动过了」,别让人事后才发现。"""
        root = self.service.store.root
        ticket = self.internal_ticket(self.worker, "命令行走一遍")
        self.service.claim(ticket["编号"], self.worker)
        voided = run_local_cli(
            ["void", ticket["编号"], "--reason", "建错了", "--by", SLOT], root,
        )
        self.assertEqual(0, voided.returncode, voided.stderr)
        self.assertIn(service_module.AUTO_RETIRE_PREFIX, voided.stdout)
        self.assertIn(self.worker, voided.stdout)
        self.assertEqual("已收窗", self.state_of(self.worker))


class StaticAssetCacheHeaderTests(HttpServiceTests):
    """静态资产必须带缓存指令(T-001658,根因候选:旧 JS 配今天的数据)。

    /tickets.js 原来只有 Last-Modified、没有任何 Cache-Control,浏览器按启发式
    缓存可以拿几天前的脚本配今天的数据跑;no-cache 每次再验证、可 304,不加流量。
    API 响应自己的 no-store 不许被盖掉。
    """

    def test_static_assets_get_no_cache_and_api_keeps_no_store(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("GET", "/tickets.js")
        static = connection.getresponse()
        static.read()
        self.assertEqual(200, static.status)
        self.assertEqual("no-cache", static.getheader("Cache-Control"))
        connection.close()

        status, _payload = self.request("GET", "/api/tickets")
        self.assertEqual(200, status)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("GET", "/api/tickets")
        api = connection.getresponse()
        api.read()
        self.assertEqual("no-store", api.getheader("Cache-Control"))
        connection.close()


class StaffNumberWidenedTests(TicketTestCase):
    """员工编号扩到三位(T-001659,后端·服务与接口 -99 用完)。

    不走回收重用:员工号被判语/断点件/章程/记忆大量引用,同一个号在两个时期
    指向两个人,查判退责任会错到别人头上。
    """

    def test_1_three_digit_numbers_register_and_sign_back_to_the_slot(self):
        staff = self.service.store.load_staff()
        group = staff.setdefault("总监位", {}).setdefault(SLOT, {"下一个编号": 1, "员工": []})
        group["下一个编号"] = 100
        self.service.store.save_staff(staff)
        member = self.service.staff_new(SLOT, "model-a")
        self.assertEqual(f"{SLOT}-100", member["员工名"])
        # -100 的署名必须能收敛回总监位,否则权限闸认不出这是哪位的员工。
        self.assertEqual(SLOT, self.service._signer_slot(f"{SLOT}-100"))
        self.assertEqual(SLOT, self.service._signer_slot(f"{SLOT}-07"))

    def test_2_the_ceiling_still_blocks_at_999(self):
        staff = self.service.store.load_staff()
        group = staff.setdefault("总监位", {}).setdefault(SLOT, {"下一个编号": 1, "员工": []})
        group["下一个编号"] = 1000
        self.service.store.save_staff(staff)
        with self.assertRaisesRegex(TicketError, "上限 -999"):
            self.service.staff_new(SLOT, "model-a")


if __name__ == "__main__":
    unittest.main()


# ══════════════════════════════════════════════════════════════════════════
# 网页台面（web/）的钉测。移植真台面之后重新生效。
#
# ★这一批**只钉源码文本**，抓不到行为坏掉——真执行那几条在 web/tests/*.mjs 里，
#   由下面 BrowserProbeTests 接进本套件。两种都要：
#   文本钉测守「这一行还在不在」，真执行守「它还对不对」。
# ★台面不在时全部干净跳过（web_file_or_skip），不红。
# ══════════════════════════════════════════════════════════════════════════


class WebDeskPinTests(TicketTestCase):
    """网页台面的形状钉测。每一条后面都有一次真事故，见各自 docstring。"""

    def script(self):
        return web_file_or_skip(self, "tickets.js").read_text(encoding="utf-8")

    def page(self):
        return web_file_or_skip(self, "index.html").read_text(encoding="utf-8")

    # ── 口径来自服务端，不在前端另抄一份 ────────────────────────────────
    def test_the_page_takes_its_vocabulary_from_the_server(self):
        """位名、停滞阈值、实机来源标签一律走 /api/config。

        ★这是本次移植改掉的最要紧一处：原版在前端各抄一份，靠注释里一句
          「与服务端同步修改」约束，而那句话拦不住任何人——症状是页面**安静地**
          显示错的东西（名单少一位，那一位的单在离线模式下整个看不见）。
        """
        script = self.script()
        self.assertIn('await api("/api/config")', script)
        self.assertIn("async function loadDeskConfig()", script)
        # 兜底值必须存在(离线模式要用)，但必须是在 DESK 这一处，不许散落
        self.assertIn("const DESK = {", script)
        # 读不到配置要出声，不许安静地用兜底值跑
        self.assertIn("读不到 /api/config", script)
        # 配置必须先于数据:先拿数据再拿配置的话，第一帧会拿兜底名单去筛服务端的单
        self.assertLess(script.index("const configured=await loadDeskConfig()"),
                        script.index("const live=API_MODE?await readApi():false"))

    def test_the_state_board_reads_the_server_instead_of_transcribing_it(self):
        """顶栏那几项必须来自服务端值面，不能是页面里另抄一份常量。"""
        self.assertIn('id="stateBoard"', self.page())
        script = self.script()
        self.assertIn("/api/state", script)
        self.assertIn("renderStateBoard", script)
        self.assertIn("最近改动文本", script)

    # ── 开窗指令卡（本次移植点名要保住的三样之一）──────────────────────
    def test_the_dispatch_card_only_reads_the_server_taskbook_field(self):
        """开窗路径只认服务端工单字段——本地缓存代表不了另一台机器看到的真值。"""
        script = self.script()
        self.assertIn('function dispatchInitialPath(t){return String(t.任务书路径||"").trim();}', script)
        self.assertNotIn("taskbook" + "Guess", script)
        self.assertNotIn("deskDispatch" + "Path", script)

    def test_the_dispatch_lines_come_from_the_server(self):
        """三行开窗指令由服务端 dispatch_instructions 生成，前端不留第二份模板。"""
        script = self.script()
        self.assertIn("function dispatchLineTexts(t){return Array.isArray(t.开窗指令)?t.开窗指令:[];}", script)
        self.assertNotIn("function dispatchClaimLine", script)

    def test_the_dispatch_card_marks_a_missing_taskbook_and_saves_via_set(self):
        script = self.script()
        self.assertIn(">缺任务书</span>", script)
        self.assertIn("{op:'set',ticket:id,taskbook:input.value.trim(),by:ticket.所属总监位}", script)
        self.assertIn('data-cooldown-key="taskbook:${esc(t.编号)}"', script)
        self.assertIn("当前是只读回落数据，不能补任务书路径", script)

    def test_the_taskbook_placeholder_follows_the_configured_workspace(self):
        """placeholder 跟着服务端给的工作区目录走，不写死任何一台机器的盘符。"""
        script = self.script()
        self.assertIn("const placeholder=`${DESK.任务书目录}/", script)

    def test_open_window_keeps_confirm_before_model_choice_and_r_prefixed_stamp(self):
        """二次确认 → 选模型 → 落本机标记 → 发写请求，四步顺序不许动。

        乐观更新（先落标记再发请求）是为了不让人干等一次整包重拉；
        换来的风险由 onFail 回滚兜住，所以那条断言不能删。
        """
        script = self.script()
        confirm_at = script.index("if(next&&!confirm(`确认已经把")
        prompt_at = script.index("const chosen=prompt(`请选择本次实际模型与档位")
        stamp_at = script.index("lsSet(`deskOpened:${id}`,reworkStamp(ticket))")
        api_at = script.index("op:'open-window'")
        self.assertLess(confirm_at, prompt_at)
        self.assertLess(prompt_at, stamp_at)
        self.assertLess(stamp_at, api_at)
        self.assertIn("onFail:()=>{lsDel(`deskOpened:${id}`)", script)
        self.assertIn("app.data.items[index]=saved", script)
        self.assertIn('function reworkStamp(t){return "r"+String(t?.返工次数 ?? 0)}', script)

    def test_the_page_checks_the_server_marker_before_local_storage(self):
        """服务端「已开窗」优先于浏览器本地标记；本地那份只是旧包的兜底。"""
        script = self.script()
        start = script.index("function isOpened(t){")
        body = script[start:script.index("\n}", start)]
        server_at = body.index("t?.已开窗")
        explicit_at = body.index('hasOwnProperty.call(t,"已开窗")')
        local_at = body.index("lsGet(`deskOpened:${t.编号}`)")
        self.assertLess(server_at, explicit_at)
        self.assertLess(explicit_at, local_at)
        self.assertIn("return false", body[explicit_at:local_at])
        self.assertIn('return v==="0"&&Number(t?.返工次数 ?? 0)===0', body[local_at:])

    def test_the_rework_card_says_the_server_marker_was_cleared(self):
        self.assertIn("已开窗标记已清,请重新开窗", self.script())

    def test_the_rework_card_tells_the_director_to_swap_the_taskbook_first(self):
        """返工是**换书不换号**那一档：把「换书后再认领」和当前任务书摆在眼前。

        ★判语全文不在前端拼——员工窗跑 receipt 看服务端生成的那一份，两处各拼必然漂。
        """
        note = self.script().split("function reworkNote(t){", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("换书后再认领", note)
        self.assertIn("set ${esc(t.编号)} --taskbook", note)
        self.assertIn("const taskbook=dispatchInitialPath(t)", note)
        self.assertIn("还没填任务书路径", note)
        self.assertNotIn("上一轮为什么被退", note)
        self.assertNotIn("t.判语", note)

    # ── 催办句（第二样）──────────────────────────────────────────────
    def test_the_reminder_uses_the_configured_cli_path(self):
        """催办句里的命令行路径来自 DESK.命令行路径（服务端 config.cli_path()）。

        原版把绝对路径写死了四遍，换台机器就全错，而错了没有任何地方会报。
        """
        script = self.script()
        reminder = script.split('const text=t.状态==="已认领"', 1)[1].split(
            ':(t.状态==="新建"&&isOpened(t))', 1)[0]
        self.assertEqual(4, reminder.count("python ${DESK.命令行路径} "))
        # 一个写死的绝对路径都不许有:换台机器就全错,而错了没有任何地方会报。
        self.assertNotRegex(reminder, r"[A-Za-z]:\\\\")
        self.assertIn("先核通道:python", reminder)
        self.assertIn("receipt ${t.编号}", reminder)
        self.assertIn("submit ${t.编号} --verify-command", reminder)
        self.assertIn("--raw-output", reminder)
        self.assertIn("attach ${t.编号}", reminder)
        self.assertIn("--origin world", reminder)

    def test_the_wake_button_copies_a_short_nudge(self):
        """唤醒段那一下只复制一句「查收工单」+ 贴给谁，不复制一整段。"""
        script = self.script()
        self.assertIn("data-copy-wake", script)
        self.assertIn("查收工单", script)
        self.assertIn("data-copy-remind", script)

    # ── 转交（第三样的一半）─────────────────────────────────────────
    def test_the_transfer_controls_offer_four_targets_and_require_a_reason(self):
        script = self.script()
        self.assertIn("转给指定总监", script)
        # 四个 target 必须是**插值**进属性的,不能是字面量。
        # ★这条是移植时真踩的:批量把角色名换成 DESK.xxx,顺手把模板字符串里的
        #   `data-transfer-target="总编排"` 也换了 —— 属性值变成字面量 `DESK.总编排位`,
        #   于是「转给总编排」会把单转给一个不存在的位,而页面一个字都不报。
        for expr in ("DESK.总编排位", "DESK.复检位", "DESK.拍板人"):
            self.assertIn('data-transfer-target="${esc(%s)}"' % expr, script)
        self.assertNotRegex(script, r'data-transfer-target=DESK\.')
        self.assertIn("data-transfer-reason", script)
        self.assertIn("required", script.split("function transferControls(t)", 1)[1][:900])
        # 多行原因框:回车留给换行,Ctrl+Enter 才提交
        self.assertIn("event.ctrlKey||event.metaKey", script)

    # ── 按位与状态的筛选（第三样的另一半）───────────────────────────
    def test_the_slot_tabs_and_stage_bar_are_both_filters(self):
        script = self.script()
        self.assertIn("data-slot=", script)          # 按位
        self.assertIn("data-stage=", script)         # 按阶段(件数条)
        self.assertIn("function stageFilterPanel()", script)
        self.assertIn("app.stageFilter===stage.dataset.stage", script)   # 再点同一格取消
        self.assertIn("data-window-filter=", script)  # 按建议窗口
        # 状态分组:要人动手 / 进行中 / 已办
        self.assertIn("要人动手", script)
        self.assertIn("进行中 · ", script)
        self.assertIn("已办 · ", script)

    def test_the_page_hides_the_dispatch_form_for_a_relay_only_slot(self):
        """只分发不派单的位，网页那侧也不给建派单入口（服务端才是真闸）。"""
        script = self.script()
        self.assertIn("function noDispatch(slot){ return (DESK.只分发不派单位||[]).includes(slot) }", script)
        self.assertIn("writable()&&!noDispatch(app.slot)?", script)

    # ── 终态、阻塞、免独图 ──────────────────────────────────────────
    def test_the_front_end_uses_the_same_terminal_judgement_as_the_server(self):
        """前端与服务端必须是同一条判据:两处各写一套，页面与命令行会各说各话。"""
        script = self.script()
        self.assertIn('function isTerminal(t){return nonStale().has(t.状态)||(t.状态==="已合并"&&!!t.非用户可感知)}', script)
        self.assertIn('if(isTerminal(t)||t.类型==="阻塞")return null;', script)
        self.assertIn('if(t.状态==="已合并"&&isTerminal(t))return 6;', script)

    def test_the_front_end_treats_void_as_a_terminal_state_everywhere(self):
        script = self.script()
        self.assertIn('const TERMINAL_STATES = ["实机复验过","关闭","作废"];', script)
        self.assertIn('const doneRows=["已答","关闭","已合并","实机复验过","作废"].map', script)
        self.assertIn("if(isTerminal(t))", script)
        self.assertIn("isOpened(t)&&!isTerminal(t)", script)
        self.assertIn("阻塞:-1,作废:-1}", script)
        self.assertIn("已答:['close'],作废:[]}", script)
        self.assertIn("const attach=!['关闭','作废'].includes(t.状态)?", script)
        self.assertIn(".ticket.voided", web_file_or_skip(self, "tickets.css").read_text(encoding="utf-8"))

    def test_the_page_shows_blocked_tickets_but_never_answers_them(self):
        """网页只显示不答复:解阻的事实只在所属总监手里，页面上按下去等于替他背名。"""
        script = self.script()
        wants = re.search(r"function wantsDesignerAnswer\(t\)\{(.*?)\n\}", script, re.S)
        self.assertIsNotNone(wants, "没找到 wantsDesignerAnswer，网页钉测的锚点漂了")
        self.assertNotIn("阻塞", wants.group(1), "阻塞不该进拍板人的待答队列")
        answer_fn = re.search(r"async function answerTicket\(id\)\{.*", script)
        self.assertIsNotNone(answer_fn, "没找到 answerTicket，网页钉测的锚点漂了")
        body = answer_fn.group(0)
        guard = re.search(r"if\(t&&t\.类型==='阻塞'\)\{(.*?)return\}", body)
        self.assertIsNotNone(guard, "answerTicket 里没有「阻塞就地 return」那一段")
        self.assertNotIn("api(", guard.group(1), "阻塞分支不许发请求")
        self.assertIn("notify(", guard.group(1), "阻塞分支要留一句人话")
        self.assertIsNotNone(re.search(r"等 \$\{[^}]*所属总监位[^}]*\} 答", script),
                             "卡片 meta 行没有「等 <所属总监位> 答」那一句")

    def test_the_card_shows_why_a_shot_was_exempted(self):
        """免独图要连原因一起显示:只显示三个字，看不出这张单凭什么免。"""
        script = self.script()
        self.assertIn("免独图原因", script)
        self.assertIn('value==="免独图"', script)
        self.assertIn("待独图", script)
        self.assertIn("shotMark(t)", script)

    def test_the_page_says_out_loud_that_web_upload_is_not_gated(self):
        """闸只盖命令行，网页照旧能传——不写这一句，人会以为网页传的图也核过尺寸了。"""
        script = self.script()
        self.assertIn("web-upload-note", script)
        self.assertIn("网页上传<b>不核出处</b>", script)
        self.assertIn("判卷人自己开原图核", script)

    def test_the_page_hides_retired_staff_and_surfaces_the_notice(self):
        script = self.script()
        self.assertIn("const onDuty=staff.filter(m=>m.状态==='在岗'),retired=staff.filter(m=>m.状态!=='在岗');", script)
        self.assertIn("已收窗 ${retired.length} 位", script)
        self.assertIn("${result?.自动退役提示?`\\n${result.自动退役提示}`:''}", script)

    # ── 缓存、增量、搜索 ────────────────────────────────────────────
    def test_the_page_offers_a_way_out_of_its_cache(self):
        """任何缓存都该有一条一键回到干净状态的退路。"""
        self.assertIn('id="forceReload"', self.page())
        script = self.script()
        self.assertIn("function dropCache()", script)
        self.assertIn("forceButton.onclick", script)

    def test_the_page_checks_the_total_so_deletions_cannot_hide(self):
        """★护栏:条数对不上就整份重取。单确实会消失过——鬼单活不过一次刷新。"""
        script = self.script()
        self.assertIn("if(merged.length===delta.总数)", script)

    def test_threads_are_deliberately_not_cached(self):
        """对话线看着只追加，其实 mark-read 会回头改已有行——按行数切片发现不了。"""
        script = self.script()
        self.assertIn("★摘要故意不进缓存", script)
        self.assertIn('data.threadSummary=await api("/api/thread-summary")', script)

    def test_the_page_reads_unread_counts_from_the_summary(self):
        script = self.script()
        self.assertIn("function unread(slot) { return unreadFor(slot, DESK.拍板人); }", script)
        self.assertIn("function slotUnreadForOwner(slot){ return unreadFor(slot, slot); }", script)
        self.assertIn("function unreadFor(slot,actor)", script)
        self.assertIn("data.threads[app.slot]=await loadThread(app.slot);", script)
        self.assertIn("await ensureThread(app.slot);", script)
        self.assertIn("async function showWakeFull(slot){", script)
        self.assertIn("await ensureAllThreads();", script)

    def test_search_runs_on_the_server_and_says_so_when_it_cannot(self):
        """搜索走服务端(列表那趟不发正文);连不上时必须**说明白**搜得不全。"""
        script = self.script()
        self.assertIn("async function searchTickets(q){", script)
        self.assertIn("await api(`/api/tickets?q=${encodeURIComponent(q)}`)", script)
        self.assertIn("results=q?await searchTickets(q):[]", script)
        self.assertIn("正文、答复、接线证据里的词搜不到", script)

    def test_the_search_index_is_built_only_when_searching(self):
        """索引用到才建:绝大多数刷新根本不搜，那一趟十几 MB 的序列化纯属白烧。"""
        script = self.script()
        self.assertIn("function ensureSearchIndex()", script)
        self.assertIn("async function doSearch(value){ await ensureAllThreads(); ensureSearchIndex();", script)
        self.assertIn("function rebuildSearchIndex(){ searchTexts=new WeakMap(); searchIndexReady=false; }", script)

    def test_search_box_only_fires_on_button_or_enter(self):
        """逐键 input 会对全部单做序列化 + 整页重绘，打字巨卡。

        ★绑定必须用 onkeydown 赋值而不是 addEventListener:doSearch 自己会调 bindCommon(),
          用 addEventListener 每搜一次就叠一层，搜 N 次后按一下回车会跑 N 遍。
        """
        script = self.script()
        self.assertNotIn("searchBox').addEventListener('input'", script)
        self.assertIn("searchBox.onkeydown=", script)
        self.assertIn("data-search-run", script)
        self.assertIn("!e.isComposing", script)   # 中文输入法选词的 Enter 不算

    # ── 页面骨架 ────────────────────────────────────────────────────
    def test_the_page_has_five_views_and_carries_every_gate_message(self):
        page, script = self.page(), self.script()
        for label in ("总监位", "设计者队列", "总编排日览", "搜索", "新单"):
            self.assertIn(label, page)
        # 一个外链都不许有:工单台常装在内网,一个 CDN 链接就能让页面白屏,而白屏不报错
        self.assertNotRegex(page, r"https?://")
        both = page + script
        for marker in ("showDirectoryPicker", "indexedDB", "tickets-bundle.js",
                       "判卷人不能与执行员工", "复检人必须与执行员工、判卷人都不同",
                       "只能由总编排", "名册里格式正确的在岗员工", "toBlob"):
            self.assertIn(marker, both)
        for marker in ("一、这是什么", "交付项", "非用户可感知", "验证命令", "原样输出", "判语", "verify_command"):
            self.assertIn(marker, both)


class WebDispatchGateTests(TicketTestCase):
    """网页建派单也得过服务端那道闸——前端少给入口不算数。"""

    def test_web_dispatch_without_the_facing_flag_is_rejected_by_the_server(self):
        handler = partial(TicketRequestHandler, directory=str(WEB_ROOT))
        server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = {"op": "new", "slot": SLOT, "title": "网页漏标", "source": ["DECISIONS.md:测试"],
                    "consumer": "主场景/UiRoot", "deliverables": [str(self.deliverable)],
                    "tier": "乙", "by": SLOT}
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)

            def post(payload: dict) -> tuple[int, dict]:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                connection.request("POST", "/api/action", body=body,
                                   headers={"Content-Type": "application/json; charset=utf-8"})
                response = connection.getresponse()
                return response.status, json.loads(response.read().decode("utf-8"))

            status, payload = post(base)
            self.assertEqual(400, status)
            self.assertIn("二选一", payload["reason"])
            status, payload = post({**base, "title": "网页内部单", "internal": True})
            self.assertEqual(200, status)
            self.assertTrue(payload["result"]["非用户可感知"])
            status, payload = post({**base, "title": "网页用户单", "internal": False})
            self.assertEqual(200, status)
            self.assertFalse(payload["result"]["非用户可感知"])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_the_config_endpoint_gives_the_page_everything_it_needs(self):
        """/api/config 的每一格都要有人用；少一格页面就得回去用兜底值。"""
        payload = self.service.desk_config()
        for key in ("位名", "只分发不派单位", "总编排位", "复检位", "拍板人", "开窗平台",
                    "主场景", "实机来源", "值面标签", "停滞阈值", "非停滞态",
                    "新建已开窗阈值", "命令行路径", "任务书目录", "离线包全局名"):
            self.assertIn(key, payload)
        self.assertEqual(list(model.SLOTS), payload["位名"])
        self.assertEqual(dict(service_module.STALE_STATE_HOURS), payload["停滞阈值"])
        self.assertEqual(service_module.ORIGIN_MAP, payload["实机来源"])
        # 前端兜底那一份的键必须是它的子集,否则 Object.assign 覆盖之后会留下孤儿键
        script = web_file_or_skip(self, "tickets.js").read_text(encoding="utf-8")
        block = script.split("const DESK = {", 1)[1].split("\n};", 1)[0]
        for key in payload:
            self.assertIn(f"{key}:", block, f"前端兜底 DESK 缺 {key}")


@unittest.skipUnless(shutil.which("node"), "这台机器上没有 node，跑不了网页真执行探针")
class BrowserProbeTests(unittest.TestCase):
    """把 web/tests 下的**真执行**探针接进本套件。

    ★为什么非要真跑不可:上面 WebDeskPinTests 那一批只断言源码里有没有某一行。
      原版就栽过——把「要你去唤醒的窗口」从 10 格改塌成 2 格，而那批文本钉测**全绿**，
      因为它们从没真的调用过 wakeList()。只看形状的用例永远抓不到这种事。
    ★.mjs 不会被 pytest 自动发现，全靠下面逐条手写接进来。加探针要同时加一条。
    """

    def run_probe(self, name: str):
        script = WEB_ROOT / "tests" / name
        if not script.is_file():
            self.skipTest(f"{PACKAGE_TREE_SKIP_PREFIX}，这条要读 {script}")
        result = subprocess.run(
            [shutil.which("node"), str(script)], cwd=str(ROOT), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("全过", result.stdout)

    def test_the_wake_list_probe_passes(self):
        """真跑 readApi() 再看唤醒段:把 fix 撤掉它会报「实际 2、期望 13」。"""
        self.run_probe("wake-list.mjs")

    def test_the_stage_counts_probe_passes(self):
        """真跑 stageGroups()，钉死「件数条第一格 == 下面那一段逐张相等」。"""
        self.run_probe("stage-counts.mjs")

    def test_the_stale_banner_probe_passes(self):
        """红条的两个数永远不许是 undefined——数取不到必须整条不渲染。"""
        self.run_probe("stale-banner.mjs")

    def test_the_staff_name_probe_passes(self):
        """三位员工号(-100 起)在网页上要看得见:两位正则会把整张单挡出队列。"""
        self.run_probe("staff-name.mjs")

    def test_every_probe_file_is_wired_into_this_suite(self):
        """★防漏:web/tests 下新增一个 .mjs 却忘了接进来，这条会红。

        不接就等于那条探针从不跑，而它看起来是存在的——最坏的一种「闸装了没通电」。
        """
        directory = WEB_ROOT / "tests"
        if not directory.is_dir():
            self.skipTest(f"{PACKAGE_TREE_SKIP_PREFIX}，这条要读 {directory}")
        on_disk = sorted(p.name for p in directory.glob("*.mjs"))
        source = inspect.getsource(type(self))
        missing = [name for name in on_disk if f'run_probe("{name}")' not in source]
        self.assertEqual([], missing, f"这些探针没接进套件:{missing}")
