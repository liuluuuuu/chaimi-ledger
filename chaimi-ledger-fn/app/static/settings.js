/* ===== 设置层 ===== */
/* 应用级区块（AI 配置/备份与日志）仅 NAS 管理员可见可写（MULTIUSER-PLAN §3.3；
   服务端 _admin_or_403 同口径双保险）；微信机器人 A 通道按账本收口，全员可见；
   本地开发（无网关身份）视为管理员 */
const stIsAdmin = () => !ME.uid || !!ME.isadmin;
function stAdminVis(){
  const admin = stIsAdmin();
  ["ai","backup"].forEach(s=>{
    const tab = document.querySelector(`#stTabs button[data-s="${s}"]`);
    const sec = document.querySelector(`.st-sec[data-s="${s}"]`);
    if(tab) tab.style.display = admin ? "" : "none";
    if(sec) sec.style.display = admin ? "" : "none";
  });
  // 机器人：区块全员可见；模式/白名单是应用级配置，非管理员只读（服务端同口径 admin 门）
  $("#bot_mode_box").style.display = admin ? "" : "none";
  $("#bot_save").style.display = admin ? "" : "none";
  $("#bot_mode_ro").style.display = admin ? "none" : "";
  $("#bot_users_row").style.display = admin ? "" : "none";
  if(!admin){
    const on = document.querySelector("#stTabs button.on");
    if(on && on.style.display === "none") stTab("book");
  }
}
function stLoad(){
  stAdminVis();
  loadLedgers();loadCatMgr();stAccs();stAttrs();
  loadBotCfg();   // 机器人区块全员可见；账本下拉/成员表服务端按身份收口
  if(stIsAdmin()) { loadCfg();loadBks();loadBkCfg(); }   // /api/backups 等已 admin 门，非管理员不拉
}
function stTab(name){
  document.querySelectorAll("#stTabs button").forEach(b=>b.classList.toggle("on", b.dataset.s===name));
  document.querySelectorAll(".st-sec").forEach(x=>x.classList.toggle("on", x.dataset.s===name));
}
document.querySelectorAll("#stTabs button").forEach(b=>b.onclick=()=>stTab(b.dataset.s));

// 平台预设：切换后自动填入地址/模型名（Key 不覆盖，用户手填）
const LLM_PRESETS=[
  {id:"bailian",name:"阿里云百炼（Qwen，文本+视觉）",base_url:"https://dashscope.aliyuncs.com/compatible-mode/v1",model:"qwen-plus",vl_model:"qwen3-vl-plus"},
  {id:"zhipu",name:"智谱 AI（GLM，文本+视觉）",base_url:"https://open.bigmodel.cn/api/paas/v4",model:"glm-4-air",vl_model:"glm-4v-flash"},
  {id:"deepseek",name:"DeepSeek（文本+视觉）",base_url:"https://api.deepseek.com/v1",model:"deepseek-chat",vl_model:"deepseek-flash"},
  {id:"ollama",name:"本地 Ollama（需先 ollama pull 模型；Key 随便填）",base_url:"http://localhost:11434/v1",model:"qwen2.5:3b",vl_model:"qwen2.5vl:3b"},
  {id:"lmstudio",name:"本地 LM Studio（需在软件里开启本地服务器）",base_url:"http://localhost:1234/v1",model:"qwen2.5-3b-instruct",vl_model:"qwen2.5-vl-3b-instruct"},
];
function applyPreset(id){
  const p=LLM_PRESETS.find(x=>x.id===id);
  const custom = !p;
  $("#vlApiRow").style.display = custom ? "" : "none";
  if(!custom){
    $("#llm_url").value=p.base_url; $("#llm_model").value=p.model; $("#vl_model").value=p.vl_model;
    $("#vl_url").value=""; $("#vl_key").value="";
  }
}
async function loadCfg(){
  const c = await getCfg();
  $("#llm_preset").innerHTML=LLM_PRESETS.map(p=>`<option value="${p.id}">${p.name}</option>`).join("")
    +`<option value="custom">自定义</option>`;
  $("#llm_url").value=c.llm.base_url; $("#llm_model").value=c.llm.model;
  $("#vl_model").value=c.llm.vl_model||""; $("#llm_key").value=c.llm.api_key;
  $("#vl_url").value=c.llm.vl_base_url||""; $("#vl_key").value=c.llm.vl_api_key||"";
  const hit=LLM_PRESETS.find(x=>x.base_url===c.llm.base_url);
  $("#llm_preset").value=hit?hit.id:"custom";   // 按地址回显预设；手改过的显示为自定义
  $("#vlApiRow").style.display = (hit && !c.llm.vl_base_url && !c.llm.vl_api_key) ? "none" : "";
}
async function saveCfg(){
  await apiSend("/api/config","PUT",{llm:{base_url:$("#llm_url").value,model:$("#llm_model").value,vl_model:$("#vl_model").value,api_key:$("#llm_key").value,
      vl_base_url:$("#vl_url").value.trim(),vl_api_key:$("#vl_key").value.trim()}});
  _cfgCache = null;   // 配置已变：缓存失效（loadCfg 重取）
  msg("已保存 ✅"); loadCfg();
}
document.addEventListener("DOMContentLoaded", () => {
  const t = document.getElementById("verTag");
  if (t) t.textContent = "chaimi-ledger v" + (window.CHAIMI_VER || "?");
});

