/* 工单台：http(s) 走同源 API；file:// 只读 bundle 回落。 */
"use strict";

/* ★口径只有一份,在服务端(service.desk_config → GET /api/config)。
   下面这些是**读不到服务端时**的兜底值,不是第二份真源。

   为什么这么改:这几样原来在前端各抄一份,靠注释里一句「与服务端同步修改」约束。
   那句话拦不住任何人——位名、停滞阈值、实机来源标签都真漂开过,
   而症状是**页面安静地显示错的东西**:名单少一位,那一位的单在离线模式下整个看不见,
   页面一个字都不报。所以开机第一件事是把 DESK 用服务端那份覆盖掉。 */
const DESK = {
  产品名: "Ticket Desk",
  位名: ["前端","后端","内容","复检·合并与部署","平台·工单系统","总编排"],
  只分发不派单位: [],
  总编排位: "总编排",
  复检位: "复检·合并与部署",
  平台位: "平台·工单系统",
  拍板人: "设计者",
  开窗平台: ["claude","codex","vscode","zcode"],
  主场景: "主场景",
  实机来源: {world:"主场景真登录",isolated:"隔离场景",other:"其他"},
  值面标签: ["判据图","部署头","延迟预算","已改未并公共工具"],
  停滞阈值: {新建:24,已认领:8,返工:8,待判:4,待复检:8,已合并:24,待答:24},
  非停滞态: ["关闭","作废","实机复验过","阻塞"],
  新建已开窗阈值: 4,
  命令行路径: "ticket.py",
  任务书目录: "_office",
  离线包全局名: "TICKET_DESK",
};
/* 开机第一趟。读不到就沿用上面的兜底值,并在状态行里说清这一点——
   ★不许安静地用兜底值跑:那正是「页面显示错的东西却不报」的来源。 */
async function loadDeskConfig(){
  if(!API_MODE)return false;
  try{ Object.assign(DESK, await api("/api/config")); return true; }
  catch{ return false; }
}
function slotNames(){ return DESK.位名 }
function noDispatch(slot){ return (DESK.只分发不派单位||[]).includes(slot) }
function origins(){ return DESK.实机来源 }
function staleHours(){ return DESK.停滞阈值 }
function nonStale(){ return new Set(DESK.非停滞态||[]) }
const SERVICE_GATE_COPY = ["判卷人不能与执行员工是同一个人","复检人必须与执行员工、判卷人都不同","只能由总编排","员工名格式或所属位不对"];
const DECISION_TEMPLATE = "一、这是什么\n\n二、选了会怎样\n\n三、推荐\n";
const STATE_UNSET = "未填";
const DB_NAME = "ticket-desk", HANDLE_KEY = "ticketsRoot";
const API_MODE = ["http:","https:"].includes(location.protocol);
const app = { handle:null, data:null, view:"slots", slot:DESK.位名[0], imageUrls:new Map(), token:"" };
const $ = s => document.querySelector(s);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const nowText = () => new Date().toISOString();

function notify(text, ok=false) {
  const box = $("#notice"); box.hidden = false; box.textContent = text;
  box.style.borderColor = ok ? "#397851" : "#886b35";
  clearTimeout(box._timer); box._timer = setTimeout(() => box.hidden = true, 6500);
}

function openDb() {
  return new Promise(resolve => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore("handles");
    request.onsuccess = () => resolve(request.result); request.onerror = () => resolve(null);
  });
}
async function loadHandle() {
  const db = await openDb(); if (!db) return null;
  return new Promise(resolve => { const request = db.transaction("handles").objectStore("handles").get(HANDLE_KEY); request.onsuccess=()=>resolve(request.result||null); request.onerror=()=>resolve(null); });
}
async function saveHandle(handle) {
  const db = await openDb(); if (!db) return;
  db.transaction("handles","readwrite").objectStore("handles").put(handle,HANDLE_KEY);
}
async function permission(handle, write=false) {
  if (!handle) return false; const mode = write ? "readwrite" : "read";
  try { let state=await handle.queryPermission({mode}); if (state!=="granted") state=await handle.requestPermission({mode}); return state==="granted"; } catch { return false; }
}
async function readFile(dir, name, fallback="") {
  try { const handle=await dir.getFileHandle(name); return await (await handle.getFile()).text(); } catch { return fallback; }
}
async function readJson(dir,name,fallback) { try { return JSON.parse(await readFile(dir,name,"")); } catch { return fallback; } }
async function writeFile(dir,name,text) { const file=await dir.getFileHandle(name,{create:true}); const writer=await file.createWritable(); await writer.write(text); await writer.close(); }
async function writeJson(dir,name,value) { await writeFile(dir,name,JSON.stringify(value,null,2)+"\n"); }

function blankData() {
  /* ★模型名册留空:兜底不该替谁编一份名册出来。读得到 slots.json 就以它为准,
     读不到就该看得出「这里没有数据」,而不是看见一串不存在的模型名。
     ★threads 必须是空对象,**不能**给每一位预填空数组:
       「有没有全文」是靠 Array.isArray 判断的,空数组也是数组——预填会让
       除当前那一位之外的全部位被算成「0 条未读」,唤醒段整段塌掉,而页面一个字都不报。 */
  const slots=slotNames();
  return {slots:{主力模型集合:[],模型名册:[],停用阈值:{同位:3,全项目:5},总监位:slots.map(名字=>({名字,启用:true,主力模型:true}))},staff:{模型记分:{},模型停用:{全项目:[],按位:Object.fromEntries(slots.map(s=>[s,[]]))},总监位:Object.fromEntries(slots.map(s=>[s,{下一个编号:1,员工:[]}]))},items:[],threads:{},log:[],modelStats:[],state:{值:{},最近改动:{},未填:[],项:[]}};
}
function fallbackData() { return window[DESK.离线包全局名] || blankData(); }

async function api(path, options={}, mayAskToken=true) {
  const headers={...(options.headers||{})};
  if(options.body) headers["Content-Type"]="application/json";
  if(app.token) headers["X-Ticket-Token"]=app.token;
  const response=await fetch(path,{...options,headers});
  let payload={}; try{payload=await response.json()}catch{}
  if(response.status===401&&mayAskToken){const token=prompt("此工单台需要访问令牌（只保留在当前页面内存）：",app.token)||"";if(token){app.token=token;return api(path,options,false)}}
  if(!response.ok||payload.ok===false) throw new Error(payload.reason||`服务返回 HTTP ${response.status}`);
  return payload.result;
}

/* ★T-001308:增量刷新。刷新慢的实测:一次刷新下行 11.17 MB,
   而**稳态下真正变过的只有个位数张单**(最近 5 分钟 3 张 / 15 KB)。
   书签用服务端流水行号:每写一次单必写一行流水,两件事在同一把锁里完成、行号严格递增,
   所以「第 N 行之后有什么动静」是精确的——一笔改动躲不过去,不像时间戳会撞同秒边界。

   ★三道护栏,因为缓存最坏的毛病是**安静地显示旧数据**:
   ① 服务端每次带总单数,与本地对不上就整份重取(演示数据被归档那八张单证明「单会消失」真会发生);
   ② 维护类操作会在流水里带「整份重取」标,客户端照办;
   ③ 顶栏有【强制重取】,任何时候能一键回到干净状态。
   缓存放 sessionStorage:关掉标签页就没了,不会有一份跨天的旧数据躺在盘上。 */
const CACHE_KEY="deskCacheV1";
function readCache(){
  try{ const raw=sessionStorage.getItem(CACHE_KEY); return raw?JSON.parse(raw):null; }catch{ return null; }
}
function writeCache(value){
  try{ sessionStorage.setItem(CACHE_KEY,JSON.stringify(value)); }
  catch{ try{ sessionStorage.removeItem(CACHE_KEY); }catch{} }   /* 存不下就退回每次全量,不报错 */
}
function dropCache(){ try{ sessionStorage.removeItem(CACHE_KEY); }catch{} }

async function readApi() {
  const cache=readCache();
  const [meta,state]=await Promise.all([api("/api/slots"),api("/api/state").catch(()=>null)]);
  const data=blankData(); data.slots=meta.slots; data.staff=meta.staff; if(state) data.state=state;
  data.charters=meta.charters||{};   /* D9-462 补:章程路径随名册下发,老服务端没有这一格就留空 */
  let items=null,cursor=0;
  if(cache&&Array.isArray(cache.items)){
    const delta=await api(`/api/changes?since=${encodeURIComponent(cache.cursor||0)}`).catch(()=>null);
    if(delta&&!delta.整份重取){
      const byId=new Map(cache.items.map(t=>[t.编号,t]));
      (delta.工单||[]).forEach(t=>byId.set(t.编号,t));
      const merged=[...byId.values()].sort((a,b)=>String(a.编号).localeCompare(String(b.编号)));
      /* ★护栏①:条数对不上说明有单被删过(或本地漏了什么),这一份不能信,整份重取。 */
      if(merged.length===delta.总数){ items=merged; cursor=delta.游标; }
    }
  }
  if(items===null){ items=await api("/api/tickets"); cursor=(await api("/api/changes?since=999999999").catch(()=>({游标:0}))).游标||0; }
  data.items=items;
  /* ★对话线:摘要每次现算(不缓存),全文只拉**当前在看的那一位**(T-001313)。
     原来 13 条线全文每次都拉,压后约 1.01 MB、近 5 秒——而页面真正要用的只有几个数。
     ★摘要故意不进缓存:已读标记是会被回头改的(inbox --mark-read 改的是已有行),
     缓存它就会出现「标了已读、角标还亮着」这种旧数据。服务端现算就没这问题。 */
  data.threadSummary=await api("/api/thread-summary").catch(()=>({}));
  /* ★★ 必须先清空:blankData() 会给每一位预填一个**空数组**,而「有没有全文」是靠
     Array.isArray 判断的——空数组也是数组,于是除当前这一位外全被算成「0 条未读」,
     「要你去唤醒的窗口」从 10 格塌成 2 格(曾被改坏过)。
     那两格恰好是:当前在看的那一位(真有全文)+ 平台位(它不在兜底名单那
     12 个里、没被预填,所以反而正确地走了摘要)。
     ⇒ 「没加载」必须是 undefined,不能是 []。空数组的含义是「这一位真的一条都没有」。 */
  data.threads={};
  data.threads[app.slot]=await loadThread(app.slot);
  app.data=data;
  writeCache({cursor,items});
  return true;
}
async function loadThread(slot){
  return api(`/api/inbox?slot=${encodeURIComponent(slot)}&for=${encodeURIComponent(DESK.拍板人)}&all=1`).catch(()=>[]);
}
/* 切位时才把那一位的全文拉下来;拉过的留在内存里,同一次会话里来回切不重复拉。 */
async function ensureThread(slot){
  if(Array.isArray(app.data?.threads?.[slot]))return;
  app.data.threads[slot]=await loadThread(slot);
}
/* 搜索要翻全部对话线——它本来就是「用到才建索引」,所以这里顺带把没拉的线补齐。 */
async function ensureAllThreads(){
  await Promise.all(slotsOf().map(slot=>ensureThread(slot)));
}
function threadSummary(slot){ return app.data?.threadSummary?.[slot]||null; }
/* 有全文就按全文算(那是当前这一位,最准);没有就用服务端现算的摘要。 */
function unreadFor(slot,actor){
  const rows=app.data?.threads?.[slot];
  if(Array.isArray(rows))return rows.filter(row=>row.发言人!==actor&&!(row.已读标记||[]).includes(actor)).length;
  return Number(threadSummary(slot)?.未读?.[actor]||0);
}

async function readDisk() {
  if (!app.handle || !(await permission(app.handle))) return false;
  const data=blankData();
  data.slots=await readJson(app.handle,"slots.json",data.slots); data.staff=await readJson(app.handle,"staff.json",data.staff);
  data.log=(await readFile(app.handle,"log.jsonl","")).split(/\r?\n/).filter(Boolean).map(line=>{try{return JSON.parse(line)}catch{return null}}).filter(Boolean);
  try { const items=await app.handle.getDirectoryHandle("items"); data.items=[]; for await (const [name,handle] of items.entries()) if(handle.kind==="file"&&/^T-\d{6}\.json$/i.test(name)) { try { data.items.push(JSON.parse(await (await handle.getFile()).text())); } catch{} } data.items.sort((a,b)=>a.编号.localeCompare(b.编号)); } catch{}
  try { const threads=await app.handle.getDirectoryHandle("threads"); for(const slot of slotsOf(data)) { const text=await readFile(threads,`${slot}.jsonl`,""); data.threads[slot]=text.split(/\r?\n/).filter(Boolean).map(line=>{try{return JSON.parse(line)}catch{return null}}).filter(Boolean); } } catch{}
  app.data=data; return true;
}
function slotsOf(data=app.data) { return (data?.slots?.总监位||[]).filter(x=>x.启用!==false).map(x=>x.名字); }
function writable(){return API_MODE}

/* 后台写队列:页面操作不再等网络。一次只跑一笔(串行),避免两次点击互相踩;
   队列里还有活时右上角显示「后台处理中 N」,跑空了自己消失。
   失败不吞:回滚本机改动 + 红字报错,让人当场知道那一下没落库。 */
const writeQueue = { items: [], running: false };
function queueStatus(){
  let box=document.querySelector("#queueStatus");
  const n=writeQueue.items.length+(writeQueue.running?1:0);
  if(!n){ if(box)box.remove(); return; }
  if(!box){ box=document.createElement("div"); box.id="queueStatus"; document.body.appendChild(box); }
  box.textContent=`后台处理中 ${n}`;
}
function queueWrite(job){ writeQueue.items.push(job); queueStatus(); runQueue(); }
async function runQueue(){
  if(writeQueue.running)return;
  const job=writeQueue.items.shift();
  if(!job){queueStatus();return}
  writeQueue.running=true; queueStatus();
  try{
    const result=await job.run();
    if(result?.提示)notify(result.提示);
  }catch(error){
    try{job.onFail&&job.onFail()}catch{}
    notify(`${job.label} 没写进去,已退回:${error.message}`);
  }finally{
    writeQueue.running=false; queueStatus(); runQueue();
  }
}

async function refresh() {
  /* ★配置必须先于数据:位名、实机来源、停滞阈值都在它里面,
     数据先到、配置后到的话,第一帧会拿兜底名单去筛服务端的单——
     筛出来是空的,而页面只会显示「这个总监位还没有工单」。 */
  const configured=await loadDeskConfig();
  if(API_MODE&&!configured)notify("读不到 /api/config,这一次用的是页面自带的兜底口径(位名、停滞阈值可能与服务端不一致)。刷新页面可再试一次。");
  if(!slotNames().includes(app.slot))app.slot=slotNames()[0];
  const live=API_MODE?await readApi():false; if(!live) app.data=fallbackData();
  $("#sourceStatus").textContent=live?"服务模式已连接，页面操作与命令行共用同一套门禁。":"离线只读（file:// 回落包）。要能编辑请跑 ticket.py serve 后用 http 打开。";
  renderStateBoard();
  rebuildSearchIndex();
  render();
}

/* 当前值面顶栏（T-000892）：四项常显，悬停看这一项最近一次是谁、何时、旧值→新值改的。
   谁都能读，能改的只有复检席与总编排，写口子只在命令行 state set 上。 */
