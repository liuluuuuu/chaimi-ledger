/* ===== AI 记一笔 ===== */


function dupHintText(dh){
  if(!dh || !dh.kind) return "";
  if(dh.kind === "batch") return `与本批第 ${dh.no} 笔重复`;
  const o = dh.of || {};
  return `同日同额已有：${o.name || "（未命名）"} ¥${((o.amount || 0) / 100).toFixed(2)}${o.source ? `（来源 ${o.source}）` : ""}`;
}
function aiItems(list, noteText){   // AI 结果 → 弹窗条目：分类名/账户名映射为 id，摘要挂第一笔备注
  return list.map((it, i)=>{
    const type = ["expense","income","transfer"].includes(it.type) ? it.type : "expense";
    // 分类两段式匹配（与 bot 侧 _resolve_category 同口径）：先按类型精确名，再双向包含
    // ——LLM/词典给简称（「餐饮」）也能落到「餐饮美食」，不再直接掉到未分类
    const cat = it.category_name
      ? (categories.find(c=>c.type===type && !c.parent_id && c.name===it.category_name)
         || categories.find(c=>c.type===type && !c.parent_id
                            && (it.category_name.includes(c.name) || c.name.includes(it.category_name))))
      : null;
    const acc = accounts.find(a=>a.name===it.account);
    return {
      type,
      amount: it.amount || 0,
      category_id: cat ? cat.id : null,
      account_id: acc ? acc.id : null,
      date: (it.date || "").slice(0,16) || today(),   // 保留 LLM 给的时刻（中午→12:00），无时刻回落今天
      name: it.name || "",
      note: it.note || (i===0 && noteText) || "",
      tags: {},
      dup_hint: it.dup_hint || null
    };
  });
}
function aiGrow(el){   // 输入框随文字换行自动增高：先归零再取 scrollHeight，超过 CSS max-height 后转为滚动
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
}


function aiChip(q){
  $("#ai_in").value = q;
}
/* 今日卡片（.ai-today）：默认态顶部的记账反馈闭环——今日收支汇总 + 最近 3 笔（点击行可编辑，点汇总去流水页）。
   复用流水接口按今天过滤（from/to 同日=自定义区间语义，后端膨胀到当天末）；
   退款净额口径与流水日汇总一致（netSum/absorbedIds）。
   刷新钩子：loadAll / showPage(ai) / afterTxWrite（core.js 三处） */