async function llmModelsOpen(){
  // 惯例同 catNew:内联 display:flex 覆盖 .modal-mask 的 CSS display:none
  // (曾误用 display="" 只清内联,回落 CSS 仍 none——弹窗 DOM 开着但视觉隐形)
  $("#llmModelsMask").style.display = "flex";
  const b=$("#llmModelsBody");
  b.innerHTML='<div class="sub" style="padding:6px 0">获取中…</div>';
  let d; try{ d=await api("/api/llm/models"); }
  catch(e){ b.innerHTML=`<div class="sub">获取失败: ${esc(e.message)}</div>`; return; }
  if(d.error&&!d.text.length&&!d.vision.length){ b.innerHTML=`<div class="sub">${esc(d.error)}</div>`; return; }
  // 模型名来自外部 LLM 服务商的 /models 接口（不可信输入）：文本与 onclick 一律转义
  const grp=(t,arr,fill)=>`<label style="display:block;margin-top:10px">${t} <span class="sub" style="font-size:11px">点击选用</span></label>`+
    (arr.length?`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">${arr.map(x=>
      `<button class="btn btn-ghost" style="padding:3px 10px;font-size:12px" onclick="$('#${fill}').value='${qEsc(x)}';llmModelsClose()">${esc(x)}</button>`).join("")}</div>`
    :'<div class="sub" style="margin-top:4px">无可用模型</div>');
  b.innerHTML=grp("文本模型",d.text,"llm_model")+grp("视觉模型",d.vision,"vl_model")
    +(d.error?`<div class="sub" style="margin-top:10px">${esc(d.error)}</div>`:"");
}
function llmModelsClose(){ $("#llmModelsMask").style.display="none"; }

const LEDGER_CURS = [["CNY","人民币 ¥"],["USD","美元 $"],["EUR","欧元 €"],["JPY","日元 ¥"],["HKD","港币 HK$"],["GBP","英镑 £"]];
const lgMine = l => l.mine !== false;   // mine=false=别人共享来的（undefined=系统上下文/旧接口，视为自己）
async function loadLedgers(){
  const ls = await getLedgers();
  // 共享按钮固定最右列（删除列在其左，单账本时删除列为空、共享仍贴右缘）；
  // 币种：owner 可改下拉，共享来的账本只读文本；删除/共享管理均 owner-only（服务端同口径）
  document.querySelector("#ledgerTable tbody").innerHTML=ls.map(l=>
    `<tr><td>${l.icon?esc(l.icon)+" ":""}${esc(l.name)}</td><td>${
        lgMine(l)
        ? `<select class="lg-cur" onchange="setLedgerCur(${l.id}, this)">
             ${LEDGER_CURS.map(([v,t])=>`<option value="${v}" ${l.currency===v?"selected":""}>${t}</option>`).join("")}
             ${LEDGER_CURS.some(([v])=>v===l.currency)?"":`<option selected>${esc(l.currency)}</option>`}
           </select>`
        : `<span class="sub">${esc(l.currency||"CNY")}</span>`
      }</td>
     <td class="lg-del">${ls.length>1 && lgMine(l) ? `<button class="btn danger" onclick="delLedger(${l.id})">删除</button>` : ""}</td>
     <td style="text-align:right">${lgMine(l)
        ? `<button class="btn btn-ghost" style="padding:4px 10px;font-size:12px" onclick="shareOpen(${l.id},'${qEsc(l.name)}')">共享</button>`
        : `<span class="sub">共享给我</span>`}</td></tr>`).join("");
  $("#lg_cur").innerHTML=LEDGER_CURS.map(([v,t])=>`<option value="${v}">${t}</option>`).join("");   // 新建行币种下拉（幂等填充）
}

