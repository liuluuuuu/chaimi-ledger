/* ===== 流水 ===== */
let txPages = 1;

let txRecentDefault = true;

/* 搜索框三件套（输入防抖 / 回车立即且不换行 / ✕ 清空）：流水与人情页共用。
   onClear(hadText) 可选：✕ 点击时回传「清空前是否有内容」——流水页据此区分
   「清空后只刷新按钮态」与「本就空、收起搜索」，不必另靠 pointerdown 快照
   抢在清空前取值 */
function bindSearch(box, clearBtn, apply, delay, onClear){
  let timer = null;
  const run = now =>{ clearTimeout(timer); now ? apply() : timer = setTimeout(apply, delay || 350); };
  box.addEventListener("input", ()=>{ clearBtn.style.display = box.value ? "" : "none"; run(false); });
  box.addEventListener("keydown", e=>{ if(e.key === "Enter"){ e.preventDefault(); box.blur(); run(true); } });
  clearBtn.addEventListener("click", ()=>{
    const hadText = !!box.value.trim();
    box.value = ""; clearBtn.style.display = "none"; run(true);
    if(onClear) onClear(hadText);
  });
}
bindSearch($("#q_text"), $("#q_clear"), ()=>{ txPageNo = 1; loadTx(); }, null,
           hadText => hadText ? txSearchBtnState() : txSearchToggle(false));
/* 搜索：工具条内联展开（点按钮后按钮让位、搜索框自按钮位向左展开）+ ✕ 复用
   （有内容=清空 / 无内容=收起），另支持 Esc/点工具条外收起；有关键词时按钮亮起
   （关着但关键词在 = 搜索仍生效）。✕ 恒显由 CSS 声明（压过模板内联 display:none
   与 bindSearch 的按值显隐） */
const txToolbar = () => document.querySelector("#tab-list .tx-filter .filters");   // 流水工具条（筛选行 = 搜索按钮宿主），单源
function txSearchToggle(open){
  const bar = txToolbar();
  const will = open === undefined ? !bar.classList.contains("searching") : open;
  bar.classList.toggle("searching", will);
  if(will) $("#q_text").focus({preventScroll: true});   // 防移动端聚焦时浏览器自动滚动容器
  txSearchBtnState();
}
function txSearchBtnState(){
  const hasText = !!$("#q_text").value.trim();
  $("#txSearchBtn").classList.toggle("on", txToolbar().classList.contains("searching") || hasText);
  $("#q_clear").title = hasText ? "清空" : "收起搜索";
}
document.addEventListener("click", e => {
  const bar = txToolbar();
  if(bar.classList.contains("searching") && !bar.contains(e.target)) txSearchToggle(false);
});
$("#q_text").addEventListener("keydown", e => { if(e.key === "Escape") txSearchToggle(false); });
$("#q_text").addEventListener("input", txSearchBtnState);
// 类型筛选与搜索/清空同幅重置页码（徽标清零自动退出 confirm 筛选的路径不重置，由 loadTx 回拉末页兜住）
function setTypeFilter(b){ curTypeFilter=b.dataset.ft; txPageNo=1; document.querySelectorAll("#tab-list .fchip[data-ft]").forEach(x=>x.classList.toggle("on",x===b)); loadTx(); }
const txGen = makeSeq();   // 请求代数（makeSeq 单源）：快速切月份/范围/账本时，迟到的旧响应整包丢弃（不回填 DOM/txCache）
async function loadTx(){
  const seq = txGen.next();
  $("#ftBase").textContent = txRecentDefault ? "最近" : "全部";
  const q = new URLSearchParams({ledger:lid(), page:txPageNo, page_size: txRecentDefault ? 20 : 50});
  const qt = $("#q_text").value.trim();
  const confirmOnly = curTypeFilter === "confirm";   // 先声明再参与下方 showPager（TDZ：此前引用在声明前，打开应用即抛 ReferenceError）
  // 最近态无翻页（纯速览），搜索/待确认超一页时出底部翻页（#txPager）以便看全匹配结果——
  // 待确认此前漏了：>20 笔时第 21 笔起无任何入口可见（徽标数与列表对不上）
  const showPager = !txRecentDefault || !!qt || confirmOnly;
  document.querySelectorAll("#tab-list .tx-type-only").forEach(x=>x.style.display = txRecentDefault ? "none" : "");
  $("#txPager").style.display = showPager ? "" : "none";
  if(qt) q.set("q", qt);
  // 跨月份的导入流水都可能待确认，忽略时间范围，否则列表为空
  if(confirmOnly) q.set("need_confirm","1");
  else{
    if(curTypeFilter) q.set("type", curTypeFilter);
    // 用过范围选择器后按所选范围（「全部」是 2000→2100 无效边界，不带 from/to 省一次无谓过滤）。
    // 条件此前笔误为 !== "custom"：自定义区间列表反而不过滤，与顶部汇总条（走范围口径）
    // 同屏两套数；to 一律排他上界（rangeDto 单源转次日），与统计/导出同口径
    if(!txRecentDefault && appScope() !== "all"){
      const r = statRange || curStatRange();
      q.set("from", r.from);
      q.set("to", rangeDto(r));
    }
  }
  let r = await api("/api/transactions?"+q);
  if(!txGen.isCurrent(seq)) return;   // 期间又发起了新 loadTx（切月/范围/账本）：本响应已过期
  // 会停在不存在的页上，指示器出现「N 笔 · 第 X 页」却列空——回拉到末页重查一次
  let pages = Math.max(1, Math.ceil(r.total / (r.page_size || 50)));
  if(txPageNo > pages){
    txPageNo = pages;
    q.set("page", txPageNo);
    r = await api("/api/transactions?"+q);
    if(!txGen.isCurrent(seq)) return;
  }
  txPages = pages;
  $("#txPage").textContent = `${r.total} 笔 · 第 ${r.page} 页`;
  // 按日分组
  const groups = {};
  r.items.forEach(t=>{ const d=(t.date||"").slice(0,10); (groups[d]=groups[d]||[]).push(t); });
  // 退款对：已被退款「吸收」的原交易不再单独渲染（在退款行的双卡片里展示）。
  // absorbedIds 必须在按日分组前全局构建（跨日场景：原交易在 A 日、退款在 B 日，
  // 分组后 B 日组里看不到 A 日的原交易，逐组判定会漏吸收）。
  const absorbed = absorbedIds(r.items);
  $("#txList").innerHTML = Object.entries(groups).map(([d,items])=>{
    const vis = items.filter(x=>!absorbed.has(x.id));
    if(!vis.length) return "";   // 整日都被退款吸收（跨日退款的原消费日）：不渲染空头卡——原消费已在退款日的双行卡内展示
    return `
    <div class="tx-day">
      <div class="day-head"><span>${d}</span><span class="num">${daySum(items, absorbed)}</span></div>
      <div class="card">${vis.map(txRow).join("")}</div>
    </div>`;
  }).join("") ||
    `<div class="empty"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/></svg>暂无流水</div>`;
  txSelDecorate();
}
function netSum(items, absorbed){
  let e = 0, i = 0;
  const refundByOrigin = {};
  items.forEach(t=>{ if(t.refund_for) refundByOrigin[t.refund_for] = t; });
  items.forEach(t=>{
    if(absorbed && absorbed.has(t.id)){
      const r = refundByOrigin[t.id];
      if(r){
        // 净额 = signed(原) + signed(退款)：同号相加，原支出 100 + 退款入账 100 = 0；半退则留差额
        const net = signed(t.type, t.amount) + signed(r.type, r.amount);
        if(net < 0) e += -net;       // 净支出
        else if(net > 0) i += net;   // 净收入（罕见：退款大于原消费）
        return;
      }
      // 跨日退款：原交易被整页范围（absorbed）里别日的退款行吸收，但当日组内没有
      // 退款行可配对——回退按毛额计（与当日弹窗同口径），否则原交易从当日合计「凭空消失」
    }
    if(t.type === "expense") e += t.amount;
    else if(t.type === "income") i += t.amount;
  });
  return {e, i};
}
function daySum(items, absorbed){
  const {e, i} = netSum(items, absorbed);
  return `支出 ${fmt(e)} · 收入 ${fmt(i)}`;
}
async function txPage(d){
  const target = Math.min(Math.max(1, txPageNo + d), txPages);
  if(target === txPageNo) return;
  txPageNo = target;
  await loadTx();
  // #txList 自滚动（结构化钉顶）：翻页回到列表顶端——筛选区是独立 flex 行，
  // 无遮挡无需让位计算（旧 sticky 版要按钉顶区 rect 反推落点）
  $("#txList").scrollTo({top:0, behavior:"smooth"});
}