function renderStateBoard(){
  const box=$("#stateBoard"); if(!box)return;
  const rows=app.data?.state?.项;
  const items=(Array.isArray(rows)&&rows.length)?rows:(DESK.值面标签||[]).map(标签=>({标签,文本:STATE_UNSET,已填:false,最近改动文本:"还没有人填过这一项"}));
  box.innerHTML=items.map(row=>{
    const text=String(row.文本??"").trim()||STATE_UNSET;
    return `<span class="button ghost state-chip${row.已填?"":" unset"}" title="${esc(row.最近改动文本||"还没有人填过这一项")}"><b>${esc(row.标签)}</b> ${esc(text)}</span>`;
  }).join("");
}
async function chooseRoot() {
  if(!window.showDirectoryPicker) return notify("当前浏览器不支持直接选择目录，请改用新版 Edge 或 Chrome。")
  try { const handle=await showDirectoryPicker({mode:"readwrite",id:"ticket-desk"}); if(!(await permission(handle,true))) return; app.handle=handle; await saveHandle(handle); await refresh(); notify("工单目录已连接。",true); } catch(error) { if(error.name!=="AbortError") notify(`目录没有连上：${error.message}`); }
}

function render() {
  document.querySelectorAll("#mainNav button").forEach(b=>b.classList.toggle("active",b.dataset.view===app.view));
  ({slots:renderSlots,designer:renderDesigner,digest:renderDigest,search:renderSearch,new:renderNew}[app.view]||renderSlots)();
  bindCommon(); hydrateImages();
}
function staffFor(slot) { return app.data?.staff?.总监位?.[slot]?.员工||[]; }
/* T-001081：呼出名单默认只显示在岗——非固定工位到终态会自动收窗，攒下来的编号
   会把「现在能派给谁」这件事淹掉。已收窗的收进一个默认折起的 details，点开仍可查履历；
   数据一份没删，模型合格率也照旧读全量（账按模型统计，不按编号）。 */
function staffChip(m){return `<span class="staff ${m.状态==='在岗'?'on':'off'}" data-staff="${esc(m.员工名)}">${esc(m.员工名)} · ${esc(m['工具/窗类型'])} · ${esc(m.状态)}${m.固定工位?' · 固定工位':''}</span>`}
function staffRoster(staff){
  const onDuty=staff.filter(m=>m.状态==='在岗'),retired=staff.filter(m=>m.状态!=='在岗');
  if(!staff.length)return '尚未登记员工';
  const head=onDuty.length?onDuty.map(staffChip).join(''):'<span class="staff off">没有在岗员工</span>';
  if(!retired.length)return head;
  return head+`<details class="staff-retired pref-group" data-pref="deskRetiredOpen"${lsGet(`deskRetiredOpen:${app.slot}`)==="1"?" open":""}><summary>已收窗 ${retired.length} 位</summary>${retired.map(staffChip).join('')}</details>`;
}
function itemsFor(slot) { return (app.data?.items||[]).filter(t=>t.所属总监位===slot||(t.转交可见位||[]).includes(slot)); }
/* D9-462 补 ④:章程路径登记为开窗来源,摆在这一位工单的最上面——
   新开一扇窗的人第一件事就该读它,不该让人自己去猜办公目录在哪。
   老服务端不下发 charters,那时整行不显示(空比「未填」好:那一格没值不是谁的锅)。 */
function charterRow(slot){
  const path=String(app.data?.charters?.[slot]||"").trim();
  if(!path)return '';
  return `<div class="charter-row"><b>章程</b><code>${esc(path)}</code>`
    +`<button type="button" class="button ghost" data-copy-charter="${esc(path)}">复制路径</button>`
    +`<span class="btn-note">(开窗第一件事:读它)</span></div>`;
}

function renderSlots() {
  const slots=slotsOf(); if(!slots.includes(app.slot)) app.slot=slots[0]||DESK.位名[0];
  const staff=staffFor(app.slot), tickets=itemsFor(app.slot), states=[...new Set(tickets.map(t=>t.状态))];
  const todoStates=["新建","已认领","待判","待复检","返工","阻塞","待答"];
  const actStates=todoStates.filter(s=>["新建","返工","待答"].includes(s)&&states.includes(s));
  const doingRows=todoStates.filter(s=>["已认领","待判","待复检","阻塞"].includes(s)).map(state=>[state,tickets.filter(t=>t.状态===state).length]).filter(([,count])=>count>0);
  const doneRows=["已答","关闭","已合并","实机复验过","作废"].map(state=>[state,tickets.filter(t=>t.状态===state).length]).filter(([,count])=>count>0);
  const stateColumn=state=>`<div class="state-column"><h3>${esc(state)} · ${tickets.filter(t=>t.状态===state).length}</h3>${tickets.filter(t=>t.状态===state).map(ticketCard).join("")}</div>`;
  const actGrid=actStates.length?`<div class="state-group"><h3 class="group-title">要人动手</h3><div class="state-grid">${actStates.map(stateColumn).join("")}</div></div>`:"";
  const doingBlock=doingRows.length?`<details class="done-group pref-group" data-pref="deskDoingOpen"${lsGet(`deskDoingOpen:${app.slot}`)==="1"?" open":""}><summary>进行中 · ${doingRows.map(([state,count])=>`${esc(state)} ${count}`).join(" · ")}</summary><div class="state-grid">${doingRows.map(([state])=>stateColumn(state)).join("")}</div></details>`:"";
  const doneBlock=doneRows.length?`<details class="done-group pref-group" data-pref="deskDoneOpen"${lsGet(`deskDoneOpen:${app.slot}`)==="1"?" open":""}><summary>已办 · ${doneRows.map(([state,count])=>`${esc(state)} ${count}`).join(" · ")}</summary><div class="state-grid">${doneRows.map(([state])=>stateColumn(state)).join("")}</div></details>`:"";
  $("#app").innerHTML=`<div class="slot-tabs">${slots.map(s=>`<button data-slot="${esc(s)}" class="${s===app.slot?'active':''}" title="${esc(designerTodoTitle(s))}">${esc(s)}${designerTodo(s)?` · ${designerTodo(s)}`:''}</button>`).join("")}</div>
  <div class="slot-layout"><section class="panel"><h2>${esc(app.slot)}工单</h2>${charterRow(app.slot)}<div class="staff-row"><b>员工名册</b>${staffRoster(staff)}</div>
  ${writable()&&!noDispatch(app.slot)?`<details class="dispatch-create"><summary>总监建派单</summary><form class="form" data-dispatch-slot="${esc(app.slot)}"><label>任务档（总监定）<select name="tier" required><option>甲</option><option selected>乙</option><option>丙</option></select></label><label>上下文预算（仅丙档必填，最多 2000 行）<input name="context" type="number" min="0" max="2000"></label><label>标题<input name="title" required placeholder="一句话写清交付物；要指定开窗工具就以【claude】【codex】【vscode】【zcode】之一开头"></label><label>真源指针<input name="source" required placeholder="绝对路径或决定标题"></label><label>实机消费者<input name="consumer" required placeholder="${esc(DESK.主场景)}链上由谁加载"></label><label>交付项（逐行列出必须产出的文件）<textarea name="deliverables" required placeholder="D:\\output\\result.json&#10;T-000123-01.jpg"></textarea></label><label class="check-row"><input name="internal" type="checkbox"> 非用户可感知（内部工具；用验证命令和原样输出交板）</label><label>指派员工<select name="assign"><option value="">暂不指派</option>${staff.filter(m=>m.状态==='在岗').map(m=>`<option>${esc(m.员工名)}</option>`).join('')}</select></label><label>备注<textarea name="notes" placeholder="执行边界或验收说明"></textarea></label><button>建立派单</button></form></details>`:''}
  ${(actGrid+doingBlock+doneBlock)||'<div class="empty">这个总监位还没有工单。</div>'}</section>${chatPanel(app.slot)}</div>`;
}
/* ★T-001313:这三个数原来都靠「把 13 条线全文拉下来自己数」。现在只有当前在看的那一位有全文,
   其余走服务端现算的摘要(unreadFor / threadSummary)。取值口径一模一样,只是算在哪一端不同。 */
function unread(slot) { return unreadFor(slot, DESK.拍板人); }
/* T-000047:unread 数的是「设计者没读的行」,唤醒段要的是「那位总监自己没读的行」,两者不同。 */
function slotUnreadForOwner(slot){ return unreadFor(slot, slot); }
function slotLatestUnread(slot){
  const rows=app.data?.threads?.[slot];
  if(Array.isArray(rows)){
    const mine=rows.filter(row=>row.发言人!==slot&&!(row.已读标记||[]).includes(slot));
    if(!mine.length)return null;
    return mine.reduce((best,row)=>timeNoLater(best.时间,row.时间)?row:best);
  }
  /* 没全文时用摘要:唤醒段只用到 时间/发言人/前 40 字,服务端已经算好发来了。 */
  const latest=threadSummary(slot)?.最新未读;
  return latest?{时间:latest.时间,发言人:latest.发言人,文字:latest.摘要}:null;
}
function timeNoLater(a,b){const ta=Date.parse(a),tb=Date.parse(b);if(!Number.isNaN(ta)&&!Number.isNaN(tb))return ta<=tb;return String(a)<=String(b)}
/* deskWoke:<位名> 存「点已唤醒那一刻最新一条未读的时间」,只压住不晚于它的未读;来了更新的未读自动重新冒出来(不许存布尔,布尔会把后来的新未读永久压掉)。 */
function wakeList(){
  const result=[];
  for(const slot of slotsOf()){
    const latest=slotLatestUnread(slot);if(!latest)continue;
    const woke=lsGet(`deskWoke:${slot}`);
    if(woke!==null&&timeNoLater(String(latest.时间||""),woke))continue;
    result.push({slot,count:slotUnreadForOwner(slot),time:String(latest.时间||""),speaker:String(latest.发言人||""),preview:String(latest.文字||"").slice(0,40)});
  }
  return result;
}
function lsGet(key){try{return localStorage.getItem(key)}catch{return null}}
function lsSet(key,value){try{localStorage.setItem(key,value)}catch{}}
function lsDel(key){try{localStorage.removeItem(key)}catch{}}
/* 只列设计者按得动的。需求单按宪法只有总编排能答(service.py 的 answer 闸),
   所以「所属位是本位的待答需求」不能算进来——那会让设计者的队列里冒出三张他点不动的单,
   正是「不知道我该做什么」那一种。T-000039 R2 的口径写宽了,这里改回去:
   一律以「这张单指派给设计者」为准,与 T-000034 定的「送到设计者的待答拍板/疑问/需求」一致。
   需求要设计者拍板,应当由总编排 transfer 给设计者,转过来指派给就是设计者,自然进这一段。
   阻塞为什么**不**收进这一段(T-000683 第二轮定的口径):
   服务端这一轮确实放行了「所属总监位 + 总编排」两方答阻塞,但网页端不开这个入口——
   解阻的事实只在所属总监手里,设计者在页面上按下去等于替总监背名,记录就假了;
   而总监答单本来就走命令行,不需要网页入口。所以阻塞不进设计者队列、不出答复框,
   页面只在卡片 meta 行标一句「等 <所属总监位> 答」告诉他在等谁,不给他按钮。 */
function wantsDesignerAnswer(t){
  return t.状态==="待答"&&["拍板","疑问","需求"].includes(t.类型)&&t.指派给===DESK.拍板人;
}
function designerTodoParts(slot) {
  const tickets=itemsFor(slot);
  const waitingOpen=tickets.filter(wantsDispatch).length;
  const waitingAnswer=tickets.filter(wantsDesignerAnswer).length;
  const waitingWake=wakeList().some(row=>row.slot===slot)?1:0;
  return [waitingOpen,waitingAnswer,waitingWake];
}
/* localStorage 兜底值存的是点下去那一刻的返工次数,不是布尔；新包以服务端「已开窗」字段为准。
   判退一次,返工次数 +1,存的值就对不上了,这张单自动回到「要你传达的」并带上复制按钮;
   而刚点的那一下不会被任何 render 抹掉(旧版在 render 里清标记,点了等于没点)。
   旧数据里存的是 "1",对从没返工过的单等价于 "0",一并认。 */
/* 标记里存的是「点已开窗那一刻的返工次数」,前缀 r 是为了不跟历史值撞号:
   最早这里存的是字面量 "1"(布尔意义的「已开窗」)。改成存次数之后,
   一张返工过 1 次的单,次数也是 1——两个 "1" 撞上,台子就以为这一轮已经传达过,
   于是返工单再也进不了设计者队列。T-000067 就是这么消失的。
   这个撞号当初被评为「无害」,那个判断是错的,现在用 r 前缀彻底分开。
   旧值的认法:"r<n>" 是新式;裸 "0" 是中间那版、只可能表示没返工过;
   裸 "1" 无法分辨是「老式已开窗」还是「中间版返工1次」——一律当没开过窗,
   宁可让它多出现在队列里让人再点一次,也不能把该开的窗藏起来。 */
function reworkStamp(t){return "r"+String(t?.返工次数 ?? 0)}
function isOpened(t){
  if(t?.已开窗&&Number(t.已开窗.轮次)===Number(t?.返工次数||0))return true;
  if(t&&Object.prototype.hasOwnProperty.call(t,"已开窗"))return false;
  const v=lsGet(`deskOpened:${t.编号}`);
  if(v===null)return false;
  if(v===reworkStamp(t))return true;
  return v==="0"&&Number(t?.返工次数 ?? 0)===0;
}
const STAFF_NAME = /^.+-\d{2,3}$/;  /* T-001659 补漏:员工号已扩三位(-100 起),两位正则会把三位员工的单整张挡出队列 */
/* 进设计者队列「要你传达的」要同时满足:是派单、还没走到交板、有任务书路径、他还没点已开窗,
   ★而且必须已经指派到一个合法员工编号。
   少了最后这条会出事:一张冒烟测试单(指派给为空、任务书路径指向一个不存在的文件)
   照样排进了队列,设计者照着开了窗,烧掉一个窗口 6 分半才发现无事可做。
   指派给为空 = 还没定谁做 = 开了窗也不知道该让谁认领,本来就不该请设计者去传达。
   这类单留在总监位页等总监补指派,不进设计者的活儿。 */
/* ★T-001256 ③(D9-453 / v2.31 八章 6「非业务闸不停车」):「待回核」不再挡在队列外。
   它的意思只是「建单那台机器当时核不到那个路径」——路径本身写着,员工照常 claim 照常做,
   这是**账面**不是活。挡在外面的后果反而更重:单子既不在「要你传达的」、
   也没人当回事,设计者不开窗,那扇窗就一直不开(近两日多次)。
   现在改成:照常进队列,卡片上挂一行黄字提醒总监顺手补核。
   ★「没指派员工」那一条**仍然拦**,它是真的活没准备好:开了窗也不知道该让谁认领。 */
