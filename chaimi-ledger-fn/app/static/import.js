/* ===== 导入 ===== */
function impAiNote(a){   // AI 兜底分类的结果说明（服务端 ai.resolve_subcategories_batch 的状态）：
  // 此前失败/未配置一律无声，用户只看到「待确认很多」，无从知道 AI 根本没生效
  if(!a || a.state === "none") return "";
  const fail = a.error ? `：${esc(a.error)}` : "";
  const unsure = a.unsure ? `，${a.unsure} 笔 AI 也拿不准（仍待确认）` : "";
  const txt = {
    no_key: `AI 兜底未启用：未配置 API Key（设置页填写后可自动归类）`,
    ok: `AI 兜底：归类 <b>${a.hit}</b> 笔${unsure}`,
    partial: `AI 兜底：归类 <b>${a.hit}</b> 笔${unsure}，<b style="color:var(--warn)">${a.failed} 笔未完成</b>${fail}`,
    error: `<b style="color:var(--warn)">AI 兜底失败</b>（${a.failed} 笔仍待确认）${fail}`,
  }[a.state];
  return txt ? `<br><span class="sub">${txt}</span>` : "";
}
function impSummaryHtml(r){
  // 全重复提示：重导同账单会被 row_hash 精确去重全部跳过——要重新匹配须先撤销原批次
  const allDup = (+r.new === 0 && +r.duplicate > 0)
    ? `——<b>该账单已导入过</b>：要重新匹配请先在下方批次表撤销原批次再导入` : "";
  // 柴米账单（本应用导出）：按原分类直入，不走自动分类与 AI 兜底（服务端同口径）
  const chaimi = r.chaimi ? `<b>柴米账单</b>：按原分类直接入库（不做自动分类与 AI 兜底）。` : "";
  return chaimi + `共 <b>${r.total}</b> 行：新 <b>${r.new}</b>，重复 ${r.duplicate}（将跳过）` +
    (r.cross_dupe ? `，跨源重复 <b style="color:var(--warn)">${r.cross_dupe}</b>（已在其他账单或手动记账）` : "") +
    `，无效 ${r.skip}，待确认 <b style="color:var(--pending)">${r.need_confirm}</b>` + allDup + impAiNote(r.ai);
}
function impOnPick(input){
  const f = input.files[0];
  const btn = $("#impBtn"), name = $("#imp_name");
  if(f){ btn.classList.add("has-file"); name.textContent = f.name; }
  else { btn.classList.remove("has-file"); name.textContent = "未选择文件"; }
}
const impGen = makeSeq();   // 解析在途代数（makeSeq 单源）：连点解析/试解析时并发响应乱序到达，
                            // 旧响应会把 impPreviews/impMeta/汇总整体回滚成上一次的结果（用户按旧预览确认导入）
