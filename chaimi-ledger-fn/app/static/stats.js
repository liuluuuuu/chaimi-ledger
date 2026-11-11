/* ===== 统计 & 日历 ===== */
const fmtYuan = v => (+v||0).toLocaleString("zh-CN",{minimumFractionDigits:2, maximumFractionDigits:2});   // 元+千分位（图例/tooltip 共用）
let statRange = null;      // 当前统计区间 {from,to,label}，供下钻用
let curScopeVal = "month";   // 当前范围
function appScope(){ return curScopeVal; }
function attrDetailToggle(i){
  const el = $("#attrDetail"+i);
  if(el) el.style.display = el.style.display==="none" ? "" : "none";
}
async function fillYearSel(){
  const cur = new Date().getFullYear();
  const sc = ledgerScope();   // 账本身份守卫（ledgerScope 单源）：旧账本年份数据的迟到响应不写年份选项
  let years = [cur];
  try{
    const ys = await api(`/api/stats/yearly?ledger=${sc.id}`);
    if(!sc.isCurrent()) return;
    years = [...new Set([...ys.map(x=>+x.year), cur])].sort((a,b)=>b-a);
  }catch(e){ console.warn("年份数据加载失败，年份下拉只列今年", e); if(!sc.isCurrent()) return; }
  _yearOpts = years.map(y=>({v:String(y), t:y+"年"}));
  if(!_yearOpts.some(o=>o.v === dtGet($("#yearSel")))) dtSet($("#yearSel"), _yearOpts[0].v);
}

let _dt = null;   // {el, mode, y, m}
function _popPlace(pop, el){   // 锚定在 el 下方；下方放不下换上方，右缘不越界
  _popHide();
  pop.style.display = "block";
  const r = el.getBoundingClientRect(), pw = pop.offsetWidth, ph = pop.offsetHeight;
  let top = r.bottom + 6;
  if(top + ph > innerHeight - 8) top = Math.max(8, r.top - ph - 6);
  pop.style.top = top + "px";
  pop.style.left = Math.max(8, Math.min(r.left, innerWidth - pw - 8)) + "px";
}
function _popHide(){
  $("#dtPop").style.display="none"; $("#ddPop").style.display="none";
  const av=$("#avPop"); if(av) av.style.display="none";
}
document.addEventListener("click", e => {
  // 时 closest 恒为 null，误判「点在弹窗外」→ 弹窗闪关、箭头看起来点不动——直接放行
  if(e.target.isConnected === false) return;
  if(!e.target.closest("#dtPop,#ddPop,#avPop") && !e.target.closest("input[onclick^='pickDate'],#appScope,#yearSel")) _popHide();
});
function pickDate(el, mode){
  const v0 = dtGet(el) || today();
  _dt = {el, mode, y:+v0.slice(0,4), m:+v0.slice(5,7) || 1};
  dtRender();
  _popPlace($("#dtPop"), el);
}
function dtNav(d){
  if(!_dt) return;
  if(_dt.mode === "month") _dt.y += d;
  else { _dt.m += d; if(_dt.m < 1){ _dt.m = 12; _dt.y--; } if(_dt.m > 12){ _dt.m = 1; _dt.y++; } }
  dtRender();
}
function dtPick(val){
  if(!_dt) return;
  dtSet(_dt.el, val);
  _popHide();
  _dt.el.dispatchEvent(new Event("change"));
  _dt = null;
}
function dtRender(){
  if(!_dt) return;
  const dt = _dt;
  const head = `<div class="dt-head">
    <button class="icon-btn" onclick="dtNav(-1)" title="上一页"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg></button>
    <b class="num">${dt.mode === "month" ? dt.y + "年" : dt.y + "年" + dt.m + "月"}</b>
    <button class="icon-btn" onclick="dtNav(1)" title="下一页"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg></button>
  </div>`;
  let grid;
  if(dt.mode === "month"){
    grid = `<div class="dt-grid dt-months">` + Array.from({length:12}, (_,i) => {
      const v = `${dt.y}-${pad2(i+1)}`;
      return `<button class="dt-c${v===dtGet(dt.el)?" sel":""}${v===thisMonth()?" today":""}" onclick="dtPick('${v}')">${i+1}月</button>`;
    }).join("") + `</div>`;
  }else{
    const first = new Date(dt.y, dt.m-1, 1), days = new Date(dt.y, dt.m, 0).getDate();
    const startWd = first.getDay() || 7;   // 1=Mon..7=Sun（与日历页一致）
    let html = ["一","二","三","四","五","六","日"].map(d=>`<span class="dt-wd">${d}</span>`).join("");
    for(let i=1;i<startWd;i++) html += "<span></span>";
    for(let d=1;d<=days;d++){
      const v = `${dt.y}-${pad2(dt.m)}-${pad2(d)}`;
      html += `<button class="dt-c${v===dtGet(dt.el)?" sel":""}${v===today()?" today":""}" onclick="dtPick('${v}')">${d}</button>`;
    }
    grid = `<div class="dt-grid dt-days">${html}</div>`;
  }
  const todayBtn = `<div style="display:flex;justify-content:flex-end;margin-top:10px">
    <button class="btn btn-ghost" style="font-size:12px;padding:5px 12px" onclick="dtPick(_dt.mode==='month'?thisMonth():today())">今天</button></div>`;
  $("#dtPop").innerHTML = head + grid + todayBtn;
}