async function aiToday(){
  const box = $("#ai_today"); if(!box) return;
  try{
    const d = today();
    const r = await api(`/api/transactions?ledger=${lid()}&page=1&page_size=50&from=${d}&to=${d}`);
    const items = r.items || [];
    const absorbed = absorbedIds(items);
    const rows = items.filter(t=>!absorbed.has(t.id));
    box.style.display = "";
    if(!rows.length){   // 今天还没记账：空态提示（不整卡跳转，引导用输入）
      box.innerHTML = `<div class="ai-th" style="border-bottom:none;cursor:default">
          <span class="ai-badge">今天</span>
          <span class="ai-tsum">今天还没记账，说一句试试</span></div>`;
      return;
    }
    const {e, i} = netSum(items, absorbed);
    const nE = rows.filter(t=>t.type==="expense").length;
    const nI = rows.filter(t=>t.type==="income").length;
    let sum;
    if(e > 0) sum = `支出 <b>${curSym()}${fmt(e)}</b> · ${nE} 笔` + (i > 0 ? ` · <span class="inc">收 ${curSym()}${fmt(i)}</span>` : "");
    else if(i > 0) sum = `收入 <b class="inc">${curSym()}${fmt(i)}</b> · ${nI} 笔`;   // 颜色由 .ai-tsum .inc 样式承担
    else sum = `转账 ${rows.length} 笔`;
    box.innerHTML = `<div class="ai-th" onclick="showPage('list')" title="查看流水">
        <span class="ai-badge">今天</span>
        <span class="ai-tsum">${sum}</span>
        <svg class="ai-thc" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="m9 18 6-6-6-6"/></svg>
      </div>` +
      rows.slice(0, 3).map(t=>{
        txCache[t.id] = t;   // 行点击 openEdit 从缓存取（与聊天确认卡同款机制）
        const sign = t.type==="expense" ? "−" : t.type==="income" ? "+" : "";
        const color = t.type==="expense" ? "var(--expense)" : t.type==="income" ? "var(--income)" : "var(--muted)";
        const sub = [t.category_name || TYPE_CN[t.type] || "", (t.date||"").slice(11,16)].filter(Boolean).join(" · ");
        return `<div class="ai-tr" onclick="openEdit(${t.id})">${txIcon(t)}
          <div class="ai-trb"><div class="ai-trn">${esc(t.name || t.note || "（未命名）")}</div><div class="sub" style="font-size:11px;margin-top:1px">${esc(sub)}</div></div>
          <div class="ai-tra num" style="color:${color}">${sign}${fmt(t.amount)}</div></div>`;
      }).join("") +
      (rows.length > 3 ? `<div class="sub" style="font-size:11px;text-align:center;padding:7px 0 8px;color:var(--muted)">还有 ${rows.length - 3} 笔 · 点顶部看全部</div>` : "");
  }catch(_){ box.style.display = "none"; }   // 接口异常藏卡不占位，引导区仍兜底
}
async function aiReceipt(){
  const f = $("#rcp_file").files[0]; if(!f) return;
  $("#rcp_file").value = "";
  if(!await aiConfigured()) return msg("未配置 AI：请先在设置页填写 LLM API Key", false);
  aiChatOn();
  const waiting = addBubble("ai","");
  const wtxt = document.createElement("span"); waiting.appendChild(wtxt);
  wtxt.textContent = ""; aiWaitStart(wtxt, "小票识别中");
  const fd = new FormData(); fd.append("file", f);
  try{
    const r = await api(`/api/ai/receipt?ledger=${lid()}`, {method:"POST", body:fd});
    aiWaitStop(waiting); waiting.remove();
    if(r.error){ addBubble("ai", `[${r.error}]`); return; }
    const items = aiItems([{type:r.type, amount:Math.round((r.amount||0)*100), category_name:r.category_name,
      date:r.date, name:r.name||""}], r.note || "");
    aiBookingCard(items, {title:"小票识别结果，请确认", receipt:r.receipt_path});
  }catch(e){ aiWaitStop(waiting); waiting.remove(); addBubble("ai", `[${e.message}]`); }
}


/* 分类底色/字色单源见 core.js CAT_SKIN；这里只留收支/转账的语义兜底色 */
const ICON_BG_FB = {expense:"var(--m-peach-50)", income:"var(--m-mint-50)", transfer:"var(--m-gray-50)"};
const ICON_FG_FB = {expense:"var(--m-peach)", income:"var(--m-mint)", transfer:"var(--m-gray)"};


