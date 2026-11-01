/* ===== 导入 ===== */
function impSummaryHtml(r){
  // 全重复提示：重导同账单会被 row_hash 精确去重全部跳过——要重新匹配须先撤销原批次
  const allDup = (+r.new === 0 && +r.duplicate > 0)
    ? `——<b>该账单已导入过</b>：要重新匹配请先在下方批次表撤销原批次再导入` : "";
  return `共 <b>${r.total}</b> 行：新 <b>${r.new}</b>，重复 ${r.duplicate}（将跳过）` +
    (r.cross_dupe ? `，跨源重复 <b style="color:var(--warn,#e8a93a)">${r.cross_dupe}</b>（已在其他账单或手动记账）` : "") +
    `，无效 ${r.skip}，待确认 <b style="color:var(--pending)">${r.need_confirm}</b>` + allDup;
}
function impOnPick(input){
  const f = input.files[0];
  const btn = $("#impBtn"), name = $("#imp_name");
  if(f){ btn.classList.add("has-file"); name.textContent = f.name; }
  else { btn.classList.remove("has-file"); name.textContent = "未选择文件"; }
}
async function impParse(tpl){   // tpl=向导试解析的自定义模板；缺省=常规解析（未识别时进向导）
  const f = tpl ? wizFile : $("#imp_file").files[0];
  if(!f){ if(!tpl) msg("请选择文件", false); return; }
  let useAi = $("#imp_ai").checked;
  if(useAi && !(await aiConfigured())){
    if(!tpl) msg("未配置 AI：请到设置页填写 LLM API Key，本次按未启用 AI 解析", false);   // 向导路径静默降级
    useAi = false;
  }
  const fd = new FormData(); fd.append("file", f);
  if(tpl) fd.append("template", JSON.stringify(tpl));
  $("#imp_summary").textContent = (tpl ? "按模板" : "") + "解析中…" + (!tpl && useAi ? "（AI 兜底可能较慢）" : "");
  try{
    const r = await api(`/api/import/parse?ledger=${lid()}&use_ai=${useAi}`, {method:"POST", body:fd});
    impPreviews = r.previews;
    impMeta = {filename:r.filename, source:r.previews.find(p=>p.source)?.source || (tpl ? "custom" : "manual")};
    await loadAccs();   // 解析期可能自动建了银行卡账户，同步账户列表供编辑弹窗/记一笔使用
    $("#imp_summary").innerHTML = impSummaryHtml(r);
    document.querySelector("#pv_summary").innerHTML = $("#imp_summary").innerHTML;
    renderPv();
    openPv();
  }catch(e){
    $("#imp_summary").textContent="";
    if(!tpl && e.message.includes("无法识别")){ msg("未识别的格式——进入模板向导", true, "info"); wizStart(f); }
    else msg(e.message, false);
  }
}

/* ===== 自定义模板向导（未识别格式 → 列映射 → 存模板） ===== */
const WIZ_FIELDS = [["date","交易时间 *"],["amount","金额 *"],["flow","收/支列"],["type","类型列"],
                    ["name","名称"],["order","订单号"],["pay","支付方式"],["merchant","商户/对方"],["note","备注"]];