let _scopeHasCustom = true, _yearOpts = [];
const SC_LABEL = {month:"月", year:"年", all:"全部", custom:"自定义"};
function ddOpen(el, opts, cur){   // cur=选中值（缺省取 el.value）；显示文案与取值可分离
  const pop = $("#ddPop");
  const curV = cur !== undefined ? cur : el.value;
  const vis = opts.filter(o=>!o.hidden);
  pop.innerHTML = vis.map(o=>
    `<button class="dd-c${o.v===curV?" sel":""}" data-v="${esc(o.v)}">${esc(o.t)}</button>`).join("");
  const nch = Math.max(...vis.map(o=>[...o.t].length), 1);
  pop.style.width = `calc(${nch}em + 46px)`;   // 选项左右 12×2 + 浮层 padding 8×2 + 边框余量
  _popPlace(pop, el);
  pop.querySelectorAll(".dd-c").forEach(b => b.onclick = () => {
    _popHide();
    ddPick(el, b.dataset.v);
  });
}
function ddPick(el, v){   // appScope 直接走 setScope；其余（年份）写值并派发 change
  if(el.id === "appScope"){ setScope(v); return; }
  dtSet(el, v);
  el.dispatchEvent(new Event("change"));
}
function scopeDD(){
  ddOpen($("#appScope"), [
    ...["month", "year", "all"].map(v => ({v, t: SC_LABEL[v]})),   // 文案与 setScope 回显共用 SC_LABEL
    {v:"custom", t:SC_LABEL.custom, hidden: !_scopeHasCustom},
  ], curScopeVal);
}
function yearDD(){ ddOpen($("#yearSel"), _yearOpts); }