/* ===== 上滑收起汇总条（所有可滚动页面通用） =====
   各页滚动容器（流水=#txList、AI=消息区/工作台、其余=.page.on 自身内部滚动）滚动
   1:1 驱动 #gsum 高度：汇总随内容上滑收起（跟手无补间），收到底后顶栏只余范围条，
   滚回顶部展开复位。事件用捕获挂在 document（scroll 不冒泡但捕获能拦到所有子孙
   容器），apply 只按当前页 scroller 取值——其余容器（弹窗 body 等）滚动自然无效；
   顶栏整体隐藏/无汇总的页（人情/导入/设置）量到高度 0 自动跳过。页切经
   core.showPage 先复位再按保留的滚动位对齐（无跳变）。 */
let _sumBase = 0;   // 汇总条展开态自然高度（首次收起前实测；回顶/换页清零重测）
function _pageScroller(){
  const p = document.body.dataset.page;
  if(p === "list") return document.getElementById("txList");
  if(p === "ai"){
    const ai = document.getElementById("tab-ai");
    return ai && ai.classList.contains("chat-on")
      ? document.querySelector("#tab-ai .msgs") : document.querySelector("#tab-ai .ai-desk");
  }
  return document.querySelector(".page.on");
}
function sumbarCollapseReset(){
  const g = document.getElementById("gsum");
  if(g){ g.style.height = ""; g.classList.remove("sum-pin"); }
  _sumBase = 0;
}
function sumbarCollapseApply(){
  const g = document.getElementById("gsum");
  const sc = _pageScroller();
  if(!g || !sc) return;
  const st = sc.scrollTop;
  if(st <= 0){ sumbarCollapseReset(); return; }
  if(g.classList.contains("sum-pin")) return;   // 已收起：保持 0（自然高度不可测）
  if(!_sumBase){
    _sumBase = g.offsetHeight;                  // 展开态实测（含子级 margin，BFC 已收住）
    if(!_sumBase) return;                       // 该页无汇总（顶栏隐藏/未渲染）
  }
  const h = Math.max(0, _sumBase - st);
  g.style.height = h + "px";
  g.classList.toggle("sum-pin", h === 0);
}
document.addEventListener("scroll", sumbarCollapseApply, { capture: true, passive: true });
/* 导出页区间参数（小票 zip / CSV / xlsx 三口共用）：只拼已选端 */
function expRangeParams(){
  const f = dtGet($("#exp_from")), t = dtGet($("#exp_to"));
  // to 一律排他上界（用户语义的「含当天」由 nextDayStr 单点转次日），与统计/流水同口径
  return (f ? `&from=${f}` : "") + (t ? `&to=${nextDayStr(t)}` : "");
}
async function exportReceipts(){
  const l = lid();   // 点击时的账本：大 zip 下载期间切账本，文件名不能换新账本、内容却还是旧账本的
  const lg = await curLedger();   // 同在 fetch 前取名，不随下载期间的切换变化
  let url = `api/receipts/export?ledger=${l}` + expRangeParams();
  try{
    const r = await fetch(url);
    if(!r.ok){
      const e = await r.json().catch(() => ({}));
      return msg(e.detail || "下载失败", false);
    }
    const blob = await r.blob();
    const now = new Date();   // 不用 t：外层已有 to 值的 t，遮蔽易误读
    const stamp = `${now.getFullYear()}${pad2(now.getMonth()+1)}${pad2(now.getDate())}`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `chaimi-ledger-${(lg && lg.name || "").trim() || "账本"}小票-${stamp}.zip`;
    a.click();
    URL.revokeObjectURL(a.href);
    msg("小票已打包下载 ✅");
  }catch(e){ msg("下载失败：" + e.message, false); }
}
function exportCsv(format="csv"){   // 参数名避开全局金额格式化 fmt
  location.href = `api/export?ledger=${lid()}&format=${format}` + expRangeParams();
}
// 导出页快捷区间：全部/本月/本年（默认全部=数据库流水起始日至今天）
function expInitRange(){ expSetRange("all"); }
async function expSetRange(mode){
  const sc = ledgerScope();   // 账本身份守卫：trend 在途时切账本，旧账本最早流水月不得写导出区间
  let f = today(), t = today();   // 兜底：库为空时今天~今天
  if(mode === "month") f = thisMonth() + "-01";
  else if(mode === "year") f = thisMonth().slice(0, 4) + "-01-01";
  else try{   // all：查最早流水月
    const trend = await api(`/api/stats/trend?ledger=${sc.id}&from=2000-01-01&to=2100-01-01`);
    if(!sc.isCurrent()) return;
    if(trend.length) f = trend[0].month + "-01";
  }catch(e){ console.warn("trend 查询失败，区间回落今天", e); }
  if(!sc.isCurrent()) return;
  dtSet($("#exp_from"), f); dtSet($("#exp_to"), t);
  document.querySelectorAll(".exp-presets .btn").forEach(b =>
    b.classList.toggle("on", b.dataset.mode === mode));
}