function taskbookPending(t){return String(t?.任务书校验||"")==="待回核"}
function wantsDispatch(t){
  return t.类型==="派单"
    &&["新建","已认领","返工"].includes(t.状态)
    &&STAFF_NAME.test(String(t.指派给||""))
    &&!!dispatchInitialPath(t)
    &&!isOpened(t);
}
function designerTodoTitle(slot){const [waitingOpen,waitingAnswer,waitingWake]=designerTodoParts(slot);return `待开窗 ${waitingOpen} · 待你答 ${waitingAnswer} · 待唤醒 ${waitingWake}`}
function designerTodo(slot){return designerTodoParts(slot).reduce((a,b)=>a+b,0)}
function refreshBadges(){document.querySelectorAll(".slot-tabs [data-slot]").forEach(button=>{const slot=button.dataset.slot,count=designerTodo(slot);button.textContent=`${slot}${count?` · ${count}`:""}`;button.title=designerTodoTitle(slot);})}
/* T-001509:未上服结案的单终态显示合成状态,与实机复验过后关掉的单分得清;悬停看原因与结案人。 */
function stateLabel(t){return t.未上服结案?'已合并·未上服·已结案':t.状态}
function stateTitle(t){const nd=t.未上服结案;if(!nd)return'';return `未上服结案:${String(nd.原因||'')} · 结案人 ${String(nd.结案人||'')}`}
function ticketCard(t) {
  const images=[...(t.图片列表||[])];
  const stale=staleInfo(t),staleBadge=staleRenderable(stale)?`<span class="stale-badge" title="超过 ${stale.threshold} 小时线">卡 ${esc(staleShort(stale.hours))}</span>`:'';
  const cross=t.发起位&&t.发起位!==t.所属总监位?`<span class="cross-ticket">跨位单 · ${esc(t.发起位)}→${esc(t.所属总监位)}</span>`:'';
  const internal=t.非用户可感知?'<span class="internal-ticket">非用户可感知 · 内部工具</span>':'';
  const missingTaskbook=t.类型==="派单"&&!dispatchInitialPath(t)?'<span class="cross-ticket">缺任务书</span>':'';
  const transfers=(t.转交历史||[]).map(row=>`<li>${esc(row.从)}→${esc(row.到)} · ${esc(row.原因)} · ${esc((row.时间||'').replace('T',' ').slice(0,16))}</li>`).join('');
  /* 阻塞单网页上没有答复框(见 wantsDesignerAnswer 上面那段),所以 meta 行要替他把话说完:
     在等谁。放行的是所属总监位与总编排,转交过的单 transfer 已经把所属位改成接收位。 */
  const blockedWait=t.类型==='阻塞'&&t.状态==='待答'?` · 等 ${esc(t.所属总监位||DESK.总编排位)} 答`:'';
  return `<article class="ticket ${t.状态==='阻塞'?'blocked':''} ${t.状态==='关闭'?'closed':''} ${t.状态==='作废'?'voided':''}" data-ticket="${esc(t.编号)}"><div class="ticket-head"><span class="ticket-id">${esc(t.编号)} · ${esc(t.类型)}</span><span class="ticket-flags"><span class="ticket-state"${stateTitle(t)?` title="${esc(stateTitle(t))}"`:''}>${esc(stateLabel(t))}</span>${staleBadge}</span></div>${cross}${internal}${missingTaskbook}<h4>${esc(t.标题)}</h4>
  <div class="meta">任务档(总监定)：${esc(t.任务档||'待总监定')}${windowHintText(t)}${t.上下文预算!=null?` · 上下文 ${esc(t.上下文预算)} 行`:''} · 实际模型：${esc(t.实际模型||'待开窗写回')} · 经手：${esc(t.指派给||'未指派')} · 判卷：${esc(t.判卷人||'未填')} · 复检：${esc(t.复检人||'未填')}${shotMark(t)}${blockedWait}</div><div class="source">依据：${esc((t.真源指针||[]).join('；')||'未填')}</div><div class="consumer">上线后由：${esc(t.实机消费者||'未填')}</div>
  ${t.类型==="派单"?dispatchBox(t):''}
  ${t.判语?`<div class="verdict"><b>判语</b><div>${esc(t.判语)}</div></div>`:''}${(t.交付项||[]).length?`<div class="deliverables"><b>交付项</b>${(t.交付项||[]).map(row=>`<div>${esc(row)}</div>`).join('')}</div>`:''}${transfers?`<div class="transfer-history"><b>转交历史</b><ul>${transfers}</ul></div>`:''}
  ${images.length?`<div class="thumbs">${images.map(img=>`<span><img class="thumb" data-ticket-image="${esc(img.文件名)}" alt="${esc(img.来源标注)}"><small>${esc(img.来源标注)}</small></span>`).join('')}</div>`:''}${actionButtons(t)}</article>`;
}
/* 免独图要连原因一起显示：只显示「免独图」三个字，设计者看不出这张单凭什么免（T-000693）。 */
function shotMark(t){const value=String(t.实机图标记||"");if(!value)return"";if(value==="免独图"){const reason=String(t.免独图原因||"");if(!reason)return ` · ${esc(value)}`;const short=reason.length>24?reason.slice(0,24)+"…":reason;return ` · <span title="${esc(value)} · ${esc(reason)}">${esc(value)} · ${esc(short)}</span>`}return value==="待独图"?` · <span style="color:#ffb3ab;font-weight:700">${esc(value)}</span>`:` · ${esc(value)}`}
/* 开窗路径只认服务端工单字段。真源指针和浏览器本地缓存都不能代表另一台机器看到的真值。 */
function dispatchInitialPath(t){return String(t.任务书路径||"").trim();}
/* 宪法 v2.9 / D9-323:总监只定任务档(甲/乙/丙),模型与档位由设计者自由搭配。
   所以开窗指令第二行不再写员工登记的模型与档位——那等于把选择权写死在总监手里。
   任务档是下限不是指定:设计者拿甲档模型跑乙丙的活随意。
   dispatchToolName 保留不删:员工名旁边显示实际模型仍然有用,只是不再进开窗指令。 */
function dispatchToolName(t){for(const group of Object.values(app.data?.staff?.总监位||{})){const member=(group?.员工||[]).find(m=>m.员工名===t.指派给);if(member)return member["工具/窗类型"]||"<未指派>";}return "<未指派>";}
function modelRosterText(){return (app.data?.slots?.模型名册||[]).map(row=>`${row.任务档上限||'?'}档 · ${row.模型}${(row.可选档位||[]).length?` ${(row.可选档位||[]).join('/')}`:''}${row.状态&&row.状态!=="可用"?` · ${row.状态}`:''}`).join('\n')||"名册暂空，可自由填写实际模型与档位";}
/* 三行文案由服务端 TicketService.dispatch_instructions 生成；前端不再保存第二份模板。 */
function dispatchLineTexts(t){return Array.isArray(t.开窗指令)?t.开窗指令:[];}
/* D9-408 / 宪法 v2.18:总监给的开窗平台建议。真源只有一处——派单标题开头的【X】。
   这里**现算不读字段**:服务端的「建议窗口」是派生值,老服务端和旧回落快照上根本没有它,
   而标题在哪个版本上都有,所以照标题算既不会跟服务端漂,也不挑服务端版本。
   留空就整格不显示——「建议窗口 空」比不写还坏。开窗指令三行由服务端下发,且一个平台名都不带。 */
const windowPlatforms=()=>DESK.开窗平台||[];
/* 现造而不是模块加载时算一次:开窗平台名单来自服务端,而它要等 loadDeskConfig() 回来才到位。 */
const windowPrefixRe=()=>new RegExp(`^\\s*【(${windowPlatforms().join("|")})】`,"i");
function windowHint(t){const m=windowPrefixRe().exec(String(t?.标题||''));return m?m[1].toLowerCase():'';}
function windowHintText(t){const v=windowHint(t);return v?` · 建议窗口 ${esc(v)}`:'';}
function dispatchBox(t){
  const initial=dispatchInitialPath(t),opened=isOpened(t),lines=dispatchLineTexts(t);
  const placeholder=`${DESK.任务书目录}/${t.所属总监位||""}/任务书/${t.编号}_<主题>.md`;
  const readOnly=!writable();
  const hint=readOnly
    ?`<div class="copy-hint" data-copy-hint>当前是只读回落数据，不能补任务书路径；请打开服务模式后再填。</div>`
    :(initial?"":`<div class="copy-hint" data-copy-hint>这张单还没有任务书路径,总监填了才能开窗。</div>`);
  return `<div class="dispatch-box" data-dispatch="${esc(t.编号)}"><b>开窗指令</b>
  <label>任务书路径<div class="taskbook-field"><input type="text" data-dispatch-path="${esc(t.编号)}" value="${esc(initial)}" placeholder="${esc(placeholder)}"${readOnly?" disabled":""}><button type="button" data-save-taskbook="${esc(t.编号)}" data-cooldown-key="taskbook:${esc(t.编号)}"${readOnly?' data-readonly="1" disabled':""}>保存路径</button></div></label>
  <div class="dispatch-lines">${lines.map((row,i)=>`<div data-dispatch-line="${i+1}">${esc(row)}</div>`).join("")}</div>
  <div class="dispatch-actions"><button type="button" data-copy-dispatch="${esc(t.编号)}"${initial?"":" disabled"}>复制开窗指令</button><button type="button" class="opened-toggle${opened?" opened":""}" data-opened-toggle="${esc(t.编号)}">${opened?"已开窗 ✓":"已开窗"}</button></div>${hint}</div>`;
}
async function copyDispatchText(button,text){
  let ok=false;
  try{await navigator.clipboard.writeText(text);ok=true}catch{}
  if(!ok){const area=document.createElement("textarea");area.value=text;area.className="copy-fallback";document.body.appendChild(area);area.select();try{ok=document.execCommand("copy")}catch{}area.remove();}
  if(ok){const label=button.textContent;button.textContent="已复制";clearTimeout(button._timer);button._timer=setTimeout(()=>{button.textContent=label},2000)}
  else notify("复制没有成功，请手动选中那两行复制。");
}
function transferControls(t){if(!writable())return'';return`<div class="transfer-actions"><button type="button" data-transfer-target="${esc(DESK.总编排位)}" data-transfer-id="${esc(t.编号)}">转给${esc(DESK.总编排位)}</button><button type="button" data-transfer-target="${esc(DESK.复检位)}" data-transfer-id="${esc(t.编号)}">转给${esc(DESK.复检位)}</button><button type="button" data-transfer-target="__slot__" data-transfer-id="${esc(t.编号)}">转给指定总监</button><button type="button" data-transfer-target="${esc(DESK.拍板人)}" data-transfer-id="${esc(t.编号)}">转给${esc(DESK.拍板人)}</button></div><form class="transfer-panel" data-transfer-form="${esc(t.编号)}" hidden><select data-transfer-slot>${slotsOf().map(slot=>`<option>${esc(slot)}</option>`).join('')}</select><textarea data-transfer-reason required rows="2" placeholder="写清转交原因；Ctrl+Enter 直接转交"></textarea><button>确认转交</button></form>`}
function actionButtons(t) {
  if(!writable()) return '';
  const map={新建:['claim','block'],已认领:['submit','block'],待判:['pass','rework','block'],待复检:['merge','block'],已合并:['live','block'],实机复验过:t.实机图标记==='待独图'?['live','close','block']:['close','block'],返工:['claim','block'],阻塞:['unblock'],待答:[],已答:['close'],作废:[]};
  const labels={claim:'认领',submit:'交板',pass:'判卷通过',rework:'判退返工',merge:'复检并合并',live:'实机复验',close:'关闭',block:'标为阻塞',unblock:'解除阻塞'};
  const buttons=(map[t.状态]||[]).map(a=>`<button data-action="${a}" data-id="${esc(t.编号)}">${labels[a]}</button>`).join('');
  const attach=!['关闭','作废'].includes(t.状态)?`<div class="attach-box"><input type="text" data-uploader placeholder="上传人（员工窗名）" value="${esc(t.指派给||t.发起人||'')}"><select data-origin><option value="other" selected>其他</option><option value="world">${esc(origins().world)}</option><option value="isolated">隔离场景</option></select><input class="drop-input" type="file" accept="image/*" multiple data-attach="${esc(t.编号)}"><div class="drop-zone" tabindex="0" data-drop-ticket="${esc(t.编号)}"><b>拖图到这张工单</b><span>可一次多张；聚焦后 Ctrl+V 也可粘贴</span></div>
  <div class="web-upload-note">★网页上传<b>不核出处</b>:浏览器拿不到图旁边那个 <code>.出处.txt</code>,所以 2560×1440 那道闸只盖命令行。判卷人自己开原图核。</div></div>`:'';
  return `<div class="actions">${buttons}</div>${transferControls(t)}${attach}`;
}
function chatPanel(slot) {
  /* T-001313:全文只对当前这一位拉。还没到的时候要说「正在读取」——
     否则切位那一瞬间会显示「还没有对话」,看着像这一位真的没说过话。 */
  const loaded=Array.isArray(app.data?.threads?.[slot]);
  const rows=loaded?app.data.threads[slot]:[];
  const emptyText=loaded?'<div class="empty">还没有对话。</div>':'<div class="empty">正在读取这一位的对话…</div>';
  return `<aside class="panel"><h2>三方对话 · ${esc(slot)}</h2><div class="chat-list">${rows.length?rows.map(r=>`<div class="chat"><span class="who">${esc(r.发言人)}</span><time>${esc((r.时间||'').replace('T',' ').slice(0,16))}</time><div>${esc(r.文字)}</div>${r.引用工单号?`<small>引用 ${esc(r.引用工单号)}</small>`:''}</div>`).join(''):emptyText}</div>${writable()?`<form class="chat-form" data-chat-slot="${esc(slot)}"><label>发言人<select name="actor"><option>设计者</option><option>${esc(slot)}</option><option>总编排</option></select></label><label>要说的话<textarea name="text" placeholder="把问题或答复直接写清楚；可在这里 Ctrl+V 粘贴图片"></textarea></label><label>引用工单号（可不填）<input name="ref" placeholder="T-000001"></label><input class="drop-input" name="image" type="file" accept="image/*" multiple><div class="drop-zone" tabindex="0" data-drop-chat><b>拖图到对话</b><span>默认标为“其他”；支持多图和 Ctrl+V</span><div class="drop-preview"></div></div><button data-cooldown-key="say:${esc(slot)}">发送到本位对话线</button></form>`:'<p class="meta">离线只读，启动工单台服务后可发言。</p>'}</aside>`;
}

/* 派单七步(T-000043 R6 写死,「现在轮到谁」按此推导):
 1 新建   → 轮到 发起总监(还没派出去)
 2 已认领 → 未传达:设计者传达开窗指令;已传达:指派给的那位员工执行
 3 待判   → 轮到 <所属总监位> 判卷
 4 返工   → 轮到 设计者 重新传达开窗指令
 5 待复检 → 轮到 复检·合并与部署 复验
 6 已合并 → 轮到 复检·合并与部署 或 <所属总监位> 上服/实机
 7 实机复验过、关闭 → 走完了,不再显示
 待答类(拍板/疑问/需求):指派给设计者 → 轮到设计者;否则轮到 指派给 那一方 */
/* 派单七步流程条:当前一步高亮。设计者要一眼看出「走到哪了、下一步是谁」。 */
/* 设计者队列的终态集合:走到这三态就不再要设计者动手,并自动清掉 deskOpened/deskDone/deskReviewed 三键。
   T-000075:「作废」与「实机复验过 / 关闭」同等,建错的单一作废就该从队列里消失。 */