/* ===== 账本共享（多用户）===== */
let _shareLid = 0;
async function shareOpen(id, name){
  _shareLid = id;
  $("#shareTitle").textContent = `共享「${name}」`;
  $("#shareMask").style.display = "flex";
  await shareRefresh();
}
function shareClose(){ $("#shareMask").style.display = "none"; }
async function shareRefresh(){
  try{
    const [ms, us] = await Promise.all([
      api(`/api/ledgers/${_shareLid}/members`),
      api("/api/users")
    ]);
    const ids = new Set(ms.map(m=>+m.uid));
    document.querySelector("#shareTable tbody").innerHTML = ms.map(m=>
      `<tr><td>${esc(m.username || ("uid " + m.uid))}</td>
       <td style="text-align:right"><button class="btn danger" style="padding:2px 10px;font-size:12px" onclick="shareDel(${m.uid})">移除</button></td></tr>`).join("")
      || '<tr><td colspan="2" class="sub">尚未共享给任何人</td></tr>';
    const cand = us.filter(u=>!ids.has(+u.uid) && +u.uid !== +ME.uid);
    $("#shareUserSel").innerHTML = cand.length
      ? cand.map(u=>`<option value="${u.uid}">${esc(u.username || ("uid "+u.uid))}${u.isadmin ? "（管理员）" : ""}</option>`).join("")
      : `<option value="">（没有其他用户——让对方先打开一次柴米记账）</option>`;
  }catch(e){ msg(e.message, false); }
}
async function shareAdd(){
  const v = +$("#shareUserSel").value || 0;
  if(!v) return msg("请选择要共享的用户", false);
  try{ await apiSend(`/api/ledgers/${_shareLid}/members`,"PUT",{uid:v}); }
  catch(e){ return msg(e.message, false); }
  msg("已共享 ✅ 对方刷新应用即可看到"); shareRefresh();
}
async function shareDel(uid){
  if(!await uiConfirm("移除后对方将看不到这本账（已记的流水保留）？", "移除")) return;
  try{ await api(`/api/ledgers/${_shareLid}/members?uid=${uid}`,{method:"DELETE"}); }
  catch(e){ return msg(e.message, false); }
  shareRefresh();
}

async function setLedgerCur(id, sel){
  const l = (await getLedgers()).find(x=>x.id===id);
  try{
    await apiSend(`/api/ledgers/${id}`,"PUT",{name:l.name, icon:l.icon||"", currency:sel.value});
    msg("币种已更新 ✅");
    await loadAll();
  }catch(e){ msg(e.message, false); loadLedgers(); }
}
async function addLedger(){
  if(!$("#lg_name").value)return msg("请输入账本名",false);
  const r=await apiSend("/api/ledgers","POST",{name:$("#lg_name").value, currency:$("#lg_cur").value});
  $("#lg_name").value="";$("#lg_cur").value="CNY";
  msg("账本已创建并切换 ✅");loadAll(r.id);   // 新建后自动切到新账本（loadAll 强刷账本缓存）
}
async function delLedger(id){
  if(!await uiConfirm("删除账本将删除其全部流水，确定？", "删除"))return;
  const wasCur = id === lid();
  await api(`/api/ledgers/${id}`,{method:"DELETE"});
  try{ localStorage.removeItem("chaimi_user_"+id); }catch(_){}   // 被删账本的用户缓存随账本清理
  // 删的是当前账本 → 回用户选择页（home 拉账本列表自愈回落第一个；chaimi_ledger 失效由 loadAll/enter 写回纠正）
  if(wasCur){ location.replace("static/home.html"); return; }
  loadLedgers();loadAll();
}

const CAT_GROUP_CN = {expense:"支出", income:"收入", transfer:"退款和不计收支"};
const catEdit = mkEditToggle("catEditBtn", () => loadCatMgr());
function toggleCatEdit(){ catEdit.toggle(); }   // app.html onclick 入口
async function loadCatMgr(){
  const [cats, maps] = await Promise.all([
    api(`/api/categories?ledger=${lid()}`),
    api(`/api/mappings?ledger=${lid()}`)
  ]);
  categories = cats;   // 全局分类表同步（记一笔下拉/编辑弹窗同源）
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
  const name = await uiPrompt("分类改名（流水、来源名自动同步）：", c.name);
  if(!name || name===c.name) return;
  try{
    await apiSend(`/api/categories/${id}`, "PUT", {name:name.trim(), icon:c.icon||""});
  }catch(e){ return msg(e.message, false); }
  msg("已改名（流水自动同步） ✅"); await loadCatMgr();
}
async function catMgrAliasAdd(id){
  const c = categories.find(x=>x.id===id); if(!c) return;
  const source = await uiPrompt(`添加「${c.name}」包含的账单来源名（如：汽车用品）：`);
  if(!source || !source.trim()) return;
  // 同来源已挂其他分类时 INSERT OR REPLACE 自动改挂到这里（移动语义）；
  try{
    await apiSend(`/api/mappings?ledger=${lid()}`, "POST",
      {flow_type: c.type==="transfer" ? "expense" : c.type, source: source.trim(), category_id: id});
  }catch(e){ return msg(e.message, false); }
  msg("来源已添加 ✅"); await loadCatMgr();
}
async function catMgrAliasDel(id){
  if(!await uiConfirm("移除这个来源名？（该账单分类下次导入不再落到此分类）", "移除")) return;
  await api(`/api/mappings/${id}`, {method:"DELETE"});
  await loadCatMgr();
}

