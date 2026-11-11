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
  // 机器人：区块全员可见；模式/记账确认/白名单是应用级配置，非管理员只读（服务端同口径 admin 门）
  $("#bot_mode_box").style.display = admin ? "" : "none";
  $("#bot_save").style.display = admin ? "" : "none";
  $("#bot_mode_ro").style.display = admin ? "none" : "";
  $("#bot_users_row").style.display = admin ? "" : "none";
  $("#bot_confirm_row").style.display = admin ? "" : "none";
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
  const llm = c.llm || {};   // 新装/恢复异常时可能缺键：与 loadBotCfg 的 c.bot||{} 同口径防空引用
  $("#llm_preset").innerHTML=LLM_PRESETS.map(p=>`<option value="${p.id}">${p.name}</option>`).join("")
    +`<option value="custom">自定义</option>`;
  $("#llm_url").value=llm.base_url||""; $("#llm_model").value=llm.model||"";
  $("#vl_model").value=llm.vl_model||""; $("#llm_key").value=llm.api_key||"";
  $("#vl_url").value=llm.vl_base_url||""; $("#vl_key").value=llm.vl_api_key||"";
  const hit=LLM_PRESETS.find(x=>x.base_url===llm.base_url);
  $("#llm_preset").value=hit?hit.id:"custom";   // 按地址回显预设；手改过的显示为自定义
  $("#vlApiRow").style.display = (hit && !llm.vl_base_url && !llm.vl_api_key) ? "none" : "";
}
async function saveCfg(){
  // putConfig 单源（core.js）：写后缓存失效不再各写一遍
  await putConfig("llm", {base_url:$("#llm_url").value,model:$("#llm_model").value,vl_model:$("#vl_model").value,api_key:$("#llm_key").value,
      vl_base_url:$("#vl_url").value.trim(),vl_api_key:$("#vl_key").value.trim()});
  msg("已保存 ✅"); loadCfg();
}
document.addEventListener("DOMContentLoaded", () => {
  const t = document.getElementById("verTag");
  if (t) t.textContent = "chaimi-ledger v" + (window.CHAIMI_VER || "?");
});

const llmModelsGen = makeSeq();   // 模型列表请求代数（makeSeq 单源）：连点「模型列表」时旧响应
                                  // 后到会覆盖新内容（settings 页其它加载器同款守卫，此处补齐）