const TERMINAL_STATES = ["实机复验过","关闭","作废"];
const FLOW_STEPS = ["建单","开窗","执行","判卷","复检","上服","关闭"];
function flowIndex(t){
  if(["拍板","疑问","需求"].includes(t.类型))return -1;
  /* 新建但设计者已经开过窗:窗开了、员工在做,只是它还没跑 claim。
     再把箭头停在「建单」,看的人会以为自己没传达过。 */
  if(t.状态==="新建"&&isOpened(t))return 2;
  /* T-001053:内部单并线即到头,箭头停在「上服」会让件数条一直显示一批不需要上服的单。 */
  if(t.状态==="已合并"&&isTerminal(t))return 6;
  return {新建:0,返工:1,已认领:2,待判:3,待复检:4,已合并:5,实机复验过:6,关闭:6,阻塞:-1,作废:-1}[t.状态] ?? -1;
}
function flowStrip(t){
  const at=flowIndex(t);
  if(at<0)return '';
  return `<div class="flow-strip">${FLOW_STEPS.map((s,i)=>`<span class="flow-step${i===at?' now':''}${i<at?' past':''}">${esc(s)}</span>`).join('<i class="flow-arrow">→</i>')}</div>`;
}
/* 设计者这一步到底要做什么,写成一句人话,别让他自己从「轮到谁」里推。 */
function todoLine(t){
  const [who]=turnOf(t);
  if(t.状态==="新建"&&isOpened(t))return `你要做的:点【复制催办句】贴给 ${nameTag(t.指派给||"那个员工")} 的窗口——你已经传达过了,是它还没跑 claim 认领,单子才一直停在「新建」。`;
  if(t.状态==="返工")return `你要做的:把开窗指令重新贴给 ${nameTag(t.指派给||"原员工")} 的窗口;那个窗若已关,让 ${nameTag(t.所属总监位)} 重开一个编号发续单。`;
  if(t.状态==="已认领"&&!isOpened(t))return `你要做的:复制开窗指令,贴给新开的 ${nameTag(t.指派给||"员工")} 窗口。`;
  if(who===DESK.拍板人)return `你要做的:在下面直接答复。`;
  return `你要做的:点【复制催办句】,把那两行贴给 ${nameTag(who)} 的窗口。`;
}
function nameTag(name){return `<b class="name-tag">${esc(name)}</b>`}
function turnOf(t){
  if(["拍板","疑问","需求"].includes(t.类型))return [t.指派给===DESK.拍板人?DESK.拍板人:(t.指派给||"<未指派>"),"答复"];
  if(t.状态==="新建")return isOpened(t)
    ?[t.指派给||t.所属总监位||"<未指派>","认领并执行(它还没跑 claim)"]
    :[t.发起人||t.所属总监位||"<未指派>","还没派出去"];
  if(t.状态==="已认领")return isOpened(t)?[t.指派给||"<未指派>","执行"]:[DESK.拍板人,"传达开窗指令"];
  if(t.状态==="待判")return [t.所属总监位,"判卷"];
  if(t.状态==="返工")return [DESK.拍板人,"重新传达开窗指令"];
  if(t.状态==="待复检")return [DESK.复检位,"复验"];
  if(t.状态==="已合并")return [`复检·合并与部署 或 ${t.所属总监位}`,"上服/实机"];
  return [t.所属总监位,"处理"];
}
/* T-001053(落宪法 v2.20 ④):内部单并线即到头——它没有用户可见的产出,也就没有「真登录图」那一步。
   原来只把 关闭/作废/实机复验过/阻塞 当终态,于是内部单并线后仍按 24 小时老化,
   一直涌进「卡住了」段、也一直挂在「上服」那一格里(撞到过:
   T-000559 卡 38 小时、T-000628 卡 37 小时,两张都是已合并的内部单,没有人该动它们)。
   ★与服务端 service.py is_terminal 是同一条判据,改一处必须两处一起改。 */
function isTerminal(t){return nonStale().has(t.状态)||(t.状态==="已合并"&&!!t.非用户可感知)}
function staleInfo(t,now=Date.now()){
  if(isTerminal(t)||t.类型==="阻塞")return null;
  const threshold=t.状态==="新建"&&isOpened(t)?DESK.新建已开窗阈值:staleHours()[t.状态];
  if(threshold==null)return null;
  const entered=Date.parse(t.状态进入时间||t.最后更新时间);
  if(Number.isNaN(entered))return null;
  const elapsed=Math.max(0,now-entered);
  if(elapsed<=threshold*60*60*1000)return null;
  const hours=Math.floor(elapsed/(60*60*1000));
  /* T-001658:出口只回**有限数**。阈值或时长任一不是有限数就不算「卡住」——
     宁可少报一条告警,不许把 undefined 送上设计者的屏。 */
  if(!Number.isFinite(threshold)||!Number.isFinite(hours))return null;
  return {threshold,hours};
}
/* T-001658:渲染端唯一闸门——两个数都取得到才许画红条,取不到整条不渲染。
   历代模板直接插 stale.hours/stale.threshold,stale 只要是无键真值就把 undefined 上屏。 */
function staleRenderable(stale){return !!stale&&Number.isFinite(stale.threshold)&&Number.isFinite(stale.hours)}
function staleDuration(hours){return hours>48?`${Math.floor(hours/24)} 天`:`${hours} 小时`}
function staleShort(hours){return hours>48?`${Math.floor(hours/24)}天`:`${hours}h`}
/* 设计者要「一眼看出各阶段各有多少」。按派单七步归类计数,顺带把作废与非派单也点出来。
   数的是全项目,不是某一位——他关心的是整盘活走到哪了。 */
/* ★T-001322 补:前两格是**设计者的待办**,后面那些是**全项目的流程分布**——两个维度,
   同一张单会同时出现在两边,这是有意的,不是重复计数。
   撞到过:件数条写「3 建单」,而下面「要你传达的」列了 5 张,
   会让人以为哪一边数错了。其实一张「已认领、但还没点过已开窗」的单,
   按流程已经走到「执行」那一格,按待办仍然等他去传达——两边各自都对。
   缺的就是这两格(把他的待办直接摆出来)和每一格 title 里的那句口径。 */
const TODO_STAGES = {
  "等你开窗": "就是下面「要你传达的」那一段,数的是全项目。★与后面几格是两个维度:后面按派单七步分流程,一张「已认领但你还没传达」的单在那边算「执行」、在这边算「等你开窗」,同一张单两边都会出现。",
  "等你答": "就是下面「要你答的」那一段:指派到你名下的拍板/疑问/需求。末尾那个「待答」格数的是全部待答单,含不需要你答的。",
  "待复验": "已交板、还没复验过的单(D9-460 ①:交板即可复验,不必等总监判过)。归复检席,不用你动手——放这里只是让你看得见那条线堵没堵。",
  "可并": "判过 ∧ 复验过,两道都齐了,就等复检席按一下 merge。停在这一格太久说明并线卡住了。",
};
/* D9-460 ①:复验是挂在单上的一格独立结论,与判卷并行。服务端 is_verified 同口径,改一处两处一起改。
   内部单六项机器闸全绿时,服务端会把「复验」那一格的复验人写成「机器闸」——所以这里只读结论,不看是谁。 */
function isVerified(t){return String(t?.复验?.结论||"")==="过"}
function isJudgedForMerge(t){return t?.状态==="待复检"&&!!String(t?.判卷人||"").trim()}
function awaitingVerify(t){return t?.类型==="派单"&&["待判","待复检"].includes(t?.状态)&&!isVerified(t)}
function readyToMerge(t){return t?.类型==="派单"&&isJudgedForMerge(t)&&isVerified(t)}
const FLOW_STAGE_HINT = "按派单七步分的**全项目**流程分布,不是你的待办;要你动手的看最前面两格。";
function stageGroups(){
  const items=app.data?.items||[];
  const groups=new Map();
  /* 待办两格放最前,设计者一进这一页先看见的就该是「要我动手几个」。 */
  groups.set("等你开窗",items.filter(wantsDispatch));
  groups.set("等你答",items.filter(wantsDesignerAnswer));
  /* D9-460 ④:复检那条线的两格。它们不是设计者的待办(归复检席),所以排在待办两格之后、
     流程分布之前——他要看的是「那边堵没堵」,不是要自己动手。 */
  groups.set("待复验",items.filter(awaitingVerify));
  groups.set("可并",items.filter(readyToMerge));
  for(const name of FLOW_STEPS)groups.set(name,[]);
  groups.set("作废",[]); groups.set("待答",[]);
  for(const t of items){
    if(t.类型!=="派单"){ if(t.状态==="待答")groups.get("待答").push(t); continue; }
    if(t.状态==="作废"){ groups.get("作废").push(t); continue; }
    const at=flowIndex(t);
    if(at>=0)groups.get(FLOW_STEPS[at]).push(t);
  }
  return groups;
}
function stageBar(){
  const groups=stageGroups();
  const cells=[];
  for(const [name,list] of groups){
    if(!list.length)continue;
    const todo=name in TODO_STAGES;
    const muted=(name==="作废"||name==="待答")?" muted":"";
    const on=app.stageFilter===name?" on":"";
    const title=todo?TODO_STAGES[name]:FLOW_STAGE_HINT;
    cells.push(`<button type="button" class="stage-cell${todo?" todo":""}${muted}${on}" data-stage="${esc(name)}" title="${esc(title)}"><b>${list.length}</b> ${esc(name)}</button>`);
  }
  if(!cells.length)return '';
  return `<div class="stage-bar">${cells.join("")}</div>`;
}
/* 设计者要的是筛选而不是弹窗:「点击按钮后,下面自动筛选,这样我也知道哪些能直接操作」。
   所以点一格就把该阶段的单就地列在下面,并且用 ticketCard 渲染——那张卡带着认领/交板/判卷/
   合并/实机/关闭这些动作键,他能当场动手,不像弹窗只能看。再点一次同一格取消筛选。 */
function stageFilterPanel(){
  const name=app.stageFilter;
  if(!name)return '';
  const list=stageGroups().get(name)||[];
  const bySlot=new Map();
  for(const t of list){ const k=t.所属总监位||"未定"; if(!bySlot.has(k))bySlot.set(k,[]); bySlot.get(k).push(t); }
  const blocks=[...bySlot.entries()].sort((a,b)=>b[1].length-a[1].length).map(([slot,rows])=>
    `<div class="onepage-group"><h3>${esc(slot)} · ${rows.length}</h3><div class="state-grid">${rows.map(ticketCard).join("")}</div></div>`).join("");
  return `<section class="panel stage-panel"><h2>筛选:${esc(name)} · ${list.length} 张 <button type="button" class="stage-clear" data-stage-clear>清除筛选</button></h2>
  ${blocks||'<div class="empty">这一档现在没有单。</div>'}</section>`;
}

/* 「要你传达的」按工具筛。做法照抄上面那条件数条:点一格就筛,再点同一格取消,数字写在格子上。
   ★不另造组件、不新起一套 class——stage-cell/stage-bar 的样式和交互都直接沿用。
   标题不带【X】的单归「不限」那一格,不会因为没填标签就从队列里消失。
   只剩一格能点时整条不显示:一个格子的筛选条没有意义,只会占掉设计者一行。 */
function windowGroups(list){
  const groups=new Map(windowPlatforms().map(name=>[name,[]]));
  groups.set("不限",[]);
  for(const t of list)groups.get(windowHint(t)||"不限").push(t);
  return groups;
}
function windowBar(list){
  const cells=[];
  for(const [name,rows] of windowGroups(list)){
    if(!rows.length)continue;
    const muted=name==="不限"?" muted":"";
    const on=app.windowFilter===name?" on":"";
    cells.push(`<button type="button" class="stage-cell${muted}${on}" data-window-filter="${esc(name)}"><b>${rows.length}</b> ${esc(name)}</button>`);
  }
  return cells.length>1?`<div class="stage-bar">${cells.join("")}</div>`:'';
}
/* 派单进不了「要你传达的」的两种正当原因:没指派员工、没填任务书路径。
   闸本身是对的(T-000037/T-000075:未指派或任务书不存在的单让设计者开了窗,员工进去才发现无事可做,
   烧掉一个窗口 6 分半)。错的是它一声不吭——件数条写着「14 建单」,
   在队列里却一张也找不到,看的人只能以为工单台坏了。
   所以这里把被挡住的单列出来,写清缺什么、该找谁补,并且明说不用他动手。 */
/* ★这里列的原因必须和 wantsDispatch 的闸一一对应,少一条就有单两边都不出现、彻底隐身。
   就这么坑过一次:T-000308 指派有了、任务书路径也有了,
   却因为「任务书校验=待回核」被 wantsDispatch 挡下,而当时这个函数只查指派与路径,
   于是它既不在「要你传达的」、也不在「还没准备好开窗」——内容位喊他开窗,他怎么也找不到。
   以后 wantsDispatch 再加闸,这里必须同步加一条理由。 */
/* ★T-001256 ③:「任务书还没回核」从这张清单里**撤走**了——它已经不再是一道闸,
   单子照常进「要你传达的」,只在卡片上挂一行黄字提醒补核。上面那条「一一对应」的规矩仍然成立:
   wantsDispatch 现在只剩指派与路径两条,这里就只列这两条。 */
