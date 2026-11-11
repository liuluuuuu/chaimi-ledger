/* ===== 共享分类管理模块（app 设置页 #catMgr 与 home 建用户页抽屉复用）=====
   两页上下文差异由顶部适配层吸收：app（core.js+settings.js）有 lid/uiConfirm/
   uiPrompt/loadTx 等；home 独立页经 shared-lite.js 提供 api/apiSend/esc/qEsc/$。
   样式与「新建分类」弹窗 DOM 内置（首次使用注入/构建），views.css 原分类管理
   样式已并入此处（.catmgr-root 作用域，单源）。 */
const CAT_GROUP_CN = {expense: "支出", income: "收入", transfer: "退款和不计收支"};

/* ---- 上下文适配层 ---- */
const _cmLid = () => typeof lid === "function" ? lid() : (typeof LEDGER !== "undefined" ? LEDGER : 0);
const _cmConfirm = (t, okLabel) => typeof uiConfirm === "function" ? uiConfirm(t, okLabel)
  : Promise.resolve(confirm(t));
const _cmPrompt = (t, def) => typeof uiPrompt === "function" ? uiPrompt(t, def)
  : Promise.resolve(prompt(t, def == null ? "" : def) || null);
/* 挂来源映射（catMgrAliasAdd 与 catNewSubmit 共用一份请求体构造）；不计收支组按支出侧落行，解析端不分侧命中 */
const _cmAddMapping = (l, type, source, cid) =>
  apiSend(`/api/mappings?ledger=${l}`, "POST",
          {flow_type: type === "transfer" ? "expense" : type, source, category_id: cid});

/* ---- 内置样式（.catmgr-root 作用域；变量沿用 base.css token，两页都有。
   新建分类弹窗直接用 base.css 标准结构（.modal-mask/.modal/.modal-head，与流水
   弹窗同款，含顶部渐变条与 ✕；home 页同样加载 base.css），不再自绘卡片） ---- */
(function(){
  const st = document.createElement("style");
  st.textContent = `
.catmgr-root .mchip{display:inline-flex;align-items:center;gap:4px;background:var(--chip);border:1px solid var(--border);border-radius:8px;padding:2px 8px;margin:2px 4px 2px 0;font-size:13px;cursor:pointer}
.catmgr-root .mchip:hover{border-color:var(--primary-2)}
.catmgr-root .mchip b{color:var(--muted);font-weight:400;padding:0 2px}
.catmgr-root .mchip b:hover{color:var(--expense)}
.catmgr-root .cm-group{margin-top:14px}
.catmgr-root .cm-gtitle{font-weight:600;font-size:13px;color:var(--muted);margin-bottom:6px;display:flex;align-items:center}
.catmgr-root .cm-cat{border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:8px;display:flex;align-items:center;gap:10px}
.catmgr-root .cm-cname{flex:0 0 auto;font-weight:600}
.catmgr-root .cm-cname.edit{cursor:pointer;color:var(--primary);text-decoration:underline dotted}
.catmgr-root .cm-chips{flex:1 1 auto;min-width:0;display:flex;flex-wrap:wrap;gap:2px 4px;align-items:center;justify-content:flex-end}`;
  document.head.appendChild(st);
})();