async function llmModelsOpen(){
  // 惯例同 catNew:内联 display:flex 覆盖 .modal-mask 的 CSS display:none
  // (曾误用 display="" 只清内联,回落 CSS 仍 none——弹窗 DOM 开着但视觉隐形)
  const seq = llmModelsGen.next();
  $("#llmModelsMask").style.display = "flex";
  const b=$("#llmModelsBody");
  b.innerHTML='<div class="sub" style="padding:6px 0">获取中…</div>';
  let d; try{ d=await api("/api/llm/models"); }
  catch(e){ if(!llmModelsGen.isCurrent(seq)) return; b.innerHTML=`<div class="sub">获取失败: ${esc(e.message)}</div>`; return; }
  if(!llmModelsGen.isCurrent(seq)) return;   // 弹窗已关/重开：旧响应不回填
  // 部分异常路径只回 {error} 不带 text/vision 字段：先归一成数组再判空/遍历，
  // 否则 d.text.length 直接 TypeError（下方 grp 的 arr.map 同样会炸）
  d.text=d.text||[]; d.vision=d.vision||[];
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

const LEDGER_CURS = Object.entries(CUR_INFO).map(([v, [t, s]]) => [v, `${t} ${s}`]);   // 币种下拉（CUR_INFO 单源，见 core.js）
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
const shareGen = makeSeq();   // 共享弹窗请求代数（makeSeq 单源）：shareOpen(A) 在途时 shareOpen(B)，
                              // A 的成员表迟到响应不得渲进 B 的弹窗（与 botLuGen 同类，此前漏守卫）
async function shareRefresh(){
  const seq = shareGen.next();
  const lg = _shareLid;
  try{
    const [ms, us] = await Promise.all([
      api(`/api/ledgers/${lg}/members`),
      api("/api/users")
    ]);
    if(!shareGen.isCurrent(seq)) return;
    const ids = new Set(ms.map(m=>+m.uid));
    document.querySelector("#shareTable tbody").innerHTML = ms.map(m=>
      `<tr><td>${esc(m.username || ("uid " + m.uid))}</td>
       <td style="text-align:right"><button class="btn danger" style="padding:2px 10px;font-size:12px" onclick="shareDel(${m.uid})">移除</button></td></tr>`).join("")
      || '<tr><td colspan="2" class="sub">尚未共享给任何人</td></tr>';
    const cand = us.filter(u=>!ids.has(+u.uid) && +u.uid !== +ME.uid);
    $("#shareUserSel").innerHTML = cand.length
      ? cand.map(u=>`<option value="${u.uid}">${esc(u.username || ("uid "+u.uid))}${u.isadmin ? "（管理员）" : ""}</option>`).join("")
      : `<option value="">（没有其他用户——让对方先打开一次柴米记账）</option>`;
  }catch(e){ if(shareGen.isCurrent(seq)) msg(e.message, false); }   // 过期失败的 toast 不晚到
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
  if(!l) return msg("账本不存在或已失效，请刷新重试", false);   // 缓存陈旧（账本刚被 owner 删除）时 find 为 undefined
  try{
    // PUT 为部分更新语义：只发改动字段，name/icon 由服务端保持原值（不再读-改-写回显）
    await apiSend(`/api/ledgers/${id}`,"PUT",{currency:sel.value});
    msg("币种已更新 ✅");
    await loadAll();
  }catch(e){ msg(e.message, false); loadLedgers(); }
}
async function addLedger(){   // 分类来源在建用户页（home）选：创建默认账本 → 跳 home 设用户时再复用
  if(!$("#lg_name").value)return msg("请输入账本名",false);
  const r=await apiSend("/api/ledgers","POST",{name:$("#lg_name").value, currency:$("#lg_cur").value});
  $("#lg_name").value="";$("#lg_cur").value="CNY";
  msg("账本已创建并切换 ✅");loadAll(r.id);   // 新建后自动切到新账本（loadAll 强刷账本缓存）
}
async function delLedger(id){
  if(!await uiConfirm("删除账本将删除其全部流水，确定？", "删除"))return;
  const wasCur = id === lid();
  await api(`/api/ledgers/${id}`,{method:"DELETE"});
  ckDel("chaimi_user_"+id);   // 被删账本的用户记忆随账本清理
  // 删的是当前账本 → 回用户选择页（home 拉账本列表自愈回落第一个；chaimi_ledger 失效由 loadAll/enter 写回纠正）
  if(wasCur){ location.replace("static/home.html"); return; }
  loadLedgers();loadAll();
}

/* 分类管理（loadCatMgr 与 catMgr/catNew 系列）已抽至 catmgr.js 共享模块，app 设置页与 home 抽屉复用 */
function accScopeInfo(){
  const sc = appScope();
  if(sc === "all") return {from: "", to: "", label: "全部"};
  const r = curStatRange();
  // /api/accounts 后端按 date < to 排他：to 的翻译走 stats.js rangeDto 单源（排他界原样；
  // 仅自定义区间的裸截止日转次日），否则截止日当天消费漏计。
  // 预算折算的月数（net_expense/months）由服务端随区间一并下发（db.range_months 单源），
  // 前端不再各算一份
  return {from: r.from, to: rangeDto(r), label: ymLabel(r.label)};
}

/* mkEditToggle 定义在 shared-lite.js（settings 页与 catmgr 抽屉共用单源） */
const accEdit = mkEditToggle("accEditBtn", () => stAccs());
let accRows = [];   // stAccs 最近一次拉取的账户行（delAcc 弹确认框取名字/笔数用）
let bdgDirty = false;   // 预算输入框被用户改过（input 置位）：他端改预算/切账本回填时不覆盖正在编辑的草稿
$("#bdg_amt").oninput = () => { bdgDirty = true; };
async function stAccs(){
  const sc = ledgerScope();   // 账本身份守卫（ledgerScope 单源）：旧账本账户表的迟到响应不渲染
  const r = accScopeInfo();
  const resp = await api(`/api/accounts?ledger=${sc.id}` + (r.from ? `&from=${r.from}&to=${r.to}` : ""));
  if(!sc.isCurrent()) return;
  const as = resp.items;
  accRows = as;
  // 消费金额 = 所选范围净支出（支出 − 退款回账；收入/转账不计）
  document.querySelector("#accTable tbody").innerHTML = as.map(a =>
    `<tr><td>${esc(a.name)}</td><td class="num c-exp">${curSym()}${fmt(a.spend || 0)}</td>
     ${accEdit.on ? `<td style="text-align:right"><button class="btn danger" onclick="delAcc(${a.id})">删</button></td>` : ""}</tr>`).join("") ||
    '<tr><td colspan="3" class="sub">暂无账户</td></tr>';
  const spendSum = as.reduce((s, a) => s + (a.spend || 0), 0);
  $("#accTotal").textContent = `${r.label}消费合计 ${curSym()}${fmt(spendSum)}`;
  // 区间净支出与预算折算月数由服务端随区间下发（net_expense 含未选账户支出；「全部」
  // 范围不带区间参数、无此二者，按账户 spend 合计展示——口径差异见端点注释）
  const cur = await curLedger();
  if(!sc.isCurrent()) return;   // 账本行加载期间切了账本：旧账本的预算值不回填（下方全在写 DOM）
  const hasBudget = !!(cur && cur.budget);
  if(cur && !bdgDirty) $("#bdg_amt").value = cur.budget ? fmt(cur.budget) : "";   // 未编辑时跟随最新预算（含他端改动与清零），编辑中不覆盖
  const spend = resp.net_expense !== undefined ? resp.net_expense : spendSum;
  const months = resp.months || 0;
  if(hasBudget && months){
    const budget = cur.budget * months;
    const pct = Math.round(spend / budget * 100);
    const bLabel = months > 1 ? `预算(月预算×${months})` : "预算";   // 年范围折算值,标明口径防误读为未生效
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
  document.querySelector("#attrGroupTable tbody").innerHTML=gs.map(g=>{
    const mems = g.members?g.members.split("、").map(m=>`<span class="attr-m">${esc(m)}</span>`).join(""):"—";
    return userEdit.on
      ? `<tr><td colspan="3"><div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px">${avHtml(g)}<span style="white-space:nowrap">${esc(g.name)}</span><span class="sub" style="flex:1 1 100px;min-width:0;word-break:break-all">${mems}</span><span style="display:inline-flex;gap:6px;margin-left:auto"><button class="btn btn-ghost" onclick="event.stopPropagation();avatarDD(${g.id},this)">头像</button><button class="btn btn-ghost" onclick="renameAttrGroup(${g.id})">改名</button><button class="btn danger" onclick="delAttrGroup(${g.id})">删除</button></span></div></td></tr>`
      : `<tr><td style="vertical-align:middle;white-space:nowrap">${avHtml(g)}&nbsp;${esc(g.name)}</td><td class="sub" style="vertical-align:middle;word-break:break-all">${mems}</td></tr>`;
  }).join("");
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
       <img src="${avatarUrl(v)}" alt="${v==="m"?"男牛牛":"女牛牛"}">
     </button>`).join("");
  _popPlace(pop, el);
  pop.querySelectorAll(".av-pick").forEach(b=>b.onclick=async()=>{
    _popHide();
    try{ await apiSend(`/api/attribution-groups/${id}`,"PUT",{avatar:b.dataset.v}); }   // 部分更新：只发改动字段
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
  try{ await apiSend(`/api/attribution-groups/${id}`,"PUT",{name}); }   // 部分更新：avatar 不随带，服务端保持原值
  catch(e){return msg(e.message,false);}
  msg("用户已改名 ✅（相关流水同步更新）");stAttrs();
}
async function delAttrGroup(id){
  const g=attrGroups.find(x=>x.id===id);
  const wasCur=!!g&&g.id===curUserId;
  const warn=wasCur?"该用户是当前用户，删除后自动切换到第一个用户；":"";
  if(!await uiConfirm(`删除用户：${warn}成员回到未分组，导入流水归属回落原始归属、手动选择的清空`,"删除"))return;
  // 删的是当前用户：补救**前置**到删除发出之前——DELETE 的 await 窗口内长轮询唤醒
  // loadAttrs→restoreCurUser 读到已删 id 就会排队跳登录页（setCurUser 持久化新 id，
  // 此后任何 loadAttrs 都看到合法用户，不触发跳转）
  if(wasCur){
    const nxt = attrGroups.find(x=>x.id!==id);
    if(nxt) setCurUser(nxt.id);
  }
  try{ await api(`/api/attribution-groups/${id}`,{method:"DELETE"}); }
  catch(e){
    // 删除失败：把误切的当前用户回滚（前置补救已持久化新 id，不回滚后续记账归属记错人）
    if(wasCur && g) setCurUser(g.id);
    return msg(e.message, false);
  }
  await stAttrs();   // 刷新列表与解析状态（当前 id 已由上方预置补救持久化，无需再兜底切人）
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
  // 三个真 td 对齐表头（时间/大小/操作），不再 colspan+flex 挤一格——表头与列内容曾因此错位
  document.querySelector("#bkTable tbody").innerHTML=bs.map(b=>
    `<tr><td>${esc(b.mtime)}</td><td class="num">${(b.size/1024).toFixed(0)} KB</td>
     <td><span style="display:inline-flex;gap:6px">
     <button class="btn btn-ghost" style="padding:4px 10px;font-size:12px" onclick="location.href='api/backups/download?file=${qEsc(encodeURIComponent(b.file))}'">下载</button>
     <button class="btn danger" style="padding:4px 10px;font-size:12px" onclick="restore('${qEsc(b.file)}')">恢复</button>
     <button class="btn danger" style="padding:4px 10px;font-size:12px" onclick="delBk('${qEsc(b.file)}')">删除</button></span></td></tr>`).join("")||'<tr><td colspan="3" class="sub">暂无备份</td></tr>';
}
function dlLogs(){location.href="api/logs/download";}
async function loadBkCfg(){
  const c = await getCfg();
  const bk = c.backup || {};   // 缺键时表单回落默认值，与 loadCfg 的 llm||{} 同口径
  $("#bk_enabled").checked=!!bk.enabled;
  $("#bk_hour").value=bk.hour;
  $("#bk_months").value=bk.keep_months;
  $("#bk_years").value=bk.keep_years;
}
async function saveBkCfg(){
  await putConfig("backup", {
    enabled:$("#bk_enabled").checked, hour:$("#bk_hour").value===""?3:+$("#bk_hour").value,
    keep_months:$("#bk_months").value===""?0:+$("#bk_months").value, keep_years:$("#bk_years").value===""?0:+$("#bk_years").value});
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
  // 库被整体替换：在途 AI 流与查账缓存一并作废——此前不停流，恢复前发起的对话把旧统计卡/
  // 文字续写进已清空的聊天区，aiLastStats 还能下钻到已不存在的数据
  aiSseStop();
  aiLastStats = null; aiLastStatsLid = null;
  // 聊天流水卡与缓存全部作废。只清业务气泡、重新亮出 #chat_empty 开场
  // （aiClearIntro 是 display 隐藏而非 remove——节点恒在，见其注释契约，无需重建分支）
  document.querySelectorAll("#ai_msgs .bubble").forEach(b=>b.remove());
  const intro = document.querySelector("#ai_msgs #chat_empty");
  if(intro) intro.style.display = "";
  txCache = {};
  if(txSelMode) txSelExit();   // 整库替换后批量选择集里的 id 已不存在：退出选择态防残留计数
  msg("已恢复 ✅");loadAll();loadCfg();loadBkCfg();loadBotCfg();
}

let _ilinkTimer = null, _qrTimer = null;
const ILINK_STATE_CN = {off:"未启动", pending_scan:"等待扫码", scaned:"已扫码待确认",
  logged_in:"已连接 ✓", expired:"凭证过期", error:"连接异常"};

const BOT_CONFIRM_CN = {reply:"回复确认入库", auto:"识别直接入库"};
async function loadBotCfg(){
  const c = await getCfg();
  const b = c.bot || {};
  const cm = BOT_CONFIRM_CN[b.confirm_mode] ? b.confirm_mode : "reply";   // 与服务端读侧归一同口径
  $("#bot_mode").value = ["off","direct"].includes(b.mode) ? b.mode : "off";
  $("#bot_confirm").value = cm;
  $("#bot_mode_ro").textContent = b.mode === "direct"
    ? `模式：微信扫码（管理员已开启）· 记账：${BOT_CONFIRM_CN[cm]}` : "模式：未开启（需管理员开启微信扫码）";
  $("#bot_users").value = (b.allowed_users || []).join("\n");
  botGwVis();
  await botLedgers();
  await botMembers();
}
function botGwVis(){
  $("#bot_dir").style.display = $("#bot_mode").value === "direct" ? "" : "none";
}
async function saveBotCfg(){
  const users = $("#bot_users").value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean);
  const m = $("#bot_mode").value, cm = $("#bot_confirm").value;
  await putConfig("bot", {mode:m, confirm_mode:cm, allowed_users:users});
  msg(m === "direct" ? `已开启微信扫码 ✅ 记账：${BOT_CONFIRM_CN[cm]}` : "机器人已关闭 ✅");
  loadBotCfg();
}
async function botLedgers(){
  const ls = await api("/api/ledgers?all=1");   // admin=全库账本；普通用户=自己拥有+共享来的（服务端同口径过滤）
  const cur = $("#bot_lg_sel").value;
  $("#bot_lg_sel").innerHTML = ledgerOptions(ls, cur ? +cur : 0);
  if(cur) $("#bot_lg_sel").value = cur;
  await botLedgerUsers();
}
const botLuGen = makeSeq();   // 账本下拉切换请求代数（makeSeq 单源）：旧账本用户列表的迟到响应不回填（防串选错人）
async function botLedgerUsers(){
  // 当前账本的用户=归属分组(用户与预算页维护);绑定谁,记的账就带谁的归属
  const lg = $("#bot_lg_sel").value;
  if(!lg) return;
  const seq = botLuGen.next();
  try{
    const gs = await api(`/api/attribution-groups?ledger=${lg}`);
    if(!botLuGen.isCurrent(seq)) return;
    $("#bot_user_sel").innerHTML = (gs||[]).length
      ? gs.map(g=>`<option value="${esc(g.name)}">${esc(g.name)}</option>`).join("")
      : `<option value="">（先到 用户与预算 添加用户）</option>`;
  }catch(_){ if(botLuGen.isCurrent(seq)) $("#bot_user_sel").innerHTML = `<option value="">（用户列表加载失败）</option>`; }
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
        <td style="text-align:right">${rescan}<button class="btn danger" onclick="botUnbind('${qEsc(x.account_id)}')">解绑</button></td></tr>`;
    }).join("") || `<tr><td colspan="4" class="sub">尚无绑定——选账本和用户生成二维码,谁扫谁绑定</td></tr>`;
  }catch(e){
    console.warn("botMembers 加载失败", e);   // 静默清空在线数会掩盖接口故障，留痕控制台
    $("#bot_sum").textContent = "";
  }
}
async function botBind(ledger, label){
  const lg = ledger || parseInt($("#bot_lg_sel").value) || 0;
  if(!lg) return msg("请先选择要绑定的账本", false);   // 不再兜底账本1：多用户同库下会静默绑到别人账本
  const lb = label != null ? label : $("#bot_user_sel").value;
  try{
    await apiSend("/api/bot/ilink/qrcode","POST",{ledger:lg, label:lb});
    msg(`二维码已生成（${_lgName(lg)}${lb?" · "+lb:""}），请用微信扫码`);
    botQrShow();
  }catch(e){ msg(e.message, false); }
}
const botQrGen = makeSeq();   // 扫码流程代数（makeSeq 单源）：botQrShow 每次生成 +1。
                              // 旧流程在途的 botPoll 响应（如「off=已转正」）不得关掉
                              // 新流程的二维码框/杀掉新倒计时（与 botLuGen/shareGen 同款）
