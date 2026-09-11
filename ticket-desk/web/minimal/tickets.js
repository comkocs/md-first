'use strict';
// 最小只读台面。全部数据来自服务端 API，页面自己不算任何业务结论——
// ★这一条是有意的：状态机、权限闸、口径全在服务端一处。页面再算一份，两处必漂，
//   而漂开的那一天你只会看到「屏上写的和 CLI 说的不一样」，查不出谁对。

const api = async (path) => {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  const payload = await response.json().catch(() => ({ ok: false, reason: '返回的不是 JSON' }));
  if (!response.ok || payload.ok === false) throw new Error(payload.reason || `HTTP ${response.status}`);
  return payload.result === undefined ? payload : payload.result;
};

const el = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let all = [];
// 小写全文索引只在**数据刷新那一刻**算一次；搜索时只查缓存不重算。
// 逐键全量搜索在一千多张单上会把输入框卡住，而那一卡是没有任何报错的。
let index = new Map();

function buildIndex(rows) {
  index = new Map(rows.map((row) => [row.编号, JSON.stringify(row).toLowerCase()]));
}

function fillOptions(select, values, keep) {
  select.innerHTML = '<option value="">全部</option>' +
    values.map((value) => `<option${value === keep ? ' selected' : ''}>${esc(value)}</option>`).join('');
}

function card(row) {
  const shot = row.实机图标记 ? ` · ${esc(row.实机图标记)}` : '';
  const tier = row.任务档 ? `${esc(row.任务档)}档` : '未标档';
  const lines = (row.开窗指令 || []);
  return `<article class="ticket state-${esc(row.状态)}">
    <h3><a href="/api/ticket/${esc(row.编号)}" target="_blank">${esc(row.编号)}</a> ${esc(row.标题)}</h3>
    <p class="meta">${esc(row.状态)} · ${tier} · ${esc(row.所属总监位)} · 指派 ${esc(row.指派给 || '未指派')}${shot}</p>
    ${row.判语 ? `<p class="verdict">判语：${esc(String(row.判语).split('\n')[0])}</p>` : ''}
    ${lines.length ? `<details><summary>开窗指令</summary><pre>${esc(lines.join('\n'))}</pre></details>` : ''}
    ${(row.未发送字段 || []).length
      ? `<p class="omitted">列表页未传：${esc(row.未发送字段.join('、'))}（点单号看全文）</p>` : ''}
  </article>`;
}

function render() {
  const slot = el('slot').value;
  const state = el('stateFilter').value;
  const needle = el('searchBox').value.trim().toLowerCase();
  const rows = all.filter((row) =>
    (!slot || row.所属总监位 === slot) &&
    (!state || row.状态 === state) &&
    (!needle || (index.get(row.编号) || '').includes(needle)));
  el('count').textContent = `${rows.length} / ${all.length} 张`;
  el('list').innerHTML = rows.length ? rows.map(card).join('') : '<p>没有符合条件的单。</p>';
}

async function load() {
  el('list').textContent = '读取中…';
  try {
    all = await api('/api/tickets');
    buildIndex(all);
    const keepSlot = el('slot').value;
    const keepState = el('stateFilter').value;
    fillOptions(el('slot'), [...new Set(all.map((row) => row.所属总监位))].sort(), keepSlot);
    fillOptions(el('stateFilter'), [...new Set(all.map((row) => row.状态))].sort(), keepState);
    render();
  } catch (error) {
    el('list').innerHTML = `<p class="error">读不到工单：${esc(error.message)}</p>`;
  }
}

async function loadState() {
  try {
    const board = await api('/api/state');
    el('state').textContent = '当前值面：' + (board.项 || [])
      .map((item) => `${item.标签} ${item.文本}`).join(' · ');
  } catch {
    // 值面读不到不算错：老服务端可能没有这个接口。宁可这一行空着，也不弹一个吓人的报错。
    el('state').textContent = '';
  }
}

el('refresh').onclick = load;
// ★搜索只在按钮与回车上触发，不绑 input 事件：逐键重算在大台面上会卡住输入框。
el('searchButton').onclick = render;
el('searchBox').addEventListener('keydown', (event) => { if (event.key === 'Enter') render(); });
el('slot').onchange = render;
el('stateFilter').onchange = render;

loadState();
load();