let catNewType = "expense";
function catMgrNew(type){
  catNewType = type;
  $("#catNewTitle").textContent = `新建${CAT_GROUP_CN[type]||""}分类`;
  $("#catNewName").value = ""; $("#catNewSources").value = "";
  $("#catNewMask").style.display = "flex";
  setTimeout(()=>$("#catNewName").focus(), 50);
}
function catNewClose(){ $("#catNewMask").style.display = "none"; }
async function catNewSubmit(){
  const name = $("#catNewName").value.trim();
  if(!name) return msg("请填写分类名", false);
  try{
    const r = await apiSend(`/api/categories?ledger=${lid()}`, "POST", {name, type: catNewType});
    // 来源名逐个挂到新分类（同来源已挂别处自动改挂）；不计收支组按支出侧落行，解析端不分侧命中
    const srcs = [...new Set($("#catNewSources").value.split(/[，,、;；\s]+/).map(s=>s.trim()).filter(Boolean))];
    for(const s of srcs){
      await apiSend(`/api/mappings?ledger=${lid()}`, "POST",
        {flow_type: catNewType==="transfer" ? "expense" : catNewType, source:s, category_id:r.id});
    }
  }catch(e){ return msg(e.message, false); }
  catNewClose(); await loadCatMgr(); msg("分类已创建 ✅");
}
async function catMgrDel(id, name){
  if(!await uiConfirm(`删除分类「${name}」：其历史流水将变为未分类并进待确认，包含的来源名一并移除。确定？`, "删除")) return;
  await api(`/api/categories/${id}`, {method:"DELETE"});
  await loadCatMgr();
  loadTx(); loadPendingBadge();   // 受影响流水进待确认，列表与徽标同步刷新
}
function accScopeInfo(){
  const sc = appScope();
  if(sc === "all") return {from: "", to: "", label: "全部", months: 0};
  const r = curStatRange();
  const label = sc === "month" ? r.label.replace(/^(\d{4})-(\d{2})$/, "$1年$2月") : r.label;   // 裸 YYYY-MM 润色
  // /api/accounts 后端按 date < to 排他：月/年的 to 本就是下一周期首日（排他界）；
  // 仅自定义区间的 to 是含当天的裸截止日——转次日作排他上界，否则截止日当天消费漏计
  return {from: r.from, to: sc === "custom" ? nextDayStr(r.to) : r.to, label, months: sc === "year" ? 12 : 1};
}