const ICON_SVG = {
  /* 支出 */
  "餐饮美食":'<path d="M3 2v7a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2V2M7 2v20M21 15V2a5 5 0 0 0-5 5v8h5zM21 15v7"/>',
  "日用百货":'<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.27 6.96 12 12.01l8.73-5.05M12 22.08V12"/>',
  "数码电器":'<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M12 18h.01"/>',
  "服饰装扮":'<path d="M20.38 3.46 16 2a4 4 0 0 1-8 0L3.62 3.46a2 2 0 0 0-1.34 2.23l.58 3.47a1 1 0 0 0 .99.84H6v10a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V10h2.15a1 1 0 0 0 .99-.84l.58-3.47a2 2 0 0 0-1.34-2.23z"/>',
  "美容美发":'<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.12 15.88M14.47 14.48 20 20M8.12 8.12 12 12"/>',
  "文化休闲":'<path d="M2 4h6a4 4 0 0 1 4 4v12a3 3 0 0 0-3-3H2zM22 4h-6a4 4 0 0 0-4 4v12a3 3 0 0 1 3-3h7z"/>',
  "医疗健康":'<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7z"/><path d="M3.22 12H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"/>',
  "教育培训":'<path d="M22 10 12 5 2 10l10 5 10-5zM6 12v5c3 3 9 3 12 0v-5M22 10v6"/>',
  "爱车养车":'<path d="M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.5 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2"/><circle cx="7" cy="17" r="2"/><circle cx="17" cy="17" r="2"/><path d="M9 17h6"/>',
  "加油充电":'<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
  "交通出行":'<rect x="4" y="3" width="16" height="13" rx="2"/><path d="M4 10h16"/><circle cx="8" cy="19" r="1.6"/><circle cx="16" cy="19" r="1.6"/><path d="M8 16v1.4M16 16v1.4"/>',
  "充值缴费":'<path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1z"/><path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/>',
  "住房物业":'<path d="M3 9.5 12 3l9 6.5V20a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V13h6v9"/>',
  "母婴亲子":'<circle cx="12" cy="7" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
  "亲友代付":'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  "人情往来":'<rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13M19 12v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-7M7.5 8a2.5 2.5 0 0 1 0-5C11 3 12 8 12 8s1-5 4.5-5a2.5 2.5 0 0 1 0 5"/>',
  "红包礼金":'<circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18M7 6h1v4m8.71 3.88.7.71-2.82 2.82"/>',
  "其他":'<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4zM8 12h8M8 16h5"/>',
  /* 收入 */
  "工资薪酬":'<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
  "兼职副业":'<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  "理财收益":'<path d="M22 7 13.5 15.5 8.5 10.5 2 17M16 7h6v6"/>',
  "退款返还":'<path d="M3 7v6h6M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/>',
  "报销":'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h10M7 13h10M7 17h6"/>',
  "投资本金":'<path d="M3 22h18M6 18v-7M10 18v-7M14 18v-7M18 18v-7M3 9l9-5 9 5"/>',
  "其他收入":'<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
};
const _svgWrap = inner => `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
function txIcon(t){
  /* 图形按分类（ICON_SVG），底色/字色按分类色系（CAT_SKIN 单源），双 fallback 到收支语义色 */
  const cat = t.category_name || "";
  const skin = CAT_SKIN[cat];
  const bg = skin ? `var(${skin[1]})` : (ICON_BG_FB[t.type] || ICON_BG_FB.transfer);
  const fg = skin ? `var(${skin[0]})` : (ICON_FG_FB[t.type] || ICON_FG_FB.transfer);
  const svg = ICON_SVG[cat]
    ? _svgWrap(ICON_SVG[cat])
    : t.type==="income"
    ? _svgWrap('<path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>')
    : t.type==="transfer"
    ? _svgWrap('<path d="M5 12h14M12 5l7 7-7 7"/>')
    : _svgWrap('<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><path d="M3 6h18M16 10a4 4 0 0 1-8 0"/>');
  return `<div class="tx-icon" style="background:${bg};color:${fg}">${svg}</div>`;
}
function pvCatOptions(cur){
  // 选组内分类由 impSetCat 把整行转不计收支（与导入映射同口径）
  const normal = categories.filter(c=>!c.parent_id && c.type!=="" && c.type!=="transfer");
  const curId = (categories.find(c=>!c.parent_id && c.type==="transfer" && c.name===cur) || {}).id;
  const tHtml = transferOptgroup(curId);
  return normal.map(c=>`<option value="${c.id}" ${c.name===cur?"selected":""}>${esc(c.name)}</option>`).join("")
    + (tHtml ? `<optgroup label="退款和不计收支">${tHtml}</optgroup>` : "");
}
function confTags(t){   // 待确认/退款置信标签组（单行卡与双行退款卡的退款行共用）
  const ts = [];
  if(t.need_confirm) ts.push('<span class="tag warn">待确认</span>');
  const rc = t.refund_confidence;
  if(rc === "high" || rc === "low") ts.push(`<span class="tag refund-${rc}">退款</span>`);
  else if(rc === "unmatched") ts.push('<span class="tag refund-unmatched">退回</span>');
  return ts;
}
function txRow(t, mode){
  // 字段显示与 flow 完全一致；仅 4 处预览态差异：
  //   1) duplicate 标签 + 透明度（仅 preview：提示已被导入过）
  //   2) 分类 select（仅 preview：允许改分类）
  //   3) 无 openEdit 点击（preview 未入库）
  //   4) txCache 不缓存（preview 无 t.id）
  // 单行 / 双行 / 多行 复用同一套样式，行间渐隐横线统一由 .tx-row + .tx-row::before 控制
  // 双行退款卡片里两行都不显示 source，仅净额后方 .tx-side-src 显示
  const pv = mode === "preview";
  if(!pv) txCache[t.id] = t;
  const tags = [];
  if(pv && t.duplicate) tags.push('<span class="tag dup">已导入</span>');
  if(pv && t.pay_dupe){
    // 手动记账配对与渠道跨源配对同框架：默认跳过防双计，点行导入=用户确认恢复
    const manual = (t.pay_dupe_via || "").includes("手动");
    const tip = manual ? "与手动记的同一笔重复，默认跳过防双计；点击可改为导入"
                       : "同一笔消费已在微信/支付宝/京东账单中记账，跳过防重复；点击可改为导入";
    const label = (manual ? "手动重复" : "跨源重复") + (t.skip ? " 点击导入" : " 点击跳过");
    tags.push(`<button class="tag dup-hint" title="${tip}" onclick="impSkipRow(${t._pvIdx})">${label}</button>`);
  }
  else if(pv && t.skip && t._pvSkip) tags.push(`<button class="tag dup-hint" onclick="impSkipRow(${t._pvIdx})">已跳过 · 点此恢复</button>`);
  else if(pv && t.dup_hint) tags.push(`<button class="tag dup-hint" title="${esc(dupHintText(t.dup_hint))}" onclick="impSkipRow(${t._pvIdx})">疑似重复 点击跳过</button>`);
  tags.push(...confTags(t));
  const srcTag = t.source && t.source!=="manual" ? `<span class="tag plain tx-src">${t.source}</span>` : "";
  const rcpTag = t.receipt_path ? `<span class="tag plain tx-src" title="查看小票" onclick="event.stopPropagation();viewReceipt('${qEsc(t.receipt_path)}')">🧾</span>` : "";
  // 源栈：jd 在上、小票在下；只有其中一个时单独显示
  const srcStack = (srcTag || rcpTag) ? `<div class="tx-src-stack">${srcTag}${rcpTag}</div>` : "";
  // 分类：预览 → select；流水 → 文本
  let catHtml;
  if(pv){
    catHtml = t.type === "transfer"
      ? `<span class="tag plain">转账 · ${esc(t.category_name || "账户转账")}</span>`
      : `<select data-pv-idx="${t._pvIdx}" onchange="impSetCat(this)">${pvCatOptions(t.parent_category)}<option value="__new__">＋新建分类…</option></select>`;
  } else {
    catHtml = t.category_name || TYPE_CN[t.type] || "";
  }
  function makeRow(td, opts){
    const cc2 = td.type==="expense"?"var(--expense)":td.type==="income"?"var(--income)":"var(--transfer)";
    const sign2 = td.type==="expense"?"−":td.type==="income"?"+":"";
    /* 流水行字段全部来自账单/AI 文本，一律 esc() 转义防注入（catHtml 为预构建 HTML 除外） */
    const head2 = esc((td.name || td.note || "（未命名）").slice(0,40));
    const l22 = [
      (td.date||"").slice(0,16),
      (td.note && td.note!==td.name) ? esc(td.note.slice(0,40)) : "",
      opts.catHtml !== undefined ? opts.catHtml : esc(td.category_name || TYPE_CN[td.type] || ""),
      esc(td.attr_display || (td.tags && td.tags.attribution) || ""),
      esc(td.order_no||""),
      esc(td.account_name||"")
    ].filter(Boolean).join(" · ");
    return `<div class="tx-row"${opts.clickAttr || ""}>${txIcon(td)}
      <div class="tx-body"><div class="tx-note">${head2}</div>
      <div class="sub tx-sub">${l22}</div></div>
      <div class="tx-right">
        <div class="tx-amt-row"><div class="tx-amt num" style="color:${cc2}">${sign2}${curSym()}${fmt(td.amount)}</div>${opts.srcStack || ""}</div>
        <div class="tx-tags">${opts.tags.join(" ")}</div>
      </div>
    </div>`;
  }
  const catSlug = t.parent_category || t.category_name || t.type || "";
  if(pv || !t.refund_origin){
    // 分类依据悬浮：预览行上直接看出这行分到当前分类的来源（词典/映射/同名/AI/兜底/手动）
    const viaTip = pv && t.classified_via
      ? ` title="分类依据：${({keyword:"关键词词典", mapping:"学习映射", "direct-name":"同名直连",
          ai:"AI 兜底", transfer:"转账子类", manual:"手动指定", raw:"兜底（待确认）"})[t.classified_via] || t.classified_via}"`
      : "";
    const cardAttr = pv
      ? ` data-pv-idx="${t._pvIdx}"${(t.duplicate || t.skip) ? ' style="opacity:.45"' : ''} data-cat="${esc(catSlug)}"${viaTip}`
      : ` onclick="openEdit(${t.id})" data-cat="${esc(catSlug)}"${t.id ? ` data-txid="${t.id}"` : ""}`;
    return `<div class="tx-card"${cardAttr}>${makeRow(t, {tags, catHtml, srcStack})}</div>`;
  }
  const ro = t.refund_origin;
  // 第二行 onclick 调 openEdit(ro.id) 时取不到数据会静默 return → 看起来"第二行点不动"。
  // 这里在渲染双行卡时把原交易一并缓存，第二行即可正常打开编辑。
  txCache[ro.id] = ro;
  const refundTags = confTags(t);
  if(t.receipt_path) refundTags.push(`<span class="tag plain" title="查看小票" onclick="event.stopPropagation();viewReceipt('${qEsc(t.receipt_path)}')">🧾</span>`);
  const roTags = [];
  if(ro.receipt_path) roTags.push(`<span class="tag plain" title="查看小票" onclick="event.stopPropagation();viewReceipt('${qEsc(ro.receipt_path)}')">🧾</span>`);
  // 例：原支出 4996 + 退款入账 4996 = 0（已全额抵销），而非 9992
  const net = signed(ro.type, ro.amount) + signed(t.type, t.amount);
  const netSign = net > 0 ? "+" : net < 0 ? "−" : "";
  const netVal = fmt(Math.abs(net));
  const netColor = net > 0 ? "var(--income)" : net < 0 ? "var(--expense)" : "var(--muted)";
  const netTitle = net > 0 ? "净收入" : net < 0 ? "实际花费" : "已全额抵销";
  const refundRow = makeRow(t, {tags: refundTags, clickAttr: ""});
  const originRow = makeRow(ro, {tags: roTags, clickAttr: ` onclick="event.stopPropagation();openEdit(${ro.id})"`});
  // 两者包进同一 .tx-side wrapper 内部横向排列，整体贴右
  const sideSrc = t.source && t.source!=="manual"
    ? `<span class="tag plain">${t.source}</span>` : "";
  const sideCells = `<div class="tx-side"><div class="tx-side-net num" style="color:${netColor}" title="${netTitle}">${netSign}${curSym()}${netVal}</div>${sideSrc}</div>`;
  return `<div class="tx-card tx-card-double" onclick="openEdit(${t.id})" data-cat="${esc(catSlug)}"${t.id ? ` data-txid="${t.id}"` : ""}>${refundRow}${originRow}${sideCells}</div>`;
}


