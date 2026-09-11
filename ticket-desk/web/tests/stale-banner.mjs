/* T-001658:红条 undefined 的真执行探针。
 *
 * 待复检卡片红条曾显示「已 undefined 小时,超过 undefined 小时线」,
 * 据此误判复检卡了二十多单。当时的用例只看源码形状,从没把 waitingCard 真跑起来——
 * 这条探针真执行渲染:数取不到(含 stale 是无键真值的极端形态)必须整条不渲染,
 * 连一个 undefined 都不许出现在产物里。
 *
 * 跑法:node web/tests/stale-banner.mjs   (退 0 即通过)
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
const exported = "staleInfo,staleRenderable,waitingCard,staleHours";
vm.runInContext(`${source}\n;globalThis.__probe = {${exported}};`, sandbox, { filename: "tickets.js" });
const probe = sandbox.__probe;

const failures = [];
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures.push(`${name}\n    实际 ${JSON.stringify(actual)}\n    期望 ${JSON.stringify(expected)}`);
}
const HOUR = 60 * 60 * 1000;
const base = { 编号: "T-000001", 类型: "派单", 标题: "探针单", 所属总监位: "前端·界面与交互", 任务书路径: "", 指派给: "" };
const entered = (hours) => new Date(Date.now() - hours * HOUR).toISOString();

/* ① 未知状态 / 垃圾时间:staleInfo 必须回 null,不许带病对象。 */
check("未知状态回 null", probe.staleInfo({ ...base, 状态: "神秘态", 状态进入时间: entered(30) }), null);
check("垃圾时间回 null", probe.staleInfo({ ...base, 状态: "待复检", 状态进入时间: "不是时间" }), null);
check("空时间回 null", probe.staleInfo({ ...base, 状态: "待复检", 状态进入时间: "", 最后更新时间: "" }), null);
/* ② 真越线:数要真的算得出来(待复检 8 小时线)。 */
check("越线 9 小时", probe.staleInfo({ ...base, 状态: "待复检", 状态进入时间: entered(9) }), { threshold: 8, hours: 9 });
check("未越线不报", probe.staleInfo({ ...base, 状态: "待复检", 状态进入时间: entered(2) }), null);
/* ③ 渲染闸:无键真值(屏上那种)一律不渲染。 */
check("staleRenderable 空对象", probe.staleRenderable({}), false);
check("staleRenderable null", probe.staleRenderable(null), false);
check("staleRenderable 缺阈值", probe.staleRenderable({ hours: 9 }), false);
check("staleRenderable 正常", probe.staleRenderable({ threshold: 8, hours: 9 }), true);
/* ④ waitingCard 真跑:产物里不许出现 undefined,也不许出残缺红条。 */
const broken = probe.waitingCard({ ...base, 状态: "待复检" }, {});
check("无键真值不渲染红条", broken.includes("卡在「"), false);
check("无键真值不上 undefined", broken.includes("undefined"), false);
const fine = probe.waitingCard({ ...base, 状态: "待复检" }, { threshold: 8, hours: 9 });
check("正常红条在", fine.includes("卡在「待复检」已 9 小时,超过 8 小时线"), true);
check("正常产物无 undefined", fine.includes("undefined"), false);
/* ⑤ 卡片徽标那条路(staleBadge)也被同一闸守着——用同判据复核一遍有限数。 */
check("徽标判据同闸", probe.staleRenderable({ threshold: NaN, hours: 9 }), false);

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("stale-banner 探针:11 项全过");
