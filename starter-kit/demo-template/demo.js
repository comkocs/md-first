'use strict';
// ═══════════════════════════════════════════════════════════════════════
// 改这一块就够了。下面的渲染代码基本不用动。
//
// ★假数据要「假得像真的」。
//   「张三 / 1280 元 / 房租」会让人产生意见；
//   「测试用户 / 123 / test」不会——而产生意见正是这页 DEMO 的全部目的。
// ═══════════════════════════════════════════════════════════════════════

const DATA = {
  标题: '这个月花了多少',
  副标题: '我自己，晚上躺床上，想知道还剩多少能花',

  // ① 第一眼看见的那个数
  统计: {
    名字: '本月已花',
    值: '¥4,280',
    旁注: '预算 ¥6,000，还剩 ¥1,720 · 还有 11 天',
  },

  // ② 清单。字段名随便改，渲染会自动跟着走。
  清单标题: '最近的花销',
  清单: [
    { 主: '房租', 次: '9 月 1 日 · 住', 值: '¥2,400', 类: '住' },
    { 主: '菜市场', 次: '9 月 9 日 · 吃', 值: '¥86', 类: '吃' },
    { 主: '地铁月卡', 次: '9 月 3 日 · 行', 值: '¥150', 类: '行' },
    { 主: '外卖', 次: '9 月 10 日 · 吃', 值: '¥38', 类: '吃' },
    { 主: '朋友生日礼物', 次: '9 月 7 日 · 其他', 值: '¥260', 类: '其他' },
    { 主: '水电燃气', 次: '9 月 5 日 · 住', 值: '¥180', 类: '住' },
  ],

  // ③ 一个能点的动作
  动作标题: '记一笔',
  动作提示: '花了多少，买了什么（例：38 外卖）',
  动作按钮: '记下',
  // 点了之后回一句话。假的，但要像真的会发生的事。
  动作回应: (输入) => `已记下「${输入}」。本月已花 ¥4,318，还剩 ¥1,682。`,
};

// ═══════════════════════════════════════════════════════════════════════
// 下面不用改
// ═══════════════════════════════════════════════════════════════════════

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

$('title').textContent = DATA.标题;
$('subtitle').textContent = DATA.副标题;
$('statLabel').textContent = DATA.统计.名字;
$('statValue').textContent = DATA.统计.值;
$('statNote').textContent = DATA.统计.旁注;
$('listTitle').textContent = DATA.清单标题;
$('actionTitle').textContent = DATA.动作标题;
$('actionInput').placeholder = DATA.动作提示;
$('actionForm').querySelector('button').textContent = DATA.动作按钮;
document.title = `DEMO · ${DATA.标题}`;

const 全部类 = [...new Set(DATA.清单.map((r) => r.类).filter(Boolean))];
$('filter').innerHTML = '<option value="">全部</option>' +
  全部类.map((c) => `<option>${esc(c)}</option>`).join('');

function render() {
  const 选中 = $('filter').value;
  const 行 = DATA.清单.filter((r) => !选中 || r.类 === 选中);
  $('list').innerHTML = 行.length ? 行.map((r) => `
    <div class="item">
      <div>
        <div class="item-main">${esc(r.主)}</div>
        <div class="item-sub muted">${esc(r.次)}</div>
      </div>
      <div class="item-value">${esc(r.值)}</div>
    </div>`).join('') : '<p class="muted">这一类还没有记录。</p>';
}

$('filter').onchange = render;
render();

$('actionForm').onsubmit = (e) => {
  e.preventDefault();
  const 输入 = $('actionInput').value.trim();
  if (!输入) return;
  // ★故意不真的往清单里加：这是 DEMO，不是应用。
  //   它只要让人看见「点了之后大概会发生什么」，然后说出哪里不对。
  $('actionResult').textContent = DATA.动作回应(输入);
  $('actionInput').value = '';
};