async function impParse(tpl){   // tpl=向导试解析的自定义模板；缺省=常规解析（未识别时进向导）
  const seq = impGen.next();
  const f = tpl ? wizFile : $("#imp_file").files[0];
  if(!f){ if(!tpl) msg("请选择文件", false); return; }
  let useAi = $("#imp_ai").checked;
  if(useAi && !(await aiConfigured())){
    if(!tpl) msg("未配置 AI：请到设置页填写 LLM API Key，本次按未启用 AI 解析", false);   // 向导路径静默降级
    useAi = false;
  }
  if(!impGen.isCurrent(seq)) return;   // aiConfigured 弹确认期间用户又点了一次：让位给最新那次
  const sc = ledgerScope();   // 账本身份守卫：解析期间切账本，旧账本的预览/下拉刷新不回填本账本
  const fd = new FormData(); fd.append("file", f);
  if(tpl) fd.append("template", JSON.stringify(tpl));
  $("#imp_summary").textContent = (tpl ? "按模板" : "") + "解析中…" + (!tpl && useAi ? "（含 AI 兜底，最多约 40 秒）" : "");
  try{
    const r = await api(`/api/import/parse?ledger=${sc.id}&use_ai=${useAi}`, {method:"POST", body:fd});
    if(!impGen.isCurrent(seq) || !sc.isCurrent()) return;   // 已有更新一次解析/已切账本：本响应作废
    impPreviews = r.previews;
    // 冻结解析时的账本：confirm 用实时 lid() 时，预览期间切账本会把预览行提交到另一本账
    // （带旧账本分类/账户引用的行被服务端按行静默跳过，无引用行则入错账）
    impMeta = {ledger: sc.id, filename:r.filename, source:r.previews.find(p=>p.source)?.source || (tpl ? "custom" : "manual")};
    // 解析期可能自动建了银行卡账户 / 柴米账单缺失的同名分类：同步两张表，预览下拉才能回显
    await Promise.all([loadAccs(), loadCats()]);
    if(!impGen.isCurrent(seq) || !sc.isCurrent()) return;
    const sumHtml = impSummaryHtml(r);   // 一份 HTML 两处直赋（此前经 DOM 二次搬运）
    $("#imp_summary").innerHTML = sumHtml;
    document.querySelector("#pv_summary").innerHTML = sumHtml;
    renderPv();
    openPv();
  }catch(e){
    if(!impGen.isCurrent(seq) || !sc.isCurrent()) return;
    $("#imp_summary").textContent="";
    if(!tpl && e.message.includes("无法识别")){ msg("未识别的格式——进入模板向导", true, "info"); wizStart(f); }
    else msg(e.message, false);
  }
}

/* ===== 自定义模板向导（未识别格式 → 列映射 → 存模板） ===== */
const WIZ_FIELDS = [["date","交易时间 *"],["amount","金额 *"],["flow","收/支列"],["type","类型列"],
                    ["name","名称"],["order","订单号"],["pay","支付方式"],["merchant","商户/对方"],["note","备注"]];
