#!/usr/bin/env python3
"""重拍 README 里那三张截图（造演示数据 → 真起服务 → Playwright 真拍）。

    python docs/make_screens.py

为什么要有这个脚本：上一版截图是手工敲命令 + 手工截的，**没有可重复的办法**。
结果是改了屏上文案之后，图还停在旧词上，而没有任何一道闸会因此变红——
README 第一屏给读者看的是过期的东西。现在这件事有脚本了，改完随手重拍。

依赖：pip install playwright && playwright install chromium
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "ticket_desk" / "ticket.py"
OUT = REPO / "docs" / "screens"
# 1440x1000 @2x = 2880x2000 的成图，字大小和原版一致。
# ★别写成 2880x1000 @1x：像素数一样，但屏上字会缩成一半，README 里根本看不清。
VIEWPORT = {"width": 1440, "height": 1000}
SCALE = 2


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 截图里会印出「工作区根目录」拼出来的章程路径。默认值是本仓的上一级，
# 也就是**拍图这台机器上的真实目录**——印进 README 就成了「某人的机器」。
# 这里给一个明显是示例的路径；它只用来拼提示文字，工具不会去建目录。
# 同理，「命令行路径」印在每张卡片的认领命令里，默认是本仓的真实绝对路径。
DEMO_WORKSPACE = r"D:\我的项目"
DEMO_CLI = r"D:\ticket-desk\ticket_desk\ticket.py"


def demo_config(root: Path) -> Path:
    """写一份只给拍图用的配置：把会印在屏上的两条本机路径换成示例路径。"""
    import json
    path = root / "config.json"
    path.write_text(json.dumps({"工作区根目录": DEMO_WORKSPACE, "命令行路径": DEMO_CLI},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def desk_env(root: Path) -> dict[str, str]:
    return {**os.environ, "TICKET_ROOT": str(root), "PYTHONIOENCODING": "utf-8",
            "TICKET_CONFIG": str(root / "config.json")}


class Desk:
    """把 ticket.py 的本机模式包一层，失败就当场炸，不许静默往下走。"""

    def __init__(self, root: Path):
        self.root = root
        self.env = desk_env(root)

    def __call__(self, *args: str) -> str:
        r = subprocess.run(
            [sys.executable, str(CLI), *args, "--local"],
            cwd=REPO, env=self.env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            raise SystemExit(f"★ 命令失败：{' '.join(args)}\n{r.stdout}\n{r.stderr}")
        return r.stdout.strip()

    def new(self, slot: str, title: str, tier: str, consumer: str, deliverable: str,
            body: str, facing: str = "--internal") -> str:
        num = self(
            "new", "--slot", slot, "--title", title, "--tier", tier, facing,
            "--consumer", consumer, "--source", "DECISIONS.md:口径",
            "--deliverable", deliverable, "--deliverable-unchecked", "--body", body,
        ).splitlines()[0]
        book = self.root / "books" / f"{title}.md"
        book.parent.mkdir(parents=True, exist_ok=True)
        book.write_text(f"# {title}\n\n第 0 步 …… 收尾问答。\n", encoding="utf-8")
        self("set", num, "--taskbook", str(book), "--by", slot)
        return num


def seed(root: Path) -> None:
    t = Desk(root)
    FE, BE, DATA, ORCH = (
        "前端·界面与交互", "后端·服务与接口", "后端·数据与规则", "总编排",
    )
    for slot in (FE, BE, DATA):
        t("staff", "new", "--slot", slot, "--tool", "model-a")

    # 顶栏那排值面项：不填的话整排都是「未填」，截图上像个没起来的系统
    for key, value in (
        ("screenshot_resolution", "2560x1440"),
        ("deploy_head", "a1b2c3d4e"),
        ("latency_budget", "p50=120,p99=480"),
    ):
        t("state", "set", key, value, "--by", ORCH)

    # ① 后端一张甲档单，走到「待复检」——搜索页和总监位都要用它
    n1 = t.new(BE, "【claude】接入登录限流", "甲", "网关 middleware 链",
               "ticket_desk/service.py", "登录接口每分钟限次，超了回 429。")
    t("claim", n1, "--by", f"{BE}-01")
    t("submit", n1, "--evidence", "本机连打 11 次，第 11 次收到 429",
      "--verify-command", "python -m pytest tests -q -k rate_limit",
      "--raw-output", "12 passed in 3.41s")
    t("say", "--slot", BE, "--by", ORCH, "--ref", n1, f"{ORCH} 建了派单 {n1} · 【claude】接入登录限流")
    t("say", "--slot", BE, "--by", f"{BE}-01", "--ref", n1,
      f"{BE}-01 交板了 {n1} · 【claude】接入登录限流，等你判卷")

    # ② 前端一张乙档单，判退成「返工」——总监位那一栏要用它
    n2 = t.new(FE, "【codex】工单卡片加状态色条", "乙", "网关 middleware 链",
               "web/tickets.css", "工单卡片按状态加一道色条，深色模式下要看得清。")
    t("claim", n2, "--by", f"{FE}-01")
    t("submit", n2, "--evidence", "本机起服务，五个视图逐个看过",
      "--verify-command", "for f in web/tests/*.mjs; do node \"$f\"; done",
      "--raw-output", "四条探针全 OK")
    t("judge", n2, "--rework", "色条在深色模式下看不清", "--by", FE, "--blame", "模型",
      "--verdict", "模型责任：色条颜色是执行方自己挑的，任务书没指定。"
                   "用户怎么打开它：开任意工单页，卡片左侧色条与底色对比度不足。")
    t("say", "--slot", FE, "--by", ORCH, "--ref", n2, f"{ORCH} 判退了 {n2}，色条对比度不够")

    # ③ 前端再来一张「新建」的——总监位那一页要同时看到「新建」和「返工」两栏
    t.new(FE, "工单台顶栏值面常显", "乙", "网关 middleware 链",
          "web/tickets.css", "顶栏那几个值面项常显，不要折叠。")

    # ④ 一张待答的拍板单——设计者队列那一页的主角
    t("ask", "--type", "拍板", "--slot", BE, "--title", "限流阈值定多少", "--by", BE,
      "--body", "一、这是什么 登录接口每分钟允许几次。 二、选了会怎样 "
                "10 次偏严，正常用户改密码会撞到；30 次偏松。 三、推荐 推荐 20 次/分钟。")

    # ⑤ 再来一张带「限流」的新建单，让搜索页有三条命中
    t.new(DATA, "补一条迁移：限流计数表", "乙", "生产环境 /api 链",
          "ticket_desk/model.py", "加一张限流计数表和对应迁移。")


def shoot(port: int) -> None:
    from playwright.sync_api import sync_playwright

    base = f"http://127.0.0.1:{port}/"
    OUT.mkdir(parents=True, exist_ok=True)
    shots = [
        ("01-总监位.png",  'button[data-view="slots"]',    None),
        ("02-队列.png",    'button[data-view="designer"]', None),
        ("03-搜索.png",    'button[data-view="search"]',   "限流"),
    ]
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=SCALE)
        page.goto(base, wait_until="networkidle")
        for name, nav, query in shots:
            page.click(nav)
            page.wait_for_timeout(400)
            if query:
                page.fill("#searchBox", query)
                page.click("[data-search-run]")
                page.wait_for_timeout(700)
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / name))
            print(f"  拍好 {name}")
        browser.close()


def main() -> None:
    # 目录名固定叫 ticket-desk-demo：它会印在截图的「任务书路径」上，
    # 随机后缀会让读者以为那串乱码是必需的。
    root = Path(tempfile.gettempdir()) / "ticket-desk-demo"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    port = free_port()
    server = None
    try:
        print(f"造演示数据 → {root}")
        demo_config(root)
        seed(root)
        print(f"起服务 127.0.0.1:{port}")
        server = subprocess.Popen(
            [sys.executable, str(CLI), "serve", "--host", "127.0.0.1", "--port", str(port)],
            cwd=REPO, env=desk_env(root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )
        for _ in range(80):                       # 最多等 8 秒
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                if server.poll() is not None:
                    raise SystemExit(f"★ 服务没起来：\n{server.stdout.read()}")
                time.sleep(0.1)
        else:
            raise SystemExit("★ 服务 8 秒内没起来")
        print("拍图")
        shoot(port)
    finally:
        if server and server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n完成 → {OUT}")


if __name__ == "__main__":
    main()
