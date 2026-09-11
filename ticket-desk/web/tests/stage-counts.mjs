/* 件数条与设计者三段的**真执行**用例(T-001322 补)。
 *
 * 起因:队列页上「实际要开窗的是 5 个,上面却显示 3 个」。
 * 查下来两边都没算错,是**两个维度**:
 *   · 件数条后面那几格 = 按派单七步分的全项目流程分布,「建单」只数状态为「新建」的;
 *   · 「要你传达的」   = 待办,含「已认领/返工但还没点过已开窗」的单。
 * 一张已认领、没传达过的单,在流程上算「执行」,在待办上仍等开窗——两边都对,
 * 可屏上看不出这件事,只会让人怀疑数错了。
 *
 * ⇒ 加了「等你开窗 / 等你答」两格放最前,并给每一格写了 title 说明口径。
 * 这条用例钉的就是那个数:**件数条第一格必须与下面那一段逐张相等**。
 * 只断言源码里有没有某一行是没用的(上一窗刚栽过),所以这里真跑 stageGroups()。
 *
 * 跑法:node web/tests/stage-counts.mjs   (退 0 即通过)
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "tickets.js"), "utf8");

const store = new Map();
const stubElement = () => new Proxy({}, {
  get: (target, key) => (key in target ? target[key] : (key === "classList" ? { add(){}, remove(){} } : () => {})),
  set: (target, key, value) => { target[key] = value; return true; },
});
const sandbox = {
  location: { protocol: "https:", href: "https://desk/" },
  document: {
    querySelector: () => stubElement(),
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => stubElement(),
    body: null,
  },
  indexedDB: { open: () => ({}) },
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setTimeout, clearTimeout, console, fetch: async () => ({}),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
/* ★只加一行导出,tickets.js 本身一个字都不改——用例不许为了好测而改真源。 */
const exported = "app,stageGroups,stageBar,wantsDispatch,wantsDesignerAnswer,flowIndex,TODO_STAGES,"
  + "isVerified,isJudgedForMerge,awaitingVerify,readyToMerge";
vm.runInContext(`${source}\n;globalThis.__probe = {${exported}};`, sandbox, { filename: "tickets.js" });
const probe = sandbox.__probe;

const failures = [];
function check(name, actual, expected) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    failures.push(`${name}\n    实际 ${JSON.stringify(actual)}\n    期望 ${JSON.stringify(expected)}`);
  }
}

const SLOT = "后端·服务与接口";
let seq = 0;
/* 造一张够真的派单:进「要你传达的」要同时满足 类型=派单 / 状态∈{新建,已认领,返工} /
   指派给是合法员工编号 / 有任务书路径 / 没点过已开窗。少一条它就不该出现在那一段。 */
function ticket(状态, extra = {}) {
  seq += 1;
  return {
    编号: `T-90${String(seq).padStart(4, "0")}`, 类型: "派单", 状态,
    标题: `【claude】造的第 ${seq} 张`, 所属总监位: SLOT, 指派给: `${SLOT}-01`,
    任务书路径: "D:\\任务书.md", 返工次数: 0, 已开窗: null, 非用户可感知: false,
    ...extra,
  };
}
/* 「已开窗」戳记:轮次与返工次数相等才算传达过(T-000172 撞号那一课)。 */
const opened = (返工次数 = 0) => ({ 已开窗: { 轮次: 返工次数, 时间: "2026-09-08T07:00:00+08:00" }, 返工次数 });

const items = [
  ticket("新建"),                                  // 等你开窗 + 建单
  ticket("新建"),                                  // 等你开窗 + 建单
  ticket("新建"),                                  // 等你开窗 + 建单
  ticket("返工", { 返工次数: 1 }),                  // 等你开窗 + 开窗
  ticket("已认领"),                                // ★等你开窗 + 执行 —— 撞到的正是这一张
  ticket("已认领", opened()),                      // 传达过了:只进「执行」
  ticket("新建", opened()),                        // 传达过、还没 claim:flowIndex 算「执行」
  ticket("待判"),                                  // 判卷
  ticket("关闭"),                                  // 关闭
  ticket("作废"),                                  // 作废
  { 编号: "T-909001", 类型: "需求", 状态: "待答", 标题: "要设计者答的",
    所属总监位: SLOT, 指派给: "设计者", 返工次数: 0, 已开窗: null },
  { 编号: "T-909002", 类型: "需求", 状态: "待答", 标题: "不要他答的",
    所属总监位: SLOT, 指派给: SLOT, 返工次数: 0, 已开窗: null },
];
probe.app.data = {
  slots: { 总监位: [{ 名字: SLOT, 启用: true }] },
  staff: { 总监位: {} }, items, threads: {}, threadSummary: {}, log: [],
  state: { 值: {}, 最近改动: {}, 未填: [], 项: [] },
};