function mkEditToggle(btnId, rerender){
  const st = {on: false};
  st.toggle = () => { st.on = !st.on; $("#"+btnId).textContent = st.on ? "完成" : "编辑"; rerender(); };
  return st;
}
const accEdit = mkEditToggle("accEditBtn", () => stAccs());
let accRows = [];   // stAccs 最近一次拉取的账户行（delAcc 弹确认框取名字/笔数用）
let bdgDirty = false;   // 预算输入框被用户改过（input 置位）：他端改预算/切账本回填时不覆盖正在编辑的草稿
$("#bdg_amt").oninput = () => { bdgDirty = true; };
async function stAccs(){
  const r = accScopeInfo();
  const as = await api(`/api/accounts?ledger=${lid()}` + (r.from ? `&from=${r.from}&to=${r.to}` : ""));
  accRows = as;
  // 消费金额 = 所选范围净支出（支出 − 退款回账；收入/转账不计）
  document.querySelector("#accTable tbody").innerHTML = as.map(a =>
    `<tr><td>${esc(a.name)}</td><td class="num c-exp">${curSym()}${fmt(a.spend || 0)}</td>
     ${accEdit.on ? `<td style="text-align:right"><button class="btn danger" onclick="delAcc(${a.id})">删</button></td>` : ""}</tr>`).join("") ||
    '<tr><td colspan="3" class="sub">暂无账户</td></tr>';
  const spendSum = as.reduce((s, a) => s + (a.spend || 0), 0);
  $("#accTotal").textContent = `${r.label}消费合计 ${curSym()}${fmt(spendSum)}`;
  // 避免与账户维度的 spend 合计因口径差异对不上；「全部」范围无该字段时回落账户合计
  const cur = await curLedger();
  const hasBudget = !!(cur && cur.budget);
  if(cur && !bdgDirty) $("#bdg_amt").value = cur.budget ? fmt(cur.budget) : "";   // 未编辑时跟随最新预算（含他端改动与清零），编辑中不覆盖
  const spend = as[0] && as[0].net_expense !== undefined ? as[0].net_expense : spendSum;
  if(hasBudget && r.months){
    const budget = cur.budget * r.months;
    const pct = Math.round(spend / budget * 100);
    const bLabel = r.months > 1 ? `预算(月预算×${r.months})` : "预算";   // 年范围折算值,标明口径防误读为未生效
    $("#bdg_stat").innerHTML = `${r.label}已消费 <b style="color:${pct > 100 ? "var(--expense)" : "var(--fg)"}">${curSym()}${fmt(spend)}</b> / ${bLabel} ${curSym()}${fmt(budget)}（${pct}%${pct > 100 ? " 超支" : ""}）`;
  }else{
    $("#bdg_stat").innerHTML = `${r.label}消费合计 <b>${curSym()}${fmt(spend)}</b>${hasBudget ? "（「全部」范围不按预算折算）" : ""}`;
  }
}
async function saveBudget(){
  // 预算是账本级共享设置：独立端点，共享成员也可改（名称/币种仍 owner-only）
  await apiSend(`/api/ledgers/${lid()}/budget`, "PUT",
    {budget: toCents($("#bdg_amt").value)});
  await getLedgers(true);   // 强刷缓存：不刷则 stAccs 仍读到旧预算,统计行不更新
  bdgDirty = false;   // 已保存成最新值：回到跟随模式（他端后续改动/切账本可回填）
  msg("预算已保存 ✅"); stAccs();
}
const userEdit = mkEditToggle("userEditBtn", () => stAttrs());
async function stAttrs(){   // 用户设置：拉取与状态解析走 loadAttrs 单源，此处只渲染两张表
  const [ats,gs]=await loadAttrs();
  const avHtml=g=>`<img src="${avatarUrl(g.avatar)}" style="width:30px;height:30px;border-radius:9px;vertical-align:middle">`;
  document.querySelector("#attrGroupTable tbody").innerHTML=gs.map(g=>
    `<tr>
      <td style="vertical-align:middle;white-space:nowrap">${avHtml(g)}&nbsp;${esc(g.name)}</td>
      <td class="sub" style="vertical-align:middle;word-break:break-all">${g.members?g.members.split("、").map(m=>`<span class="attr-m">${esc(m)}</span>`).join(""):"—"}</td>
      ${userEdit.on?`<td style="white-space:nowrap;text-align:right;vertical-align:middle">
        <button class="btn btn-ghost" onclick="event.stopPropagation();avatarDD(${g.id},this)">头像</button>
        <button class="btn btn-ghost" onclick="renameAttrGroup(${g.id})">改名</button>
        <button class="btn danger" onclick="delAttrGroup(${g.id})">删</button></td>`:""}
    </tr>`).join("");
  const sel=v=>`<option value=""${v?"":" selected"}>未分组</option>`
    +gs.map(g=>`<option value="${g.id}"${v===g.id?" selected":""}>${esc(g.name)}</option>`).join("");
  document.querySelector("#attrMngTable tbody").innerHTML=ats.map(a=>
    `<tr><td style="vertical-align:middle">${esc(a.name)}</td>
     <td><select onchange="setAttrGroup(${a.id},this.value)">${sel(a.group_id)}</select></td></tr>`).join("")
    ||'<tr><td colspan="2" class="sub">暂无原始归属，账单导入后自动出现</td></tr>';
}
function avatarDD(id, el){
  const g=attrGroups.find(x=>x.id===id);if(!g)return;
  _popHide();
  let pop=$("#avPop");
  if(!pop){
    pop=document.createElement("div");
    pop.id="avPop"; pop.className="ui-pop";
    document.body.appendChild(pop);
  }
  const cur=g.avatar==="m"?"m":"f";
  pop.innerHTML=["m","f"].map(v=>
    `<button type="button" class="av-pick${v===cur?" on":""}" data-v="${v}" title="${v==="m"?"男牛牛":"女牛牛"}">
       <img src="images/avatar-cow-${v}.png" alt="${v==="m"?"男牛牛":"女牛牛"}">
     </button>`).join("");
  _popPlace(pop, el);
  pop.querySelectorAll(".av-pick").forEach(b=>b.onclick=async()=>{
    _popHide();
    try{ await apiSend(`/api/attribution-groups/${id}`,"PUT",{name:g.name,avatar:b.dataset.v}); }
    catch(e){ return msg(e.message,false); }
    stAttrs();
  });
}
let stNewAv="f";
function stPickAv(btn){
  stNewAv=btn.dataset.av;
  document.querySelectorAll("#atg_av button").forEach(b=>b.classList.toggle("on",b===btn));
}
async function addAttrGroup(){
  if(!$("#atg_name").value)return msg("请输入用户名",false);
  try{ await apiSend(`/api/attribution-groups?ledger=${lid()}`,"POST",{name:$("#atg_name").value.trim(),avatar:stNewAv}); }
  catch(e){return msg(e.message,false);}
  $("#atg_name").value="";
  stPickAv(document.querySelector("#atg_av button[data-av='f']"));   // 重置回默认女牛
  msg("用户已创建 ✅ 在下方「原始归属」表把账单归属归入该用户");stAttrs();
}
async function renameAttrGroup(id){
  const g=attrGroups.find(x=>x.id===id);if(!g)return;
  const name=await uiPrompt(`用户「${g.name}」改名：`,g.name);
  if(!name||name===g.name)return;
  try{ await apiSend(`/api/attribution-groups/${id}`,"PUT",{name, avatar:g.avatar==="m"?"m":"f"}); }   // avatar 随带，避免被重置
  catch(e){return msg(e.message,false);}
  msg("用户已改名 ✅（相关流水同步更新）");stAttrs();
}
async function delAttrGroup(id){
  const g=attrGroups.find(x=>x.id===id);
  const wasCur=!!g&&g.id===curUserId;
  const warn=wasCur?"该用户是当前用户，删除后自动切换到第一个用户；":"";
  if(!await uiConfirm(`删除用户：${warn}成员回到未分组，导入流水归属回落原始归属、手动选择的清空`,"删除"))return;
  try{ await api(`/api/attribution-groups/${id}`,{method:"DELETE"}); }
  catch(e){return msg(e.message,false);}
  await stAttrs();   // 刷新列表与解析状态
  if(wasCur && attrGroups.length) setCurUser(attrGroups[0].id);   // 删的是当前用户：直接切第一个，不弹登录页
}
async function setAttrGroup(id,gid){
  try{ await apiSend(`/api/attributions/${id}`,"PUT",{group_id:gid?+gid:null}); }
  catch(e){return msg(e.message,false);}
  msg("已保存 ✅");stAttrs();
}
async function addAccount(){
  if(!$("#ac_name").value)return msg("请输入账户名",false);
  await apiSend(`/api/accounts?ledger=${lid()}`,"POST",{name:$("#ac_name").value});
  $("#ac_name").value="";msg("账户已添加 ✅");stAccs();loadAccs();
}
async function delAcc(id){
  const a = accRows.find(x => x.id === id) || {};
  const name = a.name || "该账户";   // 极端情况（列表未拉到）兜底
  const n = a.tx_count || 0;
  const tip = n > 0
    ? `删除账户「${name}」？其 ${n} 笔历史流水将变为「未指定账户」——分类和统计不变，也不会重新进入待确认。注意：之后重建同名账户不会自动关联回这些流水。`
    : `删除账户「${name}」？该账户下没有关联流水。`;
  if(!await uiConfirm(tip, "删除")) return;
  await api(`/api/accounts/${id}`,{method:"DELETE"});stAccs();loadAccs();
}
async function loadBks(){
  const bs=await api("/api/backups");
  document.querySelector("#bkTable tbody").innerHTML=bs.map(b=>
    `<tr><td>${esc(b.mtime)}</td><td class="num">${(b.size/1024).toFixed(0)} KB</td>
     <td><button class="btn btn-ghost" style="padding:4px 10px;font-size:12px" onclick="location.href='api/backups/download?file=${encodeURIComponent(b.file)}'">下载</button>
     <button class="btn danger" style="padding:4px 10px;font-size:12px" onclick="restore('${qEsc(b.file)}')">恢复</button>
     <button class="btn danger" style="padding:4px 10px;font-size:12px" onclick="delBk('${qEsc(b.file)}')">删除</button></td></tr>`).join("")||'<tr><td colspan="3" class="sub">暂无备份</td></tr>';
}
function dlLogs(){location.href="api/logs/download";}
async function loadBkCfg(){
  const c = await getCfg();
  $("#bk_enabled").checked=!!c.backup.enabled;
  $("#bk_hour").value=c.backup.hour;
  $("#bk_months").value=c.backup.keep_months;
  $("#bk_years").value=c.backup.keep_years;
}
async function saveBkCfg(){
  await apiSend("/api/config","PUT",{backup:{
    enabled:$("#bk_enabled").checked, hour:$("#bk_hour").value===""?3:+$("#bk_hour").value,
    keep_months:$("#bk_months").value===""?0:+$("#bk_months").value, keep_years:$("#bk_years").value===""?0:+$("#bk_years").value}});
  _cfgCache = null;
  msg("自动备份已保存 ✅");
}
async function delBk(f){
  if(!await uiConfirm(`删除备份 ${f}？不可恢复。`, "删除"))return;
  await apiSend("/api/backups/delete","POST",{file:f});
  msg("已删除");loadBks();
}
async function doBackup(){await api("/api/backup",{method:"POST"});msg("备份完成 ✅");loadBks();}
async function restore(f){
  if(!await uiConfirm("恢复将覆盖当前全部数据，确定？", "恢复"))return;
  await apiSend("/api/restore","POST",{file:f});
  _cfgCache = null;   // 恢复会替换 chaimi.conf：配置缓存必须失效
  $("#ai_msgs").innerHTML = ""; txCache = {};   // 库被整体替换：聊天流水卡与缓存全部作废
  msg("已恢复 ✅");loadAll();loadCfg();loadBkCfg();loadBotCfg();
}