function dtSet(el, iso){
  el.dataset.date = iso || "";
  el.value = !iso ? "" : el.id === "yearSel" ? iso
    : iso.length === 7 ? `${+iso.slice(0,4)}.${+iso.slice(5,7)}`
    : `${+iso.slice(0,4)}.${+iso.slice(5,7)}.${+iso.slice(8,10)}`;
}
function dtGet(el){ return el.dataset.date || ""; }
function syncScopeChips(){   // 取值控件显隐（不触发重载）
  const sc = appScope();
  $("#monthSel").style.display = sc==="month" ? "" : "none";
  $("#yearSel").style.display = sc==="year" ? "" : "none";
  $("#sc_dates").style.display = sc==="custom" ? "flex" : "none";
  // 横向可滚提示：滚动条已隐藏，右缘渐隐（.can-scroll）让用户知道还有内容
  const bar = document.getElementById("scopeBar");
  if(bar) bar.classList.toggle("can-scroll", bar.scrollWidth > bar.clientWidth + 2);
}
function setScope(v){
  $("#appScope").value = SC_LABEL[v] || v;
  if(curScopeVal === v){
    syncScopeChips();
    if(txRecentDefault){ txRecentDefault = false; loadTx(); }
    return;
  }
  curScopeVal = v;
  syncScopeChips();
  onTimeChange();
}
function onTimeChange(){
  statRange = curStatRange();
  txRecentDefault = false;
  loadTx();
  loadSumbarList();
  refreshStats();
  if(document.body.dataset.page === "settings") stAccs();
}
function curStatRange(){
  const sc = appScope();
  // toExclusive：to 是否已是排他上界（下一周期首日/远期末日）。仅自定义区间的 to 是
  // 含当天的裸截止日——位在构造时落定，唯一消费方 rangeDto 据此决定原样传或转次日
  //（服务端全部端点只认排他上界，无双编码）
  const xe = sc !== "custom";
  if(sc === "all") return {from:"2000-01-01", to:"2100-01-01", label:"全部", toExclusive:xe};
  if(sc === "custom") return {from:dtGet($("#sc_from"))||"2000-01-01", to:dtGet($("#sc_to"))||"2100-01-01", label:"自定义区间", toExclusive:xe};
  if(sc === "year"){
    const y = dtGet($("#yearSel")) || new Date().getFullYear();
    return {from:`${y}-01-01`, to:`${+y+1}-01-01`, label:`${y}年`, year:y, toExclusive:xe};   // year 直带（fetchStatsBundle 取月趋势用，不再从文案反解）
  }
  const m = dtGet($("#monthSel")) || thisMonth();
  const y = m.slice(0,4);
  const to = nextMonthFirst(m);
  return {from:`${y}-${m.slice(5,7)}-01`, to, label:m, toExclusive:xe};
}
function gotoMonth(ymd){
  dtSet($("#monthSel"), ymd.slice(0,7));
  onTimeChange();
}
const _fmtAmt = () => window.matchMedia("(max-width:640px)").matches ? fmtI : fmt;
const _ymd = (y, m, d) => `${y}-${pad2(m)}-${pad2(d)}`;
/* 'YYYY-MM' → 下月首日（跨年 +1），月边界三处共用 */
const nextMonthFirst = ym => { const y=+ym.slice(0,4), m=+ym.slice(5,7); return m===12 ? `${y+1}-01-01` : `${y}-${pad2(m+1)}-01`; };
/* 统计/流水/导出请求的 to 参数（全站唯一转换点）：排他界原样传；自定义区间的裸截止日
   转次日作排他上界——服务端全部区间端点只认排他，补 23:59:59 的旧「含当天」编码已删 */
const rangeDto = r => r.toExclusive ? r.to : nextDayStr(r.to);
function _heatClass(r){ return r>0.66?"h4":r>0.4?"h3":r>0.18?"h2":r>0.02?"h1":""; }

const _sbRanges = {};   // page → {from,to,label,toExclusive}，点击时取回渲染时的区间
const sbLabel = r => ymLabel(r.label);   // 裸 YYYY-MM 润色为中文年月（ymLabel 单源）
function renderSumbar(page, s, range){
  const curPage = document.body.dataset.page;   // loadAll 已在取数前恢复停留页，此处恒有值
  if(curPage !== page || !s) return;   // 只渲染当前页（多页异步竞态时后到者不覆盖）
  const host = document.getElementById("gsum");
  if(!host) return;
  _sbRanges[page] = range;
  const bal = s.income - s.net_expense;
  host.innerHTML = `
    <div class="sb-cells">
      <div class="sb-cell sb-in" onclick="sumbarDetail('${page}','income')">
        <div class="k"><span class="ic" style="background:var(--income)"></span>收入 <span class="arr">›</span></div>
        <div class="v sb-v-in">+${curSym()}${fmt(s.income)}</div></div>
      <div class="sb-cell sb-net" onclick="sumbarDetail('${page}','net')" title="${s.refund_deduction>0?`支出 = 毛支出 − 退款 ${curSym()}${fmt(s.refund_deduction)}`:"支出 = 毛支出 − 退款"}">
        <div class="k"><span class="ic" style="background:var(--m-peach)"></span>支出 <span class="arr">›</span></div>
        <div class="v sb-v-net">${curSym()}${fmt(s.net_expense)}</div></div>
      <div class="sb-cell sb-bal" onclick="sumbarDetail('${page}','balance')">
        <div class="k"><span class="ic" style="background:var(--m-blue)"></span>结余 <span class="arr">›</span></div>
        <div class="v${bal<0?" sb-v-neg":""}">${bal>=0?"+":"−"}${curSym()}${fmt(Math.abs(bal))}</div></div>
    </div>`;
}
/* gbarFit/--gbar-h/ResizeObserver 测高链已随流水筛选区 sticky 退役（结构化钉顶后
   顶栏与页面是同列 flex 行，高度关系由布局承担，无需实测回写） */