const groups = probe.stageGroups();
const count = (name) => (groups.get(name) || []).length;
const dispatchAll = items.filter(probe.wantsDispatch);
const answerAll = items.filter(probe.wantsDesignerAnswer);

/* ★★ 这条就是上面那件事:第一格必须和下面那一段是同一个数。 */
check("① 件数条「等你开窗」= 下面「要你传达的」张数", count("等你开窗"), dispatchAll.length);
check("① 而且是 5 张,不是 3 张(那三张只是其中「新建」的那部分)", count("等你开窗"), 5);
check("② 件数条「等你答」= 下面「要你答的」张数", count("等你答"), answerAll.length);
check("② 只数指派给设计者的,不数别位自答的", count("等你答"), 1);

/* 后面那几格仍是流程分布,一张单同时出现在两边是**有意的**。 */
check("③ 「建单」仍只数状态为新建且没传达过的", count("建单"), 3);
check("③ 「开窗」这一格就是返工", count("开窗"), 1);
check("③ 已认领 2 张 + 已开窗的新建 1 张都算「执行」", count("执行"), 3);
check("③ 末尾「待答」数的是全部待答单,含不要他答的那张", count("待答"), 2);

/* ④ 两个维度会重叠——这一条是把「重叠是对的」钉死,免得日后有人当重复计数去掉它。 */
const 已认领没传达 = items.find((t) => t.状态 === "已认领" && !t.已开窗);
check("④ 同一张「已认领没传达」的单,待办算等你开窗",
  probe.wantsDispatch(已认领没传达), true);
check("④ 同一张单,流程算「执行」",
  probe.flowIndex(已认领没传达), 2);

/* ⑤ 待办两格必须排在最前,并且带得上口径说明——数对上之后,人还得看得懂为什么会重叠。 */
check("⑤ 待办两格排在最前", [...groups.keys()].slice(0, 2), ["等你开窗", "等你答"]);
const bar = probe.stageBar();
check("⑤ 两格都渲染出来了", /stage-cell todo[^"]*" data-stage="等你开窗"/.test(bar), true);
check("⑤ 每一格都要有 title 说清口径", (bar.match(/title="/g) || []).length, (bar.match(/data-stage="/g) || []).length);
check("⑤ 口径里要点明两边会重叠", /同一张单两边都会出现/.test(probe.TODO_STAGES["等你开窗"]), true);

/* ⑥ D9-460 ④:复检那条线的两格。判卷与复验**并行**,所以「待复验」必须含「待判」——
   这正是那一条的意义:员工一交板复检席就能动手,不必等总监判过。
   ★与服务端 pending_verify / ready_to_merge 是同一把尺子,两边口径漂了这里就该红。 */
const verified = { 复验: { 结论: "过", 复验人: "独立复检" } };
const review = [
  ticket("待判"),                                     // 交板了没复验 → 待复验
  ticket("待判", verified),                           // 复验过了但没判 → 都不算
  ticket("待复检", { 判卷人: "UI总监" }),               // 判过没复验 → 待复验
  ticket("待复检", { 判卷人: "UI总监", ...verified }),  // 两道齐 → 可并
  ticket("已合并", { 判卷人: "UI总监", ...verified }),  // 并过了 → 都不算
];
probe.app.data.items = review;
const reviewGroups = probe.stageGroups();
check("⑥ 待复验含「待判」——不等判卷是这一条的本体",
  (reviewGroups.get("待复验") || []).length, 2);
check("⑥ 可并 = 判过 ∧ 复验过", (reviewGroups.get("可并") || []).length, 1);
check("⑥ 复验过但没判卷的,不算可并", probe.readyToMerge(review[1]), false);
check("⑥ 判过但没复验的,不算可并", probe.readyToMerge(review[2]), false);
check("⑥ 已并线的不再进这两格",
  probe.awaitingVerify(review[4]) || probe.readyToMerge(review[4]), false);
check("⑥ 机器闸写的复验也认(只读结论,不看是谁)",
  probe.isVerified({ 复验: { 结论: "过", 复验人: "机器闸" } }), true);

/* ⑦ 空台面不该冒出一排 0 格。 */
probe.app.data.items = [];
check("⑦ 没单时件数条整条不显示", probe.stageBar(), "");

if (failures.length) {
  console.error("stage-counts 用例红了:\n  - " + failures.join("\n  - "));
  process.exit(1);
}
console.log(`stage-counts 用例全过(件数条 ${groups.size} 格)`);
