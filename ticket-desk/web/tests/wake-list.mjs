/* tickets.js 的第一个**真执行**用例(T-001313 补)。
 *
 * 为什么非要真跑不可:「要你去唤醒的窗口」曾被从 10 格改塌成 2 格,
 * 而当时那一批用例**全绿**——因为它们只断言源码里有没有某一行,没有真的调用过 wakeList()。
 * 病根是 blankData() 给每一位预填了空数组 `[]`,而「有没有全文」用 Array.isArray 判断,
 * 空数组也是数组,于是除当前那一位外全被算成「0 条未读」筛掉了。
 * 只看形状的用例永远抓不到这种事,只有真跑才抓得到。
 *
 * 跑法:node web/tests/wake-list.mjs   (退 0 即通过)
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "tickets.js"), "utf8");

/* tickets.js 在顶层就会碰 location / document / indexedDB,所以先把它们垫上。
   这里只垫到「能把函数定义跑出来」为止,不模拟真浏览器。 */
const store = new Map();
const stubElement = () => new Proxy({}, {
  get: (target, key) => (key in target ? target[key] : (key === "classList" ? { add(){}, remove(){} } : () => {})),
  set: (target, key, value) => { target[key] = value; return true; },
});
const sandbox = {
  location: { protocol: "https:", href: "https://desk/" },
  document: {
    /* 顶层有几处 $("#x").onclick=… 的绑定,所以这里要回一个哑元件而不是 null。 */
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
/* const/let 在 vm 里是词法绑定,不会挂到全局上,所以在同一段作用域末尾把要测的导出来。
   ★只加一行导出,tickets.js 本身一个字都不改——用例不许为了好测而改真源。 */
const exported = "DESK,slotNames,app,unread,wakeList,slotUnreadForOwner,slotLatestUnread,unreadFor,readApi";
vm.runInContext(
  `${source}
;globalThis.__probe = {${exported}};`,
  sandbox, { filename: "tickets.js" },
);
const probe = sandbox.__probe;

/* 位名一律从 DESK 取:它开机时会被 /api/config 覆盖,这里跑在 vm 里没有服务端,
   拿到的就是页面自带的兜底名单——本探针要的正是「兜底那一份也不能出错」。
   ★不许在这里自己 concat 补位:补出来的重复项会让唤醒段的计数看着对、其实是靠重复凑的。 */
const SLOTS = probe.slotNames();
const failures = [];
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures.push(`${name}\n    实际 ${JSON.stringify(actual)}\n    期望 ${JSON.stringify(expected)}`);
}

/* 造一份「服务端摘要说 12 位都有未读,而本地一条全文都没加载」的状态。
   这正是刷新之后的真实形态:全文只拉当前在看的那一位。 */
const summary = {};
for (const slot of SLOTS) {
  summary[slot] = {
    总行数: 3,
    未读: { 设计者: 2, 总编排: 1, [slot]: 2 },
    最新未读: { 时间: "2026-09-08T06:00:00+08:00", 发言人: "某某-01", 摘要: "交板了" },
  };
}
probe.app.data = {
  slots: { 总监位: SLOTS.map((名字) => ({ 名字, 启用: true })) },
  staff: { 总监位: {} },
  items: [],
  threads: {},          // ← 没有任何全文
  threadSummary: summary,
  log: [],
  state: { 值: {}, 最近改动: {}, 未填: [], 项: [] },
};
probe.app.slot = SLOTS[0];

check("① 没有全文时,未读数要走摘要", probe.unread(SLOTS[1]), 2);
check("② 唤醒段要列出全部有未读的位", probe.wakeList().length, SLOTS.length);

/* ★核心回归:把 threads 换成 blankData() 那种「每位预填空数组」的形态。
   空数组的含义必须是「这一位真的一条都没有」,不能被当成「没加载」——
   反过来也一样:没加载的位必须是 undefined,才轮得到摘要。 */
probe.app.data.threads = Object.fromEntries(SLOTS.map((s) => [s, []]));
check("③ 空数组=真的没有,未读就是 0", probe.unread(SLOTS[1]), 0);
check("③ 空数组=真的没有,唤醒段也该是空的", probe.wakeList().length, 0);

/* 有全文的那一位按全文算,且要盖过摘要(全文最准)。 */
probe.app.data.threads = {};
probe.app.data.threads[SLOTS[1]] = [
  { 发言人: "别人", 时间: "2026-09-08T07:00:00+08:00", 文字: "一条", 已读标记: [] },
];
check("④ 有全文的位按全文算,盖过摘要", probe.unread(SLOTS[1]), 1);
check("④ 其余位仍走摘要", probe.unread(SLOTS[2]), 2);

/* ★★ 最要紧的一条:真跑 readApi(),因为 bug 就在**它怎么造 app.data** 那一步。
   上面那几条只喂状态、不跑 readApi,所以把 fix 撤掉它们照样全绿——
   实测过:变异活下来了,补上这一条才抓得到。 */
const canned = {
  "/api/slots": { slots: { 总监位: SLOTS.map((名字) => ({ 名字, 启用: true })) }, staff: { 总监位: {} } },
  "/api/state": { 值: {}, 最近改动: {}, 未填: [], 项: [] },
  "/api/tickets": [],
  "/api/thread-summary": summary,
};
sandbox.fetch = async (path) => {
  let result = canned[path];
  if (result === undefined) {
    if (path.startsWith("/api/changes")) result = { 游标: 7, 整份重取: false, 总数: 0, 工单: [] };
    /* 当前那一位会真的拉全文——给它一条未读,这样 13 位应当全部出现在唤醒段:
       12 位走摘要 + 1 位走全文。少了哪一边都能看出来。 */
    else if (path.startsWith("/api/inbox")) result = [
      { 发言人: "别人-01", 时间: "2026-09-08T06:30:00+08:00", 文字: "当前位的一条未读", 已读标记: [] },
    ];
    else result = {};
  }
  return { ok: true, status: 200, json: async () => ({ ok: true, result }) };
};
probe.app.slot = SLOTS[0];
await probe.readApi();
check("⑤ readApi 之后,唤醒段必须列出全部有未读的位", probe.wakeList().length, SLOTS.length);
check("⑤ 非当前位的未读也要有(走摘要)", probe.unread(SLOTS[3]), 2);

if (failures.length) {
  console.error("wake-list 用例红了:\n  - " + failures.join("\n  - "));
  process.exit(1);
}
console.log(`wake-list 用例全过(${SLOTS.length} 位)`);