function notReadyReasons(t){
  const 缺=[];
  if(!STAFF_NAME.test(String(t.指派给||"")))缺.push("没指派员工");
  if(!dispatchInitialPath(t))缺.push("没填任务书路径");
  return 缺;
}
function notReadyList(){
  return (app.data?.items||[]).filter(t=>
    t.类型==="派单" && ["新建","已认领","返工"].includes(t.状态) && !isOpened(t)
  ).map(t=>[t,notReadyReasons(t)]).filter(([,缺])=>缺.length);
}
function notReadyCard([t,缺]){
  return `<article class="queue-item onepage-card not-ready" data-ticket="${esc(t.编号)}"><div class="meta">${esc(t.编号)} · ${nameTag(t.所属总监位)} · ${esc(t.状态)}</div><h3>${esc(t.标题)}</h3>
  <div class="not-ready-line">还不能开窗:<b>${esc(缺.join(" + "))}</b>。要 ${nameTag(t.所属总监位)} 补齐之后才会进「要你传达的」——<b>不用你动手</b>,需要的话点【复制「查收工单」】去催那一位。
  ${缺.includes("任务书还没回核")?`<div class="not-ready-how">「任务书还没回核」是怎么回事:建单时用了 {ticket} 占位符,拿到单号后客户端要回头确认那个 md 真的落盘了才算数,这一步没跑完。让 ${esc(t.所属总监位)} 在本机把路径原样再 set 一次(<code>set ${esc(t.编号)} --taskbook &lt;同一条路径&gt;</code>)就会当场回核。</div>`:''}</div>
  <div class="dispatch-actions"><button type="button" data-copy-wake="${esc(t.所属总监位)}">复制「查收工单」</button><span class="btn-note">(贴给 ${esc(t.所属总监位)} 的窗口,让它把缺的补上)</span></div></article>`;
}
function renderDesigner() {
  (app.data.items||[]).forEach(t=>{if(isTerminal(t))["deskOpened","deskDone","deskReviewed"].forEach(key=>lsDel(`${key}:${t.编号}`))});
  /* ★「要你传达的」优先于「卡住了」:等设计者动手的单绝不能被折叠段吞掉。
     撞到过:T-000262 卡到 23.9 小时,再过几分钟越过 24 小时线就会被挪进「卡住了」,
     而那一段当天刚改成默认收起——内容位喊他开窗,他在队列里怎么也找不到。
     「卡住了」的定位是「多半在等别人动手」,一张等他开窗的单不属于那里。 */
  const wake=wakeList();
  /* 件数条要按**全部**待传达的单来数,筛完再数就只剩自己那一格,别的工具从此点不回来。 */
  const dispatchAll=(app.data.items||[]).filter(wantsDispatch);
  const dispatch=app.windowFilter?dispatchAll.filter(t=>(windowHint(t)||"不限")===app.windowFilter):dispatchAll;
  const bySlot={};
  /* ★这里必须用 dispatchAll:按工具筛掉的单只是暂时不显示,不是「不等设计者动手」了。
     拿筛后的集合去排「卡住了」,一按筛选就会有单同时出现在两段里。 */
  const dispatchIds=new Set(dispatchAll.map(t=>t.编号));
  const stalled=(app.data.items||[]).map(t=>[t,staleInfo(t)]).filter(([t,info])=>info&&!dispatchIds.has(t.编号));
  const notReady=notReadyList();
  const staleIds=new Set(stalled.map(([t])=>t.编号));
  dispatch.forEach(t=>((bySlot[t.所属总监位]??=[]).push(t)));
  const waiting=(app.data.items||[]).filter(t=>isOpened(t)&&!isTerminal(t)&&!staleIds.has(t.编号));
  const answered=(app.data.items||[]).filter(t=>wantsDesignerAnswer(t)&&!staleIds.has(t.编号));
  const empty=!(wake.length||stalled.length||dispatchAll.length||waiting.length||answered.length||notReady.length);
  $("#app").innerHTML=`<p class="page-hint">这一页只列要你亲自动手的事;工单全貌在「总监位」页。</p>
  ${stageBar()}
  ${stageFilterPanel()}
  ${empty?'<section class="panel"><div class="empty">现在没有要你动手的事。</div></section>':''}
  ${answered.length?`<section class="panel"><h2>要你答的 · ${answered.length} 张</h2>${answered.map(answerCard).join("")}</section>`:''}
  ${dispatchAll.length?`<section class="panel"><h2>要你传达的 · ${dispatchAll.length} 张${app.windowFilter?` · 只看 ${esc(app.windowFilter)} · ${dispatch.length} 张`:''}</h2>${windowBar(dispatchAll)}${dispatch.length?Object.entries(bySlot).map(([slot,list])=>`<div class="onepage-group"><h3>${esc(slot)}</h3>${list.map(dispatchCard).join("")}</div>`).join(""):'<div class="empty">这个工具下现在没有单;再点一次那一格就能看回全部。</div>'}</section>`:''}
  ${wake.length?`<section class="panel"><h2>要你去唤醒的窗口</h2>${wake.map(wakeCard).join("")}</section>`:''}
  ${stalled.length?`<section class="panel stale-panel"><details class="stale-box"${lsGet("deskStaleOpen")==="1"?" open":""} data-stale-box><summary><b>卡住了 · ${stalled.length}</b><span class="btn-note">(点开查看;这一段多半在等别人动手,所以排在你的待办后面)</span></summary>${stalled.map(([t,info])=>waitingCard(t,info)).join("")}</details></section>`:''}
  ${waiting.length?`<section class="panel"><h2>已传达·等回音</h2>${waiting.map(waitingCard).join("")}</section>`:''}
  ${notReady.length?`<section class="panel"><details class="not-ready-box"${lsGet("deskNotReadyOpen")==="1"?" open":""} data-not-ready-box><summary><b>还没准备好开窗 · ${notReady.length}</b><span class="btn-note">(没指派 / 缺任务书 / 任务书没回核,等对应总监补齐;不用你动手,点开可看是谁欠什么)</span></summary>${notReady.map(notReadyCard).join("")}</details></section>`:''}`;
}
function wakeCard(row){
  return `<article class="queue-item wake-row" data-wake="${esc(row.slot)}"><div class="meta">${nameTag(row.slot)} · 未读 ${row.count} 条 · 最近一条来自 ${esc(row.speaker)} · ${esc(row.preview)}…</div><div class="dispatch-actions"><button type="button" data-copy-wake="${esc(row.slot)}">复制「查收工单」</button><button type="button" data-wake-full="${esc(row.slot)}">看全文</button><button type="button" data-woke-toggle="${esc(row.slot)}">已唤醒</button></div></article>`;
}
/* 唤醒行只显示前 40 字,设计者看不到后面写了什么,没法判断这一位到底急不急。
   点开列出该位全部未读的原文(谁说的、什么时候、整段),不截断。 */
async function showWakeFull(slot){
  await ensureThread(slot);   /* 唤醒段可以点任意一位,那一位的全文未必拉过(T-001313) */
  const rows=(app.data?.threads?.[slot]||[]).filter(row=>row.发言人!==slot&&!(row.已读标记||[]).includes(slot));
  const blocks=rows.map(row=>`<div class="wake-full-row"><div class="meta">${esc(row.发言人||'')} · ${esc(String(row.时间||'').replace('T',' ').slice(0,16))}${row.引用工单号?` · 引用 ${esc(row.引用工单号)}`:''}</div><div class="wake-full-text">${esc(row.文字||'')}</div></div>`).join("");
  showModal(`<h2>${esc(slot)} · 未读 ${rows.length} 条</h2>${blocks||'<p>这一位现在没有未读。</p>'}`);
}
function dispatchCard(t){
  /* 留在这一段但已经越线的单,红字照旧要出——只是不再把它挪走。 */
  const stale=staleInfo(t);
  return `<article class="queue-item onepage-card" data-ticket="${esc(t.编号)}"><div class="meta">${esc(t.编号)} · ${nameTag(t.所属总监位)} · ${esc(t.任务档||'待总监定')}档${windowHintText(t)}</div><h3>${esc(t.标题)}</h3>
  ${flowStrip(t)}
  ${staleRenderable(stale)?`<div class="stale-line">这张已经等你 ${esc(staleDuration(stale.hours))}了,超过 ${stale.threshold} 小时线——贴出去它才会动。</div>`:''}
  ${taskbookPending(t)?`<div class="taskbook-pending-line">任务书「待回核」:建单那台机器当时核不到那个路径而已,<b>不挡开窗、照贴</b>。顺手让 ${nameTag(t.所属总监位)} 在本机原样再 set 一次(<code>set ${esc(t.编号)} --taskbook &lt;同一条路径&gt;</code>)就转「已核存在」。</div>`:''}
  ${reworkNote(t)}
  <div class="todo-line">${todoLine(t)}</div>
  ${dispatchBox(t)}${transferControls(t)}</article>`;
}
/* 判退回来的单要说清「为什么回来」。拍板人不会实时盯着工单——
   他看到一张单回到「要你传达的」,分不清这是新单还是被打回来的,更不知道要跟员工交代改什么。
   ★返工不回建单流程:单号不变、返工次数累计,回到的只是「等设计者重新传达」这一步。
     退回新建会丢掉返工历史与合格率统计,所以这句话直接写在提示里,免得再被误解。 */
function reworkNote(t){
  if(t.状态!=="返工")return '';
  const rows=t.返工原因列表||[];
  const last=rows.length?rows[rows.length-1]:null;
  const times=Number(t.返工次数||rows.length||1);
  const judge=last?String(last.判卷人||""):"";
  const reason=last?String(last.原因||""):"";
  const short=reason.length>80?reason.slice(0,80)+"…":reason;
  const more=reason.length>80?`<button type="button" class="link-button" data-rework-full="${esc(t.编号)}">看全文</button>`:'';
  /* T-000891:返工态是**换书不换号**的那一档——任务书可以就地改（可改态白名单里本来就有返工），
     改完员工 claim 一次即回「已认领」。解阻塞现在也一律落这里,而单子会被阻塞多半正说明书要改。
     所以把「换书后再认领」连同当前任务书路径摆在眼前:总监一眼知道能改、改的是哪一条。
     ★判语全文不在这里拼——员工窗跑 receipt 看服务端生成的那一份,两处各拼必然漂。 */
  const taskbook=dispatchInitialPath(t);
  return `<div class="rework-line">这张是<b>第 ${times} 次判退</b>回来的${judge?`(判卷:${esc(judge)})`:''},不是新单——单号不变、返工次数累计,要做的是把开窗指令重新贴一次。已开窗标记已清,请重新开窗。
  ${reason?`<div class="rework-reason">判退原因:${esc(short)}${more}</div>`:''}
  <div class="rework-taskbook"><b>换书后再认领</b>:任务书要改就现在改(<code>set ${esc(t.编号)} --taskbook &lt;新路径&gt;</code>),单号不变;员工 claim 一次即回「已认领」。当前任务书:${taskbook?`<code>${esc(taskbook)}</code>`:'<span class="cross-ticket">还没填任务书路径</span>'}</div></div>`;
}
/* 窗死在「已认领」是另一种病:单子已被点过【已开窗】,戳记有效,于是它躺在「已传达·等回音」里,
   哪怕 25 小时没有任何事件,队列也不提示要重开(T-000154/155 各挂 25.7 小时)。
   工具其实一直都在(T-000156 保留的折叠开窗词里有【撤销已开窗】与【复制开窗指令】),
   缺的是指路牌——所以这里把它端到眼前,并成一键。 */
function needsReopen(t,stale){
  /* 不看 isOpened:点过【重新开窗】之后标记就清了,但单子仍卡在这一段(它还没动),
     按钮与展开的开窗词必须留在原地,否则他点完一次就再也复制不到第二次。
     ★除了越线的,还有一种不看时长也要给重开入口:点过【已开窗】、状态却还停在「新建」——
     那扇窗从来没跑过 claim,等于根本没起来。T-000262 的窗被喊开过,
     却在队列里找不到,就是这种(早先按旧版开过一次,标记还在)。 */
  if(t.类型!=="派单"||!dispatchInitialPath(t))return false;
  if(t.状态==="新建"&&isOpened(t))return true;
  return !!stale && ["新建","已认领","返工"].includes(t.状态);
}
function reopenBox(t,stale){
  if(!needsReopen(t,stale))return '';
  const who=t.指派给||"原员工";
  const 头=staleRenderable(stale)
    ?`这张停在「${t.状态}」已 ${staleDuration(stale.hours)}没有任何事件,多半是那扇窗已经关了。`
    :`你点过【已开窗】,但它还停在「新建」——那扇窗从来没跑过 claim,等于没起来。`;
  return `<div class="reopen-line">${esc(头)}
  点【重新开窗】把指令重贴给 ${nameTag(who)} 的新窗口;它进去后会先跑 claim,从断点接着做。</div>
  <div class="dispatch-actions"><button type="button" class="reopen-button" data-reopen-window="${esc(t.编号)}" data-cooldown-key="reopen:${esc(t.编号)}" data-cooldown-label="重新开窗">重新开窗</button><span class="btn-note">(一步做两件:清掉开窗标记 + 复制开窗指令。贴进新窗口后回来点【已开窗】;在那扇窗真的动起来之前,这张单会一直留在「卡住了」)</span></div>`;
}
function waitingCard(t,stale=null){
  const [who,doing]=turnOf(t),done=lsGet(`deskDone:${t.编号}`)==="1",reviewed=lsGet(`deskReviewed:${t.编号}`)==="1";
  const doneMismatch=done&&!["待判","待复检","已合并","实机复验过","关闭","作废"].includes(t.状态);
  const reviewedMismatch=reviewed&&!["已合并","实机复验过","关闭","作废"].includes(t.状态);
  return `<article class="queue-item onepage-card${doneMismatch||reviewedMismatch?" mismatch":""}" data-ticket="${esc(t.编号)}"><div class="meta">${esc(t.编号)} · ${esc(t.状态)}</div><h3>${esc(t.标题)}</h3>
  ${flowStrip(t)}
  ${staleRenderable(stale)?`<div class="stale-line">卡在「${esc(t.状态)}」已 ${esc(staleDuration(stale.hours))},超过 ${stale.threshold} 小时线;现在轮到 ${esc(who)}</div>`:''}
  ${reworkNote(t)}
  ${reopenBox(t,stale)}
  <div class="turn-line">现在轮到:${nameTag(who)} ${esc(doing)}</div>
  <div class="todo-line">${todoLine(t)}</div>
  ${doneMismatch?`<div class="mismatch-line">员工说做完了,但工单还停在「${esc(t.状态)}」——多半是它没跑 submit,请总监核。</div>`:''}
  ${reviewedMismatch?`<div class="mismatch-line">复检说验完了,但工单还停在「${esc(t.状态)}」——复检记录没落库,请总监核。</div>`:''}
  <div class="dispatch-actions"><button type="button" data-copy-remind="${esc(t.编号)}">复制催办句</button><span class="btn-note">(工位完成任务后,把这句发给下一个要动手的窗口)</span><button type="button" class="check-toggle${done?" on":""}" data-done-toggle="${esc(t.编号)}">${done?"已完成 ✓":"已完成"}</button><button type="button" class="check-toggle${reviewed?" on":""}" data-reviewed-toggle="${esc(t.编号)}">${reviewed?"已复检 ✓":"已复检"}</button></div>
  ${dispatchRecall(t,needsReopen(t,stale))}
  ${transferControls(t)}</article>`;
}
/* 误触【已开窗】后这张单就落到「等回音」段,开窗词随之从眼前消失,想重贴一次找不回来。
   这里原样留一份,默认收起(设计者要的是兜底,不是又多一块占地方的东西)。 */
function dispatchRecall(t,forceOpen=false){
  if(t.类型!=="派单")return '';
  const path=dispatchInitialPath(t);
  if(!path)return '';
  /* 越过停滞线的单默认展开:这时候他要的就是那几行指令,不该再让他先点一次折叠块。 */
  const lines=dispatchLineTexts(t),open=forceOpen||lsGet(`deskRecallOpen:${t.编号}`)==="1";
  return `<details class="recall-box"${open?" open":""} data-recall="${esc(t.编号)}"><summary>开窗词(点开可再复制一次)</summary>
  <div class="dispatch-lines" data-recall-lines="${esc(t.编号)}">${lines.map(row=>`<div>${esc(row)}</div>`).join("")}</div>
  <div class="dispatch-actions"><button type="button" data-copy-recall="${esc(t.编号)}">复制开窗指令</button><button type="button" data-undo-opened="${esc(t.编号)}">撤销已开窗</button></div></details>`;
}
function answerCard(t){
  return `<article class="queue-item onepage-card" data-ticket="${esc(t.编号)}"><div class="meta">${esc(t.编号)} · ${nameTag(t.所属总监位)} · ${esc(t.类型)}</div><h3>${esc(t.标题)}</h3><p>${esc(t.正文||'')}</p>${writable()?`<div class="two-actions"><textarea data-answer="${esc(t.编号)}" rows="4" placeholder="在这里写清答复；长文会自动换行"></textarea><button data-do-answer="${esc(t.编号)}" data-cooldown-key="answer:${esc(t.编号)}">答复</button></div>${transferControls(t)}`:'<p class="meta">当前是只读回落数据。</p>'}</article>`;
}
function digestLines() {
  const now=Date.now(), day=24*3600e3, rows=app.data.items||[], lines=[];
  const recent=rows.filter(t=>now-Date.parse(t.发起时间)<=day), stale=rows.map(t=>[t,staleInfo(t,now)]).filter(([,info])=>info), blocked=rows.filter(t=>t.状态==='阻塞'||(t.类型==='阻塞'&&t.状态!=='关闭')), waiting=rows.filter(t=>['需求','总工单'].includes(t.类型)&&t.状态==='待答');
  const transfers=rows.flatMap(t=>(t.转交历史||[]).filter(row=>now-Date.parse(row.时间)<=day).map(row=>({ticket:t,row}))),cross=rows.filter(t=>t.发起位&&t.发起位!==t.所属总监位);
  const stateCounts=Object.keys(staleHours()).map(state=>[state,stale.filter(([t])=>t.状态===state).length]).filter(([,count])=>count).map(([state,count])=>`${state} ${count}`);
  const staleSummary=`停滞 ${stale.length} 张${stateCounts.length?`(${stateCounts.join(' · ')})`:''}`;
  lines.push(`最近24小时新单 ${recent.length} · ${staleSummary} · 阻塞 ${blocked.length} · 待答需求/总工单 ${waiting.length}`);
  stale.forEach(([t,info])=>{const [who]=turnOf(t);lines.push(`[停滞] ${t.编号} · ${t.标题} · ${t.状态} 卡了 ${staleDuration(info.hours)} · 轮到 ${who}`)});
  lines.push(`今日转交 ${transfers.length}`);transfers.forEach(({ticket,row})=>lines.push(`[转交] ${ticket.编号} · ${row.从}→${row.到} · ${row.原因}`));
  [["新单",recent],["阻塞",blocked],["待答",waiting]].forEach(([label,list])=>list.forEach(t=>lines.push(`[${label}] ${t.编号} · ${t.标题} · ${t.状态} · ${t.所属总监位}`)));
  cross.forEach(t=>lines.push(`[跨位单] ${t.编号} · ${t.发起位}→${t.所属总监位} · 抄送总编排`));
  slotsOf().forEach(slot=>{const count=unreadFor(slot,DESK.总编排位);if(count)lines.push(`[未读对话] ${slot} · ${count} 条`)}); lines.push('各模型合格率：模型 | 交板 | 判过 | 判退 | 合格率 | 状态'); modelStats().forEach(r=>lines.push(`[模型] ${r.模型} | ${r.交板数} | ${r.判过} | ${r.判退} | ${r.合格率} | ${r.状态}`)); return lines.slice(0,Math.max(60,1+stale.length));
}
function renderDigest(){ const lines=digestLines(); $("#app").innerHTML=`<section class="panel"><h2>总编排日览</h2>${lines.map(x=>`<div class="digest-line">${esc(x)}</div>`).join('')}</section>`; }
function renderSearch(){ $("#app").innerHTML=`<section class="panel"><h2>全文搜索</h2><div style="display:flex;gap:8px;margin:8px 0 14px"><input id="searchBox" autofocus placeholder="搜标题、编号、依据、上线后的加载者或对话文字" style="flex:1"><button type="button" data-search-run>搜索</button></div><div id="searchResults" class="state-grid"></div></section>`; }
/* ★这里必须 bindCommon() 再 hydrateImages(),顺序与 render() 一致。
   搜索页是唯一一处自己往 #searchResults 里塞 HTML、不走 render() 的地方,
   早期它只调了 hydrateImages:缩略图有 src 能显示,但从来没绑过 onclick——
   设计者点图「没反应」就是这么来的。被这一行漏掉害死的不止图片:
   data-action(认领/交板/判卷/合并/实机/关闭)、data-transfer-target(转交)、
   data-attach(上传图)、data-do-answer(答复)、data-staff(员工履历)全都绑在 bindCommon 里,
   也就是说搜索页上那些按钮一个都按不动。以后再加「自己塞 HTML」的渲染路径,两个都要调。 */
