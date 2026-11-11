/* ===== AI 记一笔 ===== */


function dupHintText(dh){
  if(!dh || !dh.kind) return "";
  if(dh.kind === "batch") return `与本批第 ${dh.no} 笔重复`;
  const o = dh.of || {};
  return `同日同额已有：${o.name || "（未命名）"} ${curSym()}${fmt(o.amount || 0)}${o.source ? `（来源 ${o.source}）` : ""}`;
}
function aiItems(list, noteText){   // AI 结果 → 弹窗条目：分类/账户 id 由后端单点解析（ai._cat_ids
  return list.map((it, i)=>{         // 两段式匹配）随 API 下发，前端不再保留一份名称匹配副本
    const type = ["expense","income","transfer"].includes(it.type) ? it.type : "expense";
    return {
      type,
      amount: it.amount || 0,
      category_id: it.category_id ?? null,
      account_id: it.account_id ?? null,
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
/* 今日卡片：今日收支汇总 + 最近 3 笔（行点击编辑、汇总去流水页）。条数用 r.total、
   净额走 /api/stats/range（前端只拉 50 笔，自行 sum 会截断）。刷新：loadAll/showPage/afterTxWrite */
async function aiToday(){
  const box = $("#ai_today"); if(!box) return;
  const sc = ledgerScope();   // 账本身份守卫（ledgerScope 单源）：旧账本今日卡的迟到响应不回填
  try{
    const d = today();
    // to 排他上界：含当天须传次日（nextDayStr 单点，同下方 range 统计与 ledger.js rangeDto）
    const r = await api(`/api/transactions?ledger=${sc.id}&page=1&page_size=50&from=${d}&to=${nextDayStr(d)}`);
    if(!sc.isCurrent()) return;
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
    // 与服务端统计单源：to 排他上界=明天（nextDayStr），from/to 语义同 stats.js 自定义区间。
    // 第二请求沿用捕获的 sc.id 并复核：期间切账本时用实时 lid() 会把新账本统计灌进旧账本的今日卡
    const s = await api(`/api/stats/range?ledger=${sc.id}&from=${d}&to=${nextDayStr(d)}`);
    if(!sc.isCurrent()) return;
    const e = s.net_expense || 0, i = s.income || 0;
    const n = r.total || 0;   // 全量计数（含被吸收原交易/退款行/转账），不再数截断的当前页
    let sum;
    if(e > 0) sum = `支出 <b>${curSym()}${fmt(e)}</b> · ${n} 笔` + (i > 0 ? ` · <span class="inc">收 ${curSym()}${fmt(i)}</span>` : "");
    else if(i > 0) sum = `收入 <b class="inc">${curSym()}${fmt(i)}</b> · ${n} 笔`;   // 颜色由 .ai-tsum .inc 样式承担
    else sum = `转账 ${n} 笔`;
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
      (n > 3 ? `<div class="sub" style="font-size:11px;text-align:center;padding:7px 0 8px;color:var(--muted)">还有 ${n - 3} 笔 · 点顶部看全部</div>` : "");
  }catch(e){ console.warn("今日卡片加载失败", e); if(sc.isCurrent()) box.style.display = "none"; }   // 接口异常藏卡（旧账本的迟到失败不误藏新账本已渲染的卡）
}
async function aiReceipt(){
  const f = $("#rcp_file").files[0]; if(!f) return;
  if(!await aiConfigured()) return msg("未配置 AI：请先在设置页填写 LLM API Key", false);
  // 通过预检才清空选择框：清空过早会让未配置 Key/失败的用户被迫重新选文件
  $("#rcp_file").value = "";
  aiChatOn();
  const waiting = addBubble("ai","");
  const wtxt = document.createElement("span"); waiting.appendChild(wtxt);
  aiWaitStart(wtxt, "小票识别中");
  try{
    const r = await uploadReceipt(f);   // 大小预检+上传的公共链路（core.js，与记账弹窗共用）
    aiWaitStop(wtxt); waiting.remove();   // 计时器挂在 wtxt（aiWaitStart 的参数），传气泡 div 清不掉
    if(r.error){ addBubble("ai", `[${r.error}]`); return; }
    const items = aiItems([{type:r.type, amount:toCents(r.amount),   // 元→分走 toCents 单点（receipt 返回元）
      category_id:r.category_id, account_id:r.account_id ?? null,
      date:r.date, name:r.name||""}], r.note || "");
    aiBookingCard(items, {title:"小票识别结果，请确认", receipt:r.receipt_path});
  }catch(e){ aiWaitStop(wtxt); waiting.remove(); addBubble("ai", `[${e.message}]`); }
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
function pvCatOptions(t){
  // 选组内分类由 impSetCat 把整行转不计收支（与导入映射同口径）。
  // 刻意不与 core.js txCatOptions 合并：预览行收支类型混合，下拉须列**全部**非转账一级分类
  // （txCatOptions 按单一 type 过滤），语义不同——硬合并要加模式开关，得不偿失。
  // 按 category_id 选中（而非分类名）：重渲染下拉的回显与提交值才一致
  // 首项「未分类」与 core.js txCatOptions 同款：柴米账单源头未分类的行 category_id 为空，
  // 缺这一项时下拉会把首个分类显示成选中（所见非所提交）
  const normal = categories.filter(c=>!c.parent_id && c.type!=="" && c.type!=="transfer");
  const tHtml = transferOptgroup(t.category_id);
  return `<option value=""${t.category_id ? "" : " selected"}>未分类</option>`
    + normal.map(c=>`<option value="${c.id}" ${c.id===t.category_id?"selected":""}>${esc(c.name)}</option>`).join("")
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
function txPreviewTags(t){
  // 预览态专属标签（已导入/跨源或手动重复/已跳过/疑似重复），与 confTags 拼配使用
  const tags = [];
  if(t.duplicate) tags.push('<span class="tag dup">已导入</span>');
  if(t.pay_dupe){
    // 手动记账配对与渠道跨源配对同框架：默认跳过防双计，点行导入=用户确认恢复
    const manual = (t.pay_dupe_via || "").includes("手动");
    const tip = manual ? "与手动记的同一笔重复，默认跳过防双计；点击可改为导入"
                       : "同一笔消费已在微信/支付宝/京东账单中记账，跳过防重复；点击可改为导入";
    const label = (manual ? "手动重复" : "跨源重复") + (t.skip ? " 点击导入" : " 点击跳过");
    tags.push(`<button class="tag dup-hint" title="${tip}" onclick="impSkipRow(${t._pvIdx})">${label}</button>`);
  }
  else if(t.skip && t._pvSkip) tags.push(`<button class="tag dup-hint" onclick="impSkipRow(${t._pvIdx})">已跳过 · 点此恢复</button>`);
  else if(t.dup_hint) tags.push(`<button class="tag dup-hint" title="${esc(dupHintText(t.dup_hint))}" onclick="impSkipRow(${t._pvIdx})">疑似重复 点击跳过</button>`);
  return tags;
}

function makeTxRow(td, opts){
  // 单行流水行（txRow 的行构造件，参数化后供单行/退款双行两态复用）
  const cc2 = td.type==="expense"?"var(--expense)":td.type==="income"?"var(--income)":"var(--transfer)";
  const sign2 = td.type==="expense"?"−":td.type==="income"?"+":"";
  const head2 = esc((td.name || td.note || "（未命名）").slice(0,40));
  const l22 = [
    esc((td.date||"").slice(0,16)),
    (td.note && td.note!==td.name) ? esc(td.note.slice(0,40)) : "",
    opts.catHtml !== undefined ? opts.catHtml : esc(td.category_name || TYPE_CN[td.type] || ""),
    esc(td.attr_display || (td.tags && td.tags.attribution) || ""),
    esc(td.order_no||""),
    esc(td.account_name||"")
  ].filter(Boolean).join(" · ");
  const amt = opts.amtHtml !== undefined ? opts.amtHtml
    : `<span style="color:${cc2}">${sign2}${curSym()}${fmt(td.amount)}</span>`;
  return `<div class="tx-row"${opts.clickAttr || ""}>${opts.noIcon ? "" : txIcon(td)}
    <div class="tx-body"><div class="tx-note">${head2}</div>
    <div class="sub tx-sub">${l22}</div></div>
    <div class="tx-right">
      <div class="tx-amt-row"><div class="tx-amt num">${amt}</div></div>
      <div class="tx-tags">${opts.srcTags || ""}${opts.tags.join(" ")}</div>
    </div>
  </div>`;
}

function txRowDouble(t, catSlug, srcTags){
  // 退款双行卡：主行右侧显示这对账的净额（净支出/净收入/已全额抵销，标签 12px 金额 15px），
  // 回执行右侧显示退款金额（绿色）。图标在主行行内（与单行卡同结构，飞牛客户端兼容）。
  const ro = t.refund_origin;
  const net = signed(ro.type, ro.amount) + signed(t.type, t.amount);
  const netColor = net > 0 ? "var(--income)" : net < 0 ? "var(--expense)" : "var(--muted)";
  const netLabel = net > 0 ? "净收入" : net < 0 ? "净支出" : "已全额抵销";
  const netAmt = net === 0 ? "" : ` <span class="num" style="color:${netColor}">${net > 0 ? "+" : "−"}${curSym()}${fmt(Math.abs(net))}</span>`;
  const conf = confTags(t);
  const mainTags = conf.filter(h => !h.includes("tag refund-"));
  const refundRow = makeTxRow(t, {tags: mainTags, clickAttr: "", srcTags,
                                  amtHtml: `<span style="color:${netColor};font-size:12px">${netLabel}</span>${netAmt}`});
  const roRcp = ro.receipt_path ? ` <span class="tag plain" title="查看小票" onclick="event.stopPropagation();viewReceipt('${qEsc(ro.receipt_path)}')">🧾</span>` : "";
  const roFull = ro.name || ro.note || "（未命名）";
  const roName = esc(roFull.length > 10 ? roFull.slice(0, 10) + "…" : roFull);
  const roDate = esc((ro.date||"").slice(0,10));
  const originRow = `<div class="tx-origin" title="点击编辑原消费" onclick="event.stopPropagation();openEdit(${ro.id})">
    <span class="tx-origin-text">原消费 <span>${roDate}</span> <b class="tx-origin-name" title="${esc(roFull)}">${roName}</b> <b class="num c-exp">−${curSym()}${fmt(ro.amount)}</b>${roRcp}</span>
    <span class="tx-origin-net num c-inc" title="退款金额">退款 ${curSym()}${fmt(t.amount)}</span>
  </div>`;
  return `<div class="tx-card tx-card-double" onclick="openEdit(${t.id})" data-cat="${esc(catSlug)}"${t.id ? ` data-txid="${t.id}"` : ""}><div class="tx-double-body">${refundRow}${originRow}</div></div>`;
}

function txRow(t, mode){
  // 字段显示与 flow 完全一致；仅 4 处预览态差异：
  //   1) duplicate 标签 + 透明度（仅 preview：提示已被导入过）
  //   2) 分类 select（仅 preview：允许改分类）
  //   3) 无 openEdit 点击（preview 未入库）
  //   4) txCache 不缓存（preview 无 t.id）
  // 单行 / 退款双行卡复用同一套行样式（makeTxRow）
  const pv = mode === "preview";
  if(!pv) txCache[t.id] = t;
  const tags = pv ? txPreviewTags(t) : [];
  tags.push(...confTags(t));
  const srcTag = t.source && t.source!=="manual" ? `<span class="tag plain tx-src">${esc(t.source)}</span>` : "";   // manual=历史/测试口径，不显示来源标签
  const rcpTag = t.receipt_path ? `<span class="tag plain tx-src" title="查看小票" onclick="event.stopPropagation();viewReceipt('${qEsc(t.receipt_path)}')">🧾</span>` : "";
  const srcTags = rcpTag + srcTag;   // 小票在来源左侧：[🧾][jd]，并入金额下方的标签行
  // 分类：预览 → select；流水 → 文本
  let catHtml;
  if(pv){
    catHtml = t.type === "transfer"
      ? `<span class="tag plain">转账 · ${esc(t.category_name || TX_SUB_DEFAULT)}</span>`   // 兜底子类走 core.js 单点
      : `<select data-pv-idx="${t._pvIdx}" onchange="impSetCat(this)">${pvCatOptions(t)}<option value="__new__">＋新建分类…</option></select>`;
  } else {
    catHtml = esc(t.category_name || TYPE_CN[t.type] || "");   // 流水态：原始分类名裸拼进 innerHTML 会注入，必须转义
  }
  const catSlug = t.parent_category || t.category_name || t.type || "";
  if(pv || !t.refund_origin){
    // 分类依据悬浮：预览行上直接看出这行分到当前分类的来源（词典/映射/同名/AI/兜底/手动）
    const viaTip = pv && t.classified_via
      ? ` title="分类依据：${esc(({keyword:"关键词词典", mapping:"学习映射", "direct-name":"同名直连",
          ai:"AI 兜底", transfer:"转账子类", manual:"手动指定", export:"柴米账单原分类",
          raw:"兜底（待确认）"})[t.classified_via] || t.classified_via)}"`
      : "";
    const cardAttr = pv
      ? ` data-pv-idx="${t._pvIdx}"${(t.duplicate || t.skip) ? ' style="opacity:.45"' : ''} data-cat="${esc(catSlug)}"${viaTip}`
      : ` onclick="openEdit(${t.id})" data-cat="${esc(catSlug)}"${t.id ? ` data-txid="${t.id}"` : ""}`;
    return `<div class="tx-card"${cardAttr}>${makeTxRow(t, {tags, catHtml, srcTags})}</div>`;
  }
  return txRowDouble(t, catSlug, srcTags);
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
    (t,err)=>{ aiWaitStop(txt); buf += err ? `[${t}]` : t; txt.textContent = buf; scrollChat(); },
    (err)=>{   // 空返回/中断兜底与 aiSend 同款（onDone(errText)），别停在省略号残迹上
      aiWaitStop(txt);
      if(!buf) txt.textContent = err ? `[${err}]` : "（没有返回内容）";
    });
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

let _aiWaitEl = null;   // 当前等待动画元素：es.close() 不触发 onerror/onDone，interval 只能由这里兜底清理
function aiWaitStart(el, prefix){
  if(_aiWaitEl && _aiWaitEl !== el) aiWaitStop(_aiWaitEl);   // 新等待顶掉旧等待（旧流的回调已永不可达）
  _aiWaitEl = el;
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
/* 首次出消息（或出记账卡片）后收起对话空状态开场（#chat_empty）。
   用 display 隐藏而非 remove：恢复备份清空消息后开场要能重新亮出来（remove 过的节点无人重建） */
function aiClearIntro(){ const e = $("#chat_empty"); if(e) e.style.display = "none"; }
function addBubble(cls, text){
  aiClearIntro();
  const d = document.createElement("div"); d.className="bubble "+cls; d.textContent=text;
  $("#ai_msgs").appendChild(d); scrollChat(); return d;
}
// sse：onChunk(text, isErr) 文本流 / onDone(errText) 流结束 / onBooking(bk) 记账草稿 / onStats(st) 查账图表附件。
// 单例 EventSource 存 _aiEs：切离 AI 页时 core.showPage 关闭它——否则流继续打字进隐藏
// DOM、服务端 LLM 继续烧 token 直到 [DONE]（与 rev 长轮询的 AbortController 同思路）
let _aiEs = null;
function sse(btn, url, onChunk, onDone, onBooking, onStats){
  if(btn) btn.disabled = true;
  const done = ()=>{ if(btn) btn.disabled = false; };
  if(_aiEs){ _aiEs.close(); _aiEs = null; }   // 新流顶掉旧流（按钮禁用已挡常规并发，此为兜底）
  const es = new EventSource(url.replace(/^\//,""));
  _aiEs = es;
  es.onmessage = ev => {
    if(ev.data === "[DONE]"){ es.close(); if(_aiEs===es) _aiEs=null; done(); onDone&&onDone(); return; }
    try{ const d = JSON.parse(ev.data);
      if(d.error){ onChunk(d.error, true); }
      else if(d.booking){ onBooking && onBooking(d.booking); }
      else if(d.stats){ onStats && onStats(d.stats); }
      else if(d.text) onChunk(d.text, false);
    }catch(e){ console.warn("SSE 帧解析失败（丢帧）", ev.data, e); }   // LLM 输出坏 JSON 时留痕，不再静默缺字
  };
  es.onerror = ()=>{ es.close(); if(_aiEs===es) _aiEs=null; done(); onDone&&onDone("连接中断，请重试"); };
}
function aiSseStop(){   // 切离 AI 页调用：关流 + 清等待动画 + 复位发送按钮（流的中断文本不再有可见意义）
  if(_aiEs){ _aiEs.close(); _aiEs = null; }
  if(_aiWaitEl){   // 关流后 onChunk/onDone 永不回调：不清则 400ms interval 永久泄漏、气泡卡「….」
    const el = _aiWaitEl, waiting = el.dataset.waiting;
    aiWaitStop(el);
    if(waiting) el.textContent += "（已中断）";
    _aiWaitEl = null;
  }
  const b = document.getElementById("ai_send");
  if(b) b.disabled = false;
}
let aiLastStats = null, aiLastStatsLid = null;
function aiStatsRange(st){
  // toExclusive：AI 附件的 to 已是排他上界（次日/下周期首日），rangeDto 原样传不转换
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
  // 忙碌态统一在函数内处理：键盘路径（onkeydown 直调）此前无 btn，可在流进行中无限叠加并发 SSE
  btn = btn || $("#ai_send");
  if(btn && btn.disabled) return;
  aiChatOn();
  addBubble("user", q); inp.value = ""; aiGrow(inp);
  // 气泡内文本放独立子节点，查账图表附件可共存于同一气泡
  const out = addBubble("ai","");
  const txt = document.createElement("div"); out.appendChild(txt);
  let buf = ""; aiWaitStart(txt);
  // 查账范围不传（后端 scope=auto）：模型按这句话自行解析时间（上周/上月/去年/全部…）
  const l = lid();   // SSE 发送时定账本；流中途切账本时 st 仍是旧账本数据——
                     // aiLastStatsLid 必须记发送时的账本（此前在流事件时刻取实时 lid()，
                     // 会让 aiCatDetail 的账本核对形同虚设，下钻拿到旧账本区间）
  sse(btn, `/api/ai/chat?` + new URLSearchParams({ledger:l, q}),
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
    st=>{ aiLastStats = st; aiLastStatsLid = l; aiStatsCard(out, st); });   // 查账：附环形图/明细缩略；缓存供「XX明细」下钻
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
  // 图例 id 命名避开 lid：遮蔽 core.js 的全局函数 lid()（账本 id）是 readability 陷阱
  const cid = `aiDonut${seq}`, lgId = `aiDonut${seq}Lg`;   // 图例 id 统一「canvas id + Lg」约定（dropCharts 切主题按此重渲图例）
  html += `<div class="donut-row ai-stats-donut"><div class="donut-box"><canvas id="${cid}" width="150" height="150"></canvas></div>
    <div class="donut-legend" id="${lgId}"></div></div>`;
  box.innerHTML = html; bubble.appendChild(box);
  const drill = i => showCategoryDetail(top[i].name, rg, flow);
  await drawChart(cid, "doughnut", top.map(c=>c.name), top.map(c=>c.net/100), null,
    (st.attribution || st.account) ? null : (e, els)=>{ if(els && els.length) drill(els[0].index); }, lgId, {responsive:false, label:fw});
  scrollChat();
}
aiStatsCard._n = 0;   // 气泡内图表 id 序号（同一会话可出现多张环形图）
function aiBookingCard(items, opts){   // 记账草稿气泡：复用 txItemHtml(create) 的可编辑条目卡
  opts = opts || {};
  const n = items.length;
  const d = document.createElement("div");
  d.className = "bubble ai bk-bubble";
  d.dataset.lid = lid();   // 识别时的账本：确认前切过账本则拒绝提交（条目带旧账本分类 id，跨账本 POST 必 400）
  if(opts.receipt) d.dataset.receipt = opts.receipt;
  // 小票路径裸拼会注入：src 走 esc()（HTML 属性层），onclick 走 qEsc()（与 txRow 的小票标签同口径，JS 字符串+属性双层转义）
  const rcpThumb = opts.receipt
    ? `<img src="${esc(opts.receipt)}" alt="小票" style="max-height:200px;max-width:100%;border-radius:8px;cursor:pointer;margin-bottom:8px;display:block;border:1px solid var(--border)" onclick="viewReceipt('${qEsc(opts.receipt)}')">`
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
      // category_id/account_id 必须带上：txRow 会把该对象写入 txCache，openEdit 从缓存取值
      // 进编辑表单——缺 id 时下拉显示「未分类/未指定」，保存全量 PUT 会把分类/账户静默清空
      return txRow({id: ids[i], type: it.type, amount: it.amount, date: it.date, name: it.name || "",
        note: it.note || "", category_id: it.category_id ?? null, account_id: it.account_id ?? null,
        category_name: catNameOf(it.category_id), account_name: accNameOf(it.account_id),
        source: "app", tags: it.tags || {}});
    }).join("")}</div>`;
  aiClearIntro();
  $("#ai_msgs").appendChild(d); scrollChat();
}

const chatTxCards = id => document.querySelectorAll(`#ai_msgs .tx-card[data-txid="${id}"]`);   // 聊天区该流水的全部卡（确认卡/查账卡）
function aiRefreshTxCard(t){   // 编辑/批量修改后按新值就地重渲（txRow 顺手更新 txCache）
  chatTxCards(t.id).forEach(el => el.outerHTML = txRow(t));
}
/* 聊天卡按 id 重查重渲（编辑/批量修改后的统一收口）：写路径只报「改了哪些 id」，
  字段以服务端回读为准（refund_status 等派生值由后端重算）——此前编辑保存与批量
  修改各自手抄一份十来个字段的合并对象，加字段得记得改两处 */
async function aiSyncTxCards(ids){
  const live = (ids || []).filter(id => chatTxCards(id).length);
  for(let i = 0; i < live.length; i += 500){
    try{
      const r = await api(`api/transactions?ledger=${lid()}&ids=${live.slice(i, i+500).join(",")}&page=1&page_size=500`);
      r.items.forEach(aiRefreshTxCard);
    }catch(e){ console.warn("聊天卡刷新失败", e); }
  }
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
  if(bubble.dataset.lid && +bubble.dataset.lid !== lid())
    return msg("识别后切换过账本，请切回原账本确认，或重新识别", false);
  const items = txCollect(bubble.querySelector("[data-txhost]")).filter(x=>x.amount > 0);
  const verr = txValidateItems(items);
  if(verr) return msg(verr, false);
  btn.disabled = true;
  const cancel = bubble.querySelector(".bk-cancel");
  if(cancel) cancel.disabled = true;   // 提交在途禁用取消：慢网点「取消」移除气泡但 POST 照常入库，用户以为取消了
  try{
    const ids = await txPostItems(items, {source:"app", receipt: bubble.dataset.receipt || ""});
    bubble.remove();
    aiConfirmCard(items, ids);   // 确认后在聊天里展示已入库的流水卡片（多笔同显）
    afterTxWrite();
  }catch(e){
    const doneN = e.ok || 0;
    // 前 doneN 笔已入库：按与提交同源的判据/顺序裁掉，修正重试才不会重复入库
    const host = bubble.querySelector("[data-txhost]");
    if(trimPostedCards(host, doneN)) txRenumber(host);
    btn.disabled = false;
    if(cancel) cancel.disabled = false;
    msg(doneN ? `已记入 ${doneN} 笔，第 ${doneN+1} 笔失败：${e.message}` : `记账失败：${e.message}`, false);
  }
}