const sumbarGen = makeSeq();   // 请求代数（makeSeq 单源）：快速切范围/账本时丢弃迟到旧响应（renderSumbar 只挡页面错位，挡不住旧区间数据）
async function loadSumbarList(){
  if(!document.getElementById("gsum")) return;
  const seq = sumbarGen.next();
  try{
    const range = curStatRange();
    const s = appScope() === "month"
      ? await api(`/api/stats/summary?ledger=${lid()}&month=${dtGet($("#monthSel")) || thisMonth()}`)
      : await api(`/api/stats/range?ledger=${lid()}&from=${range.from}&to=${encodeURIComponent(rangeDto(range))}`);
    if(!sumbarGen.isCurrent(seq)) return;
    renderSumbar("list", s, range);
  }catch(e){ console.warn("汇总条加载失败", e); }
}
/* AI 页汇总条：固定本月（Top3 分类行已取消） */
async function loadSumbarAi(){
  const sc = ledgerScope();   // 账本身份守卫（ledgerScope 单源）：旧账本汇总的迟到响应不渲染
  const m = thisMonth();
  const range = {from:`${m}-01`, to:nextMonthFirst(m), label:m, toExclusive:true};
  try{
    const s = await api(`/api/stats/summary?ledger=${sc.id}&month=${m}`);
    if(!sc.isCurrent()) return;
    renderSumbar("ai", s, range);
  }catch(e){ console.warn("AI 页汇总条加载失败", e); }
}
/* 汇总条各格下钻：与统计卡下钻同口径（支出下钻支出明细，退款行一并展示） */
function sumbarDetail(page, kind){
  const r = _sbRanges[page] || curStatRange();
  const name = {income:"收入", net:"支出", balance:"收支"}[kind] || "收支";
  const type = kind==="income" ? "income" : kind==="balance" ? "" : "expense";
  showTxDetail(`${sbLabel(r)} ${name}明细`,
    {from:r.from, to:rangeDto(r), ...(type?{type}:{}),
     ...(kind==="net" ? {with_refunds:true} : {})});   // 支出=净额(毛−退款)，明细带退款行对账
}

async function fetchStatsBundle(sc, m){
  // 并发拉统计四件套（汇总/支出分类/收入分类/趋势）
  if(sc === "month"){
    const [s, cats, catsInc, trend] = await Promise.all([
      api(`/api/stats/summary?ledger=${lid()}&month=${m}`),
      api(`/api/stats/category?ledger=${lid()}&month=${m}`),
      api(`/api/stats/category?ledger=${lid()}&month=${m}&type=income`),
      api(`/api/stats/monthly?ledger=${lid()}&year=${m.slice(0,4)}`)]);
    return {s, cats, catsInc, trend, trendIsYearly: false};
  }
  // 自定义区间末端含当天（rangeDto 转次日排他上界，后端按 date< 判定；与流水页同口径）
  const q = `ledger=${lid()}&from=${statRange.from}&to=${encodeURIComponent(rangeDto(statRange))}`;
  const [s, cats, catsInc, trend] = await Promise.all([
    api(`/api/stats/range?${q}`),
    api(`/api/stats/category-range?${q}`),
    api(`/api/stats/category-range?${q}&type=income`),
    sc==="year"
      ? api(`/api/stats/monthly?ledger=${lid()}&year=${statRange.year}`)
      : sc==="custom"
        ? api(`/api/stats/trend?${q}`)
        : api(`/api/stats/yearly?ledger=${lid()}`)]);
  return {s, cats, catsInc, trend, trendIsYearly: sc === "all"};
}

