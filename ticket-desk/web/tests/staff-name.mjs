/* T-001659 补漏:三位员工号(-100 起)在网页上的真执行探针。
 *
 * 服务端 STAFF_PATTERN 已放宽到两三位,但网页端的 STAFF_NAME 与 activeStaff
 * 还写死恰好两位——三位员工的单被整张挡出「要你传达的」(设计者看不到、开不了窗),
 * 网页认领/建单表单也不认。这条探针真跑那几条路。
 *
 * 跑法:node web/tests/staff-name.mjs   (退 0 即通过)
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
const exported = "app,render,STAFF_NAME,wantsDispatch,activeStaff,ticketCard";
vm.runInContext(`${source}\n;globalThis.__probe = {${exported}};`, sandbox, { filename: "tickets.js" });
const probe = sandbox.__probe;

const SLOT = "后端·服务与接口";
const member = (number, state = "在岗") => ({
  编号: number, 员工名: `${SLOT}-${String(number).padStart(2, "0")}`,
  "工具/窗类型": "claude", 开窗时间: "2026-09-09T20:00:00+08:00", 状态: state,
  经手工单号列表: [], 固定工位: false, 记忆md路径: "",
});
probe.app.data = {
  items: [], slots: { 模型名册: [] },
  staff: { 总监位: { [SLOT]: { 下一个编号: 101, 员工: [member(7), member(100)] } } },
  threads: {},
};
const threeDigit = {
  编号: "T-009001", 类型: "派单", 标题: "三位号员工的单", 所属总监位: SLOT,
  状态: "已认领", 指派给: `${SLOT}-100`, 任务书路径: "D:/x.md", 已开窗: null,
};

const failures = [];
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures.push(`${name}\n    实际 ${JSON.stringify(actual)}\n    期望 ${JSON.stringify(expected)}`);
}

/* ① 员工名形状:两位、三位都认;一位、四位、裸名都不认。 */
check("两位号认", probe.STAFF_NAME.test(`${SLOT}-07`), true);
check("三位号认", probe.STAFF_NAME.test(`${SLOT}-100`), true);
check("一位号不认", probe.STAFF_NAME.test(`${SLOT}-1`), false);
check("四位号不认", probe.STAFF_NAME.test(`${SLOT}-1000`), false);
check("裸位名不认", probe.STAFF_NAME.test(SLOT), false);
/* ② 三位号的单要进「要你传达的」——这正是设计者看不到单的那道闸。 */
check("三位号进传达队列", probe.wantsDispatch(threeDigit), true);
check("没指派仍不进", probe.wantsDispatch({ ...threeDigit, 指派给: "" }), false);
/* ③ 认领闸:名册里的 -100 放行,不在册的拦。 */
probe.activeStaff(threeDigit, `${SLOT}-100`);  // 在册在岗:不抛即过
let refused = "";
try { probe.activeStaff(threeDigit, `${SLOT}-101`); } catch (e) { refused = e.message; }
check("不在册的拦", refused.includes("名册"), true);
/* ④ 整卡渲染与整页渲染都不许因三位号崩(撞到过)。 */
try {
  probe.ticketCard(threeDigit);
  check("ticketCard 三位号不崩", true, true);
} catch (e) {
  check("ticketCard 三位号不崩", e.message, "(不该抛)");
}
try {
  probe.app.data.items = [threeDigit];
  probe.app.view = "designer";
  probe.render();
  check("设计者页三位号不崩", true, true);
} catch (e) {
  check("设计者页三位号不崩", e.message, "(不该抛)");
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("staff-name 探针:10 项全过");