let wizFile = null, wizInfo = null;
function wizClose(){ $("#wizMask").style.display="none"; wizFile = null; }
async function wizStart(file){
  wizFile = file;
  $("#wiz_file").textContent = `文件：${file.name}`;
  $("#wiz_name").value = file.name.replace(/\.[^.]+$/, "").slice(0, 20);
  $("#wiz_owner").value = ""; $("#wiz_strip").value = "";
  $("#wiz_out").value = "支出"; $("#wiz_in").value = "收入"; $("#wiz_refund").value = "退款";
  $("#wiz_samples").textContent = "读取中…";
  $("#wizMask").style.display = "flex";
  try{
    const fd = new FormData(); fd.append("file", file);
    wizInfo = await api("/api/import/sniff", {method:"POST", body:fd});
  }catch(e){ wizClose(); return msg("嗅探失败：" + e.message, false); }
  if(!wizInfo.headers.length){ wizClose(); return msg("没找到像表头的行（至少 4 列）——请在 Excel 里整理后重试", false); }
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
  return { name: $("#wiz_name").value.trim(), headers: wizInfo.headers, col, flow_map,
           refund_type: $("#wiz_refund").value.trim(),
           name_strip: $("#wiz_strip").value.split(/[,，]/).map(s=>s.trim()).filter(Boolean),
           owner: $("#wiz_owner").value.trim(),
           skip_first_n: +$("#wiz_skipn")?.value || 0 };
}
function wizValidate(t){
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
  }catch(_){ $("#tplList").textContent = "无"; }
}
async function tplDel(name){
  if(!await uiConfirm(`删除模板「${name}」？不影响已导入数据。`, "删除")) return;
  await api(`/api/import/templates/${encodeURIComponent(name)}`, {method:"DELETE"});
  msg("已删除"); loadTpls();
}
function renderPv(){
  // _pvIdx 用原始数组下标（非显示序号）：impSkipRow/impSetCat 按 it 直寻 impPreviews[i]——
  // 旧版给过滤后的连续显示序号，前方有隐藏的解析期 skip 行时点击会错位到别的行
  impPreviews.forEach((p, idx) => { p._pvIdx = idx; });
  // 可见 = 正常行 + 用户跳过行(可恢复) + 跨源/手动重复行(pay_dupe 带后端 skip，
  // 旧版被当解析期无效行隐藏——汇总里数着 N 笔列表里却看不到、也无法恢复)
  const vis = impPreviews.filter(p => !p.skip || p._pvSkip || p.pay_dupe);
  // 跨源/手动重复置顶展示：默认跳过防双计，点行内标签可改为导入（=手动确认）
  const pinned = vis.filter(p => p.pay_dupe), rest = vis.filter(p => !p.pay_dupe);
  const head = pinned.length
    ? `<div class="sub" style="padding:8px 4px 4px">⇅ 与其他账单/手动记账重复 <b>${pinned.length}</b> 笔（默认跳过防双计；点行内标签可改为导入）</div>` : "";
  document.querySelector("#pv_body").innerHTML = head + [...pinned, ...rest].slice(0, 500)
    .map(p => txRow(p, "preview")).join("");
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
  if(sel.value === "__new__"){
    const r = await newCategoryAndPick(p.type || "expense", "新分类名（自定义分类）：");
    if(!r){ sel.value = p.category_id || ""; return; }
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
    p.tags = Object.assign({}, p.tags, {transfer_sub: cat.name === "退款" ? "退款" : "账户转账"});   // 兜底子类与后端 db.DEFAULT_TRANSFER_SUB 同值（改需同步）
    p.category_name = p.parent_category = p.tags.transfer_sub;
    renderPv();
    return;
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
    const r = await apiSend(`/api/import/confirm?ledger=${lid()}`, "POST", {previews:payload, ...impMeta});
    msg(`已导入 ${r.imported} 笔（批次 #${r.batch_id}，可在下方撤销）`);
    closePv(); $("#imp_summary").textContent="";
    // 与记一笔/编辑同口径走 afterTxWrite：流水列表、待确认徽标/chip、汇总条、统计全部实时刷新

    afterTxWrite(); loadBatches(); loadAccs();
  }finally{
    if(btn) btn.disabled = false;   // 失败信息由全局 unhandledrejection 兜底 toast，这里只负责恢复按钮
  }
}
async function loadBatches(){
  loadTpls();
  loadRepair();
  const bs = await api(`/api/import/batches?ledger=${lid()}`);
  document.querySelector("#batchTable tbody").innerHTML = bs.map(b=>
    `<tr><td>${(b.imported_at||"").slice(0,16)}</td><td>${esc(b.filename)}</td><td>${esc(b.source)}</td><td>${b.count}</td>
     <td><button class="btn btn-ghost" style="padding:4px 10px;font-size:12px" onclick="undoBatch(${b.id})">撤销</button></td></tr>`).join("") ||
    '<tr><td colspan="5" class="sub">暂无批次</td></tr>';
}
async function undoBatch(id){
  if(!await uiConfirm("撤销将删除该批次全部流水，确定？", "撤销")) return;
  const r = await api(`/api/import/batches/${id}`, {method:"DELETE"});
  msg(`已撤销 ${r.deleted} 笔`); loadBatches(); afterTxWrite();
}

/* ===== 数据修复（原导入自愈的确认版） =====
   旧版解析预览会静默把与词典冲突的历史分类改回去——手动整理的分类在下次预览时
   被"还原"。现改为这里只读列出差异，点「应用修复」才写库。 */
let repairInfo = null;
async function loadRepair(){
  const el = $("#imp_repair"), det = $("#imp_repair_detail");
  if(!el) return;
  try{ repairInfo = await api(`/api/repair/preview?ledger=${lid()}`); }
  catch(_){ repairInfo = null; }
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
    `<div>· ${(f.date || "").slice(0,10)} ${esc(f.name)} ¥${fmt(f.amount)}：${esc(f.current)} → <b>${esc(f.target)}</b></div>`).join("");
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
    loadRepair(); afterTxWrite(); loadAccs();
  }catch(e){ msg(e.message, false); }
}