async function renderMonthCalendar(m, isFresh){
  // 月历网格：前后月补格 + 热度底色 + 当日标记（cal api 过期即不渲；isFresh 闭包由
  // refreshStats 绑定本次代数传入——跨函数传递的是「是否仍是最新一次」谓词而非裸 token）
  const cal = await api(`/api/stats/calendar?ledger=${lid()}&month=${m}`);
  if(!isFresh()) return;
  $("#calTitle").textContent = ymLabel(m);   // 回填移到守卫后：迟到方不再写标题
  const [y,mo] = m.split("-").map(Number);
  const first = new Date(y,mo-1,1), days = new Date(y,mo,0).getDate();
  const byDay = {}; cal.forEach(c=>byDay[+c.day.slice(8)]=c);
  const maxNet = Math.max(1, ...cal.map(c=>Math.max(0,c.net||0)));
  const startWd = first.getDay() || 7;  // 1=Mon..7=Sun
  let html = ["一","二","三","四","五","六","日"].map(d=>`<div class="cal-wd">${d}</div>`).join("");
  const prevY = mo===1 ? y-1 : y, prevM = mo===1 ? 12 : mo-1;
  const prevDays = new Date(y,mo-1,0).getDate();
  for(let i=startWd-1;i>=1;i--){
    const d = prevDays - i + 1;
    html += `<div class="cal-d other" onclick="gotoMonth('${_ymd(prevY,prevM,d)}')"><span class="n">${d}</span></div>`;
  }
  const todayStr = today();
  $("#calendar").classList.remove("trend");
  const fa = _fmtAmt();
  for(let d=1;d<=days;d++){
    const c = byDay[d], dd = `${m}-${pad2(d)}`;
    const heat = c && c.net>0 ? _heatClass(c.net/maxNet) : "";
    html += `<div class="cal-d ${heat} ${dd===todayStr?'today':''}" onclick="showDay('${dd}')"><span class="n">${d}</span>
      ${c&&c.net?`<div class="v e num">-${fa(c.net)}</div>`:""}
      ${c&&c.income?`<div class="v i num">+${fa(c.income)}</div>`:""}</div>`;
  }
  // 尾部补齐：把最后一行填满到 7 的倍数
  const totalSoFar = startWd - 1 + days;
  const trailing = (7 - totalSoFar % 7) % 7;
  const nextY = mo===12 ? y+1 : y, nextM = mo===12 ? 1 : mo+1;
  for(let d=1;d<=trailing;d++){
    html += `<div class="cal-d other" onclick="gotoMonth('${_ymd(nextY,nextM,d)}')"><span class="n">${d}</span></div>`;
  }
  $("#calendar").innerHTML = html;
}

function renderTrendCells(trend, trendIsYearly){
  // 年/全局概览格：按月或按年的统计格（点击下钻明细）
  $("#calTitle").textContent = statRange.label + " 概览";
  const cells = trendIsYearly
    ? trend.map(t=>({label:t.year+"年", net:t.net||0, income:t.income||0,
        click:`showTxDetail('${t.year}年 流水', {from:'${t.year}-01-01', to:'${+t.year+1}-01-01'})`}))
    : trend.map(t=>{ const ym=t.month, mo=+ym.slice(5,7);
        const to = nextMonthFirst(ym);
        return {label:mo+"月", net:t.net||0, income:t.income||0,
          click:`showTxDetail('${ym} 流水', {from:'${ym}-01', to:'${to}'})`};});
  const maxNet = Math.max(1, ...cells.map(c=>Math.max(0,c.net)));
  const fa = _fmtAmt();
  $("#calendar").classList.add("trend");
  $("#calendar").innerHTML = cells.map(c=>{
    const heat = c.net>0 ? _heatClass(c.net/maxNet) : "";
    return `<div class="cal-d trend ${heat}" onclick="${c.click}">
      <span class="n">${c.label}</span>
      ${c.net?`<div class="v e num">${fa(c.net)}</div>`:""}
      ${c.income?`<div class="v i num">+${fa(c.income)}</div>`:""}</div>`;
  }).join("");
}

