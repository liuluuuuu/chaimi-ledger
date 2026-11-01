/* ===== 流水 ===== */
let txPages = 1;

let txRecentDefault = true;

/* 搜索框三件套（输入防抖 / 回车立即且不换行 / ✕ 清空）：流水与人情页共用 */
function bindSearch(box, clearBtn, apply, delay){
  let timer = null;
  const run = now =>{ clearTimeout(timer); now ? apply() : timer = setTimeout(apply, delay || 350); };
  box.addEventListener("input", ()=>{ clearBtn.style.display = box.value ? "" : "none"; run(false); });
  box.addEventListener("keydown", e=>{ if(e.key === "Enter"){ e.preventDefault(); box.blur(); run(true); } });
  clearBtn.addEventListener("click", ()=>{ box.value = ""; clearBtn.style.display = "none"; run(true); });
}
bindSearch($("#q_text"), $("#q_clear"), ()=>{ txPageNo = 1; loadTx(); });
// 待确认流水不满一页时直接落空显示「暂无流水」（搜索/清空一直有重置页码，类型筛选没有）
function setTypeFilter(b){ curTypeFilter=b.dataset.ft; txPageNo=1; document.querySelectorAll("#tab-list .fchip[data-ft]").forEach(x=>x.classList.toggle("on",x===b)); loadTx(); }
let _loadTxSeq = 0;   // 请求代数：快速切月份/范围/账本时，迟到的旧响应整包丢弃（不回填 DOM/txCache）
async function loadTx(){
  const seq = ++_loadTxSeq;
  $("#ftBase").textContent = txRecentDefault ? "最近" : "全部";
  const q = new URLSearchParams({ledger:lid(), page:txPageNo, page_size: txRecentDefault ? 20 : 50});
  const qt = $("#q_text").value.trim();
  // 最近态无翻页（纯速览），仅搜索时出底部翻页（#txPager）以便看全匹配结果。
  const showPager = !txRecentDefault || !!qt;
  document.querySelectorAll("#tab-list .tx-type-only").forEach(x=>x.style.display = txRecentDefault ? "none" : "");
  $("#txPager").style.display = showPager ? "" : "none";
  $("#tab-list").style.paddingBottom = showPager ? "80px" : "";
  if(qt) q.set("q", qt);
  // 跨月份的导入流水都可能待确认，忽略时间范围，否则列表为空
  const confirmOnly = curTypeFilter === "confirm";
  if(confirmOnly) q.set("need_confirm","1");
  else{
    if(curTypeFilter) q.set("type", curTypeFilter);
    // 用过范围选择器后按所选范围（范围=「全部」时仍不带 from/to）。
    // 顶部汇总条始终走范围口径（loadSumbarList），与列表态无关
    if(!txRecentDefault && appScope() !== "all"){
      const r = statRange || curStatRange();
      q.set("from", r.from);
      // 自定义区间末端含当天（后端膨胀 23:59:59）；月/年的 to 为下一周期排他界，走 to_exclusive=1
      //（旧版补 " 00:00:00" 会把 10 位纯日期的边界日手工流水多算进来）
      q.set("to", r.to);
      if(appScope() !== "custom") q.set("to_exclusive", "1");
    }
  }
  let r = await api("/api/transactions?"+q);
  if(seq !== _loadTxSeq) return;   // 期间又发起了新 loadTx（切月/范围/账本）：本响应已过期
  // 会停在不存在的页上，指示器出现「N 笔 · 第 X 页」却列空——回拉到末页重查一次
  let pages = Math.max(1, Math.ceil(r.total / (r.page_size || 50)));
  if(txPageNo > pages){
    txPageNo = pages;
    q.set("page", txPageNo);
    r = await api("/api/transactions?"+q);
    if(seq !== _loadTxSeq) return;
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
    return `
    <div style="padding:6px 6px 0">
      <div class="day-head"><span>${d}</span><span class="num">${daySum(items, absorbed)}</span></div>
      <div class="card" style="padding:6px">${items.filter(x=>!absorbed.has(x.id)).map(txRow).join("")}</div>
    </div>`;
  }).join("") ||
    `<div class="empty"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/></svg>暂无流水</div>`;
  txSelDecorate();
  txFilterPad();   // 筛选区 fixed 脱流：按当前 chips/搜索框实际高度重算列表顶部留白
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
      }
    } else if(t.type === "expense"){
      e += t.amount;
    } else if(t.type === "income"){
      i += t.amount;
    }
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
  // .main 是滚动容器（body 锁滚动）。筛选区 fixed 钉顶、rect 恒定，
  // 翻页后让 #txList 顶端落在钉顶区下缘正下方（省去复算顶栏高+筛选区高的 calc 算式）
  const sc = $(".main");
  const top = sc.scrollTop + $("#txList").getBoundingClientRect().top
            - $("#tab-list .tx-filter").getBoundingClientRect().bottom - 4;
  sc.scrollTo({top:Math.max(0,top), behavior:"smooth"});
}
async function exportReceipts(){
  const f = dtGet($("#exp_from")), t = dtGet($("#exp_to"));
  let url = `api/receipts/export?ledger=${lid()}`;
  if(f) url += `&from=${f}`;
  if(t) url += `&to=${t}`;
  try{
    const r = await fetch(url);
    if(!r.ok){
      const e = await r.json().catch(() => ({}));
      return msg(e.detail || "下载失败", false);
    }
    const blob = await r.blob();
    // 文件名：chaimi-ledger-账本名小票-日期.zip（账本名去掉图标前缀）
    const lg = await curLedger();
    const t = new Date();
    const stamp = `${t.getFullYear()}${pad2(t.getMonth()+1)}${pad2(t.getDate())}`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `chaimi-ledger-${(lg && lg.name || "").trim() || "账本"}小票-${stamp}.zip`;
    a.click();
    URL.revokeObjectURL(a.href);
    msg("小票已打包下载 ✅");
  }catch(e){ msg("下载失败：" + e.message, false); }
}
function exportCsv(fmt="csv"){
  const f = dtGet($("#exp_from")), t = dtGet($("#exp_to"));
  let url = `api/export?ledger=${lid()}&format=${fmt}`;
  if(f) url += `&from=${f}`;
  if(t) url += `&to=${t}`;
  location.href = url;
}
// 导出页快捷区间：全部/本月/本年（默认全部=数据库流水起始日至今天）
function expInitRange(){ expSetRange("all"); }
async function expSetRange(mode){
  let f = today(), t = today();   // 兜底：库为空时今天~今天
  if(mode === "month") f = thisMonth() + "-01";
  else if(mode === "year") f = thisMonth().slice(0, 4) + "-01-01";
  else try{   // all：查最早流水月
    const trend = await api(`/api/stats/trend?ledger=${lid()}&from=2000-01-01&to=2100-01-01`);
    if(trend.length) f = trend[0].month + "-01";
  }catch(_){}
  dtSet($("#exp_from"), f); dtSet($("#exp_to"), t);
  document.querySelectorAll(".exp-presets .btn").forEach(b =>
    b.classList.toggle("on", b.dataset.mode === mode));
}