let wizFile = null, wizInfo = null;
const wizGen = makeSeq();   // 嗅探代数（makeSeq 单源）：关向导/换文件后旧文件的迟到嗅探不回填
function wizClose(){ $("#wizMask").style.display="none"; wizFile = null; wizGen.next(); }
async function wizStart(file){
  const seq = wizGen.next();
  wizFile = file;
  wizInfo = null;   // 嗅探完成前不可用：此前沿用上一文件的表头（列映射整体错位）或为 null（TypeError）
  $("#wiz_file").textContent = `文件：${file.name}`;
  $("#wiz_name").value = file.name.replace(/\.[^.]+$/, "").slice(0, 20);
  $("#wiz_owner").value = ""; $("#wiz_strip").value = "";
  $("#wiz_out").value = "支出"; $("#wiz_in").value = "收入"; $("#wiz_refund").value = "退款";
  $("#wiz_samples").textContent = "读取中…";
  $("#wizMask").style.display = "flex";
  let info;
  try{
    const fd = new FormData(); fd.append("file", file);
    info = await api("/api/import/sniff", {method:"POST", body:fd});
  }catch(e){ if(!wizGen.isCurrent(seq)) return; wizClose(); return msg("嗅探失败：" + e.message, false); }
  if(!wizGen.isCurrent(seq)) return;
  if(!info.headers.length){ wizClose(); return msg("没找到像表头的行（至少 4 列）——请在 Excel 里整理后重试", false); }
  wizInfo = info;
  const opts = h => `<option value="${esc(h)}">${esc(h)}</option>`;
  $("#wiz_map").innerHTML = WIZ_FIELDS.map(([f, label]) =>
    `<div><label>${label}</label><select data-f="${f}"><option value="">（不映射）</option>${wizInfo.headers.map(opts).join("")}</select></div>`).join("");
  $("#wiz_samples").textContent = wizInfo.samples.map(r=>r.join(" | ")).join("\n");
  for(const [f] of WIZ_FIELDS){   // 预填嗅探猜测
    const sel = document.querySelector(`#wiz_map select[data-f="${f}"]`);
    if(sel && wizInfo.guess[f]) sel.value = wizInfo.guess[f];
  }
}
function wizCollect(){
  const col = {};
  document.querySelectorAll("#wiz_map select").forEach(s=>{ if(s.value) col[s.dataset.f] = s.value; });
  const flow_map = {};
  $("#wiz_out").value.split(/[,，\s]+/).filter(Boolean).forEach(v=>flow_map[v]="expense");
  $("#wiz_in").value.split(/[,，\s]+/).filter(Boolean).forEach(v=>flow_map[v]="income");
  return { name: $("#wiz_name").value.trim(), headers: wizInfo ? wizInfo.headers : [], col, flow_map,
           refund_type: $("#wiz_refund").value.trim(),
           name_strip: $("#wiz_strip").value.split(/[,，]/).map(s=>s.trim()).filter(Boolean),
           owner: $("#wiz_owner").value.trim(),
           skip_first_n: +$("#wiz_skipn")?.value || 0 };
}
function wizValidate(t){
  if(!wizInfo) return "文件还在读取中，请稍候再试";
  if(!t.name) return "请填写模板名";
  if(!t.col.date || !t.col.amount) return "交易时间与金额两列必须映射";
  if(!Object.keys(t.flow_map).length && !t.refund_type) return "请至少填写收支值或退款字样";
  return "";
}
async function wizTry(){
  const t = wizCollect(); const err = wizValidate(t);
  if(err) return msg(err, false);
  await impParse(t);   // 向导保持打开：预览叠于其上，可回来调整再试
}
async function wizSave(){
  const t = wizCollect(); const err = wizValidate(t);
  if(err) return msg(err, false);
  try{ await apiSend("/api/import/templates", "POST", t); }
  catch(e){ return msg(e.message, false); }
  msg("模板已保存，今后同格式文件自动识别 ✅");
  wizClose(); loadTpls();
  impParse();   // 保存后立即按常规流程重解析：自动分流会命中刚存的模板
}
async function loadTpls(){
  try{
    const ts = await api("/api/import/templates");
    $("#tplList").innerHTML = ts.length
      ? ts.map(t=>`<span class="tag plain">${esc(t.name)}</span> <a href="javascript:void(0)" onclick="tplDel('${qEsc(t.name)}')" style="color:var(--expense);font-size:12px">删</a>`).join(" · ")
      : "无（未识别的文件解析时会自动进入向导）";
  }catch(e){ console.warn("模板列表加载失败", e); $("#tplList").textContent = "无"; }   // 接口失败≠没有模板，留痕防误读
}
async function tplDel(name){
  if(!await uiConfirm(`删除模板「${name}」？不影响已导入数据。`, "删除")) return;
  await api(`/api/import/templates/${encodeURIComponent(name)}`, {method:"DELETE"});
  msg("已删除"); loadTpls();
}
function renderPv(){
  // _pvIdx 用原始数组下标（非显示序号）：impSkipRow/impSetCat 按 it 直寻 impPreviews[i]
  impPreviews.forEach((p, idx) => { p._pvIdx = idx; });
  // 可见 = 正常行 + 用户跳过行(可恢复) + 跨源/手动重复行(pay_dupe 带后端 skip)
  const vis = impPreviews.filter(p => !p.skip || p._pvSkip || p.pay_dupe);
  // 跨源/手动重复置顶展示：默认跳过防双计，点行内标签可改为导入（=手动确认）
  const pinned = vis.filter(p => p.pay_dupe), rest = vis.filter(p => !p.pay_dupe);
  const head = pinned.length
    ? `<div class="sub" style="padding:8px 4px 4px">⇅ 与其他账单/手动记账重复 <b>${pinned.length}</b> 笔（默认跳过防双计；点行内标签可改为导入）</div>` : "";
  const visAll = [...pinned, ...rest];
  const more = visAll.length > 500 ? `<div class="sub" style="padding:8px 4px">仅显示前 500 行（共 ${visAll.length} 行）——「确认导入」仍会提交全部行</div>` : "";
  document.querySelector("#pv_body").innerHTML = head + visAll.slice(0, 500)
    .map(p => txRow(p, "preview")).join("") + more;
}
function impSkipRow(i){
  const p = impPreviews[i]; if(!p) return;
  p.skip = !p.skip;
  if(p.skip) p._pvSkip = true; else delete p._pvSkip;
  renderPv();
}