function renderAttribution(attrs){
  // 归属蝴蝶图 + 可展开列表（中心人名，左=支出右=收入）
  $("#attrCard").style.display = attrs.length ? "" : "none";
  if(!attrs.length) return;
  const maxAmt = Math.max(...attrs.map(a => Math.max(a.expense || 0, a.income || 0)), 1);
  const barW = v => Math.max(2, (v / maxAmt) * 100);
  const amtLabel = v => v > 0 ? `<span class="sub" style="font-size:11px;flex-shrink:0;margin:0 6px">${fmt(v)}</span>` : "<span style='width:8px'></span>";
  // to 排他上界走 rangeDto 单源（与 sumbarDetail/showCategoryDetail 一致）
  const barClick = (a, dir, type) => `showTxDetail('${qEsc(a.person)} ${dir}明细',{from:'${statRange.from}',to:'${rangeDto(statRange)}',type:'${type}',attribution:'${qEsc(a.person)}'})`;
  $("#attrButterfly").innerHTML =
    `<div class="attr-b-axis"><span class="l">← 支出</span><span class="c"></span><span class="r">收入 →</span></div>` +
    attrs.map((a, i) => `
      <div class="attr-b-row">
        <div class="attr-b-half left">${amtLabel(a.expense)}<div class="attr-b-bar exp" style="width:${barW(a.expense)}%" onclick="${barClick(a, "支出", "expense")}"></div></div>
        <div class="attr-b-center" onclick="attrDetailToggle(${i})">${esc(a.person)}</div>
        <div class="attr-b-half"><div class="attr-b-bar inc" style="width:${barW(a.income)}%" onclick="${barClick(a, "收入", "income")}"></div>${amtLabel(a.income)}</div>
      </div>`).join("");
  // 下方保留可展开列表（分组/成员明细）
  const sums=x=>`<div class="num"><span class="c-exp">−${fmt(x.expense)}</span>&nbsp;·&nbsp;<span class="c-inc">+${fmt(x.income)}</span></div>`;
  $("#attrList").innerHTML = attrs.map((a,i)=>{
    const sub=a.group?`${a.members.length} 个归属 · ${a.cnt} 笔 ▸`:`${a.cnt} 笔`;
    const head=`<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;${a.group?"cursor:pointer;":""}border-bottom:1px solid rgba(128,128,128,.15)"${a.group?` onclick="attrDetailToggle(${i})"`:""}>
      <div><b>${esc(a.person)}</b>&nbsp;<span class="sub">${sub}</span></div>${sums(a)}</div>`;
    if(!a.group)return head;
    return head+`<div id="attrDetail${i}" style="display:none">${a.members.map(m=>
      `<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0 5px 14px;border-bottom:1px dashed rgba(128,128,128,.15)">
        <div><span class="sub">${esc(m.person)}</span>&nbsp;<span class="sub">${m.cnt} 笔</span></div>${sums(m)}</div>`).join("")}</div>`;
  }).join("");
}

async function renderStatCharts(cats, catsInc, trend, trendIsYearly, range, isFresh){
  // 分类饼图 + 趋势柱（仅在统计页可见时绘制，避免隐藏容器中初始化导致尺寸错乱）。
  // 每次 drawChart 前核对代数（isFresh 闭包，同 renderMonthCalendar）：drawChart 是顺序
  // await，旧响应的后续图表若不拦，会 destroy 掉新响应刚画好的图再画上旧图
  if(!$("#tab-stats").classList.contains("on")) return;
  if(cats.length && isFresh()) await drawChart("catChart","doughnut", cats.map(c=>c.name), cats.map(c=>c.net/100), null,
    (e, els)=>{ const i = els[0].index; showCategoryDetail(cats[i].name, range); }, "catChartLg");
  if(catsInc.length && isFresh()) await drawChart("catIncChart","doughnut", catsInc.map(c=>c.name), catsInc.map(c=>c.net/100), null,
    (e, els)=>{ const i = els[0].index; showCategoryDetail(catsInc[i].name, range, "income"); }, "catIncChartLg");
  if(!trend.length || !isFresh()) return;
  if(trendIsYearly){
    await drawChart("trendChart","bar", trend.map(t=>t.year+"年"),
      trend.map(t=>t.net/100), trend.map(t=>t.income/100),
      (e, els)=>{ const el = els[0]; const yv = trend[el.index].year;
        showTxDetail(`${yv}年 ${el.datasetIndex===1?"收入":"支出"}明细`,
          {from:`${yv}-01-01`, to:`${+yv+1}-01-01`, type: el.datasetIndex===1?"income":"expense"}); });
  }else{
    // 月度趋势（按月/按年/自定义区间共用）：下钻用数据自身的月份，兼容自定义跨年区间
    await drawChart("trendChart","bar", trend.map(t=>t.month.slice(5)+"月"),
      trend.map(t=>t.net/100), trend.map(t=>t.income/100),
      (e, els)=>{ const el = els[0];
        showMonthDetail(trend[el.index].month,
          el.datasetIndex===1 ? "income" : "expense"); });
  }
}

const statsGen = makeSeq();   // 请求代数（makeSeq 单源）：快速切范围/账本时丢弃迟到旧响应，
                              // 防旧数据重渲日历/蝴蝶图/饼图；guard 对象传给 renderMonthCalendar/
                              // renderStatCharts（其内部 await 后各自核对）