function aiChatOn(){ $("#tab-ai").classList.add("chat-on"); }   // 进对话态：布局无过渡，输入框本就贴底
function aiManual(){ txOpen("create"); }   // 手动记一笔：仍走统一弹窗（日历入口同款）

function aiQuickAnalyze(){
  aiChatOn();
  addBubble("user", "本月一键分析");
  const m = thisMonth();
  const out = addBubble("ai",""); const txt = document.createElement("div"); out.appendChild(txt);
  let buf = ""; aiWaitStart(txt);
  sse(null, `/api/ai/analyze?ledger=${lid()}&month=${m}`,
    (t,err)=>{ aiWaitStop(txt); buf += err ? `[${t}]` : t; txt.textContent = buf; scrollChat(); }, ()=>{ aiWaitStop(txt); });
}

function aiPlusToggle(ev, inside){
  ev.stopPropagation();
  const w = document.querySelector(".ai-plus-wrap");
  if(inside === true) w.classList.remove("open");   // 点菜单项：执行动作后收起
  else w.classList.toggle("open");
}
document.addEventListener("click", ev=>{
  const w = document.querySelector(".ai-plus-wrap");
  if(w && w.classList.contains("open") && !w.contains(ev.target)) w.classList.remove("open");
});
$("#ai_in").addEventListener("focus", aiChatOn);   // click 必先触发 focus，无需重复挂