/* ---- 内置「新建分类」弹窗 DOM（app 的 Esc 栈按 id 引用 catNewMask/catNewClose，id 保持不变） ---- */
function _cmEnsureCatNew(){
  if(document.getElementById("catNewMask")) return;
  const d = document.createElement("div");
  d.id = "catNewMask"; d.className = "modal-mask";
  d.setAttribute("onclick", "if(event.target===this)catNewClose()");
  d.style.display = "none";
  d.innerHTML = `<div class="modal" role="dialog" aria-modal="true" style="max-width:360px">
    <div class="modal-head"><b id="catNewTitle">新建分类</b>
      <button class="icon-btn" onclick="catNewClose()" title="关闭">✕</button></div>
    <div style="padding:16px 20px 20px">
      <label>分类名 <span style="font-size:11px">必填</span></label>
      <input id="catNewName" autocomplete="off" placeholder="如：宠物开销"
             onkeydown="if(event.key==='Enter')catNewSubmit();else if(event.key==='Escape')catNewClose()">
      <label style="margin-top:10px">包含来源名 <span style="font-size:11px">账单原始分类，可多个（逗号/顿号分隔）</span></label>
      <input id="catNewSources" autocomplete="off" placeholder="如：宠物服务，宠物用品"
             onkeydown="if(event.key==='Enter')catNewSubmit();else if(event.key==='Escape')catNewClose()">
    </div>
    <div class="modal-foot">
      <button class="btn btn-ghost" onclick="catNewClose()">取消</button>
      <button class="btn btn-pri" onclick="catNewSubmit()">创建</button>
    </div>
  </div>`;
  document.body.appendChild(d);
}

/* ---- 编辑态开关（按钮 id 约定 catEditBtn，两页各自放置）：mkEditToggle 单源在 shared-lite（两页共载） ---- */
const catEdit = mkEditToggle("catEditBtn", () => loadCatMgr());
function toggleCatEdit(){ catEdit.toggle(); }

async function loadCatMgr(){
  const sc = ledgerScope(_cmLid);   // 账本身份守卫（ledgerScope 单源，getter 注入适配双页）：
                                    // await 期间切账本/换页，旧响应不写分类表
  const [cats, maps] = await Promise.all([
    api(`/api/categories?ledger=${sc.id}`),
    api(`/api/mappings?ledger=${sc.id}`)
  ]);
  if(!sc.isCurrent()) return;
  /* app：写入 core.js 的全局 categories（记一笔下拉/编辑弹窗同源；跨脚本可写不可重声明）；
     home：无该全局，隐式全局（经典脚本）供本模块改名查找用 */
  categories = cats;
  const byCat = {};
  for(const m of maps) (byCat[m.category_id] = byCat[m.category_id] || []).push(m);
  $("#catMgr").innerHTML = ["expense","income","transfer"].map(g=>{
    const tops = cats.filter(c=>!c.parent_id && c.type===g);
    if(!tops.length) return "";
    return `<div class="cm-group"><div class="cm-gtitle">${CAT_GROUP_CN[g]}
        <button class="btn btn-ghost" style="padding:2px 10px;font-size:11px;margin-left:6px" onclick="catMgrNew('${g}')">＋新建分类</button></div>`
      + tops.map(c=>{
        const chips = (byCat[c.id]||[]).map(m=>
          `<span class="mchip" title="账单来源名">${esc(m.source)}${catEdit.on?`<b onclick="event.stopPropagation();catMgrAliasDel(${m.id})">×</b>`:""}</span>`).join("");
        return `<div class="cm-cat">
          <div class="cm-cname${catEdit.on?" edit":""}"${catEdit.on?` onclick="catMgrRename(${c.id})" title="点此改名"`:""}>${esc(c.name)}</div>
          <div class="cm-chips">${chips}
            ${catEdit.on?`<button class="btn btn-ghost" style="padding:2px 8px;font-size:11px" onclick="catMgrAliasAdd(${c.id})">＋来源</button>`:""}
          </div>
            ${catEdit.on?`<button class="btn danger" style="padding:2px 10px;font-size:11px;flex:0 0 auto" onclick="catMgrDel(${c.id},'${qEsc(c.name)}')">删除</button>`:""}
        </div>`;
      }).join("") + `</div>`;
  }).join("") || '<div class="sub">暂无分类</div>';
}
async function catMgrRename(id){
  const c = categories.find(x=>x.id===id); if(!c) return;
  const name = await _cmPrompt("分类改名（流水、来源名自动同步）：", c.name);
  if(!name || name===c.name) return;
  try{
    await apiSend(`/api/categories/${id}`, "PUT", {name:name.trim(), icon:c.icon||""});
  }catch(e){ return msg(e.message, false); }
  msg("已改名（流水自动同步） ✅"); await loadCatMgr();
}
async function catMgrAliasAdd(id){
  const c = categories.find(x=>x.id===id); if(!c) return;
  const source = await _cmPrompt(`添加「${c.name}」包含的账单来源名（如：汽车用品）：`);
  if(!source || !source.trim()) return;
  // 同来源已挂其他分类时 INSERT OR REPLACE 自动改挂到这里（移动语义）
  try{
    await _cmAddMapping(_cmLid(), c.type, source.trim(), id);
  }catch(e){ return msg(e.message, false); }
  msg("来源已添加 ✅"); await loadCatMgr();
}
async function catMgrAliasDel(id){
  if(!await _cmConfirm("移除这个来源名？（该账单分类下次导入不再落到此分类）", "移除")) return;
  await api(`/api/mappings/${id}`, {method:"DELETE"});
  await loadCatMgr();
}