/* ===== 编辑/确认弹窗 ===== */
let curDay = "";
let dayRefresh = null;
function closeDay(){
  if(daySelOn()) txSelExit(false);   // 弹窗整个关掉：静默退出选择态（无需重拉）
  $("#dayMask").classList.remove("on"); curDay = ""; dayRefresh = null;
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
  wrap.onpointerdown = e => { e.preventDefault(); start = cropPos(e); cropRect = null; };
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
function renderDetail(items){
  const absorbed = absorbedIds(items);
  const {e, i} = netSum(items, absorbed);
  $("#daySummary").innerHTML =
    e && i ? `<span class="c-exp">支出 ${fmt(e)}</span>&nbsp;·&nbsp;<span class="c-inc">收入 ${fmt(i)}</span>`
    : e ? `<span class="c-exp">支出 ${fmt(e)}</span>`
    : i ? `<span class="c-inc">收入 ${fmt(i)}</span>` : "";
  return `<div class="sub" style="margin-bottom:6px">支出 ${fmt(e)} · 收入 ${fmt(i)} · 点击编辑</div>` +
    (items.filter(x=>!absorbed.has(x.id)).map(txRow).join("") || '<div class="sub">暂无流水</div>');
}
async function fillDayDetail(fetcher, keep=false){   // 明细弹窗统一取数渲染（日/区间/分类/人情四个入口共用）
  // keep=数据刷新（长轮询推送/回前台触发 dayRefresh）：旧内容保持到新数据到手，不再闪「加载中…」；
  // 滚动位置还原，选择态存活（已删笔从选择集清洗），重渲染后由 txSelDecorate 重插勾选框。
  // 首开(keep=false)时 showDetail 已退出选择态，sel 恒 false，同一逻辑自然无感。
  const body = document.querySelector("#dayMask .modal-body");
  const sc = keep ? body.scrollTop : 0;
  const sel = daySelOn();
  try{
    const r = await fetcher();
    if(sel){
      const live = new Set(r.items.map(x=>x.id));
      for(const id of [...txSelSet]) if(!live.has(id)) txSelSet.delete(id);
    }
    $("#dayList").innerHTML = renderDetail(r.items);
    if(keep) body.scrollTop = sc;
    if(sel) txSelDecorate();
  }catch(e){
    if(keep){ msg(`明细刷新失败：${e.message}`, false); }   // 刷新失败保留旧内容
    else $("#dayList").innerHTML = `<div class="sub">加载失败：${esc(e.message)}</div>`;
  }
  dayEditVis();   // 成败都按当前内容重估「编辑」按钮（空列表/加载失败均应隐藏）
}
async function showDay(dd, refresh){
  curDay = dd;
  dayRefresh = () => showDay(dd, true);
  if(!refresh) showDetail(`${(+dd.slice(5,7))}月${(+dd.slice(8))}日 流水`, '<div class="sub">加载中…</div>',
    `<button class="btn btn-pri" style="width:100%" onclick="openAdd('${dd}')">＋ 记一笔</button>`);
  await fillDayDetail(()=>api(`/api/transactions?ledger=${lid()}&from=${dd}&to=${dd}&page=1&page_size=100`), refresh);
}
/* 图表/卡片下钻：拉流水明细 */
async function showTxDetail(title, params, refresh){
  curDay = "";
  dayRefresh = () => showTxDetail(title, params, true);
  if(!refresh) showDetail(title, '<div class="sub">加载中…</div>');
  // to 语义二选一：toExclusive=排他上界（下一周期首日裸日期，to_exclusive=1 服务端原样比较，
  // 对 10 位纯日期与 19 位带时间流水都正确排他）；默认=含当天的结束日（10 位裸日期，
  // 服务端膨胀为当天 23:59:59）。旧版补 " 00:00:00" 会把 10 位的边界日手工流水多算进来。
  const q = new URLSearchParams({ledger:lid(), page:1, page_size:100});
  for(const [k, v] of Object.entries(params)){ if(k !== "toExclusive") q.set(k, v); }
  if(params.toExclusive) q.set("to_exclusive", "1");
  await fillDayDetail(()=>api("/api/transactions?"+q), refresh);
}
function showMonthDetail(month, type){
  const to = nextMonthFirst(month);
  showTxDetail(`${month} ${type==="income"?"收入":type==="expense"?"支出":"全部"}明细`,
    {from: month+"-01", to, toExclusive:true, ...(type?{type}:{})});
}
async function showCategoryDetail(catName, range, type, refresh){
  dayRefresh = () => showCategoryDetail(catName, range, type, true);
  const rg = range || statRange || curStatRange(dtGet($("#monthSel")) || thisMonth());
  const flow = type || "expense";
  if(!refresh) showDetail(`${rg.label} · ${catName} 明细`, '<div class="sub">加载中…</div>');
  // 旧的「拉区间前 200 条再前端过滤」在区间流水多时会漏掉该分类的流水。
  const top = categories.find(c=>c.name===catName && !c.parent_id && c.type===flow)
           || categories.find(c=>c.name===catName && !c.parent_id);
  if(!top){ $("#dayList").innerHTML = '<div class="sub">暂无流水</div>'; dayEditVis(); return; }
  // to 语义与 showTxDetail 一致（rangeExcl 单源）：排他 → to_exclusive=1 裸日期；
  // 仅自定义区间的 to 是含当天的结束日 → 裸日期交服务端膨胀 23:59:59
  const excl = rangeExcl(rg);
  await fillDayDetail(()=>api(`/api/transactions?ledger=${lid()}&from=${rg.from}&to=${encodeURIComponent(rg.to)}`
    + `${excl ? "&to_exclusive=1" : ""}&category=${top.id}&page=1&page_size=500`), refresh);
}
function openAdd(dateStr){   // 新增交易：预填日期（日历日视图传入 dd），类型默认支出
  txOpen("create", {date: dateStr});
}
async function openEdit(tid){   // 编辑/待确认：载入该笔到统一弹窗
  const t = txCache[tid]; if(!t) return;
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
    // 也不作默认选中——按「原始订单 ≤ 退款当天」口径视为未关联，确认/保存时下发 0 清掉旧链
    const lateOrigin = t.refund_origin && (t.refund_origin.date||"").slice(0,10) > day;
    if(t.refund_origin && !lateOrigin && !list.some(x=>x.id === t.refund_origin.id)) list.unshift(t.refund_origin);
    renderRefundDD(list, lateOrigin ? 0 : (t.refund_for || 0), day);
  }else if(t.type === "income"){
    const day = (t.date||"").slice(0,10);
    const sameAmt = (await fetchExpenseCands(day, `&from=${day}`)).filter(x=>x.amount === t.amount && x.id !== t.id);
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
async function refundRowSync(item){
  // 单笔弹窗里：transfer + 分类=退款 → 显示「关联到原消费」并按当前填的日期拉封顶候选；否则收起。
  // 候选按拉取瞬间的日期封顶；之后再改日期需重选一次分类刷新（与编辑弹窗打开时拉一次同口径）
  const cat = categories.find(c=>c.id === +item.querySelector(".txf-cat").value);
  const on = item.dataset.type === "transfer" && cat && cat.name === "退款"
             && document.querySelectorAll("#txItems .txf-item").length === 1;
  if(!on){ $("#e_refund_row").style.display = "none"; return; }
  $("#e_refund_row").style.display = "block";
  const day = (item.querySelector(".txf-date").value || today()).slice(0,10);
  renderRefundDD(await fetchExpenseCands(day), txMode === "create" ? 0 : (editRefundSel || 0), day);
}
function nextDayStr(day){   // 裸日期 +1 天：作 to_exclusive=1 的排他上界，等效「≤ day 含当天」（本地拼接，避免 toISOString 的 UTC 偏移）
  const nd = new Date(day + "T00:00:00"); nd.setDate(nd.getDate()+1);
  return `${nd.getFullYear()}-${pad2(nd.getMonth()+1)}-${pad2(nd.getDate())}`;
}
// extra 附加 &from=（收入分支限同日）或 &q=（下拉搜索）
async function fetchExpenseCands(day, extra){
  const r = await api(`/api/transactions?ledger=${lid()}&type=expense${extra||""}&to=${nextDayStr(day)}&to_exclusive=1&page=1&page_size=50`);
  return r.items;
}
document.addEventListener("click", e=>{
  if(e.target.id === "e_refund_dd"){ closeRefundDD(); return; }
  if(!(e.target.closest && e.target.closest("#e_refund_dd"))) closeRefundDD();
});
window.addEventListener("resize", ()=>{
  // 旧版此处无条件 closeRefundDD 会把聚焦中的搜索框 display:none 夺焦、输入法立即收起
  positionRefundDD();
  txFilterPad();   // 宽度变化 chips 可能换行，筛选区（fixed）高度变了要重算列表留白
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
      <div class="sub tx-sub">${[(x.date||"").slice(0,16), x.name ? esc(note) : "", esc(x.order_no||"")].filter(Boolean).join(" · ")}</div>
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
       <input id="rf_q" placeholder="搜索全部支出：名称/备注/金额/商户/订单号" value="${esc(_rfQuery)}" oninput="refundSearch(this.value)">
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
async function refundSearch(q){   // 空关键词回默认匹配列表；否则防抖搜全部支出（名称/备注/商户/订单号）
  _rfQuery = q;
  clearTimeout(_rfTimer);
  if(!q.trim()){ editRefundItems = editRefundDefault; renderRefundList(false); return; }
  _rfTimer = setTimeout(async ()=>{
    try{
      editRefundItems = await fetchExpenseCands(editRefundCap, `&q=${encodeURIComponent(q.trim())}`);
      renderRefundList(true);
    }catch(_){}
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
    // 聊天里的确认卡/查账卡不在 afterTxWrite 刷新范围（只刷列表/统计/今日卡），保存后仍显旧值——
    // 以原流水 t 为底按表单值重渲（删除路径 aiPurgeTxCard 同款）；_link_refund 会把关联退款强制为 transfer
    const linked = rfVis ? (editRefundSel || 0) : (t.refund_for || 0);
    const cat = categories.find(c=>c.id===it.category_id);
    const acc = accounts.find(a=>a.id===it.account_id);
    aiRefreshTxCard(Object.assign({}, t, {
      type: linked ? "transfer" : it.type, amount: it.amount, date: it.date,
      name: it.name || "", note: it.note || "", order_no: it.order_no || "",
      category_name: cat ? cat.name : "", account_name: acc ? acc.name : "",
      receipt_path: editRcp(), tags, need_confirm: confirm ? false : t.need_confirm,
      refund_for: linked || null, parent_category: null,
      refund_confidence: linked ? (confirm ? "high" : "") : t.refund_confidence,
      refund_origin: linked ? (editRefundItems.find(x=>x.id===linked) || t.refund_origin || null) : null}));
    txClose(); afterTxWrite();
  }catch(e){
    msg("保存失败："+e.message, false);
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
  rqList = await api(`/api/stats/renqing?ledger=${lid()}`);
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
// 选择态显隐（双宿主）：弹窗=钉顶工具条上、编辑按钮让位；主列表=工具条上、筛选行（含编辑按钮）与搜索框下
function txSelVis(on){
  if(txSelHost === "#dayList"){
    $("#daySelBar").style.display = on ? "flex" : "none";
    dayEditVis();
    return;
  }
  $("#txSelBar").style.display = on ? "flex" : "none";
  document.querySelector("#tab-list .tx-filter .filters").style.display = on ? "none" : "";
  document.querySelector("#txSearchBox").style.display = on ? "none" : "";
  txFilterPad();
}
// 筛选区已 fixed 脱流：按其实时高度给列表补顶部留白（chips 换行/类型切换/选择态切换后都要重算）
function txFilterPad(){
  $("#tab-list").style.paddingTop = ($("#tab-list .tx-filter").offsetHeight + 12) + "px";
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
    card.insertBefore(cb, card.firstChild);
    // 点击=切换勾选：接管卡片 onclick；双行卡里原交易行自带独立 onclick 也一并接管。
    // 注意不能给普通 .tx-row 也挂——行处理器+冒泡到卡片处理器会切换两次，净效果=没选中
    const toggle = () => { const i = cb.querySelector("input"); i.checked = !i.checked; txSelToggle(id, i.checked); };
    card.onclick = toggle;
    card.querySelectorAll(".tx-row[onclick]").forEach(r => r.onclick = toggle);
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
    // 批量改动同步聊天卡与 txCache：不在列表查询内的笔若缓存不更新，编辑弹窗的旧基线会被全量 PUT 回滚
    const over = {};
    if(cat) Object.assign(over, {category_id:cat, parent_category:null,
      category_name:(categories.find(c=>c.id===cat) || {}).name || ""});
    if(acc) Object.assign(over, {account_id:acc,
      account_name:(accounts.find(a=>a.id===acc) || {}).name || ""});
    txSelSet.forEach(id=>{
      const t = txCache[id]; if(!t) return;
      const o = Object.assign({}, t, over);
      if(attr !== null){   // null=不改；""=清除；非空=写入（须按笔保留其余 tags 键）
        o.tags = Object.assign({}, t.tags);
        attr ? o.tags.attribution = attr : delete o.tags.attribution;
      }
      aiRefreshTxCard(o);
    });
    txSelExit(false);   // 数据已变：afterTxWrite 全局刷新（内含 loadTx），无需再单独 loadTx
    afterTxWrite();
  }catch(e){
    msg("批量修改失败：" + e.message, false);
  }finally{ btn.disabled = false; }
}