function scrollChat(){
  const m = $("#ai_msgs"); if(!m) return;
  requestAnimationFrame(()=>{ m.scrollTop = m.scrollHeight; });
}

function aiWaitStart(el, prefix){
  aiWaitStop(el);
  prefix = prefix || "";
  let n = 0;
  el.dataset.waiting = "1";
  el._waitTimer = setInterval(()=>{ n = (n + 1) % 6; el.textContent = prefix + ".".repeat(n + 1); }, 400);
  el.textContent = prefix + ".";
}
function aiWaitStop(el){
  if(!el || !el._waitTimer) return;
  clearInterval(el._waitTimer); el._waitTimer = null;
  delete el.dataset.waiting;
}
/* 首次出消息（或出记账卡片）后清掉对话空状态开场（#chat_empty） */
function aiClearIntro(){ $("#chat_empty")?.remove(); }
function addBubble(cls, text){
  aiClearIntro();
  const d = document.createElement("div"); d.className="bubble "+cls; d.textContent=text;
  $("#ai_msgs").appendChild(d); scrollChat(); return d;
}
// sse：onChunk(text, isErr) 文本流 / onDone(errText) 流结束 / onBooking(bk) 记账草稿 / onStats(st) 查账图表附件
function sse(btn, url, onChunk, onDone, onBooking, onStats){
  if(btn) btn.disabled = true;
  const done = ()=>{ if(btn) btn.disabled = false; };
  const es = new EventSource(url.replace(/^\//,""));
  es.onmessage = ev => {
    if(ev.data === "[DONE]"){ es.close(); done(); onDone&&onDone(); return; }
    try{ const d = JSON.parse(ev.data);
      if(d.error){ onChunk(d.error, true); }
      else if(d.booking){ onBooking && onBooking(d.booking); }
      else if(d.stats){ onStats && onStats(d.stats); }
      else if(d.text) onChunk(d.text, false);
    }catch(_){}
  };
  es.onerror = ()=>{ es.close(); done(); onDone&&onDone("连接中断，请重试"); };
}
let aiLastStats = null, aiLastStatsLid = null;
function aiStatsRange(st){
  // toExclusive：AI 附件的 to 是排他上界（次日/下周期首日），下钻时不得再补 23:59:59/改写
  return {label: st.label, from: st.from, to: st.to, toExclusive: true};
}

function aiCatDetail(inp, q){
  const st = aiLastStats && aiLastStatsLid === lid() ? aiLastStats : null;
  if(!st) return false;
  if(st.attribution || st.account) return false;   // 成员/账户维度附件：无「XX明细」分类下钻
  let nm = "";
  if(q === "对应明细") nm = st.category || "";
  else { const md = q.match(/^(.{1,12})明细$/); if(md) nm = md[1]; }
  nm = nm.trim();
  const hit = (st.categories || []).find(c=>c.name === nm)
           || (st.categories || []).find(c=>nm && (nm.includes(c.name) || c.name.includes(nm)));
  if(!hit) return false;
  inp.value = ""; aiGrow(inp);
  showCategoryDetail(hit.name, aiStatsRange(st), st.flow === "income" ? "income" : "expense");
  return true;
}
function aiSend(btn){   // 单输入框：模型自行判断记账还是查账
  const inp = $("#ai_in");
  const q = inp.value.trim(); if(!q) return;
  if(aiCatDetail(inp, q)) return;
  aiChatOn();
  addBubble("user", q); inp.value = ""; aiGrow(inp);
  // 气泡内文本放独立子节点，查账图表附件可共存于同一气泡
  const out = addBubble("ai","");
  const txt = document.createElement("div"); out.appendChild(txt);
  let buf = ""; aiWaitStart(txt);
  // 查账范围不传（后端 scope=auto）：模型按这句话自行解析时间（上周/上月/去年/全部…）
  sse(btn, `/api/ai/chat?` + new URLSearchParams({ledger:lid(), q}),
    (t,err)=>{ aiWaitStop(txt); buf += err?`[${t}]`:t; txt.textContent = buf; scrollChat(); },
    err=>{   // AI 失败：兜底已后移到服务端(/api/ai/chat 会补发本地拆分的 booking),此处只兜底显示
      aiWaitStop(txt);
      if(!buf) txt.textContent = err ? `[${err}]` : "（没有返回内容）";
    },
    bk=>{   // 记账意图：占位气泡换成可编辑的卡片气泡（AI 结果/服务端兜底草稿统一走这里）
      aiWaitStop(txt); out.remove();
      // bk.text≠原句=服务端兜底的提示标题,传给卡片显示;LLM booking 的 text=原句,用默认标题(与 bot 同逻辑)
      aiBookingCard(aiItems(bk.items || [], q), bk.text && bk.text !== q ? {title: bk.text} : undefined);
    },
    st=>{ aiLastStats = st; aiLastStatsLid = lid(); aiStatsCard(out, st); });   // 查账：附环形图/明细缩略；缓存供「XX明细」下钻
}

async function aiStatsCard(bubble, st){
  const box = document.createElement("div");
  box.className = "ai-stats";
  const flow = st.flow === "income" ? "income" : "expense";
  const fw = flow === "income" ? "收入" : "支出";           // 文案用
  const rg = aiStatsRange(st);
  if(st.matched && st.items && st.items.length){
    const total = st.total || st.items.length;
    box.innerHTML = `<div class="sub" style="margin-bottom:6px">${esc(st.category)} · ${esc(st.label)} · 共 ${total} 笔</div>
      <div class="ai-stats-tx">${st.items.slice(0, total<=5 ? 5 : 3).map(txRow).join("")}</div>` +
      (total > 5 ? `<button class="btn btn-ghost" style="margin-top:6px;width:100%">查看全部 ${total} 笔</button>` : "");
    const btnAll = box.querySelector("button");
    if(btnAll) btnAll.onclick = ()=>showCategoryDetail(st.category, rg, flow);
    bubble.appendChild(box); scrollChat(); return;
  }
  // 无数据 / 未命中：区间完整分类环形图（可点击下钻）
  const cats = (st.categories||[]).filter(c=>c.net>0);
  const title = st.attribution ? `${fw}成员占比 · ${st.label}`
            : st.account ? `${fw}账户占比 · ${st.label}`
            : st.category ? `${st.category}（该区间无${fw}数据）· ${st.label}`
                            : `${fw}分类占比 · ${st.label}`;
  let html = `<div class="sub" style="margin-bottom:6px">${esc(title)}</div>`;
  if(!cats.length){ box.innerHTML = html + `<div class="sub">该区间暂无${fw}</div>`; bubble.appendChild(box); return; }
  const top = cats.slice(0, 8);
  /* 图表与排版完全复用统计页：drawChart/renderLegend 画图下钻，
     .donut-row/.donut-box/.donut-legend 结构居中（气泡内只把圆图缩到 150px，见 views.css） */
  const seq = ++aiStatsCard._n;
  const cid = `aiDonut${seq}`, lid = `aiDonutLg${seq}`;
  html += `<div class="donut-row ai-stats-donut"><div class="donut-box"><canvas id="${cid}" width="150" height="150"></canvas></div>
    <div class="donut-legend" id="${lid}"></div></div>`;
  box.innerHTML = html; bubble.appendChild(box);
  const drill = i => showCategoryDetail(top[i].name, rg, flow);
  await drawChart(cid, "doughnut", top.map(c=>c.name), top.map(c=>c.net/100), null,
    (st.attribution || st.account) ? null : (e, els)=>{ if(els && els.length) drill(els[0].index); }, lid, {responsive:false, label:fw});
  scrollChat();
}
aiStatsCard._n = 0;   // 气泡内图表 id 序号（同一会话可出现多张环形图）
function aiBookingCard(items, opts){   // 记账草稿气泡：复用 txItemHtml(create) 的可编辑条目卡
  opts = opts || {};
  const n = items.length;
  const d = document.createElement("div");
  d.className = "bubble ai bk-bubble";
  if(opts.receipt) d.dataset.receipt = opts.receipt;
  const rcpThumb = opts.receipt
    ? `<img src="${opts.receipt}" alt="小票" style="max-height:200px;max-width:100%;border-radius:8px;cursor:pointer;margin-bottom:8px;display:block;border:1px solid var(--border)" onclick="viewReceipt('${opts.receipt}')">`
    : "";
  d.innerHTML = `<div class="sub" style="margin-bottom:6px;white-space:pre-line">${esc(opts.title || `识别到 ${n} 笔，请确认`)}</div>
    ${rcpThumb}
    <div data-txhost="1">${items.map((it,i)=>txItemHtml(it, i, n, "create")).join("")}</div>
    <div class="bk-foot">
      <span class="bk-count">${n} 笔</span>
      <button class="btn btn-ghost bk-cancel" onclick="aiBookingCancel(this)">取消</button>
      <button class="btn btn-pri bk-ok" onclick="aiBookingSubmit(this)">确认记入 ${n} 笔</button>
    </div>`;
  aiClearIntro();
  $("#ai_msgs").appendChild(d); scrollChat(); return d;
}
function aiBookingCancel(btn){   // 取消记账：未入库的小票图顺手清掉(服务端按流水引用校验)
  const b = btn.closest(".bk-bubble"); if(b){ rcpDiscard(b.dataset.receipt); b.remove(); }
}

function aiConfirmCard(items, ids){
  const d = document.createElement("div");
  d.className = "bubble ai bk-bubble";
  d.innerHTML = `<div class="sub" style="margin-bottom:6px">✅ 已记入 ${ids.length} 笔 · 点击卡片可编辑</div>
    <div class="ai-stats-tx">${items.map((it, i)=>{
      const cat = categories.find(c=>c.id===it.category_id);
      const acc = accounts.find(a=>a.id===it.account_id);
      return txRow({id: ids[i], type: it.type, amount: it.amount, date: it.date, name: it.name || "",
        note: it.note || "", category_name: cat ? cat.name : "", account_name: acc ? acc.name : "",
        source: "app", tags: it.tags || {}});
    }).join("")}</div>`;
  aiClearIntro();
  $("#ai_msgs").appendChild(d); scrollChat();
}

const chatTxCards = id => document.querySelectorAll(`#ai_msgs .tx-card[data-txid="${id}"]`);   // 聊天区该流水的全部卡（确认卡/查账卡）
function aiRefreshTxCard(t){   // 编辑/批量修改后按新值就地重渲（txRow 顺手更新 txCache）
  chatTxCards(t.id).forEach(el => el.outerHTML = txRow(t));
}
function aiPurgeTxCard(id){
  chatTxCards(id).forEach(el => el.remove());
  delete txCache[id];   // 防残留缓存被点开（已删的 id 再保存即 404）
  document.querySelectorAll(".bk-bubble").forEach(b => {
    const left = b.querySelectorAll(".tx-card").length;
    const head = b.querySelector(".sub");
    if(head && /已记入/.test(head.textContent)) head.textContent = left ? `✅ 剩余 ${left} 笔 · 点击卡片可编辑` : "✅ 该笔已删除";
    if(!left) b.style.opacity = ".55";
  });
}
async function aiBookingSubmit(btn){
  const bubble = btn.closest(".bk-bubble"); if(!bubble) return;
  const items = txCollect(bubble.querySelector("[data-txhost]")).filter(x=>x.amount > 0);
  const verr = txValidateItems(items);
  if(verr) return msg(verr, false);
  btn.disabled = true;
  try{
    const ids = await txPostItems(items, {source:"app", receipt: bubble.dataset.receipt || ""});
    bubble.remove();
    aiConfirmCard(items, ids);   // 确认后在聊天里展示已入库的流水卡片（多笔同显）
    afterTxWrite();
  }catch(e){
    const doneN = e.ok || 0;
    if(doneN){   // 前 doneN 笔已入库：从卡片里裁掉，避免修正后重试时重复提交
      const host = bubble.querySelector("[data-txhost]");
      [...host.querySelectorAll(".txf-item")].slice(0, doneN).forEach(el=>el.remove());
      txRenumber(host);
    }
    btn.disabled = false;
    msg(doneN ? `已记入 ${doneN} 笔，第 ${doneN+1} 笔失败：${e.message}` : `记账失败：${e.message}`, false);
  }
}