async function botQrShow(){
  const flow = botQrGen.next();
  const box = $("#bot_qrbox"), img = $("#ilink_qr");
  box.style.display = "";
  let left = 180;   // 与服务端 QR_WAIT_TIMEOUT（bot_ilink.py，秒）同值：倒计时归零即收尾
  try{
    const r = await fetch("api/bot/ilink/qrimg?t=" + Date.now());
    if(!r.ok){
      let d = ""; try{ d = (await r.json()).detail || ""; }catch(_){}
      box.style.display = "none"; return msg("二维码失败：" + (d || r.status), false);
    }
    if(img._url) URL.revokeObjectURL(img._url);   // 上一张的 objectURL 释放：重复生成不再累积滞留
    img._url = URL.createObjectURL(await r.blob());
    img.onerror = ()=> msg("二维码图片解码失败", false);
    img.src = img._url;
  }catch(e){ box.style.display = "none"; return msg(e.message, false); }
  clearInterval(_qrTimer);
  const tick = ()=>{
    $("#ilink_cd").textContent = `剩余 ${Math.floor(left/60)}:${String(left%60).padStart(2,"0")}`;
    if(left-- <= 0){
      // 归零收尾（与 expired finish 同款）：此前只停倒计时，二维码挂着「剩余 0:00」、
      // botPoll 的 2s 轮询也继续空转（弱网 catch 退避分支尤其如此）
      clearInterval(_qrTimer);
      if(_ilinkTimer){ clearTimeout(_ilinkTimer); _ilinkTimer = null; }
      if($("#bot_qrbox").style.display !== "none"){
        $("#bot_qrbox").style.display = "none";
        msg("二维码已超时，请重新生成", true);
      }
    }
  };
  tick(); _qrTimer = setInterval(tick, 1000);
  botPoll(true, flow);
}
async function botPoll(loop, flow){
  // loop=true:扫码期间 2s 自轮询。确认转正后 pending 归 off——活跃扫码中遇到 off 即流程结束。
  // flow=发起时的扫码流程代数（botQrGen，唯一入口 botQrShow 都会带上）：快速「重新扫码」时，
  // 旧流程在途的响应（尤其 off）不得关掉新流程的二维码框/杀掉新倒计时
  const isCurFlow = () => botQrGen.isCurrent(flow);
  const finish = async (m, warn)=>{
    if(!isCurFlow()) return;   // 旧流程的收尾不得触碰新流程
    clearInterval(_qrTimer);
    $("#bot_qrbox").style.display = "none";
    if(m) msg(m, !warn);
    _cfgCache = null;   // 扫码确认由服务端回调写入白名单/凭证：前端配置缓存必须失效
    await loadBotCfg(); // 白名单即时回显（内含成员表刷新）
  };
  // 排程前核对页面身份：在途响应回来时已切离设置页（qrbox 内联 display 不随切页变，
  // 只看它会自续空转轮询——showPage 清掉的定时器被这里重新排上）。try/catch 两分支共用
  const qrActive = () => document.body.dataset.page === "settings"
                    && $("#bot_qrbox").style.display !== "none";
  try{
    const s = await api("/api/bot/ilink/status");
    if(!isCurFlow()) return;
    const p = s.pending || {};
    $("#ilink_wait").textContent = ILINK_STATE_CN[p.state] || p.state || "—";
    if(p.state === "logged_in"){ await finish("绑定成功 ✅"); return; }
    if(p.state === "off" && qrActive()){ await finish(); return; }   // 已转正入成员表
    if(p.state === "expired"){ await finish("二维码已过期，请重新生成", true); return; }
    if(p.state === "error"){ await finish("扫码出错：" + (p.detail || ""), true); return; }
    await botMembers();          // 扫码期间顺带刷新成员在线状态
    if(_ilinkTimer) clearTimeout(_ilinkTimer);
    if(loop && qrActive() && ["pending_scan","scaned"].includes(p.state)){
      _ilinkTimer = setTimeout(()=>botPoll(true, flow), 2000);
    }
  }catch(_){
    if(!isCurFlow()) return;
    // 弹窗仍开着（二维码还在倒计时）时一次网络抖动不能永久停轮：退避 3s 重试；
    // finish 路径已关掉 qrbox，active=false 自然不再排程；切离设置页同停
    if(_ilinkTimer) clearTimeout(_ilinkTimer);
    _ilinkTimer = qrActive() ? setTimeout(()=>botPoll(true, flow), 3000) : null;
  }
}
async function botUnbind(aid){
  if(!await uiConfirm("解绑后该成员的微信机器人将停止记账，确定？", "解绑"))return;
  await apiSend("/api/bot/ilink/unbind","POST",{account_id:aid});
  _cfgCache = null;   // 解绑改写 chaimi.conf(ilink 凭证)：配置缓存随之失效
  msg("已解绑"); await botMembers();
}