/* ===== 编辑/确认弹窗 ===== */
let dayRefresh = null;
function closeDay(){
  if(daySelOn()) txSelExit(false);   // 弹窗整个关掉：静默退出选择态（无需重拉）
  $("#dayMask").classList.remove("on"); dayRefresh = null;
}
function viewReceipt(p){ if(!p) return; $("#rcpImg").src = p.replace(/^\//,"").split("?")[0] + "?t=" + Date.now(); $("#rcpMask").classList.add("on"); }
function closeReceipt(){ $("#rcpMask").classList.remove("on"); $("#rcpImg").src = ""; closeCrop(); }

/* ===== 小票手动裁剪 ===== */
let cropPath = "", cropRect = null;   // cropRect = {x0,y0,x1,y1} 最终选区（屏幕坐标）
function openCrop(){
  cropPath = $("#rcpImg").src;
  if(!cropPath) return;
  $("#cropImg").src = cropPath;
  $("#cropMask").style.display = "flex";
  $("#cropSel").style.display = "none";
  $("#cropDim").style.display = "none";
  $("#cropOk").disabled = true;
  cropRect = null;
  let start = null;
  const wrap = $("#cropWrap");
  wrap.onpointerdown = e => {
    e.preventDefault();
    try{ wrap.setPointerCapture(e.pointerId); }catch(_){}   // 拖出图片范围外松手仍收到 up：无捕获时 start 残留，选区"粘手"
    start = cropPos(e); cropRect = null;
  };
  wrap.onpointermove = e => {
    if(!start) return;
    e.preventDefault();
    const end = cropPos(e);
    const x0 = Math.min(start.x, end.x), y0 = Math.min(start.y, end.y);
    const w = Math.abs(end.x - start.x), h = Math.abs(end.y - start.y);
    cropRect = {x0, y0, x1: x0 + w, y1: y0 + h};
    const sel = $("#cropSel");
    sel.style.display = "block";
    sel.style.left = x0 + "px"; sel.style.top = y0 + "px";
    sel.style.width = w + "px"; sel.style.height = h + "px";
    const dim = $("#cropDim");
    dim.style.display = "block";
    dim.style.clipPath = `polygon(0 0,100% 0,100% 100%,0 100%,0 0,${x0}px 0,${x0}px ${y0+h}px,${x0+w}px ${y0+h}px,${x0+w}px ${y0}px,${x0}px ${y0}px)`;
    $("#cropOk").disabled = w < 20 || h < 20;
  };
  wrap.onpointerup = () => { start = null; };   // 只清 start，cropRect 保留供 applyCrop 用
  wrap.onpointercancel = () => { start = null; };   // 系统 gesture/来电打断：不清则后续 move 仍会拖动选区
}
function closeCrop(){ $("#cropMask").style.display = "none"; cropRect = null; }
function cropPos(e){
  const r = $("#cropImg").getBoundingClientRect();
  return {x: Math.max(0, Math.min(e.clientX - r.left, r.width)),
          y: Math.max(0, Math.min(e.clientY - r.top, r.height))};
}
async function applyCrop(){
  if(!cropRect || !cropPath) return;
  const img = $("#cropImg");
  const scaleX = img.naturalWidth / img.getBoundingClientRect().width;
  const scaleY = img.naturalHeight / img.getBoundingClientRect().height;
  const x = Math.round(cropRect.x0 * scaleX);
  const y = Math.round(cropRect.y0 * scaleY);
  const w = Math.round((cropRect.x1 - cropRect.x0) * scaleX);
  const h = Math.round((cropRect.y1 - cropRect.y0) * scaleY);
  const name = cropPath.split("/").pop().split("?")[0];
  const btn = $("#cropOk"); btn.disabled = true; btn.textContent = "裁剪中…";
  try{
    await apiSend(`/api/receipts/crop?path=${encodeURIComponent(name)}`, "POST", {x, y, w, h});
    msg("已裁剪 ✅");
    closeCrop();
    const bust = `?t=${Date.now()}`;
    const clean = cropPath.split("?")[0];
    $("#rcpImg").src = clean + bust;
    document.querySelectorAll(`img[src*="${name}"]`).forEach(el => {
      el.src = el.src.split("?")[0] + bust;
    });
  }catch(e){ msg("裁剪失败：" + e.message, false); }
  finally{ btn.disabled = false; btn.textContent = "确认裁剪"; }
}
function showDetail(title, html, actions){
  if(daySelOn()) txSelExit(false);   // 连开下一份明细：先退出上一份的选择态
  $("#dayEditBtn").style.display = "none";   // 载入中先藏，取数渲染完由 dayEditVis 按内容显隐
  $("#dayTitle").textContent = title;
  $("#daySummary").textContent = "";
  const a = $("#dayActions");
  a.style.display = actions ? "block" : "none";
  a.innerHTML = actions || "";
  $("#dayMask").classList.add("on");
  $("#dayList").innerHTML = html;
}
function renderDetail(items, sums){
  const absorbed = absorbedIds(items);
  /* 表头合计用服务端全量聚合 r.sums（前端只 sum 当前页会截断；接口恒带 sums——
     /api/transactions 无条件返回三值聚合）；退款>0 显示三项对账 */
  const e = sums.expense || 0, i = sums.income || 0, rf = sums.refund || 0;
  const headHtml = (e || i || rf)   // 全零（纯转账日）保持弹窗头空白，与旧口径一致
    ? [`<span class="c-exp">支出 ${fmt(e)}</span>`, `<span class="c-inc">收入 ${fmt(i)}</span>`]
      .concat(rf > 0 ? [`<span class="c-inc">退款 ${fmt(rf)}</span>`] : []).join("&nbsp;·&nbsp;")
    : "";
  const headText = `支出 ${fmt(e)} · 收入 ${fmt(i)}` + (rf > 0 ? ` · 退款 ${fmt(rf)}` : "");
  $("#daySummary").innerHTML = headHtml;
  return `<div class="sub" style="margin-bottom:6px">${headText} · 点击编辑</div>` +
    (items.filter(x=>!absorbed.has(x.id)).map(txRow).join("") || '<div class="sub">暂无流水</div>');
}
const dayGen = makeSeq();   // 明细弹窗请求代数（makeSeq 单源）：连开下一份明细/切账本触发 dayRefresh 时，
                            // 迟到的旧响应整包丢弃（同 loadTx txGen 口径）
async function fillDayDetail(fetcher, keep=false){   // 明细弹窗统一取数渲染（日/区间/分类/人情四个入口共用）
  // keep=数据刷新（长轮询推送/回前台触发 dayRefresh）：旧内容保持到新数据到手，不再闪「加载中…」；
  // 滚动位置还原，选择态存活（已删笔从选择集清洗），重渲染后由 txSelDecorate 重插勾选框。
  // 首开(keep=false)时 showDetail 已退出选择态，sel 恒 false，同一逻辑自然无感。
  const seq = dayGen.next();
  const body = document.querySelector("#dayMask .modal-body");
  const sc = keep ? body.scrollTop : 0;
  const sel = daySelOn();
  try{
    const r = await fetcher();
    if(!dayGen.isCurrent(seq)) return;   // 期间又发起了新的明细请求：本响应已过期，不回填 DOM/选择集（由新请求接管）
    if(sel){
      const live = new Set(r.items.map(x=>x.id));
      for(const id of [...txSelSet]) if(!live.has(id)) txSelSet.delete(id);
    }
    $("#dayList").innerHTML = renderDetail(r.items, r.sums);
    if(keep) body.scrollTop = sc;
    if(sel) txSelDecorate();
  }catch(e){
    if(!dayGen.isCurrent(seq)) return;   // 过期请求的失败不再提示/改写界面（新请求已接管）
    if(keep){ msg(`明细刷新失败：${e.message}`, false); }   // 刷新失败保留旧内容
    else $("#dayList").innerHTML = `<div class="sub">加载失败：${esc(e.message)}</div>`;
  }
  dayEditVis();   // 成败都按当前内容重估「编辑」按钮（空列表/加载失败均应隐藏）
}
async function showDay(dd, refresh){
  dayRefresh = () => showDay(dd, true);
  if(!refresh) showDetail(`${(+dd.slice(5,7))}月${(+dd.slice(8))}日 流水`, '<div class="sub">加载中…</div>',
    `<button class="btn btn-pri" style="width:100%" onclick="openAdd('${dd}')">＋ 记一笔</button>`);
  await fillDayDetail(()=>api(`/api/transactions?ledger=${lid()}&from=${dd}&to=${nextDayStr(dd)}&page=1&page_size=100`), refresh);
}
/* 图表/卡片下钻：拉流水明细 */
async function showTxDetail(title, params, refresh){
  dayRefresh = () => showTxDetail(title, params, true);
  if(!refresh) showDetail(title, '<div class="sub">加载中…</div>');
  // to 一律排他上界（调用方经 rangeDto/nextDayStr 单源转换），与统计/导出同口径
  const q = new URLSearchParams({ledger:lid(), page:1, page_size:100});
  for(const [k, v] of Object.entries(params)) q.set(k, v);
  await fillDayDetail(()=>api("/api/transactions?"+q), refresh);
}
function showMonthDetail(month, type){
  const to = nextMonthFirst(month);
  showTxDetail(`${month} ${type==="income"?"收入":type==="expense"?"支出":"全部"}明细`,
    {from: month+"-01", to, ...(type?{type}:{})});
}
async function showCategoryDetail(catName, range, type, refresh){
  dayRefresh = () => showCategoryDetail(catName, range, type, true);
  const rg = range || statRange || curStatRange();   // curStatRange 不收参（内部自取当前选择器值）
  const flow = type || "expense";
  if(!refresh) showDetail(`${rg.label} · ${catName} 明细`, '<div class="sub">加载中…</div>');
  // 「未分类」桶（后端 db.UNCATEGORIZED_NAME）没有分类实体，categories 里查不到 id：
  // 按 category=-1 过滤（服务端翻译为 category_id IS NULL），其余按名字解析分类 id
  const top = catName === "未分类" ? -1
    : (categories.find(c=>c.name===catName && !c.parent_id && c.type===flow)
       || categories.find(c=>c.name===catName && !c.parent_id) || {}).id;
  if(top === undefined){ $("#dayList").innerHTML = '<div class="sub">暂无流水</div>'; dayEditVis(); return; }
  // to 语义与 showTxDetail 一致（rangeDto 单源转排他上界，含排他位与含端位两种区间对象）
  // 未分类桶按 flow 侧过滤（type=支出/收入）：桶在饼图按 flow 分桶，不带 type 会连
  // 未分类的收入/转账行一起列出，下钻集合大于桶集合
  const typeQ = top === -1 ? `&type=${flow}` : "";
  await fillDayDetail(()=>api(`/api/transactions?ledger=${lid()}&from=${rg.from}&to=${encodeURIComponent(rangeDto(rg))}`
    + `${typeQ}&category=${top}&page=1&page_size=500`), refresh);
}
function openAdd(dateStr){   // 新增交易：预填日期（日历日视图传入 dd），类型默认支出
  txOpen("create", {date: dateStr});
}
const openGen = makeSeq();   // openEdit 请求代数（makeSeq 单源）：连点两笔/切账本后到达的旧响应整包丢弃
const rfGen = makeSeq();     // 退款下拉联动代数：openEdit 与 refundRowSync **共享同一实例**——
                             // 两套独立计数器互不可见时，openEdit 的迟到候选会整体覆盖用户在
                             // refundRowSync 里刚选好的关联（静默回滚）；这是 makeSeq 实例可
                             // 显式共享的样板（勿改成每函数自建）
async function openEdit(tid){   // 编辑/待确认：载入该笔到统一弹窗
  const seq = openGen.next();
  const rfseq = rfGen.next();   // 顺带作废在途的 refundRowSync 响应（重开弹窗后其回填对象已是新弹窗）
  // 两种身份两道闸：openGen 防「连点两笔」，ledgerScope 防「拉取在途时切账本」——后者此前
  // 缺失：旧账本流水会开进新账本弹窗（分类/账户下拉已换表，旧 id 匹配不到显示未分类），
  // 用户顺手保存即把那笔旧流水的分类/账户全量 PUT 成空
  const sc = ledgerScope();
  const stale = ()=> !openGen.isCurrent(seq) || !sc.isCurrent();
  let t = txCache[tid];
  // 缓存可能是残缺/过期对象：优先拉服务端最新行覆盖缓存，失败回落缓存（防旧值 PUT 回滚他人修改）
  try{
    const r = await api(`/api/transactions?ledger=${sc.id}&ids=${tid}&page=1&page_size=1`);
    if(stale()) return;   // 已有更新的一次打开 / 已切账本：本响应作废（防错序/跨账本污染缓存）
    if(r.items && r.items[0] && r.items[0].id === tid){
      // id 回校：后端忽略 ids 参数时返回的不是目标行，错配会把编辑指向别的流水
      t = r.items[0]; txCache[tid] = t;
    }
  }catch(_){ if(stale()) return; /* 保留缓存兜底 */ }
  if(!t) return msg("流水不存在或加载失败，请刷新重试", false);   // 打不开要有反馈，不再静默假死
  const mode = t.need_confirm ? "confirm" : "edit";
  txOpen(mode, {tx:t,
    items:[{type:t.type, amount:t.amount, category_id:t.category_id, account_id:t.account_id,
      date:(t.date||"").slice(0,19), name:t.name||"", note:t.note||"", order_no:t.order_no||"", tags:t.tags||{}}]});
  if(t.type === "transfer" && (mode === "confirm" || t.refund_for)){
    // 近 50 笔支出里同日同额排最前；当前关联的原消费置顶（旧单可能不在近 50 笔里）
    $("#e_refund_row").style.display = "block";
    const day = (t.date||"").slice(0,10);
    const isM = x=>x.amount === t.amount && (x.date||"").slice(0,10) === day;
    const list = [...await fetchExpenseCands(day)].sort((a,b)=>isM(b)-isM(a));
    if(stale() || !rfGen.isCurrent(rfseq)) return;   // 又打开了另一笔/期间联动过：旧候选不回填
    // 也不作默认选中——按「原始订单 ≤ 退款当天」口径视为未关联，确认/保存时下发 0 清掉旧链
    const lateOrigin = t.refund_origin && (t.refund_origin.date||"").slice(0,10) > day;
    if(t.refund_origin && !lateOrigin && !list.some(x=>x.id === t.refund_origin.id)) list.unshift(t.refund_origin);
    renderRefundDD(list, lateOrigin ? 0 : (t.refund_for || 0), day);
  }else if(t.type === "income"){
    const day = (t.date||"").slice(0,10);
    const sameAmt = (await fetchExpenseCands(day, `&from=${day}`)).filter(x=>x.amount === t.amount && x.id !== t.id);
    if(stale() || !rfGen.isCurrent(rfseq)) return;   // 同上：旧候选不回填新弹窗
    if(sameAmt.length){
      $("#e_refund_row").style.display = "block";
      renderRefundDD(sameAmt, t.refund_for || 0, day);
    }
  }
}
let editRefundItems = [], editRefundSel = 0, editRefundDefault = [], editRefundCap = "";

function txItemCatChange(sel){
  const item = sel.closest(".txf-item"); if(!item) return;
  const cat = categories.find(c=>c.id === +sel.value);
  const wantTransfer = !!cat && cat.type === "transfer";
  const isTransfer = item.dataset.type === "transfer";
  if(wantTransfer && isTransfer){ refundRowSync(item); return; }   // 组内切换（退款⇄不计收支）：只联动关联行
  if(isTransfer && !wantTransfer){
    if(editTx && editTx.refund_for){   // 已关联退款的流水须保持不计收支（与类型 chip 同一条校验）
      msg("该流水已关联退款，须保持不计收支；请先在下方取消关联再改类型", false);
      return txItemRevertCat(item);
    }
    item.dataset.type = item.dataset.prevType || "expense";
  }else if(wantTransfer){
    item.dataset.prevType = item.dataset.type;
    item.dataset.type = "transfer";
  }else return;
  txItemSyncTypeRow(item);
  refundRowSync(item);
}
function txItemRevertCat(item){   // 校验拦下后把分类下拉拉回 transfer 组（保持与类型一致，选中「退款」）
  const rid = (categories.find(c=>c.type==="transfer" && c.name==="退款") || {}).id;
  item.querySelector(".txf-cat").innerHTML = txCatOptions("transfer", rid);
}
function txItemSyncTypeRow(item){   // 翻转后刷新类型行 chip 选中态 / 新建分类按钮 / 分类下拉选项
  const type = item.dataset.type;
  item.querySelectorAll(".txf-types .fchip").forEach(b=>b.classList.toggle("on", b.dataset.t === type));
  const cs = item.querySelector(".txf-cat");
  cs.innerHTML = txCatOptions(type, +cs.value);
  item.querySelector(".txf-newcat").style.display = type === "transfer" ? "none" : "";
}
// rfGen 声明在 openEdit 旁（openEdit/refundRowSync 共享同一实例，见其注释）
async function refundRowSync(item){
  // 单笔弹窗里：transfer + 分类=退款 → 显示「关联到原消费」并按当前填的日期拉封顶候选；否则收起。
  // 候选按拉取瞬间的日期封顶；之后再改日期需重选一次分类刷新（与编辑弹窗打开时拉一次同口径）
  const seq = rfGen.next();
  const cat = categories.find(c=>c.id === +item.querySelector(".txf-cat").value);
  const on = item.dataset.type === "transfer" && cat && cat.name === "退款"
             && document.querySelectorAll("#txItems .txf-item").length === 1;
  if(!on){ $("#e_refund_row").style.display = "none"; return; }
  $("#e_refund_row").style.display = "block";
  const day = (item.querySelector(".txf-date").value || today()).slice(0,10);
  const list = await fetchExpenseCands(day);
  if(!rfGen.isCurrent(seq)) return;   // 网络乱序：旧请求的候选/选中态不再回填（防陈旧 editRefundSel 滞留）
  let sel = txMode === "create" ? 0 : (editRefundSel || 0);
  if(txMode !== "create"){
    // 与 openEdit 同源：当前关联的原交易不在重拉候选里时置顶，保持显示真实关联——
    // 否则 UI 显示「不关联」、保存却仍提交旧 editRefundSel（所见非所得）；
    // 原交易晚于退款日的，按「原始订单 ≤ 退款当天」口径视为未关联（下发 0 清旧链）
    const ro = editTx && editTx.refund_origin;
    const lateOrigin = ro && (ro.date||"").slice(0,10) > day;
    if(lateOrigin) sel = 0;
    else if(ro && !list.some(x=>x.id === ro.id)) list.unshift(ro);
  }
  renderRefundDD(list, sel, day);
}
// nextDayStr 定义在 shared-lite.js（stats/ai/ledger 三方单源，方向不再倒挂）
// extra 附加 &from=（收入分支限同日）或 &q=（下拉搜索）
async function fetchExpenseCands(day, extra){
  const r = await api(`/api/transactions?ledger=${lid()}&type=expense${extra||""}&to=${nextDayStr(day)}&page=1&page_size=50`);
  return r.items;
}
document.addEventListener("click", e=>{
  if(e.target.id === "e_refund_dd"){ closeRefundDD(); return; }
  if(!e.target.closest("#e_refund_dd")) closeRefundDD();
});
window.addEventListener("resize", ()=>{
  // 不无条件 closeRefundDD：会夺走聚焦中搜索框的焦点、输入法立即收起
  positionRefundDD();
  syncScopeChips();   // 宽度变化重估范围条横向滚动渐隐提示（幂等）
  // 窗口变宽回到常驻侧栏布局时，清掉抽屉推挤状态（断点复用 core.js 的 mobileMQ，与 CSS 同口径）
  if(!mobileMQ.matches){ $("#sidebar").classList.remove("open"); $("#sideMask").classList.remove("on"); $(".main").classList.remove("pushed"); }
});
if(window.visualViewport) visualViewport.addEventListener("resize", positionRefundDD);   // iOS：输入法只动 visualViewport，不触发 window resize
function closeRefundDD(){ $("#e_refund_dd") && $("#e_refund_dd").classList.remove("open"); }
function toggleRefundDD(e){
  e && e.stopPropagation();
  const dd = $("#e_refund_dd");
  if(dd.classList.contains("open")){ closeRefundDD(); return; }
  renderRefundSelf();
  dd.classList.add("open");
  positionRefundDD();
}
function renderRefundSelf(){   // 弹窗顶卡=本笔退款交易：编辑/确认取 editTx，新建取条目表单当前值
  let name = "退款", amount = 0, sub = [];
  if(editTx){
    name = editTx.name || "退款"; amount = editTx.amount || 0;
    sub = [(editTx.date || "").slice(0, 16), editTx.note || "", editTx.order_no || ""];
  }else{
    const item = document.querySelector("#txItems .txf-item");
    if(item){
      name = (item.querySelector(".txf-name").value || "").trim() || "退款";
      amount = toCents(item.querySelector(".txf-amt").value);
      sub = [(item.querySelector(".txf-date").value || "").replace("T", " "),
             (item.querySelector(".txf-note").value || "").trim(),
             (item.querySelector(".txf-order").value || "").trim()];
    }
  }
  $("#rf_self").innerHTML = `<div class="rf-body">
      <div class="tx-note">本笔退款：${esc(name)}</div>
      <div class="sub tx-sub">${esc(sub.filter(Boolean).join(" · "))}</div>
    </div>
    <div class="tx-amt num c-inc">+${fmt(amount)}</div>`;
}
function positionRefundDD(){
  const dd = $("#e_refund_dd");
  if(dd && dd.classList.contains("open"))
    $("#rf_sheet").style.maxHeight = Math.round((window.visualViewport ? visualViewport.height : innerHeight) * 0.9) + "px";
}
function refundCard(x){
  // 无名称时备注顶到首行，第二行不再重复；字段一律转义（账单派生文本）
  const note = x.note || "";
  return `<div class="rf-body">
      <div class="tx-note">${esc((x.name||note||"").slice(0,40))}</div>
      <div class="sub tx-sub">${[esc((x.date||"").slice(0,16)), x.name ? esc(note) : "", esc(x.order_no||"")].filter(Boolean).join(" · ")}</div>
    </div>
    <div class="tx-amt num c-exp">−${fmt(x.amount)}</div>`;
}
function renderRefundDD(items, selected, capDay){
  _rfQuery = "";   // 每次打开重置搜索词，避免上次残留与默认列表不一致
  editRefundCap = (capDay||"").slice(0,10);
  editRefundDefault = items; editRefundItems = items; editRefundSel = selected;
  $("#e_refund_opts").innerHTML =
    `<div class="ai-box rf-qbox" onclick="event.stopPropagation()">
       <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="flex:0 0 16px;opacity:.55"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
       <input id="rf_q" placeholder="搜索全部支出：名称/备注/金额/商户/订单号" value="" oninput="refundSearch(this.value)">
     </div><div id="rf_list"></div>`;
  renderRefundList(false);
  renderRefundCur();
  closeRefundDD();
}
function renderRefundList(searching){
  const hint = searching ? `搜索结果 ${editRefundItems.length} 笔` : "同日同额匹配优先；找不到时输入关键词查全部支出";
  $("#rf_list").innerHTML = `<div class="rf-hint sub">${hint}</div>`
    + `<div class="rf-item ${!editRefundSel?"on":""}" onclick="setRefundDD(0,event)"><div class="rf-body sub">不关联</div></div>`
    + editRefundItems.map(x=>`<div class="rf-item ${x.id===editRefundSel?"on":""}" onclick="setRefundDD(${x.id},event)">${refundCard(x)}</div>`).join("");
}
let _rfQuery = "", _rfTimer = null;
const rfSearchGen = makeSeq();   // 下拉搜索代数（makeSeq 单源）：防抖后仍可能乱序
async function refundSearch(q){   // 空关键词回默认匹配列表；否则防抖搜全部支出（名称/备注/商户/订单号）
  _rfQuery = q;
  clearTimeout(_rfTimer);
  if(!q.trim()){
    rfSearchGen.next();   // 作废在途的旧关键词响应：否则其 seq 仍是最新，返回后覆盖回搜索结果（输入框却已空）
    editRefundItems = editRefundDefault; renderRefundList(false); return;
  }
  const seq = rfSearchGen.next();
  _rfTimer = setTimeout(async ()=>{
    try{
      const items = await fetchExpenseCands(editRefundCap, `&q=${encodeURIComponent(q.trim())}`);
      if(!rfSearchGen.isCurrent(seq) || _rfQuery !== q) return;   // 双保险：防抖乱序或关键词已变（含清空）都不回填
      editRefundItems = items;
      renderRefundList(true);
    }catch(e){ console.warn("退款候选搜索失败", e); }   // 静默为空会让用户误以为无结果
  }, 250);
}
function setRefundDD(id, e){
  e && e.stopPropagation();
  editRefundSel = id;
  document.querySelectorAll("#e_refund_opts .rf-item").forEach(x=>x.classList.remove("on"));
  e && e.currentTarget && e.currentTarget.classList.add("on");
  renderRefundCur();
  closeRefundDD();
}
function renderRefundCur(){
  const x = editRefundItems.find(i=>i.id===editRefundSel);
  $("#e_refund_cur").innerHTML = x ? refundCard(x) : `<div class="rf-body sub">不关联</div>`;
}
async function saveEditTx(confirm){
  const t = editTx; if(!t) return;
  const it = txCollect()[0] || {};
  if(!it.amount) return msg("金额不能为空", false);
  if(t.refund_for && (it.type || t.type) !== "transfer")
    return msg("该流水已关联退款，须保持不计收支；请先在下方取消关联再改类型", false);
  const saveBtn = $("#e_save_btn");
  if(saveBtn.disabled) return;   // 双击防重：PUT 在途时忽略再点（txSubmitCreate 同款）
  saveBtn.disabled = true;
  const tags = Object.assign({}, t.tags||{});   // 保留导入时已有的标签
  // 保留原始归属（导入流水的 liulu 明细不被「老公」覆盖，统计组内仍见各平台构成）
  const origRaw = tags.attribution || "";
  const origShow = origRaw ? (raw2group[origRaw] || origRaw) : "";
  const finalAttr = it.attr && it.attr === origShow ? (origRaw || it.attr) : it.attr;
  if(finalAttr) tags.attribution = finalAttr; else delete tags.attribution;
  const rfVis = $("#e_refund_row").style.display !== "none";   // 关联下拉可见=本笔是退款关联场景
  const body = {type:it.type || t.type, amount:it.amount, category_id:it.category_id,
    account_id:it.account_id, to_account_id:t.to_account_id||null,
    name:it.name,
    order_no:it.order_no,
    refund_for: rfVis ? (editRefundSel||0) : null,
    date:it.date, note:it.note, tags,
    // receipt_path：null=未改（COALESCE 保留），""=清空（NULLIF 转 NULL），非空=覆盖
    receipt_path: eRcp === null ? null : eRcp,
    need_confirm: !!t.need_confirm};
  try{
    await apiSend(`/api/transactions/${t.id}`, "PUT", body);
    if(confirm){
      const rb = {};
      if(it.category_id) rb.category_id = it.category_id;
      if(it.account_id && it.account_id !== t.account_id) rb.account_id = it.account_id;
      if(rfVis) rb.refund_for = editRefundSel || 0;
      await apiSend(`/api/pending/${t.id}/resolve`, "POST", rb);
      msg("已确认 ✅");
    }else{
      msg("已保存 ✅");
    }
    // 聊天里的确认卡/查账卡不在 afterTxWrite 刷新范围（只刷列表/统计/今日卡）：按 id
    // 回读服务端重渲（refund 强转 transfer 等派生值由后端算，不再手拼合并对象）
    aiSyncTxCards([t.id]);
    txClose(); afterTxWrite();
  }catch(e){
    msg("保存失败："+e.message, false);
  }finally{
    saveBtn.disabled = false;   // 成功路径弹窗已关（重开即新弹窗，复位无副作用）；失败路径恢复可再试
  }
}
async function deleteEditTx(){
  const t = editTx; if(!t) return;
  if(!await uiConfirm("确定删除这笔交易？", "删除")) return;
  await api(`/api/transactions/${t.id}`, {method:"DELETE"});
  aiPurgeTxCard(t.id);
  msg("已删除"); txClose(); afterTxWrite();
}

/* ===== 人情往来 ===== */
let rqList = [];   // 全量汇总（已按拼音排序）；搜索只前端过滤不重拉
async function loadRenqing(){
  const sc = ledgerScope();   // 账本身份守卫（ledgerScope 单源）：await 期间切账本，旧账本人情表不写全局
  const rs = await api(`/api/stats/renqing?ledger=${sc.id}`);
  if(!sc.isCurrent()) return;
  rqList = rs;
  rqList.sort((a,b)=>a.person.localeCompare(b.person, "zh-Hans-CN-u-co-pinyin"));   // 名称按拼音首字母（环境不支持时自动回退默认排序）
  renderRenqing();
}
function renderRenqing(){
  const q = $("#rq_q").value.trim();
  const rq = rqList.filter(r=>!q || r.person.includes(q));
  document.querySelector("#renqingTable tbody").innerHTML = rq.map(r=>{
    const bal = r.received - r.given;   // 差额符号按净头寸：−=我多给出未收回，+=我收到更多（与后端 balance=given-received 相反）
    return `<tr style="cursor:pointer" onclick="showPersonDetail('${qEsc(r.person)}')">
    <td><b>${esc(r.person)}</b></td>
    <td class="num c-exp" style="text-align:right">${fmt(r.given)}</td>
    <td class="num c-inc" style="text-align:right">${fmt(r.received)}</td>
    <td class="num" style="text-align:right;font-weight:600;color:${bal>0?"var(--income)":bal<0?"var(--expense)":"var(--muted)"}">${bal>0?"+"+fmt(bal):bal<0?"−"+fmt(-bal):"0"}</td></tr>`;
  }).join("");
  $("#renqingEmpty").style.display = rq.length ? "none" : "block";
  $("#renqingTable").style.display = rq.length ? "" : "none";
  $("#renqingEmpty").textContent = rqList.length ? `没有匹配「${q}」的名称` : "暂无人情记录：分类为「人情往来」的流水会按名称显示在这里";   // textContent 不解析 HTML，无需 esc（套上会显示 &amp; 等实体）
}
bindSearch($("#rq_q"), $("#rq_q_clear"), renderRenqing);
async function showPersonDetail(person, refresh){
  dayRefresh = () => showPersonDetail(person, true);
  if(!refresh) showDetail(`${person} · 人情明细`, '<div class="sub">加载中…</div>');
  await fillDayDetail(()=>api(`/api/transactions?ledger=${lid()}&page=1&page_size=200&renqing_name=${encodeURIComponent(person)}`), refresh);
}

let txSelMode = false;
let txSelHost = "#txList";   // 选择态宿主容器：主列表 #txList / 明细弹窗 #dayList（共用 txSelSet 与批量修改弹窗）
const txSelSet = new Set();

// 明细弹窗选择态进行中（closeDay/showDetail 退出、dayEditVis 让位共用）
const daySelOn = () => txSelMode && txSelHost === "#dayList";

// 明细弹窗右上「编辑」按钮：有可选流水才露出；选择态进行中让位给钉顶工具条
function dayEditVis(){
  $("#dayEditBtn").style.display = !daySelOn() && document.querySelector("#dayList .tx-card[data-txid]") ? "" : "none";
}

function txSelEnter(host){
  txSelHost = host || "#txList";
  txSelMode = true; txSelSet.clear();
  txSelVis(true);
  txSelDecorate();
}
// 退出选择态：refresh=false 供已另行触发全局刷新的路径（afterTxWrite 内含 loadTx/dayRefresh）复用
function txSelExit(refresh = true){
  const day = txSelHost === "#dayList";
  txSelMode = false; txSelSet.clear();
  txSelVis(false);   // 先按当前宿主还原工具条/编辑按钮/筛选行，再清宿主
  txSelHost = "#txList";
  if(day){   // 弹窗内列表需重拉去掉勾选框、恢复点击编辑（重渲染走 fillDayDetail→dayEditVis）
    if(refresh && dayRefresh && $("#dayMask").classList.contains("on")) dayRefresh();
    return;
  }
  if(refresh) loadTx();   // 重渲染去掉勾选框、恢复点击编辑
}
// 选择态显隐（双宿主）：弹窗=钉顶工具条上、编辑按钮让位；主列表=工具条上、筛选行与搜索框下
function txSelVis(on){
  if(txSelHost === "#dayList"){
    $("#daySelBar").style.display = on ? "flex" : "none";
    dayEditVis();
    return;
  }
  $("#txSelBar").style.display = on ? "flex" : "none";
  txToolbar().style.display = on ? "none" : "";
  if(on) txSearchToggle(false);   // 选择态：筛选行整体让位（搜索框在内），收起搜索态
}
// 当前容器卡片 id（全选与全选框状态共用；已被退款吸收的原交易不渲染，天然不在内）
function txSelPageIds(){
  return [...document.querySelectorAll(txSelHost + " .tx-card[data-txid]")].map(c=>+c.dataset.txid);
}
// 渲染后装饰（loadTx / 弹窗重渲染后调用，DOM 每次都是新渲染）：给每张卡插勾选框并接管点击
function txSelDecorate(){
  if(!txSelMode) return;
  document.querySelectorAll(txSelHost + " .tx-card").forEach(card=>{
    const id = +card.dataset.txid;
    if(!id) return;
    const cb = document.createElement("label");
    cb.className = "tx-sel";
    cb.innerHTML = `<input type="checkbox" ${txSelSet.has(id) ? "checked" : ""}>`;
    cb.onclick = e => e.stopPropagation();
    cb.querySelector("input").onchange = e => txSelToggle(id, e.target.checked);
    const rowHost = card.querySelector(".tx-row") || card;   // 双行卡是块状：勾选框插进行内才不换行
    rowHost.insertBefore(cb, rowHost.firstChild);
    // 点击=切换勾选：接管卡片 onclick；退款双行卡内自带独立 onclick 的行也一并接管
    // （接管处理器必须 stopPropagation——否则行处理+冒泡到卡片处理会切换两次，净效果=没选中）
    const toggle = () => { const i = cb.querySelector("input"); i.checked = !i.checked; txSelToggle(id, i.checked); };
    card.onclick = toggle;
    card.querySelectorAll(".tx-row[onclick], .tx-origin[onclick]").forEach(r => r.onclick = e => { e.stopPropagation(); toggle(); });
  });
  txSelSyncUI();
}
function txSelToggle(id, on){
  on ? txSelSet.add(id) : txSelSet.delete(id);
  txSelSyncUI();
}
function txSelToggleAll(on){
  txSelPageIds().forEach(id => on ? txSelSet.add(id) : txSelSet.delete(id));
  document.querySelectorAll(txSelHost + " .tx-sel input").forEach(i => i.checked = on);
  txSelSyncUI();
}
function txSelSyncUI(){
  const p = txSelHost === "#dayList" ? "daySel" : "txSel";   // 两套工具条 id 前缀：主列表 / 明细弹窗
  $("#" + p + "Count").textContent = `已选 ${txSelSet.size} 笔`;
  $("#" + p + "ModBtn").disabled = !txSelSet.size;
  $("#" + p + "DelBtn").disabled = !txSelSet.size;
  const pageIds = txSelPageIds();
  $("#" + p + "All").checked = pageIds.length > 0 && pageIds.every(id => txSelSet.has(id));
}
async function txSelDelete(){
  if(!txSelSet.size) return;
  if(!await uiConfirm(`确定删除已选 ${txSelSet.size} 笔流水？删除原消费会自动解除其退款关联`, "删除")) return;
  try{
    const r = await apiSend(`/api/transactions/batch_delete?ledger=${lid()}`, "POST", {ids:[...txSelSet]});
    txSelSet.forEach(id => aiPurgeTxCard(id));   // 聊天区旧卡清理（已删笔不再回查）
    msg(`已删除 ${r.deleted} 笔`);
    txSelExit(false);   // 数据已变：afterTxWrite 全局刷新（内含选择集已删笔清洗）
    afterTxWrite();
  }catch(e){
    msg("批量删除失败：" + e.message, false);
  }
}
function txSelCloseModal(){ $("#txSelMask").classList.remove("on"); }
function txSelOpenModal(){
  if(!txSelSet.size) return;
  $("#txSelModHint").textContent = `将对已选 ${txSelSet.size} 笔流水应用以下修改（留「不修改」的项保持原值）`;
  // 分类：按已选流水的实际类型给对应分类——全支出→只列支出分类，全收入→只列收入分类；
  // 跨支出/收入（或含转账/退款等不计收支）时类型无法统一，分类锁定「不修改」不可选
  const selTypes = [...new Set([...txSelSet].map(id => (txCache[id]||{}).type))];
  const oneType = selTypes.length === 1 && (selTypes[0] === "expense" || selTypes[0] === "income") ? selTypes[0] : null;
  const catSel = $("#bs_cat");
  if(oneType){
    catSel.disabled = false;
    catSel.innerHTML = `<option value="">不修改</option>` +
      `<optgroup label="${oneType==="expense"?"支出":"收入"}">` +
      categories.filter(c=>!c.parent_id && c.type === oneType)
        .map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join("") + `</optgroup>`;
  }else{
    catSel.disabled = true;
    catSel.innerHTML = `<option value="">不修改（类型不一致，分类不可改）</option>`;
  }
  // 归属：分组名 + 未分组归属（与记账弹窗同口径），另给「清除归属」
  const attrNames = attrNamesAll();
  $("#bs_attr").innerHTML = `<option value="">不修改</option>` +
    (attrNames.length ? `<option value="__clear__">（清除归属）</option>` +
      attrNames.map(a=>`<option value="${esc(a)}">${esc(a)}</option>`).join("") : "");
  $("#bs_acc").innerHTML = `<option value="">不修改</option>` +
    accounts.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join("");
  $("#txSelMask").classList.add("on");
}
async function txSelApply(){
  const cat = +$("#bs_cat").value || null;
  const attrRaw = $("#bs_attr").value;
  const attr = attrRaw === "" ? null : (attrRaw === "__clear__" ? "" : attrRaw);
  const acc = +$("#bs_acc").value || null;
  if(cat === null && attr === null && acc === null) return msg("请至少选择一项要修改的内容", false);
  const btn = $("#bs_save"); btn.disabled = true;
  try{
    const r = await apiSend(`/api/transactions/batch_update?ledger=${lid()}`, "POST",
      {ids:[...txSelSet], category_id:cat, attribution:attr, account_id:acc});
    txSelCloseModal();
    msg(`已修改 ${r.updated} 笔 ✅`);
    // 聊天卡与 txCache 按服务端回读同步（txRow 顺手更新缓存：不在列表查询内的笔
    // 若缓存不更新，编辑弹窗的旧基线会被全量 PUT 回滚）；id 先快照，下面退出选择态会清集合
    const ids = [...txSelSet];
    txSelExit(false);   // 数据已变：afterTxWrite 全局刷新（内含 loadTx），无需再单独 loadTx
    afterTxWrite();
    aiSyncTxCards(ids);
  }catch(e){
    msg("批量修改失败：" + e.message, false);
  }finally{ btn.disabled = false; }
}