/* 搜索索引(本单提速的本体):每批数据只在刷新处算一次小写全文,搜索时只做 includes。
   挂在 WeakMap 上、按对象本身做键——绝不写进会被保存的工单对象,也不会跟着任何请求回传服务端;
   下一轮 rebuildSearchIndex() 用 delete-后-再算的方式保证索引内容永远来自原始字段。
   数据一变就重算:readApi/readDisk/离线兜底都汇入 refresh(),refresh 末尾重算一遍。 */
let searchTexts=new WeakMap();
/* ★T-001304:索引改成**用到才建**。
   原来每次 refresh 都把 1292 张单 + 5470 行对话逐个 JSON.stringify 一遍(十几 MB 的字符串),
   而绝大多数刷新根本不搜索——这一趟纯属白烧。现在只把索引作废,
   等真的搜第一次时再建(建完缓存,下次搜索直接用)。 */
let searchIndexReady=false;
function rebuildSearchIndex(){ searchTexts=new WeakMap(); searchIndexReady=false; }
function ensureSearchIndex(){
  if(searchIndexReady)return;
  (app.data?.items||[]).forEach(t=>searchTexts.set(t,JSON.stringify(t).toLowerCase()));
  Object.values(app.data?.threads||{}).forEach(rows=>rows.forEach(r=>searchTexts.set(r,JSON.stringify(r).toLowerCase())));
  searchIndexReady=true;
}
/* ★T-001322:搜单改走服务端。列表那一趟(/api/tickets)为了体积已经不发正文/答复/接线证据/备注,
   本地索引只能索引到「发下来的那部分」——继续在本地搜,搜正文里的词会一条都搜不到,
   而且页面还一副「确实没有」的样子。服务端在全文上匹配、只把命中的几张回全文,又准又不占体积。
   ★离线(file://)模式没有服务端,但离线包 build_bundle 走的是 list_tickets(全文),
     所以那条路上的本地索引照旧是全的,直接回落即可。
   ★API 模式下真连不上时**必须把话说明白**:宁可让人知道这一次搜得不全,
     也不能安静地少给几条结果——那正是最坏的一种失败。 */
async function searchTickets(q){
  if(API_MODE){
    try{ return await api(`/api/tickets?q=${encodeURIComponent(q)}`); }
    catch(error){
      notify(`搜索没能连上服务端(${error.message});这一次只在已加载的列表里找,正文、答复、接线证据里的词搜不到。刷新页面后再试一次。`);
    }
  }
  ensureSearchIndex();
  return (app.data.items||[]).filter(t=>(searchTexts.get(t)||'').includes(q));
}
async function doSearch(value){ await ensureAllThreads(); ensureSearchIndex(); const q=value.trim().toLowerCase(), results=q?await searchTickets(q):[]; const chats=[]; if(q)slotsOf().forEach(slot=>(app.data.threads?.[slot]||[]).forEach(r=>{if((searchTexts.get(r)||'').includes(q))chats.push({slot,row:r})})); $("#searchResults").innerHTML=[...results.map(ticketCard),...chats.map(x=>`<div class="chat"><b>${esc(x.slot)}</b> · ${esc(x.row.发言人)}<div>${esc(x.row.文字)}</div></div>`)].join('')||(q?'<div class="empty">没有找到。</div>':''); bindCommon(); hydrateImages(); }
function renderNew(){ $("#app").innerHTML=`<section class="panel"><h2>设计者直接建单</h2><p class="meta">这里只收需求、拍板和疑问；任务档与上下文预算由总监在总监位页面建立派单时决定。</p>${writable()?`<form id="newForm" class="form"><label>类型<select name="type"><option>需求</option><option>拍板</option><option>疑问</option></select></label><label>所属总监位<select name="slot">${slotsOf().map(s=>`<option>${esc(s)}</option>`).join('')}</select></label><label>标题<input name="title" required placeholder="一句话说清要处理什么"></label><label>正文<textarea name="body" required placeholder="用户会看到什么、希望改成什么；可在这里 Ctrl+V 粘贴图片"></textarea></label><label>依据位置（至少一条）<input name="source" required placeholder="例如 DECISIONS.md 的标题或 data 文件行"></label><label>上线后由谁加载<input name="consumer" placeholder="需求、拍板和疑问可暂不填"></label><label>图片来源<select name="image_origin"><option value="other" selected>其他</option><option value="world">${esc(origins().world)}</option><option value="isolated">隔离场景</option></select></label><input class="drop-input" name="images" type="file" accept="image/*" multiple><div class="drop-zone" tabindex="0" data-drop-new><b>拖图到新单</b><span>支持多图和 Ctrl+V；提交后立即入单</span><div class="drop-preview"></div></div><button>建立工单</button></form>`:'<div class="empty">离线只读（file:// 回落包）。要建单请跑 ticket.py serve 后用 http 打开。</div>'}</section>`; }

function bindCommon() {
  document.querySelectorAll('[data-slot]').forEach(b=>b.onclick=async()=>{app.slot=b.dataset.slot;render();await ensureThread(app.slot);render()});
  document.querySelectorAll('[data-staff]').forEach(b=>b.onclick=()=>showHistory(b.dataset.staff));
  /* ★绑在 bindCommon 里,不是在 renderSlots 里:搜索页那条自己塞 HTML 的路径只调 bindCommon,
     漏在别处绑过的按钮在那一页一个都按不动(「点图没反应」就是这么来的)。 */
  document.querySelectorAll('[data-copy-charter]').forEach(b=>b.onclick=()=>copyDispatchText(b,b.dataset.copyCharter));
  document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>runAction(b.dataset.id,b.dataset.action));
  document.querySelectorAll('[data-attach]').forEach(input=>input.onchange=async()=>{const card=input.closest('.ticket');await attachFiles(input.dataset.attach,[...input.files],card.querySelector('[data-origin]').value,card.querySelector('[data-uploader]').value)});
  document.querySelectorAll('[data-ticket-image]').forEach(img=>img.onclick=()=>showImage(img.src,img.alt,img.dataset.ticketImage));
  document.querySelector('[data-chat-slot]')?.addEventListener('submit',sendChat);
  document.querySelectorAll('[data-do-answer]').forEach(b=>b.onclick=()=>answerTicket(b.dataset.doAnswer));
  document.querySelectorAll('[data-transfer-target]').forEach(button=>button.onclick=()=>openTransfer(button));
  document.querySelectorAll('[data-transfer-form]').forEach(form=>form.addEventListener('submit',submitTransfer));
  /* 转交原因改成多行后,回车要留给换行,改用 Ctrl+Enter 提交 */
  document.querySelectorAll('[data-transfer-reason]').forEach(input=>input.addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();input.form.requestSubmit()}}));
  /* 搜索改成按钮/回车触发(逐键 input 会对八百多张单全量序列化+整页重绘,打字巨卡)。
     输入过程一个字都不搜;点「搜索」或框里按回车(中文输入法选词的 Enter 不算)才跑 doSearch。
     绑定必须用 onkeydown 赋值而不是 addEventListener:doSearch 自己会调 bindCommon(),
     addEventListener 每搜一次就叠一层监听,搜 N 次后按一下回车会跑 N 遍搜索。 */
  document.querySelectorAll('[data-search-run]').forEach(b=>b.onclick=()=>doSearch($('#searchBox')?.value||''));
  const searchBox=$('#searchBox'); if(searchBox)searchBox.onkeydown=e=>{if(e.key==='Enter'&&!e.isComposing){e.preventDefault();doSearch(e.target.value)}};
  $('#newForm')?.addEventListener('submit',createFromPage);
  /* 【强制重取】:丢掉本地缓存再刷一次。增量刷新自己会核对总数、也会听服务端的「整份重取」标,
     所以平时用不到它——但任何缓存都该有一条一键回到干净状态的退路(T-001308)。 */
  const forceButton=$("#forceReload");
  if(forceButton)forceButton.onclick=async()=>{dropCache();notify("已丢掉本地缓存,正在整份重取…");await refresh();notify("已整份重取完成。",true)};
  $('#newForm select[name="type"]')?.addEventListener('change',event=>syncDecisionTemplate(event.target));
  document.querySelector('[data-dispatch-slot]')?.addEventListener('submit',createDispatchFromPage);
  document.querySelectorAll('details.recall-box').forEach(box=>box.addEventListener('toggle',()=>{const id=box.dataset.recall;box.open?lsSet(`deskRecallOpen:${id}`,"1"):lsDel(`deskRecallOpen:${id}`)}));
  /* 「卡住了」默认收起:上线停滞告警后一次冒出 53 张,其中 34 张是等首次部署的已合并单,
     排在最上面会把自己要动手的三段整个压到屏外,待办工单反而看不见。
     记住他的展开选择,不要每次渲染都强制收回去。 */
  document.querySelectorAll('details.stale-box').forEach(box=>box.addEventListener('toggle',()=>{box.open?lsSet("deskStaleOpen","1"):lsDel("deskStaleOpen")}));
  document.querySelectorAll('details.not-ready-box').forEach(box=>box.addEventListener('toggle',()=>{box.open?lsSet("deskNotReadyOpen","1"):lsDel("deskNotReadyOpen")}));
  document.querySelectorAll('details.pref-group').forEach(group=>group.addEventListener('toggle',()=>{group.open?lsSet(`${group.dataset.pref}:${app.slot}`,"1"):lsDel(`${group.dataset.pref}:${app.slot}`)}));
  applyCooldowns();
  bindDropZones();
}

function openTransfer(button){const article=button.closest('[data-ticket]'),panel=article.querySelector('[data-transfer-form]'),slot=panel.querySelector('[data-transfer-slot]'),reason=panel.querySelector('[data-transfer-reason]');panel.hidden=false;panel.dataset.target=button.dataset.transferTarget;slot.hidden=button.dataset.transferTarget!=='__slot__';reason.value='';reason.focus()}
async function submitTransfer(event){event.preventDefault();const cooldownKey=`transfer:${event.target.dataset.transferForm}`;markCooldown(formButton(event.target),cooldownKey);const form=event.target,target=form.dataset.target==='__slot__'?form.querySelector('[data-transfer-slot]').value:form.dataset.target,reason=form.querySelector('[data-transfer-reason]').value.trim(),id=form.dataset.transferForm,actor=app.view==='designer'?DESK.拍板人:app.view==='slots'?app.slot:DESK.总编排位;try{await api('/api/action',{method:'POST',body:JSON.stringify({op:'transfer',ticket:id,to:target,reason,by:actor})});await refresh();notify(`${id} 已转给 ${target}。`,true)}catch(error){clearCooldown(cooldownKey);notify(`已拦下：${error.message}`)}}
function syncDecisionTemplate(select){const body=select.form.querySelector('textarea[name="body"]');if(select.value==='拍板'&&!body.value.trim())body.value=DECISION_TEMPLATE;else if(select.value!=='拍板'&&body.value===DECISION_TEMPLATE)body.value=''}

function validImages(files){const rows=[...files].filter(Boolean),images=rows.filter(file=>String(file.type).startsWith('image/'));if(rows.length&&!images.length)throw Error('这里只收图片文件；请拖入 PNG、JPEG、WebP 等图片。');if(images.length!==rows.length)notify(`已忽略 ${rows.length-images.length} 个非图片文件。`);return images}
function previewStaged(form){const zone=form.querySelector('.drop-preview');if(!zone)return;(form._previewUrls||[]).forEach(URL.revokeObjectURL);form._previewUrls=[];zone.innerHTML=(form._pendingFiles||[]).map(file=>{const url=URL.createObjectURL(file);form._previewUrls.push(url);return`<span><img src="${url}" alt="${esc(file.name)}"><small>${esc(file.name)}</small></span>`}).join('')}
function stageFiles(form,files){try{const images=validImages(files);if(!images.length)return;form._pendingFiles=[...(form._pendingFiles||[]),...images];previewStaged(form);notify(`已暂存 ${images.length} 张图，提交表单时一起上传。`,true)}catch(error){notify(`已拦下：${error.message}`)}}
function clipboardImages(event){return[...(event.clipboardData?.items||[])].filter(item=>item.kind==='file'&&item.type.startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean)}
function bindDropZones(){document.querySelectorAll('.drop-zone').forEach(zone=>{const form=zone.closest('form'),input=zone.parentElement.querySelector('.drop-input')||form?.querySelector('.drop-input');zone.onclick=()=>input?.click();zone.ondragover=event=>{event.preventDefault();zone.classList.add('drag-over')};zone.ondragleave=()=>zone.classList.remove('drag-over');zone.ondrop=async event=>{event.preventDefault();zone.classList.remove('drag-over');try{const files=validImages(event.dataTransfer.files);if(zone.dataset.dropTicket){const card=zone.closest('.ticket');await attachFiles(zone.dataset.dropTicket,files,card.querySelector('[data-origin]').value,card.querySelector('[data-uploader]').value)}else stageFiles(form,files)}catch(error){notify(`已拦下：${error.message}`)}};zone.onpaste=event=>{const files=clipboardImages(event);if(files.length){event.preventDefault();if(zone.dataset.dropTicket){const card=zone.closest('.ticket');attachFiles(zone.dataset.dropTicket,files,card.querySelector('[data-origin]').value,card.querySelector('[data-uploader]').value)}else stageFiles(form,files)}};if(input&&!zone.dataset.dropTicket)input.onchange=()=>stageFiles(form,input.files)});document.querySelectorAll('#newForm textarea,[data-chat-slot] textarea').forEach(field=>field.onpaste=event=>{const files=clipboardImages(event);if(files.length){event.preventDefault();stageFiles(field.closest('form'),files)}})}