let catNewType = "expense";
function catMgrNew(type){
  catNewType = type;
  _cmEnsureCatNew();
  $("#catNewTitle").textContent = `新建${CAT_GROUP_CN[type]||""}分类`;
  $("#catNewName").value = ""; $("#catNewSources").value = "";
  $("#catNewMask").style.display = "flex";   /* style.display 而非 class：app 的 Esc 栈按此判断 */
  setTimeout(()=>$("#catNewName").focus(), 50);
}
function catNewClose(){ $("#catNewMask").style.display = "none"; }
function catNewLayer(){   /* 弹窗开着→其关闭动作；懒构建未打开过不存在，判空——app navTopModal 与 home homeTopLayer 共用单源 */
  const cn = $("#catNewMask");
  return cn && cn.style.display === "flex" ? () => catNewClose() : null;
}
async function catNewSubmit(){
  const sc = ledgerScope(_cmLid);   // 提交期间的账本身份：弹窗关闭后侧栏可点，逐条挂接中切账本会让 mapping 落到新账本（必然 400 进 failed，且提示里的重试路径在新账本不存在）
  const name = $("#catNewName").value.trim();
  if(!name) return msg("请填写分类名", false);
  let cid;
  try{
    const r = await apiSend(`/api/categories?ledger=${sc.id}`, "POST", {name, type: catNewType});
    cid = r.id;
  }catch(e){ return msg(e.message, false); }
  // 分类已建成功：此后任一来源挂接失败都不能再把弹窗留给用户原样重提——重提会撞
  // 「分类已存在」死循环。先关弹窗再逐个补挂；失败项列出，用「添加来源名」即可幂等重试
  catNewClose();
  // 来源名逐个挂到新分类（同来源已挂别处自动改挂）
  const srcs = [...new Set($("#catNewSources").value.split(/[，,、;；\s]+/).map(s=>s.trim()).filter(Boolean))];
  const failed = [];
  let switched = false;
  for(const s of srcs){
    if(!sc.isCurrent()){ switched = true; break; }   // 已切账本：余下来源不再挂（新账本没有该分类，挂了也 400）
    try{
      await _cmAddMapping(sc.id, catNewType, s, cid);
    }catch(_){ failed.push(s); }
  }
  await loadCatMgr();
  if(switched) msg(`分类已创建到原账本，但期间切换了账本，余下来源未挂接——请切回后在分类页「添加来源名」补挂`, false);
  else if(failed.length) msg(`分类已创建，但这些来源名挂接失败：${failed.join("、")}（分类页「添加来源名」可重试）`, false);
  else msg("分类已创建 ✅");
}
async function catMgrDel(id, name){
  if(!await _cmConfirm(`删除分类「${name}」：其历史流水将变为未分类并进待确认，包含的来源名一并移除。确定？`, "删除")) return;
  await api(`/api/categories/${id}`, {method:"DELETE"});
  await loadCatMgr();
  /* app-only：受影响流水进待确认，列表与徽标同步刷新 */
  typeof loadTx === "function" && loadTx();
  typeof loadPendingBadge === "function" && loadPendingBadge();
}