function openPv(){ $("#pvMask").style.display="flex"; }
function closePv(){ $("#pvMask").style.display="none"; }
async function impSetCat(sel){
  const i = +sel.dataset.pvIdx;
  const p = impPreviews[i];
  const sc = ledgerScope();   // 预览行归属解析时的账本：await 期间切账本，旧账本 id 不写 impPreviews
  if(sel.value === "__new__"){
    let r = null;
    try{ r = await newCategoryAndPick(p.type || "expense", "新分类名（自定义分类）："); }
    catch(e){ msg(e.message || "新建分类失败", false); }   // 新建失败（重名/网络）：与取消同款回滚，停在 __new__ 会让该行静默变未分类
    if(!r){ sel.value = p.category_id || ""; return; }
    if(!sc.isCurrent()) return msg("已切换账本，预览已过期，请重新解析", false);
    const opt = document.createElement("option");
    opt.value = r.id; opt.textContent = r.name;
    sel.insertBefore(opt, sel.querySelector('option[value="__new__"]'));
    sel.value = r.id;
  }
  p.user_changed = true;   // 仅用户手动改分类才回写学习映射
  p.category_id = +sel.value || null;
  p.need_confirm = false;
  p.classified_via = "manual";
  const cat = categories.find(c=>c.id === +sel.value);
  if(cat && cat.type === "transfer"){
    p.type = "transfer";
    p.tags = Object.assign({}, p.tags, {transfer_sub: cat.name === "退款" ? "退款" : TX_SUB_DEFAULT});   // 兜底子类走 core.js 单点（与后端 db.DEFAULT_TRANSFER_SUB 同值）
    p.category_name = p.parent_category = p.tags.transfer_sub;
    renderPv();
    return;
  }
  if(cat){
    // 名字字段同步（含取消新建 cat 为空时不回抹，保留服务端兜底名）：data-cat 图标取色
    // 等按 parent_category 优先的非 select 展示与提交值一致
    p.category_name = p.parent_category = cat.name;
  }
  // 移除该行的"待确认"标签
  const row = sel.closest(".tx-row");
  row && row.querySelector(".tag.warn")?.remove();
}
async function impConfirm(btn){
  if(!impPreviews) return;
  if(btn) btn.disabled = true;   // 提交期间锁按钮防重复导入；失败也要恢复（finally），否则预览弹窗卡死
  try{
    // 去掉仅前端用的标记（_pvIdx/_pvSkip），避免传给后端
    const payload = impPreviews.map(({_pvIdx, _pvSkip, ...rest}) => rest);
    // 账本取解析时冻结值（impParse 写入）：预览与确认必须同一本账
    const r = await apiSend(`/api/import/confirm?ledger=${impMeta.ledger || lid()}`, "POST", {previews:payload, ...impMeta});
    msg(`已导入 ${r.imported} 笔（批次 #${r.batch_id}，可在下方撤销）`);
    closePv(); $("#imp_summary").textContent="";
    // 与记一笔/编辑同口径走 afterTxWrite：流水/徽标/汇总/统计/批次表（io 页）/账户全刷新——
    // 不再显式叠加 loadBatches/loadAccs（此前各双拉一次，批次表每次导入扫两遍）
    afterTxWrite();
  }finally{
    if(btn) btn.disabled = false;   // 失败信息由全局 unhandledrejection 兜底 toast，这里只负责恢复按钮
  }
}
async function loadBatches(){
  loadTpls();
  loadRepair();
  const sc = ledgerScope();   // 账本身份守卫（ledgerScope 单源）：旧账本批次表的迟到响应不渲染
  const bs = await api(`/api/import/batches?ledger=${sc.id}`);
  if(!sc.isCurrent()) return;
  document.querySelector("#batchTable tbody").innerHTML = bs.map(b=>
    `<tr><td>${(b.imported_at||"").slice(0,16)}</td><td>${esc(b.filename)}</td><td>${esc(b.source)}</td><td>${b.count}</td>
     <td><button class="btn btn-ghost" style="padding:4px 10px;font-size:12px" onclick="undoBatch(${b.id})">撤销</button></td></tr>`).join("") ||
    '<tr><td colspan="5" class="sub">暂无批次</td></tr>';
}
async function undoBatch(id){
  if(!await uiConfirm("撤销将删除该批次全部流水，确定？", "撤销")) return;
  const r = await api(`/api/import/batches/${id}`, {method:"DELETE"});
  msg(`已撤销 ${r.deleted} 笔`);
  afterTxWrite();   // io 页下已含 loadBatches（撤销入口只在导入页，无需单独再拉）
}