let _ilinkTimer = null, _qrTimer = null;
const ILINK_STATE_CN = {off:"未启动", pending_scan:"等待扫码", scaned:"已扫码待确认",
  logged_in:"已连接 ✓", expired:"凭证过期", error:"连接异常"};

async function loadBotCfg(){
  const c = await getCfg();
  const b = c.bot || {};
  $("#bot_mode").value = ["off","direct"].includes(b.mode) ? b.mode : "off";
  $("#bot_mode_ro").textContent = b.mode === "direct"
    ? "模式：微信扫码（管理员已开启）" : "模式：未开启（需管理员开启微信扫码）";
  $("#bot_users").value = (b.allowed_users || []).join(", ");
  botGwVis();
  await botLedgers();
  await botMembers();
}
function botGwVis(){
  $("#bot_dir").style.display = $("#bot_mode").value === "direct" ? "" : "none";
}
async function saveBotCfg(){
  const users = $("#bot_users").value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean);
  const m = $("#bot_mode").value;
  await apiSend("/api/config","PUT",{bot:{mode:m, allowed_users:users}});
  _cfgCache = null;
  msg(m === "direct" ? "已开启微信扫码 ✅ 轮询将启动" : "机器人已关闭 ✅");
  loadBotCfg();
}
async function botLedgers(){
  const ls = await api("/api/ledgers?all=1");   // admin=全库账本；普通用户=自己拥有+共享来的（服务端同口径过滤）
  const cur = $("#bot_lg_sel").value;
  $("#bot_lg_sel").innerHTML = ledgerOptions(ls, cur ? +cur : 0);
  if(cur) $("#bot_lg_sel").value = cur;
  await botLedgerUsers();
}
async function botLedgerUsers(){
  // 当前账本的用户=归属分组(用户与预算页维护);绑定谁,记的账就带谁的归属
  const lg = $("#bot_lg_sel").value;
  if(!lg) return;
  try{
    const gs = await api(`/api/attribution-groups?ledger=${lg}`);
    $("#bot_user_sel").innerHTML = (gs||[]).length
      ? gs.map(g=>`<option value="${esc(g.name)}">${esc(g.name)}</option>`).join("")
      : `<option value="">（先到 用户与预算 添加用户）</option>`;
  }catch(_){ $("#bot_user_sel").innerHTML = `<option value="">（用户列表加载失败）</option>`; }
}
const _lgName = id => { const o = $("#bot_lg_sel"); const v = o ? [...o.options].find(x=>x.value==id) : null; return v ? v.text : ("账本"+id); };
async function botMembers(){
  try{
    const s = await api("/api/bot/ilink/status");
    const rows = s.bots || [];
    const ok = rows.filter(x=>x.state==="logged_in").length;
    $("#bot_sum").textContent = rows.length ? `成员 ${ok}/${rows.length} 在线` : "";
    document.querySelector("#bot_members tbody").innerHTML = rows.map(x=>{
      const st = esc((ILINK_STATE_CN[x.state] || x.state) + (x.state==="error" && x.detail ? `（${x.detail}）` : ""));
      const rescan = ["expired","error"].includes(x.state)
        ? `<button class="btn btn-ghost" onclick="botBind(${x.ledger},'${qEsc(x.label||"")}')">重新扫码</button>` : "";
      return `<tr><td>${esc(_lgName(x.ledger))}</td><td>${esc(x.label || x.user || "未备注")}</td>
        <td class="sub">${st}</td>
        <td>${rescan}<button class="btn danger" onclick="botUnbind('${qEsc(x.account_id)}')">解绑</button></td></tr>`;
    }).join("") || `<tr><td colspan="4" class="sub">尚无绑定——选账本和用户生成二维码,谁扫谁绑定</td></tr>`;
  }catch(_){ $("#bot_sum").textContent = ""; }
}
async function botBind(ledger, label){
  const lg = ledger || parseInt($("#bot_lg_sel").value) || 1;
  const lb = label != null && label !== false ? label : $("#bot_user_sel").value;
  try{
    await apiSend("/api/bot/ilink/qrcode","POST",{ledger:lg, label:lb});
    msg(`二维码已生成（${_lgName(lg)}${lb?" · "+lb:""}），请用微信扫码`);
    botQrShow();
  }catch(e){ msg(e.message, false); }
}
async function botQrShow(){
  const box = $("#bot_qrbox"), img = $("#ilink_qr");
  box.style.display = "";
  let left = 180;
  try{
    const r = await fetch("api/bot/ilink/qrimg?t=" + Date.now());
    if(!r.ok){
      let d = ""; try{ d = (await r.json()).detail || ""; }catch(_){}
      box.style.display = "none"; return msg("二维码失败：" + (d || r.status), false);
    }
    img.onerror = ()=> msg("二维码图片解码失败", false);
    img.src = URL.createObjectURL(await r.blob());
  }catch(e){ box.style.display = "none"; return msg(e.message, false); }
  clearInterval(_qrTimer);
  const tick = ()=>{
    $("#ilink_cd").textContent = `剩余 ${Math.floor(left/60)}:${String(left%60).padStart(2,"0")}`;
    if(left-- <= 0){ clearInterval(_qrTimer); }
  };
  tick(); _qrTimer = setInterval(tick, 1000);
  botPoll(true);
}
async function botPoll(loop){
  // loop=true:扫码期间 2s 自轮询。确认转正后 pending 归 off——活跃扫码中遇到 off 即流程结束
  const finish = async (m, warn)=>{
    clearInterval(_qrTimer);
    $("#bot_qrbox").style.display = "none";
    if(m) msg(m, !warn);
    _cfgCache = null;   // 扫码确认由服务端回调写入白名单/凭证：前端配置缓存必须失效
    await loadBotCfg(); // 白名单即时回显（内含 botMembers 成员表刷新；旧版只刷成员表，须关开面板才见新白名单）
  };
  try{
    const s = await api("/api/bot/ilink/status");
    const p = s.pending || {};
    const active = $("#bot_qrbox").style.display !== "none";
    $("#ilink_wait").textContent = ILINK_STATE_CN[p.state] || p.state || "—";
    if(p.state === "logged_in"){ await finish("绑定成功 ✅"); return; }
    if(p.state === "off" && active){ await finish(); return; }   // 已转正入成员表
    if(p.state === "expired"){ await finish("二维码已过期，请重新生成", true); return; }
    if(p.state === "error"){ await finish("扫码出错：" + (p.detail || ""), true); return; }
    await botMembers();          // 扫码期间顺带刷新成员在线状态
    if(_ilinkTimer) clearTimeout(_ilinkTimer);
    if(loop && active && ["pending_scan","scaned"].includes(p.state)){
      _ilinkTimer = setTimeout(()=>botPoll(true), 2000);
    }
  }catch(_){ if(_ilinkTimer){ clearTimeout(_ilinkTimer); _ilinkTimer = null; } }
}
async function botUnbind(aid){
  if(!await uiConfirm("解绑后该成员的微信机器人将停止记账，确定？", "解绑"))return;
  await apiSend("/api/bot/ilink/unbind","POST",{account_id:aid});
  _cfgCache = null;   // 解绑改写 chaimi.conf(ilink 凭证)：配置缓存随之失效
  msg("已解绑"); await botMembers();
}
async function botTest(){
  const t = $("#bot_test_in").value.trim();
  if(!t) return msg("请输入测试内容", false);
  const out = $("#bot_test_out");
  out.style.display = ""; out.textContent = "…";
  try{
    const r = await apiSend("/api/bot/test","POST",{text:t});
    out.textContent = r.reply || "（空回复）";
  }catch(e){ out.textContent = "❌ " + e.message; }
}