async function refreshStats(){
  const gen = statsGen.next();
  const isFresh = () => statsGen.isCurrent(gen);   // 本次代数的守卫谓词（传给子函数，见下）
  const sc = appScope();
  const m = dtGet($("#monthSel")) || thisMonth();
  statRange = curStatRange();
  const {s, cats, catsInc, trend, trendIsYearly} = await fetchStatsBundle(sc, m);
  if(!isFresh()) return;   // 期间又发起了新 refreshStats：本响应已过期，丢弃
  // 图例与切片统一按金额(净额)从高到低排列
  cats.sort((a,b)=>b.net-a.net);
  catsInc.sort((a,b)=>b.net-a.net);
  renderSumbar("cal", s, statRange);
  renderSumbar("stats", s, statRange);
  // 月历/趋势格都在 cal 页的 #calendar 里：仅 cal 页可见时渲染（切到 cal 页时
  // showPage 会再触发整轮 refreshStats，提前渲隐藏 DOM 没有消费方——两分支同门控）
  if($("#tab-cal").classList.contains("on")){
    if(sc === "month") await renderMonthCalendar(m, isFresh);
    else renderTrendCells(trend, trendIsYearly);
  }
  // 口径与 range 系列相同（to 经 rangeDto 单源转排他上界）
  let attrs = [];
  try{
    attrs = await api(`/api/stats/attribution?ledger=${lid()}&from=${statRange.from}&to=${encodeURIComponent(rangeDto(statRange))}`);
  }catch(e){ console.warn("归属统计加载失败，蝴蝶图将缺省", e); }
  if(!isFresh()) return;   // 归属请求也可能迟到：过期即不渲蝴蝶图/图例
  renderAttribution(attrs);
  // 空态与卡片可见性
  const noData = !cats.length && !trend.length && !attrs.length;
  $("#statsEmpty").textContent = `${sbLabel(statRange)} · 暂无数据`;   // 空态文案随范围（月/年/全部/自定义），不写死"本月"
  $("#statsEmpty").style.display = noData ? "block" : "none";
  $("#catCard").style.display = cats.length ? "" : "none";
  $("#catIncCard").style.display = catsInc.length ? "" : "none";
  $("#trendCard").style.display = trend.length ? "" : "none";
  await renderStatCharts(cats, catsInc, trend, trendIsYearly, statRange, isFresh);
}
function chartColors(){
  const cs = getComputedStyle(document.body);
  const isDark = document.body.dataset.theme === "dark";
  /* 深色模式调色板（环形图）：亮度再降一档，目标 35-45% lightness，深色底上完全不刺眼 */
  const palette = isDark
    ? ["#3E72A8","#7058A8","#42A06C","#A86048","#A85A78","#42A088","#98782C","#5E6474","#3E5E84","#3E7456","#7E5028","#7E4858"]
    : ["#6CB0F5","#B395F0","#6FD5A0","#F5A878","#F095B0","#6FD5C2","#E5C870","#9095A8","#95C8F5","#95E0BA","#F5BD98","#F5B8CC"];
  /* 深色模式净支出/收入柱状主色：与环形图同一亮度档 */
  const bar = {
    expense: isDark ? "#3E72A8" : "#6CB0F5",
    income:  isDark ? "#42A06C" : "#6FD5A0",
  };
  return {
    fg: cs.getPropertyValue("--muted").trim() || "#888",
    grid: isDark ? "rgba(255,255,255,.07)" : "rgba(0,0,0,.06)",
    card: isDark ? (cs.getPropertyValue("--card").trim() || "#1A2236") : "#FFFFFF",   // 环形图扇形缝色
    palette, bar,
  };
}
const chartDrill = {};
function lgDrill(id, i){ chartDrill[id] && chartDrill[id](i); }
function renderLegend(legendId, chartId, labels, data){
  // HTML 图例：固定圆图尺寸后，上下两图的圆心与图例列起点严格对齐；点击图例=点击切片下钻
  const total = data.reduce((a,b)=>a+(+b||0),0);
  const pal = chartColors().palette;
  document.getElementById(legendId).innerHTML = labels.map((l,i)=>{
    const v = +data[i]||0, pct = total ? Math.round(v/total*100) : 0;
    return `<div class="lg-item" onclick="lgDrill('${chartId}',${i})">
      <span class="lg-dot" style="background:${pal[i%pal.length]}"></span>
      <span class="lg-name">${esc(l)}</span>
      <span class="lg-val num">${curSym()}${fmtYuan(v)}</span>
      <span class="lg-pct num">${pct}%</span></div>`;
  }).join("");
}
async function drawChart(id, type, labels, data, data2, onClick, legendId, opts){
  /* opts.responsive=false：固定 canvas 像素尺寸（AI 聊天气泡内的 150px 环形图），
     不随容器缩放；缺省 true 与统计页行为一致 */
  const fixed = opts && opts.responsive === false;
  try{ await loadChart(); }catch(e){ console.warn("Chart.js 加载失败，图表跳过", e); return; }
  if(charts[id]) charts[id].destroy();
  /* 颜色一律 scriptable（每次绘制时求值，Chart.js v4）：切主题经 dropCharts 的
     update() 即换色，不再在创建时焙成 hex 定值（此前为此维护销毁-重放机制） */
  const doughnutBorderWidth = 2;
  const palOf = () => chartColors().palette, cardC = () => chartColors().card;
  const ds = [{data, backgroundColor:type==="doughnut"
      ? ctx => palOf()[ctx.dataIndex % palOf().length]
      : () => chartColors().bar.expense,
    borderColor: cardC, borderWidth: doughnutBorderWidth,
    hoverBorderColor: cardC, label:(opts && opts.label) || "净支出", borderRadius:6}];
  if(data2) ds.push({data:data2, backgroundColor: () => chartColors().bar.income,
    borderColor: cardC, borderWidth: doughnutBorderWidth,
    hoverBorderColor: cardC, label:"收入", borderRadius:6});
  const fgC = () => chartColors().fg, gridC = () => chartColors().grid;
  charts[id] = new Chart($("#"+id), {type, data:{labels, datasets:ds},
    /* 容器均固定高度：maintainAspectRatio 必须为 false，否则 canvas 按自身宽高比撑高溢出卡片 */
    options:{responsive:!fixed, maintainAspectRatio:false, onClick: onClick||undefined,
      elements:{arc:{borderWidth: doughnutBorderWidth, hoverOffset: type==="doughnut" ? 4 : 0}},
      plugins:{legend:{display:false, labels:{color: fgC}},
        tooltip:{backgroundColor: () => document.body.dataset.theme === "dark"
                   ? "rgba(20,28,45,.92)" : "rgba(255,255,255,.95)",
                 titleColor: () => getComputedStyle(document.body).getPropertyValue("--fg").trim() || "#0F172A",
                 bodyColor: fgC, borderColor: gridC, borderWidth:1, padding:8,
          callbacks:{label:(ctx)=>{
            const v = typeof ctx.parsed==="number" ? ctx.parsed : ctx.parsed.y;
            const prefix = ctx.dataset.label || ctx.label || "";
            return ` ${prefix} ${curSym()}${fmtYuan(v)}`;
          }}}},
      scales:type==="doughnut"?undefined:{x:{ticks:{color:fgC},grid:{color:gridC}},y:{ticks:{color:fgC},grid:{color:gridC}}}}});
  if(onClick) chartDrill[id] = (i)=>onClick(null,[{index:i}]);
  if(legendId) renderLegend(legendId, id, labels, data);
}

function dropCharts(){
  /* 切主题换图表配色：drawChart 的颜色是绘制时求值的 scriptable（见其注释），
     update() 重画即得新配色——AI 聊天气泡图不必再销毁重建（其创建数据只在气泡
     闭包里，此前为此维护「快照 labels/data/下钻闭包再原位重建」的重放机制）。
     图例圆点是内联样式，按图表现存数据重渲一遍；图例 id 统一「canvas id + Lg」
     （趋势图无图例，miss 跳过）。蝴蝶图/月历格颜色走 CSS 类，无需重渲。 */
  for(const [id, c] of Object.entries(charts)){
    try{
      c.update();
      const lg = document.getElementById(id + "Lg");
      if(lg && c.data.labels && (c.data.datasets[0] || {}).data)
        renderLegend(lg.id, id, c.data.labels, c.data.datasets[0].data);
    }catch(_){ /* 气泡已删的残余实例：update 抛错跳过即可 */ }
  }
}