async function saveTicket(ticket,event,actor,detail) {
  if(!(await permission(app.handle,true))) throw new Error("没有工单目录写入权限。");
  ticket.最后更新时间=nowText(); ticket.事件序号=Number(ticket.事件序号||0)+1;
  const items=await app.handle.getDirectoryHandle("items",{create:true}); await writeJson(items,`${ticket.编号}.json`,ticket);
  const old=await readFile(app.handle,"log.jsonl",""); const row={时间:ticket.最后更新时间,工单号:ticket.编号,事件,发言人:actor,状态:ticket.状态,说明:detail,事件序号:ticket.事件序号};
  await writeFile(app.handle,"log.jsonl",old+JSON.stringify(row)+"\n");
}
function activeStaff(ticket,name){ const good=/^.+-\d{2,3}$/.test(name)&&name.startsWith(ticket.所属总监位+'-')&&staffFor(ticket.所属总监位).some(m=>m.员工名===name&&m.状态==='在岗'); if(!good)throw new Error(`员工必须是 ${ticket.所属总监位} 名册里格式正确的在岗员工。`); }
function worldImages(ticket){return (ticket.接线证据?.图片列表||[]).filter(i=>i.来源标注===origins().world)}
async function runAction(id,action){
  const t=(app.data.items||[]).find(x=>x.编号===id); if(!t)return;
  let cooldownKey="";
  try{
    let actor=DESK.总编排位,payload={op:action,ticket:id};
    if(action==='claim'){actor=prompt("请输入认领员工窗名：",t.指派给||"")||"";payload={op:'claim',ticket:id,by:actor}}
    else if(action==='submit'){const handoff=handoffPrompt();payload=t.非用户可感知?{op:'submit',ticket:id,evidence:prompt("可补一句验证说明（可不填）：","")||"",verify_command:prompt("请输入实际运行的验证命令：","")||"",raw_output:prompt("请粘贴验证命令的原样输出：","")||"",handoff}:{op:'submit',ticket:id,evidence:prompt("请用一句话说明接线证据：","")||"",handoff}}
    else if(action==='pass'||action==='rework'){actor=prompt(`${strictNotice(t)}\n请输入判卷人：`,"")||"";const verdict=prompt(action==='pass'?"请输入判语；必须含“用户怎么打开它”或“设计者怎么打开它”一句（用户打不开的活写后一句）：":"请输入判语：","")||"";payload={op:'judge',ticket:id,by:actor,passed:action==='pass',verdict,reason:action==='rework'?(prompt("请写清返工原因：","")||""):"",strike_handoff:strikePrompt(t)}}
    else if(action==='merge'){actor=prompt("请输入复检人：","")||"";payload={op:'merge',ticket:id,by:actor}}
    else if(action==='live'){const shot=prompt('请输入实机图标记：同图 或 独图',t.实机图标记==='待独图'?'独图':'同图')||'';if(t.非用户可感知){payload={op:'live',ticket:id,by:prompt('请输入复验人：',DESK.总编排位)||DESK.总编排位,shot}}else{const input=document.createElement('input');input.type='file';input.accept='image/*';input.onchange=async()=>{if(input.files[0])await attachImage(id,input.files[0],'world',prompt('请输入实机复验人：',DESK.总编排位)||DESK.总编排位,shot)};input.click();return}}
    else if(action==='close'){actor=prompt("请输入关闭人：",DESK.总编排位)||DESK.总编排位;payload={op:'close',ticket:id,by:actor}}
    else if(action==='block'){payload={op:'block',ticket:id,by:DESK.总编排位,reason:prompt("请写清阻塞原因：","")||""}}
    else if(action==='unblock'){payload={op:'unblock',ticket:id,by:DESK.总编排位}}
    /* 冷却卡在真正发请求这一刻:前面几步全是阻塞式 prompt,在那之前起冷却的话,取消 prompt 也会把按钮白灰 15 秒。 */
    cooldownKey=`action:${action}:${id}`;
    markCooldown(document.querySelector(`[data-action="${action}"][data-id="${id}"]`),cooldownKey);
    /* 「自动退役提示」与服务端 CLI 走同一条路子(T-001081):它挂在返回值上、不进盘。
       名册被动过就得当场说出来——只回一句「已关闭」,谁也不知道那个窗已经被收了。 */
    const result=await api('/api/action',{method:'POST',body:JSON.stringify(payload)});await refresh();const changed=app.data.items.find(x=>x.编号===id);notify(`${id} 已变为“${changed?.状态||'已更新'}”。${result?.流程提示?`\n${result.流程提示}`:''}${result?.自动退役提示?`\n${result.自动退役提示}`:''}`,true);
  }catch(error){if(cooldownKey)clearCooldown(cooldownKey);notify(`已拦下：${error.message}`)}
}

async function compress(file){const bitmap=await createImageBitmap(file),scale=Math.min(1,1280/Math.max(bitmap.width,bitmap.height)),canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale));const ctx=canvas.getContext('2d');ctx.drawImage(bitmap,0,0,canvas.width,canvas.height);const pixels=ctx.getImageData(0,0,canvas.width,canvas.height).data;let alpha=false;for(let i=3;i<pixels.length;i+=4)if(pixels[i]<255){alpha=true;break}const type=alpha?'image/webp':'image/jpeg',ext=alpha?'webp':'jpg';for(const q of [.88,.8,.72,.64,.56,.48,.4,.32,.24,.16]){const blob=await new Promise(r=>canvas.toBlob(r,type,q));if(blob&&blob.size<=200*1024)return{blob,ext,width:canvas.width,height:canvas.height}}throw Error("图片压缩后仍超过 200KB，请先裁掉无关区域再试。")}
async function blobBase64(blob){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',',2)[1]);reader.onerror=()=>reject(new Error('浏览器读取图片失败。'));reader.readAsDataURL(blob)})}
async function uploadTicketFiles(id,files,origin,uploader,liveShot=""){if(!uploader.trim())throw Error("上传人不能为空。");const images=validImages(files);if(!images.length)throw Error('没有找到可上传的图片。');const done=[];for(const file of images){try{const result=await compress(file),uploaded=await api('/api/upload',{method:'POST',body:JSON.stringify({ticket:id,filename:file.name,origin,by:uploader,base64:await blobBase64(result.blob)})});done.push({record:uploaded.图片,result})}catch(error){throw Error(`${file.name} 上传失败：${error.message}`)}}if(liveShot){const last=done.at(-1).record;await api('/api/action',{method:'POST',body:JSON.stringify({op:'live',ticket:id,filename:last.文件名,by:uploader,shot:liveShot})})}return done}
async function attachFiles(id,files,origin,uploader,live=false){try{const done=await uploadTicketFiles(id,files,origin,uploader,live);await refresh();const size=done.reduce((sum,row)=>sum+row.result.blob.size,0);notify(`已收下 ${done.length} 张图，共 ${Math.round(size/1024)}KB，缩略图已更新。`,true)}catch(error){notify(`已拦下：${error.message}`)}}
async function attachImage(id,file,origin,uploader,live){return attachFiles(id,[file],origin,uploader,live)}

async function nextId(){const counter=await readJson(app.handle,'counter.json',{最后编号:0});counter.最后编号=Number(counter.最后编号||0)+1;await writeJson(app.handle,'counter.json',counter);return`T-${String(counter.最后编号).padStart(6,'0')}`}
function ticketShape(id,type,slot,title,body,source,consumer,tier='乙',context=null){const stamp=nowText();return{编号:id,类型:type,所属总监位:slot,标题,发起人:DESK.拍板人,发起时间:stamp,最后更新时间:stamp,状态进入时间:stamp,状态:type==='派单'?'新建':'待答',任务档:tier,上下文预算:context,指派给:['拍板','疑问'].includes(type)?DESK.拍板人:DESK.总编排位,判卷人:'',判语:'',复检人:'',真源指针:source?[source]:[],实机消费者:consumer||'',非用户可感知:false,实机图标记:'',免独图原因:'',接线证据:{文字:'',验证命令:'',原样输出:'',图片列表:[]},关联op号:'',关联素材登记:'',返工次数:0,返工原因列表:[],阻塞原因:'',阻塞前状态:'',备注:'',正文:body,答复:'',图片列表:[],事件序号:0,已开窗:null}}
async function createFromPage(event){event.preventDefault();const cooldownKey='create:ask';markCooldown(formButton(event.target),cooldownKey);try{const form=event.target,f=new FormData(form),ticket=await api('/api/action',{method:'POST',body:JSON.stringify({op:'ask',type:f.get('type'),slot:f.get('slot'),title:String(f.get('title')).trim(),body:String(f.get('body')).trim(),source:[String(f.get('source')).trim()],consumer:String(f.get('consumer')).trim(),by:DESK.拍板人})}),files=form._pendingFiles||[];if(files.length)await uploadTicketFiles(ticket.编号,files,String(f.get('image_origin')||'other'),DESK.拍板人);await refresh();notify(`已建立 ${ticket.编号}，任务档待总监定${files.length?`，并附 ${files.length} 张图`:''}。`,true)}catch(error){clearCooldown(cooldownKey);notify(`已拦下：${error.message}`)}}
async function createDispatchFromPage(event){event.preventDefault();const cooldownKey=`create:dispatch:${event.target.dataset.dispatchSlot}`;markCooldown(formButton(event.target),cooldownKey);try{const f=new FormData(event.target),raw=String(f.get('context')).trim(),deliverables=String(f.get('deliverables')).split(/\r?\n/).map(row=>row.trim()).filter(Boolean),ticket=await api('/api/action',{method:'POST',body:JSON.stringify({op:'new',slot:event.target.dataset.dispatchSlot,title:String(f.get('title')).trim(),source:[String(f.get('source')).trim()],consumer:String(f.get('consumer')).trim(),deliverables,internal:f.get('internal')==='on',assign:String(f.get('assign')),notes:String(f.get('notes')).trim(),tier:String(f.get('tier')),context_lines:raw===''?null:Number(raw),by:event.target.dataset.dispatchSlot})});await refresh();notify(`总监派单 ${ticket.编号} 已建立。`,true)}catch(error){clearCooldown(cooldownKey);notify(`已拦下：${error.message}`)}}
/* 署名要跟服务端 answer() 的闸对上:需求只认总编排;其余(拍板/疑问)认设计者。
   阻塞就地 return、不发请求:服务端放行的是所属总监位与总编排,网页替谁署名都是假记录
   (T-000683 定的口径)。这里只留一句人话,不给它走网络。 */
async function answerTicket(id){const cooldownKey=`answer:${id}`;startCooldown(cooldownKey);try{const t=app.data.items.find(x=>x.编号===id);if(t&&t.类型==='阻塞'){clearCooldown(cooldownKey);notify(`已拦下：${id} 是阻塞单，网页不代答。请让 ${t.所属总监位||DESK.总编排位} 在命令行答：ticket.py answer ${id} "<答复>" --by "${t.所属总监位||DESK.总编排位}"。`);return}const value=document.querySelector(`[data-answer="${id}"]`).value.trim(),actor=t.类型==='需求'?DESK.总编排位:DESK.拍板人;await api('/api/action',{method:'POST',body:JSON.stringify({op:'answer',ticket:id,answer:value,by:actor})});await refresh();notify(`${id} 已答复。`,true)}catch(error){clearCooldown(cooldownKey);notify(`已拦下：${error.message}`)}}

/* 交板留「给下一窗」(T-001073 R3):只写底数与坑,不写流水账。
   固定工位的单会被 memory export 收进工位记忆;非固定工位也能填,只是没人导出。 */
function handoffPrompt(){return prompt("留给下一窗（可不填）：只写底数与坑，一行一条——这条链的真源在哪、哪个数别信、下一窗从哪起手。不要流水账。","")||""}
/* 判卷人划掉写错的行:★是划掉不是删除,原文保留并标上判卷人与时间。
   这一节是空的就别弹框问——没有行可划,问了也只能空着。 */
function strikePrompt(ticket){const rows=(ticket.留给下一窗||{}).行||[];if(!rows.length)return "";
  return prompt(`执行方留给下一窗这几行；写错的填行号（逗号分隔），没有就留空：\n${rows.map((r,i)=>`${i+1}. ${r.文字}${r.划掉判卷人?`（已被 ${r.划掉判卷人} 划掉）`:''}`).join('\n')}`,"")||""}
function strictNotice(ticket){const member=staffFor(ticket.所属总监位).find(m=>m.员工名===ticket.指派给),first=member&&(member.经手工单号列表||[]).length<=1;return first||['乙','丙'].includes(ticket.任务档||'乙')?'首检从严:逐行核,自己重跑验证命令':'判卷检查：'}
async function recordWebRework(ticket){const member=staffFor(ticket.所属总监位).find(m=>m.员工名===ticket.指派给);if(!member)return;const model=String(ticket.实际模型||member['工具/窗类型']||'未知').toLowerCase();if(model==='待定')return;const staff=app.data.staff,score=(staff.模型记分||={})[model]||((staff.模型记分)[model]={合计:0});score[ticket.所属总监位]=Number(score[ticket.所属总监位]||0)+1;score.合计=Number(score.合计||0)+1;const limits=app.data.slots.停用阈值||{同位:3,全项目:5},bans=staff.模型停用||={全项目:[],按位:{}};(bans.按位[ticket.所属总监位]||=[]);if(score[ticket.所属总监位]>=limits.同位&&!bans.按位[ticket.所属总监位].includes(model))bans.按位[ticket.所属总监位].push(model);if(score.合计>=limits.全项目&&!bans.全项目.includes(model))bans.全项目.push(model);await writeJson(app.handle,'staff.json',staff)}
function modelStats(){if(app.data.modelStats?.length)return app.data.modelStats;const people={};Object.entries(app.data.staff?.总监位||{}).forEach(([slot,g])=>(g.员工||[]).forEach(m=>people[m.员工名]={slot,model:m['工具/窗类型']}));const stats={};(app.data.log||[]).forEach(e=>{const t=(app.data.items||[]).find(x=>x.编号===e.工单号),p=t&&people[t.指派给],model=String(e.实际模型||t?.实际模型||p?.model||'').toLowerCase();if(!model||model==='待定')return;const r=stats[model]||=( {交板数:0,判过:0,判退:0} );if(e.事件==='submit')r.交板数++;if(e.事件==='judge-pass')r.判过++;if(e.事件==='judge-rework')r.判退++});const bans=app.data.staff?.模型停用||{全项目:[],按位:{}};return Object.entries(stats).map(([模型,r])=>{const n=r.判过+r.判退;return{模型,...r,合格率:n?`${(r.判过*100/n).toFixed(1)}%`:'—',状态:bans.全项目.includes(模型)?'全项目停用':Object.values(bans.按位||{}).some(v=>v.includes(模型))?'本位停用':'可用'}})}
/* 答复与发送对话线都要等服务器往返,一次点击可能卡十秒以上,
   没有反馈就会忍不住再点,而重复提交会真发出两条。
   点下去立刻置灰并倒数 15 秒;失败则马上解禁,好让人重试。
   冷却状态存在 Map 里而不是按钮上——发送成功会 refresh() 重渲染,按钮元素本身会被换掉。 */