/* ===== 数据修复：只读列出与词典冲突的差异，点「应用修复」才写库（不自动改） ===== */
let repairInfo = null;
async function loadRepair(){
  const el = $("#imp_repair"), det = $("#imp_repair_detail");
  if(!el) return;
  const sc = ledgerScope();   // 账本身份守卫：旧账本修复预览的迟到响应不写全局 repairInfo/DOM
  try{ const info = await api(`/api/repair/preview?ledger=${sc.id}`);
       if(!sc.isCurrent()) return; repairInfo = info; }
  catch(e){ console.warn("修复预览加载失败，修复区隐藏", e); if(!sc.isCurrent()) return; repairInfo = null; }
  const n = repairInfo?.tx_fixes.length || 0, m = repairInfo?.mapping_resets.length || 0,
        a = repairInfo?.account_merges.length || 0;
  if(!n && !m && !a){ el.style.display="none"; det.style.display="none"; return; }
  el.style.display="";
  const bits = [];
  if(a) bits.push(`<b>${a}</b> 组重复账户可合并`);
  if(n) bits.push(`<b>${n}</b> 笔历史流水的分类与关键词词典不一致`);
  if(m) bits.push(`<b>${m}</b> 条历史遗留的学习映射与内置默认冲突`);
  el.innerHTML = `检测到 ${bits.join("、")}（不会再自动修改）` +
    ` <a href="javascript:void(0)" onclick="repairDetail()" style="text-decoration:underline">查看</a>` +
    `<button class="btn btn-ghost" style="margin-left:8px;padding:2px 10px;font-size:12px" onclick="repairApply()">应用修复</button>`;
}
function repairDetail(){
  const det = $("#imp_repair_detail");
  if(det.style.display !== "none" && det.innerHTML) { det.style.display="none"; return; }
  const acc = (repairInfo?.account_merges || []).map(g =>
    `<div>· 账户合并：${g.accounts.map(x=>esc(x.name)).join("、")} → <b>${esc(g.key)}</b></div>`).join("");
  const tx = (repairInfo?.tx_fixes || []).slice(0, 100).map(f =>
    `<div>· ${(f.date || "").slice(0,10)} ${esc(f.name)} ${curSym()}${fmt(f.amount)}：${esc(f.current)} → <b>${esc(f.target)}</b></div>`).join("");
  const mp = (repairInfo?.mapping_resets || []).map(f =>
    `<div>· 映射「${esc(f.source)}」（${esc(f.flow_type)}）：${esc(f.learned)} → 默认 ${esc(f.default)}</div>`).join("");
  det.innerHTML = `<div class="sub" style="border-top:1px dashed var(--line,#ddd);margin-top:4px;padding-top:4px">${acc}${tx}${mp}</div>`;
  det.style.display = "";
}
async function repairApply(){
  if(!await uiConfirm("应用修复：合并归一同名的重复账户、按词典纠正冲突分类、重置历史遗留的学习映射。你手动设定过的分类不会被修改。", "应用修复")) return;
  try{
    const r = await apiSend(`/api/repair/apply?ledger=${lid()}`, "POST", {});
    msg(`已合并 ${r.accounts_merged} 个账户，纠正 ${r.tx_fixed} 笔分类，重置 ${r.mappings_reset} 条映射`);
    afterTxWrite();   // io 页下含 loadBatches→loadRepair（修复区刷新）与 loadAccs，此前三重拉取
  }catch(e){ msg(e.message, false); }
}