const COOLDOWN_SECONDS = 15;
const cooldowns = new Map();
function startCooldown(key){ cooldowns.set(key, Date.now() + COOLDOWN_SECONDS * 1000); applyCooldowns(); }
function clearCooldown(key){ cooldowns.delete(key); applyCooldowns(); }
function applyCooldowns(){
  document.querySelectorAll('[data-cooldown-key]').forEach(button=>{
    const key = button.dataset.cooldownKey;
    if(button.dataset.cooldownLabel === undefined) button.dataset.cooldownLabel = button.textContent;
    if(button._cooldownTimer){ clearInterval(button._cooldownTimer); button._cooldownTimer = null; }
    if(button.dataset.readonly==="1"){button.disabled=true;return;}
    const tick = () => {
      const until = cooldowns.get(key);
      const left = until ? Math.ceil((until - Date.now()) / 1000) : 0;
      if(left <= 0){
        cooldowns.delete(key);
        button.disabled = false;
        button.textContent = button.dataset.cooldownLabel;
        if(button._cooldownTimer){ clearInterval(button._cooldownTimer); button._cooldownTimer = null; }
        return;
      }
      button.disabled = true;
      button.textContent = `已发送 ${left}`;
    };
    if(cooldowns.has(key)){ tick(); button._cooldownTimer = setInterval(tick, 250); }
    else { button.disabled = false; button.textContent = button.dataset.cooldownLabel; }
  });
}
/* T-000177:防重复点击铺到全部写操作。
   凡是会往服务器写的按钮,点下去都立刻置灰倒数,失败立刻解禁。
   表单型(转交/建派单/新单)取它自己的 submit 按钮;动作键(认领/交板/判卷…)就是按钮本身。 */
function formButton(form){ return form.querySelector('button[type=submit]') || form.querySelector('button'); }
function markCooldown(button, key){
  if(!button) return;
  button.dataset.cooldownKey = key;
  startCooldown(key);
}
async function sendChat(event){event.preventDefault();const cooldownKey=`say:${event.target.dataset.chatSlot}`;startCooldown(cooldownKey);try{const form=event.target,f=new FormData(form),slot=form.dataset.chatSlot,actor=String(f.get('actor')),text=String(f.get('text')).trim(),ref=String(f.get('ref')).trim().toUpperCase(),files=form._pendingFiles||[],pictures=[];for(const file of files){try{const c=await compress(file),uploaded=await api('/api/upload',{method:'POST',body:JSON.stringify({slot,filename:file.name,by:actor,base64:await blobBase64(c.blob)})});pictures.push(uploaded.图片)}catch(error){throw Error(`${file.name} 上传失败：${error.message}`)}}await api('/api/say',{method:'POST',body:JSON.stringify({slot,by:actor,text,ref,images:pictures})});await refresh();notify(`对话已写入本位${pictures.length?`，附 ${pictures.length} 张图`:''}。`,true)}catch(error){clearCooldown(cooldownKey);notify(`已拦下：${error.message}`)}}

function showHistory(name){const member=staffFor(app.slot).find(m=>m.员工名===name),tickets=(member?.经手工单号列表||[]).map(id=>app.data.items.find(t=>t.编号===id)).filter(Boolean);showModal(`<h2>${esc(name)}</h2><p>${esc(member?.["工具/窗类型"]||'')} · ${esc(member?.状态||'')}</p>${tickets.length?`<table class="table"><tr><th>工单</th><th>标题</th><th>状态</th></tr>${tickets.map(t=>`<tr><td>${esc(t.编号)}</td><td>${esc(t.标题)}</td><td>${esc(t.状态)}</td></tr>`).join('')}</table>`:'<p>还没有经手工单。</p>'}`)}
/* 图片打不开时以前是一片空白,什么都不说——只报一句「图片无法打开」,
   而库里与磁盘上的字节其实都在、逐字节一致。哑失败比报错难查十倍:
   谁也不知道是没登录、会话过期、还是图真的没了。所以这里永远给两样:
   一句人话,和一条「在新标签打开」的退路(新标签会带上同一份会话 cookie,能直接看出是 401 还是 404)。 */
function showImage(src,alt,name=""){
  const label=name||alt||"这张图";
  showModal(`<h2>${esc(alt||label)}</h2>
  <div class="image-box"><img src="${esc(src)}" alt="${esc(alt)}" data-big-image onerror="this.hidden=true;this.parentNode.querySelector('.image-fallback').hidden=false">
  <div class="image-fallback" hidden>这张图没能加载出来:<b>${esc(label)}</b><br>
  多半是登录会话过期(刷新页面重登一次就好),其次才是图真的不在服务器上。
  点下面这条能直接看到服务器的原始回应——401 就是没登录,404 才是图丢了。</div>
  <div class="image-actions"><a href="${esc(src)}" target="_blank" rel="noopener">在新标签打开原图</a></div></div>`);
}
function showModal(html){const modal=$('#modal');modal.querySelector('.modal-body').innerHTML=html;modal.showModal()}
async function imageUrl(name){return API_MODE?`/api/image/${encodeURIComponent(name)}`:''}
async function hydrateImages(){
  for(const img of document.querySelectorAll('[data-ticket-image]')){
    const name=img.dataset.ticketImage,url=await imageUrl(name);
    if(!url){const label=document.createElement('span');label.className='thumb-label';label.textContent=name;img.replaceWith(label);continue}
    /* 缩略图加载失败也要看得见:以前只留一个空白框,点开还是空白,
       只能说「图片无法打开」而说不出是哪一步坏了(T-000322)。 */
    img.onerror=()=>{img.classList.add("thumb-broken");img.title=`${name} 加载失败——多半是登录会话过期,刷新页面重登一次;点开可看服务器原始回应`};
    img.src=url;
  }
}

document.querySelectorAll('#mainNav button').forEach(b=>b.onclick=()=>{app.view=b.dataset.view;render()});
$('#modal .modal-close').onclick=()=>$('#modal').close();
document.addEventListener('click',async event=>{
  const saveTaskbook=event.target?.closest?.('[data-save-taskbook]');
  if(saveTaskbook){
    const id=saveTaskbook.dataset.saveTaskbook,ticket=(app.data?.items||[]).find(x=>x.编号===id);
    const input=saveTaskbook.closest('[data-dispatch]')?.querySelector('[data-dispatch-path]');
    if(!ticket||!input)return;
    const cooldownKey=`taskbook:${id}`;startCooldown(cooldownKey);
    try{
      await api('/api/action',{method:'POST',body:JSON.stringify({op:'set',ticket:id,taskbook:input.value.trim(),by:ticket.所属总监位})});
      await refresh();notify(`${id} 的任务书路径已写回服务端。`,true);
    }catch(error){clearCooldown(cooldownKey);notify(`已拦下：${error.message}`)}
    return;
  }
  const copy=event.target?.closest?.('[data-copy-dispatch]');
  if(copy){const box=copy.closest('[data-dispatch]');await copyDispatchText(copy,[...box.querySelectorAll('[data-dispatch-line]')].map(line=>line.textContent).join('\n'));return}
  const remind=event.target?.closest?.('[data-copy-remind]');
  if(remind){
    const t=(app.data?.items||[]).find(x=>x.编号===remind.dataset.copyRemind);
    if(t){
      const [who]=turnOf(t);
      /* 单子停在「新建」而设计者已开过窗:该催的是员工窗,而且要点名让它先认领,
         否则它做完了单子还是停在新建,谁也看不出来。 */
      const text=t.状态==="已认领"
        ?`先核通道:python ${DESK.命令行路径} receipt ${t.编号}
${t.非用户可感知
  ?`交板:python ${DESK.命令行路径} submit ${t.编号} --verify-command "<你跑的验证命令>" --raw-output "<原样粘贴输出>"`
  :`先附图:python ${DESK.命令行路径} attach ${t.编号} <${DESK.实机来源.world}图路径> --origin world --by ${t.指派给||"<指派给>"}
再交板:python ${DESK.命令行路径} submit ${t.编号} --evidence "<用户怎么打开它>"`}
交付项要在提交端本机真实存在,少一个都交不了板;交完只回一行『已进入工单 ${t.编号} · 标题 · 状态』。`
        :(t.状态==="新建"&&isOpened(t))
          ?`查收工单
(贴给「${t.指派给||who}」的窗口;它需要先跑 claim ${t.编号} 认领这张单,再继续)`
          :`查收工单
(贴给「${who}」的窗口)`;
      await copyDispatchText(remind,text);
    }
    return}
  const copyWake=event.target?.closest?.('[data-copy-wake]');
  if(copyWake){await copyDispatchText(copyWake,`查收工单\n(贴给「${copyWake.dataset.copyWake}」的窗口)`);return}
  const woke=event.target?.closest?.('[data-woke-toggle]');
  if(woke){const slot=woke.dataset.wokeToggle,latest=slotLatestUnread(slot);if(latest)lsSet(`deskWoke:${slot}`,String(latest.时间||""));render();refreshBadges();return}
  const opened=event.target?.closest?.('[data-opened-toggle]');
  if(opened){
    const id=opened.dataset.openedToggle,ticket=(app.data?.items||[]).find(x=>x.编号===id);
    const next=!isOpened(ticket||{编号:id});
    /* 二次确认:这一下会把单子从「要你传达的」挪走,误触一次就得回头找开窗词。 */
    if(next&&!confirm(`确认已经把 ${id} 的开窗指令贴进新窗口了吗?

确认后这张单会移到「已传达·等回音」,开窗词仍可在那里点开重取。`))return;
    if(next){
      const before=ticket?.实际模型;
      const current=String(ticket?.实际模型||dispatchToolName(ticket)||"");
      const chosen=prompt(`请选择本次实际模型与档位（名册仅供对照，允许自由填写）：\n\n${modelRosterText()}`,current==="待定"?"":current);
      if(chosen===null)return;
      /* ★不要在这里 await。曾出现「点开窗之后越来越卡」:
         原来这一下要先等写请求回来,再 await refresh() 重拉 /api/slots + /api/tickets(409 张单的整包)
         + 13 个 /api/inbox,一共 15 个请求,然后整页重绘——他就干等着。
         改成:先就地把这一张单改好并重绘(他立刻看到卡片挪走),写请求丢进后台队列;
         成功了安静收下,失败了把本机改动退回去并大声报错。 */
      const model=String(chosen).trim();
      lsSet(`deskOpened:${id}`,reworkStamp(ticket));
      if(ticket)ticket.实际模型=model;           // 就地打补丁,不重拉 409 张单
      render();
      queueWrite({
        label:`${id} 登记已开窗`,
        run:()=>api('/api/action',{method:'POST',body:JSON.stringify({op:'open-window',ticket:id,by:DESK.拍板人,actual_model:model})}).then(result=>{
          const saved=result?.工单,index=(app.data.items||[]).findIndex(row=>row.编号===id);
          if(saved&&index>=0){app.data.items[index]=saved;render()}
          return result;
        }),
        onFail:()=>{lsDel(`deskOpened:${id}`);if(ticket)ticket.实际模型=before;render()},
      });
      return;
    }
    lsDel(`deskOpened:${id}`);
    if(app.view==="designer"){render();return}
    opened.classList.toggle('opened',next);opened.textContent=next?"已开窗 ✓":"已开窗";refreshBadges();
  }
  const wakeFull=event.target?.closest?.('[data-wake-full]');
  if(wakeFull){ showWakeFull(wakeFull.dataset.wakeFull); return}
  if(event.target?.closest?.('[data-stage-clear]')){ app.stageFilter=""; render(); return}
  const stage=event.target?.closest?.('[data-stage]');
  if(stage){ app.stageFilter = (app.stageFilter===stage.dataset.stage) ? "" : stage.dataset.stage; render(); return}
  /* 按工具筛「要你传达的」:再点同一格取消,跟阶段件数条一个手感。 */
  const windowCell=event.target?.closest?.('[data-window-filter]');
  if(windowCell){ app.windowFilter = (app.windowFilter===windowCell.dataset.windowFilter) ? "" : windowCell.dataset.windowFilter; render(); return}
  const recall=event.target?.closest?.('[data-copy-recall]');
  if(recall){
    const box=document.querySelector(`[data-recall-lines="${recall.dataset.copyRecall}"]`);
    if(box)await copyDispatchText(recall,[...box.querySelectorAll('div')].map(d=>d.textContent).join('\n'));
    return}
  const undo=event.target?.closest?.('[data-undo-opened]');
  if(undo){
    const id=undo.dataset.undoOpened;
    if(!confirm(`把 ${id} 退回「要你传达的」?

用于误触了【已开窗】、其实还没贴出去的情况。`))return;
    ["deskOpened","deskDone","deskReviewed"].forEach(key=>lsDel(`${key}:${id}`));
    render();return}
  const reopen=event.target?.closest?.('[data-reopen-window]');
  if(reopen){
    const id=reopen.dataset.reopenWindow,ticket=(app.data?.items||[]).find(x=>x.编号===id);
    if(!ticket)return;
    /* 一步做两件:清掉本机开窗标记(单子立刻回到「要你传达的」)+ 把开窗指令复制走。
       ★清标记是写动作,照 T-000177 挂 15 秒倒计时防连点——设计者每次点要等 10 秒,连点会重复清。 */
    startCooldown(`reopen:${id}`);
    ["deskOpened","deskDone","deskReviewed"].forEach(key=>lsDel(`${key}:${id}`));
    await copyDispatchText(reopen,dispatchLineTexts(ticket).join(String.fromCharCode(10)));
    notify(`${id} 的开窗标记已清掉,开窗指令已复制。贴进新窗口后回来点【已开窗】;它还卡着,所以仍留在「卡住了」段。`,true);
    render();return}
  const reworkFull=event.target?.closest?.('[data-rework-full]');
  if(reworkFull){
    const t2=(app.data?.items||[]).find(x=>x.编号===reworkFull.dataset.reworkFull);
    const rows=(t2?.返工原因列表)||[];
    showModal(`<h2>${esc(t2?.编号||"")} · 判退原因(共 ${rows.length} 次)</h2>${rows.map((row,i)=>`<div class="wake-full-row"><div class="meta">第 ${i+1} 次 · ${esc(row.判卷人||'')} · ${esc(String(row.时间||'').replace('T',' ').slice(0,16))}</div><div class="wake-full-text">${esc(row.原因||'')}</div></div>`).join("")||'<p>没有记录。</p>'}`);
    return}
  const doneT=event.target?.closest?.('[data-done-toggle]');
  if(doneT){const id=doneT.dataset.doneToggle;lsGet(`deskDone:${id}`)==="1"?lsDel(`deskDone:${id}`):lsSet(`deskDone:${id}`,"1");if(app.view==="designer")render();return}
  const reviewedT=event.target?.closest?.('[data-reviewed-toggle]');
  if(reviewedT){const id=reviewedT.dataset.reviewedToggle;lsGet(`deskReviewed:${id}`)==="1"?lsDel(`deskReviewed:${id}`):lsSet(`deskReviewed:${id}`,"1");if(app.view==="designer")render();return}
});
refresh().catch(error=>{app.data=fallbackData();rebuildSearchIndex();render();notify(`连接工单服务失败：${error.message}`)});
