const $ = s => document.querySelector(s);
const fmt = c => (c/100).toFixed(2);

/* 当前飞牛用户（/api/whoami 一次拉取；uid=null=本地开发无网关身份）：
   身份徽标/共享标记/默认归属选中/设置页 admin 门共用 */
let ME = {uid:null, username:"", isadmin:false};
let _mePromise = null;
async function loadMe(){
  if(!_mePromise) _mePromise = api("api/whoami").then(m=>{
    ME = m || ME;
    const b = $("#meBadge");
    if(b){
      b.style.display = ME.uid ? "" : "none";
      b.textContent = "👤 " + (ME.username || ("uid " + ME.uid)) + (ME.isadmin ? " · 管理员" : "");
    }
    return ME;
  }, ()=>ME);
  return _mePromise;
}

const CUR_SYMBOLS = {CNY:"¥", USD:"$", EUR:"€", JPY:"¥", HKD:"HK$", GBP:"£"};
let ledgersCache = [];   // loadAll 时刷新，供 curSym 反查当前账本币种
function curSym(){
  const cur = ledgersCache.find(l => l.id === +($("#ledgerSel")?.value || 0));
  return CUR_SYMBOLS[cur && cur.currency] || "¥";
}
// 日历单元专用：整数 + 千分位（如 "1,234"），不显示小数——缩小占用避免溢出；详情弹窗仍走 fmt 完整精度
const fmtI = c => { const n = Math.round(c/100); return n.toLocaleString("en-US"); };
const today = () => new Date().toISOString().slice(0,10);
const thisMonth = () => new Date().toISOString().slice(0,7);
/* 退款对「吸收」过滤：已被退款行引用的原交易 id 集合（流水/详情共用） */
function absorbedIds(items){ return new Set(items.filter(x=>x.refund_for).map(x=>x.refund_for)); }
/* AI 是否已配置；接口异常时按已配置处理（与各调用点原逻辑一致：失败不阻断） */
async function aiConfigured(){
  try{ const cfg = await getCfg(); return !!(cfg.llm && cfg.llm.api_key); }
  catch(_){ return true; }
}
const lid = () => +$("#ledgerSel").value || 1;
const TYPE_CN = {expense:"支出", income:"收入", transfer:"转账"};
const TX_TYPES = [{v:"expense",t:"支出"},{v:"income",t:"收入"}];
const esc = s => String(s==null?"":s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));   // 文本/属性值转义（含单引号，与 home.html 独立实现同口径）
const pad2 = n => String(n).padStart(2, "0");   // 两位补零（导出时间戳/排他上界/日历格子共用）
// onclick 内联字符串转义：先 JS 字符串层（\ ' 换行），再 HTML 属性层（& < > "，' 不转——\' 须原样到达 JS）。
// 原实现只转 '：账单商户名/账本名等可控文本里的 < 和 " 可闭合属性注入 HTML（XSS）
const qEsc = t => String(t==null?"":t).replace(/[\\']/g, c => "\\"+c).replace(/[\n\r\u2028\u2029]/g, "\\n").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const signed = (type, amount) => type === "expense" ? -amount : amount;   // 收支符号化金额（日净额/退款净额计算共用）
/* 分类→色系单源（马卡龙 CSS 变量名对）：tx-card[data-cat] 的 --cat-color/--cat-bg（启动时注入
   <style>）与 txIcon 的内联底色/字色共用这一份，改色只动这里（AI 聊天图标与流水卡永不分叉） */
const CAT_SKIN = {
  "餐饮美食": ["--m-peach", "--m-peach-50"], "加油充电": ["--m-peach", "--m-peach-50"],
  "日用百货": ["--m-gray", "--m-gray-50"], "爱车养车": ["--m-gray", "--m-gray-50"], "其他": ["--m-gray", "--m-gray-50"],
  "数码电器": ["--m-blue", "--m-blue-50"],
  "服饰装扮": ["--m-pink", "--m-pink-50"], "母婴亲子": ["--m-pink", "--m-pink-50"],
  "美容美发": ["--m-purple", "--m-purple-50"], "亲友代付": ["--m-purple", "--m-purple-50"],
  "文化休闲": ["--m-teal", "--m-teal-50"], "充值缴费": ["--m-teal", "--m-teal-50"],
  "住房物业": ["--m-butter", "--m-butter-50"],
  "医疗健康": ["--m-butter", "--m-butter-50"],
  "教育培训": ["--m-mint", "--m-mint-50"], "人情往来": ["--m-mint", "--m-mint-50"],
  "交通出行": ["--m-blue-fg", "--m-blue-100"],
  "工资薪酬": ["--m-purple", "--m-purple-50"], "兼职副业": ["--m-teal", "--m-teal-50"],
  "理财收益": ["--m-peach", "--m-peach-50"], "红包礼金": ["--m-pink", "--m-pink-50"],
  "退款返还": ["--m-mint", "--m-mint-50"], "报销": ["--m-blue", "--m-blue-50"],
  "投资本金": ["--m-blue-fg", "--m-blue-100"], "其他收入": ["--m-butter", "--m-butter-50"],
};
(function injectCatStyles(){
  const rules = Object.entries(CAT_SKIN).map(([n, [c, b]]) =>
    `.tx-card[data-cat="${n}"]{--cat-color:var(${c});--cat-bg:var(${b})}`);
  rules.push(`.tx-card[data-cat="income"]{--cat-color:var(--m-mint);--cat-bg:var(--m-mint-50)}`);
  rules.push(`.tx-card[data-cat="transfer"]{--cat-color:var(--m-gray);--cat-bg:var(--m-gray-50)}`);
  const st = document.createElement("style");
  st.textContent = rules.join("\n");
  document.head.appendChild(st);
})();
let _chartLoading = null;
function loadChart(){   // Chart.js 按需加载：只有统计页与 AI 图表附件用到，不进首屏
  if(window.Chart) return Promise.resolve();
  if(!_chartLoading){
    _chartLoading = new Promise((res, rej)=>{
      const s = document.createElement("script");
      s.src = "static/chart.umd.min.js";   // /static 路由 no-cache，升级后必取新文件
      s.onload = res;
      s.onerror = ()=>{ _chartLoading = null; rej(new Error("图表组件加载失败")); };
      document.head.appendChild(s);
    });
  }
  return _chartLoading;
}
let categories = [], accounts = [], attributions = [], attrGroups = [], attrGroupNames = [], raw2group = {}, txPageNo = 1, curTypeFilter = "",
    impPreviews = null, impMeta = null, charts = {}, txCache = {}, editTx = null;
let txMode = "create";   // 统一记账弹窗当前模式：create/edit/confirm
let eRcp = null;   // 弹窗小票三态：null=未改；""=移除；"path"=覆盖

/* ===== 基础 ===== */
/* Toast：下滑入场 + 3 秒自动消失。
   用法：msg("操作成功") 绿色 / msg("失败", false) 珊瑚红 / msg("提示", true, "info") 蓝色
   实现：先清 class 强制 reflow，再加 .on 触发动画，避免动画失效 */
let _msgTimer = null;
function msg(t, ok=true, type){
  const m = $("#msg");
  if(!m) return;
  m.textContent = t;
  const cls = type || (ok ? "ok" : "err");
  m.className = "msg";
  void m.offsetWidth;   /* 强制 reflow，让下滑动画重新触发 */
  m.classList.add(cls, "on");
  clearTimeout(_msgTimer);
  _msgTimer = setTimeout(() => m.classList.remove("on"), 3000);
}

let _cfmRes = null;
function uiConfirm(text, okLabel){
  $("#cfmText").textContent = text;
  $("#cfmOk").textContent = okLabel || "确定";
  $("#cfmMask").style.display = "flex";
  return new Promise(r => _cfmRes = r);
}

let _prmRes = null;
function uiPrompt(text, def){
  $("#prmText").textContent = text;
  const inp = $("#prmInput");
  inp.value = def || "";
  $("#prmMask").style.display = "flex";
  setTimeout(()=>{ inp.focus(); inp.select(); }, 50);
  return new Promise(r => _prmRes = r);
}
function prmClose(v){
  $("#prmMask").style.display = "none";
  if(_prmRes){ const r = _prmRes; _prmRes = null; r(v && v.trim() ? v.trim() : null); }
}
function cfmClose(ok){
  $("#cfmMask").style.display = "none";
  if(_cfmRes){ const r = _cfmRes; _cfmRes = null; r(ok); }
}
async function api(path, opts){
  const r = await fetch(path.replace(/^\//,""), opts);   // 相对路径,经 <base href> 兼容网关前缀/直连
  if(!r.ok){ let e="请求失败"; try{ e=(await r.json()).detail||e }catch(_){} throw new Error(e); }
  return r.json();
}
async function apiSend(path, method, body){   // JSON 写操作简写（各模块共用）
  return api(path, {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
}
/* 接口缓存（并发去重 + 结果复用；写操作后由调用方置空失效）：
   /api/ledgers：设置页一次打开原会重复 GET 多次；/api/config：loadCfg/loadBkCfg/loadBotCfg 三连同源 */
let _ledgersPromise = null;
async function getLedgers(force){
  if(!force && ledgersCache.length) return ledgersCache;
  if(!_ledgersPromise) _ledgersPromise = api("/api/ledgers").then(
    ls=>{ ledgersCache = ls; _ledgersPromise = null; return ls; },
    e=>{ _ledgersPromise = null; throw e; });
  return _ledgersPromise;
}
let _cfgCache = null;
async function getCfg(force){
  if(_cfgCache && !force) return _cfgCache;
  _cfgCache = await api("/api/config");
  return _cfgCache;
}
const curLedger = () => getLedgers().then(ls => ls.find(l => l.id === lid()));   // 当前账本对象（预算/币种/导出文件名共用）
function toggleSidebar(){
  const open = $("#sidebar").classList.toggle("open");
  // 判定抽屉态、JS 却判桌面态），遮罩/推挤与菜单项自动收起会在平板上失灵
  const mobile = matchMedia("(max-width:768px)").matches;
  $("#sideMask").classList.toggle("on", open && mobile);
  $(".main").classList.toggle("pushed", open && mobile);
}
function toggleTheme(){
  const b=document.body, dark=b.dataset.theme==="dark";
  b.dataset.theme = dark?"light":"dark";

  $("#ic-sun-ai").style.display = dark?"block":"none";
  $("#ic-moon-ai").style.display = dark?"none":"block";
  dropCharts();
}
let _scopeHome = null;   // 顶栏选择器的原位父节点（首次 moveScopeBar 时记录，供搬回）
function moveScopeBar(p){

  const has = ["list","cal","stats","settings"].includes(p);
  const bar = document.getElementById("scopeBar");
  if(bar){
    if(!_scopeHome) _scopeHome = bar.parentNode;
    const slot = document.getElementById("accScopeSlot");
    const target = (p === "settings" && slot) ? slot : _scopeHome;
    if(bar.parentNode !== target) target.appendChild(bar);
    bar.style.display = has ? "flex" : "none";
  }
  _scopeHasCustom = (p === "list" || p === "stats");
  if((p === "cal" || p === "settings") && appScope() === "custom") setScope("month");
}

let _navStack = [];    // 页面栈：元素=页名（loadAll 时推入初始页）；空 = 初始帧已弹尽（再按物理键=退出）
let _navSkip = false;  // popstate 恢复中重入 showPage 不再 push
/* 最顶层模态及其关闭动作（物理键优先关它；顺序≈叠放层级：确认/输入框最上，抽屉最下）。
   弹窗开启不 pushState，返回键先 pop 一条导航历史——未登记的弹窗会漏关（返回键
   表现为"没反应"，连按直接退出应用），新弹窗必须在这里登记 */
function navTopModal(){
  if($("#cfmMask").style.display === "flex") return () => cfmClose(false);    // 通用确认框
  if($("#prmMask").style.display === "flex") return () => prmClose(null);     // 通用输入框
  if($("#rcpMask").classList.contains("on")) return () => closeReceipt();      // 小票预览（叠在记账弹窗上）
  if($("#editMask").classList.contains("on")) return () => txClose();          // 统一记账弹窗
  if($("#txSelMask").classList.contains("on")) return () => txSelCloseModal();
  if($("#pvMask").style.display === "flex") return () => closePv();            // 导入预览
  if($("#wizMask").style.display === "flex") return () => wizClose();          // 模板向导（预览叠其上）
  if($("#dayMask").classList.contains("on")) return () => closeDay();          // 日历详情
  if($("#llmModelsMask").style.display === "flex") return () => llmModelsClose();   // 模型列表
  if($("#catNewMask").style.display === "flex") return () => catNewClose();    // 新建分类
  if($("#shareMask").style.display === "flex") return () => shareClose();      // 账本共享（多用户）
  if($("#avPop")?.style.display === "block") return () => _popHide();   // 头像选择浮层
  if($("#sidebar").classList.contains("open")) return () => toggleSidebar();   // 侧栏抽屉（移动端）

  const pw = $(".ai-plus-wrap");
  if(pw && pw.classList.contains("open")) return () => pw.classList.remove("open");
  const t = $("#tab-ai");
  if(t.classList.contains("chat-on")) return () => t.classList.remove("chat-on");
  return null;
}
window.addEventListener("popstate", e => {
  const m = navTopModal();
  if(m){ m(); history.pushState({chaimiGuard:1}, ""); return; }   // 模态开着：只关顶层模态，补位保持历史
  if(e.state && e.state.chaimiGuard) return;                      // 补位帧被 pop：模态已被 UI 关闭，页面不动
  _navStack.pop();
  if(!_navStack.length){ msg("再按一次退出应用", true, "info"); return; }   // 初始帧已弹尽：本次无动作，下次物理键=系统退出
  _navSkip = true;
  showPage(_navStack[_navStack.length-1]);
});
function showPage(p){
  if(_navSkip) _navSkip = false;                                  // popstate 恢复调用：历史已 pop，不再 push
  else if(document.body.dataset.page !== p){ _navStack.push(p); history.pushState({chaimiNav:p}, ""); }   // 用户切换：双栈同步 push
  document.body.dataset.page = p;
  document.querySelectorAll(".page").forEach(x=>x.classList.toggle("on", x.id==="tab-"+p));
  moveScopeBar(p);
  document.querySelectorAll(".sidebar .nav-item[data-p]").forEach(b=>b.classList.toggle("on", b.dataset.p===p));
  // 之后点输入框（focus→aiChatOn）重进对话时旧消息仍在；顺带收起「+」菜单防残留
  if(p!=="ai"){ $("#tab-ai").classList.remove("chat-on"); const pw=$(".ai-plus-wrap"); if(pw) pw.classList.remove("open"); }
  if(p!=="list" && txSelMode) txSelExit();
  if(p==="list") { loadTx(); loadSumbarList(); }
  if(p==="renqing") loadRenqing();
  if(p==="cal"||p==="stats") refreshStats();
  if(p==="io") { loadBatches(); expInitRange(); }
  if(p==="ai"){ loadSumbarAi(); aiToday(); }
  if(p==="settings") stLoad();
}
document.querySelectorAll(".sidebar .nav-item[data-p]").forEach(b=>b.onclick=()=>{
  showPage(b.dataset.p);
  // 抽屉开着就收（桌面态侧栏常驻无 open，不受影响）——不再按 innerWidth 判断（见 toggleSidebar 注释）
  if($("#sidebar").classList.contains("open")) toggleSidebar();
});

/* ===== 数据加载 ===== */
const ledgerOptions = (ls, sel) => ls.map(l=>   // 账本下拉 option（顶栏/共享标记；有图标时图标与名字间自动空格；mine=false=别人共享来的）
  `<option value="${l.id}"${sel && l.id===sel ? " selected" : ""}>${l.icon ? esc(l.icon)+" " : ""}${esc(l.name)}${l.mine===false ? "（共享）" : ""}</option>`).join("");
async function loadAll(selectId){
  await loadMe();   // 身份先行：默认归属选中/账本过滤共享标记都依赖 ME
  txCache = {};   // 切账本/初始加载：旧缓存作废（openEdit 取不到即静默返回，挡住跨账本误编辑）
  const ls = await getLedgers(true);   // 强制刷新：账本切换/增删入口都走这里，缓存随之换血
  // localStorage 是根因修复：此前整页重载(登录页回来/刷新)后 keep 取自空 select 回落第一个账本，
  // 导致「新账本建用户，进入后却回到旧账本」（真机实测 bug）
  let keep = selectId || +$("#ledgerSel").value || 0;
  if(!keep){ try{ keep = +localStorage.getItem("chaimi_ledger") || 0; }catch(_){} }
  $("#ledgerSel").innerHTML = ledgerOptions(ls, keep);
  if(!ls.length){ location.replace("static/home.html"); return; }   // 无账本（新飞牛用户/全新安装）→ home 页建账本，不再停在空白应用
  if(keep && ls.some(l=>l.id===+keep)) $("#ledgerSel").value = keep;
  try{ localStorage.setItem("chaimi_ledger", String(lid())); }catch(_){}   // 持久化当前账本（被删/失效时下次自然回落）
  await Promise.all([loadCats(), loadAccs(), loadAttrs(), loadTx(), refreshStats(), loadPendingBadge(), loadSumbarAi(), loadSumbarList(), aiToday()]);
  dtSet($("#monthSel"), thisMonth());
  await fillYearSel();
  syncScopeChips();
  $("#bdg_amt").value = ""; bdgDirty = false;   // 切账本：预算框退出编辑态，stAccs 按新账本回填（未手改才回填，防覆盖输入）
  if(document.body.dataset.page === "settings") stLoad();   // 停在设置页切账本：分类/账户/用户/预算各表随新账本重拉
  document.body.dataset.page = document.body.dataset.page || "ai";   // 初始默认 AI 页，供 body[data-page] 样式与顶栏渲染判断

  _navStack.push(document.body.dataset.page);
  history.pushState({chaimiNav: document.body.dataset.page}, "");
  moveScopeBar(document.body.dataset.page);
}
/* 账本列表同步（lrev 长轮询唤醒：他人共享给我/移除共享/建删改账本）：
   重拉列表刷新下拉；当前账本已不在列表（被移除/被删）→ home 页自愈回落，
   不再对着已失权的账本空转 403。幂等，无变化时只是重渲染下拉。 */
async function syncLedgers(){
  try{
    const ls = await getLedgers(true);
    const sel = $("#ledgerSel");
    const cur = +sel.value || 0;
    if(!ls.length || !ls.some(l=>l.id===cur)){ location.replace("static/home.html"); return; }
    sel.innerHTML = ledgerOptions(ls, cur);
  }catch(_){}
}
async function loadCats(){
  categories = await api(`/api/categories?ledger=${lid()}`);
}
async function loadAccs(){
  accounts = await api(`/api/accounts?ledger=${lid()}`);
}
async function loadPendingBadge(){
  try{
    const r = await api(`/api/transactions?ledger=${lid()}&need_confirm=1&page=1&page_size=1`);
    $("#pendBdg").textContent = r.total;
    $("#pendBdg").style.display = r.total ? "inline-block" : "none";
    const chip = $("#ftConfirm");
    if(chip){
      chip.style.display = r.total ? "" : "none";
      chip.innerHTML = "待确认" + (r.total ? ` <b class="cnt">${r.total}</b>` : "");
      if(!r.total && curTypeFilter === "confirm"){
        curTypeFilter = "";
        document.querySelectorAll("#tab-list .fchip[data-ft]").forEach(x=>x.classList.toggle("on", x.dataset.ft===""));
        loadTx();
      }
    }
  }catch(_){ $("#pendBdg").style.display="none"; }
}

function txCatOptions(type, selId, sub){   // 一级分类 option；收支两类末尾挂「不计收支」分组（退款/不计收支），
  const tHtml = transferOptgroup(selId);
  if(type === "transfer") return `<option value="">未分类</option>` + tHtml;
  return `<option value="">未分类</option>` + categories
    .filter(c=>!c.parent_id && (c.type===type || !c.type))
    .map(c=>`<option value="${c.id}" ${c.id===selId?"selected":""}>${esc(c.name)}</option>`).join("")
    + (tHtml ? `<optgroup label="退款和不计收支">${tHtml}</optgroup>` : "");
}
const transferOptgroup = selId => categories
  .filter(c=>!c.parent_id && c.type==="transfer")
  .map(c=>`<option value="${c.id}" ${c.id===selId?"selected":""}>${esc(c.name)}</option>`).join("");   // 记账/导入预览下拉共用
const attrNamesAll = cur => [...new Set([...attrGroupNames, ...attributions, ...(cur ? [cur] : [])])];   // 归属名单（含可选当前值；记账下拉/批量修改共用）
function txAttrOptionsHtml(cur){
  const curShow = cur ? (raw2group[cur] || cur) : "";
  return `<option value="">未指定</option>` + attrNamesAll(curShow)
    .map(n=>`<option ${n===curShow?"selected":""}>${esc(n)}</option>`).join("");
}
function setAttrState(ats, gs){   // 归属下拉/解析状态（loadAttrs 与设置页 stAttrs 共用，避免双份维护）
  attrGroups = gs;
  attributions = ats.filter(a=>!a.group_id).map(a=>a.name);
  attrGroupNames = gs.map(g=>g.name);
  raw2group = {};
  ats.forEach(a=>{ if(a.group_id && a.group_name) raw2group[a.name] = a.group_name; });
  restoreCurUser();   // 用户清单变化后校准当前用户（被删则回落第一个）并刷新顶栏问候
}

let curUserId = 0;
function curUser(){ return attrGroups.find(g=>g.id===curUserId) || null; }
function setCurUser(id){
  curUserId = id;
  try{ localStorage.setItem("chaimi_user_"+lid(), String(id)); }catch(_){}
  renderGreet();
}
function restoreCurUser(){
  let saved = 0;
  try{ saved = +localStorage.getItem("chaimi_user_"+lid()) || 0; }catch(_){}
  curUserId = attrGroups.some(g=>g.id===saved) ? saved : 0;
  if(!curUserId && ME.username){
    // 多用户：被共享者默认归属自己（同名归属组由共享时自动创建）；无匹配组再回落选择页
    const g = attrGroups.find(g=>g.name===ME.username);
    if(g) curUserId = g.id;
  }
  renderGreet();
  userGateOpen();
}
function greetWord(){ const h = new Date().getHours();
  return h<5?"夜深了":h<9?"早上好":h<12?"上午好":h<14?"中午好":h<18?"下午好":"晚上好"; }
function renderGreet(){
  const b = $("#gbUser"), u = curUser();
  b.style.display = u ? "inline-flex" : "none";
  if(u){
    b.value = u.id;
    const av = `<img src="${avatarUrl(u.avatar)}" alt="">`;
    b.innerHTML = av + `<b>${esc(u.name)}</b><span class="gw">${greetWord()} ▾</span>`;
  }
}
function goUserHome(){
  location.replace("static/home.html?ledger=" + lid());   // replace：回退键不回选择页
}
function userGateOpen(){
  if(curUser()) return;
  location.replace("static/home.html?ledger=" + lid());   // 走 /static 路径（网关已验证可路由）；replace：回退键不回登录页
}
async function loadAttrs(){
  try{
    const [ats, gs] = await Promise.all([
      api(`/api/attributions?ledger=${lid()}`),
      api(`/api/attribution-groups?ledger=${lid()}`)
    ]);
    setAttrState(ats, gs);
    return [ats, gs];   // 原始名单返回：设置页 stAttrs 渲染表格复用（拉取/解析单源）
  }catch(_){ attributions = []; attrGroupNames = []; raw2group = {}; return [[], []]; }
}
async function txItemAddAttr(btn){   // 条目卡内新建用户并选中（与「＋新建」分类同款交互）
  const name = await uiPrompt("新建用户名：");
  if(!name) return;
  try{ await apiSend(`/api/attribution-groups?ledger=${lid()}`, "POST", {name: name.trim()}); }
  catch(e){ return msg(e.message, false); }
  attrGroupNames = [...new Set([...attrGroupNames, name.trim()])];
  const sel = btn.closest(".txf-item").querySelector(".txf-attr");
  if(sel) sel.innerHTML = txAttrOptionsHtml(name.trim());
  msg("用户已创建并选中 ✅");
}

function txAccOptions(selId){   // 账户 option；始终含「未指定」占位
  return `<option value="">未指定</option>` + accounts
    .map(a=>`<option value="${a.id}" ${a.id===selId?"selected":""}>${esc(a.name)}</option>`).join("");
}
function avatarUrl(av){   // 牛牛头像地址（'m' 男牛，其余一律兜底女牛）——core/settings 共用，home 独立页自带
  return `images/avatar-cow-${av === "m" ? "m" : "f"}.png`;
}
function cashAccId(){   // 「现金」账户 id（新建默认）；账本无现金账户返回 0（回落未指定）
  const a = accounts.find(x=>x.name==="现金");
  return a ? a.id : 0;
}
function txfDateTime(d){   // 任意日期串 → datetime-local 值(YYYY-MM-DDTHH:MM)；无时刻(新建/AI草稿)取当前时刻
  let s = (d || "").replace("T", " ").trim();
  if(s.length <= 10) s = s + " " + new Date().toTimeString().slice(0, 8);
  return s.slice(0, 16).replace(" ", "T");
}
function txItemHtml(it, i, n, mode){   // 单张条目卡：多笔才有序号+移除；仅 create 有类型行
  mode = mode || txMode;
  const type = it.type || "expense";
  const transfer = type === "transfer";
  const showTypes = mode === "create" || transfer;
  const head = n > 1 ? `<div class="txf-head">
        <span class="txf-idx">第 ${i+1} 笔</span>
        <span style="flex:1"></span>
        <button class="icon-btn txf-rm" style="border:none;width:26px;height:26px" title="移除此笔" onclick="txRemoveItem(this)">✕</button>
      </div>` : "";
  //（否则两个 chip 都不选中、分类还被禁用，成死卡），改为只读标注后仍按 transfer 入库
  const typeRow = !showTypes ? ""
    : (transfer && mode === "create") ? `<div class="txf-ro txf-types">转账</div>`
    : `<div class="filters txf-types">${transfer   // 编辑/确认的转账：给「转账 · 不计收支」chip 可切回
        ? `<button class="fchip${type==="transfer"?" on":""}" data-t="transfer" onclick="txItemSetType(this)">转账 · 不计收支</button>` : ""}${TX_TYPES
      .map(x=>`<button class="fchip${x.v===type?" on":""}" data-t="${x.v}" onclick="txItemSetType(this)">${x.t}</button>`).join("")}</div>`;
  const catHtml = txCatOptions(type, it.category_id, it.tags && it.tags.transfer_sub);
  let attrCur = (it.tags && it.tags.attribution) || "";
  if(!attrCur && mode === "create"){ const u = curUser(); if(u) attrCur = u.name; }
  const dupHtml = it.dup_hint ? `<div class="txf-dup">⚠️ 疑似重复：${esc(dupHintText(it.dup_hint))}，确认前可删此笔</div>` : "";
  return `<div class="txf-item" data-i="${i}" data-type="${type}">${head}${typeRow}${dupHtml}
      <div class="grid3">
        <div><label>日期时间</label><input class="txf-date" type="datetime-local" value="${esc(txfDateTime(it.date))}"></div>
        <div><label>名称 <span class="sub" style="font-size:11px">必填</span></label><input class="txf-name" value="${esc(it.name)}" placeholder="品项名，如：早餐"></div>
        <div><label>备注</label><input class="txf-note" value="${esc(it.note)}" placeholder="商家/地点（可空），如：肯德基"></div>
      </div>
      <div class="grid3">
        <div><label>金额(元)</label><input class="txf-amt" type="number" step="0.01" min="0" inputmode="decimal" style="font-size:20px" value="${it.amount ? (it.amount/100).toFixed(2) : ""}"></div>
        <div><label>分类 <button class="btn btn-ghost txf-newcat" style="padding:2px 8px;font-size:11px;margin-left:4px;${transfer?"display:none":""}" onclick="txItemAddCat(this)" title="新建自定义分类">＋新建</button></label><select class="txf-cat" onchange="txItemCatChange(this)">${catHtml}</select></div>
        <div><label>流水归属 <button class="btn btn-ghost" style="padding:2px 8px;font-size:11px;margin-left:4px" onclick="txItemAddAttr(this)" title="新建用户">＋新建</button></label><select class="txf-attr">${txAttrOptionsHtml(attrCur)}</select></div>
      </div>
      <div class="grid3">
        <div><label>订单号</label><input class="txf-order" value="${esc(it.order_no || "")}" placeholder="退款匹配键，可改"></div>
        <div><label>账户</label><select class="txf-acc">${txAccOptions(it.account_id || (mode === "create" ? cashAccId() : 0))}</select></div>
      </div>
    </div>`;
}
function txItemSetType(btn){
  const item = btn.closest(".txf-item"); if(!item) return;
  item.dataset.type = btn.dataset.t;
  txItemSyncTypeRow(item);
  refundRowSync(item);
}
/* 新建分类公共流程（条目卡/导入预览/设置页共用）：prompt → POST /api/categories → 重拉分类表。
   返回新分类 {id,name}；用户取消返回 null。DOM 侧（插 option/选中）由调用方自理。 */
async function newCategoryAndPick(type, promptText){
  const name = await uiPrompt(promptText);
  if(!name) return null;
  const r = await apiSend(`/api/categories?ledger=${lid()}`, "POST", {name: name.trim(), type});
  categories = await api(`/api/categories?ledger=${lid()}`);
  return {id: r.id, name: name.trim()};
}
async function txItemAddCat(btn){   // 为所在条目卡新建分类并选中
  const item = btn.closest(".txf-item"); if(!item) return;
  const type = item.dataset.type || "expense";
  const r = await newCategoryAndPick(type, `新建${TYPE_CN[type]||""}分类名：`);
  if(!r) return;
  const opt = document.createElement("option");
  opt.value = r.id; opt.textContent = r.name; opt.selected = true;
  item.querySelector(".txf-cat").appendChild(opt);
  msg("分类已创建并选中 ✅");
}
function txRenumber(host){   // 条目卡重编号 + 按宿主刷新计数/按钮文案（弹窗 #txItems 或聊天记账卡片）
  const rest = [...host.querySelectorAll(".txf-item")];
  rest.forEach((el,i)=>{
    el.dataset.i = i;
    const head = el.querySelector(".txf-head");
    if(head) head.style.display = rest.length > 1 ? "" : "none";
    const idx = el.querySelector(".txf-idx");
    if(idx) idx.textContent = `第 ${i+1} 笔`;
  });
  if(host.id === "txItems"){
    $("#editTitle").textContent = `AI 解析 ${rest.length} 笔`;
    $("#e_save_btn").textContent = `确认记入 ${rest.length} 笔`;
  }else{
    // 聊天记账卡片：计数/按钮在气泡页脚（在 [data-txhost] 之外），需从 .bk-bubble 往上找
    const root = host.closest(".bk-bubble") || host;
    const c = root.querySelector(".bk-count"), b = root.querySelector(".bk-ok");
    if(c) c.textContent = `${rest.length} 笔`;
    if(b) b.textContent = `确认记入 ${rest.length} 笔`;
  }
  return rest;
}
function txRemoveItem(btn){   // 移除该笔并重编号；弹窗里删空即关闭，聊天卡片里删空即移除整卡
  const item = btn.closest(".txf-item"); if(!item) return;
  const host = btn.closest("[data-txhost]") || $("#txItems");
  item.remove();
  if(!txRenumber(host).length){
    if(host.id === "txItems") txClose();
    else{ const bb = host.closest(".bk-bubble"); if(bb){ rcpDiscard(bb.dataset.receipt); bb.remove(); } }
  }
}
function txOpen(mode, opts){   // 打开统一记账弹窗
  opts = opts || {};
  txMode = mode;
  editTx = opts.tx || null;                      // 编辑/确认态的原始流水（editRcp 依赖）
  eRcp = opts.receipt ? opts.receipt : null;     // null=未改，字符串=已有/新上传小票
  const txItems = (opts.items && opts.items.length) ? opts.items
    : [{type:"expense", amount:0, date: opts.date || today()}];
  const n = txItems.length;
  $("#editTitle").textContent = opts.title ||
    ({create:"记一笔", edit:"编辑交易", confirm:"确认交易"}[mode] || "记一笔");
  $("#txItems").innerHTML = txItems.map((it,i)=>txItemHtml(it, i, n)).join("");
  $("#e_refund_row").style.display = "none";     // 待确认退款由 openEdit 再按需打开
  $("#e_del_btn").style.display = (mode === "edit" || mode === "confirm") ? "" : "none";
  $("#e_confirm_btn").style.display = mode === "confirm" ? "" : "none";
  $("#e_save_btn").textContent = mode === "create" ? "记一笔" : "保存";
  $("#e_rcp_file").value = "";
  editRcpSync();
  $("#editMask").classList.add("on");
}
function txClose(){   // 关闭并复位状态；DOM 下次 txOpen 时整体重渲染
  if(eRcp) rcpDiscard(eRcp);   // 未保存的新上传小票随手清理(已入库的被服务端按流水引用拦下)
  $("#editMask").classList.remove("on");
  txMode = "create"; eRcp = null; editTx = null;
}
function txPrimary(){   // 弹窗主按钮：create 新建，edit/confirm 保存
  return txMode === "create" ? txSubmitCreate() : saveEditTx(false);
}
function txCollect(root){   // 读条目 DOM → 条目数组（金额转分）；root 供聊天记账卡片复用
  const host = root || $("#txItems");
  return [...host.querySelectorAll(".txf-item")].map(el=>({
    type: el.dataset.type || "expense",
    amount: Math.round(parseFloat(el.querySelector(".txf-amt").value||"0")*100),
    category_id: +el.querySelector(".txf-cat").value || null,
    account_id: +el.querySelector(".txf-acc").value || null,
    date: (el.querySelector(".txf-date").value || today()).replace("T", " "),   // 16 位 YYYY-MM-DD HH:MM，秒由服务端补
    name: el.querySelector(".txf-name").value.trim() || null,
    note: el.querySelector(".txf-note").value,
    attr: el.querySelector(".txf-attr").value.trim(),
    order_no: el.querySelector(".txf-order").value.trim() || null
  }));
}
async function txPostItems(items, meta){   // 逐笔 POST；失败时抛错并带上已成功笔数 e.ok（避免整卡重试导致重复入库）
  meta = meta || {};
  const ids = [];   // 各笔入库后的 id（供 AI 聊天里渲染确认卡片）
  try{
    for(let i=0;i<items.length;i++){
      const it = items[i];
      // 归属随条目（卡片内逐笔选择）；保留条目已有的其他 tags（如 AI 草稿的 transfer_sub）
      const tags = Object.assign({}, it.tags);
      if(it.attr) tags.attribution = it.attr; else delete tags.attribution;
      it.tags = tags;   // 供确认卡回显归属
      // date 原样 10 位：服务端 _norm_date 补「记账时刻」，不再固定 12:00:00
      const r = await apiSend(`/api/transactions?ledger=${lid()}`, "POST", {
        type:it.type, amount:it.amount, category_id:it.category_id, account_id:it.account_id,
        name:it.name, note:it.note, date:it.date, tags, order_no:it.order_no || null,
        refund_for: it.refund_for || null,
        receipt_path:(i===0 && meta.receipt) ? meta.receipt : "",     // 小票只挂第一笔
        source: meta.source || "app",
        ai_meta: meta.aiMeta || {}});
      ids.push(r.id);
    }
    return ids;
  }catch(e){ e.ok = ids.length; throw e; }
}
function afterTxWrite(){   // 记账成功后统一刷新各页数据
  loadTx(); loadPendingBadge(); refreshStats(); loadSumbarAi(); loadSumbarList(); loadRenqing(); aiToday();
  loadCats(); loadAccs(); loadAttrs();   // 下拉/弹窗/顶栏的全局数据源（记一笔分类、编辑弹窗账户、用户切换器）——任何端改设置后各端同步
  if(document.body.dataset.page === "io") loadBatches();   // 停在导入页时，远端导入/撤销也同步批次表
  if(document.body.dataset.page === "settings"){ loadCatMgr(); stAccs(); stAttrs(); loadLedgers(); }   // 停在设置页时，共享对方改分类/账户/用户/预算同步各表

  if($("#dayMask").classList.contains("on") && dayRefresh) dayRefresh();
}
function txValidateItems(items){   // 提交前校验（弹窗 create / 聊天记账卡共用）
  if(!items.length) return "请输入金额";
  if(items.some(x=>!x.name)) return "请填写每笔的名称（如：早餐）";
  return "";
}
async function txSubmitCreate(){   // 弹窗 create 模式：逐笔 POST，失败即停并提示已成功笔数
  const items = txCollect().filter(x=>x.amount > 0);
  const verr = txValidateItems(items);
  if(verr) return msg(verr, false);
  if($("#e_refund_row").style.display !== "none" && editRefundSel) items[0].refund_for = editRefundSel;
  const btn = $("#e_save_btn"); btn.disabled = true;
  try{
    const ids = await txPostItems(items, {receipt: editRcp(), source: "app"});
    txClose();
    msg(ids.length > 1 ? `已记入 ${ids.length} 笔 ✅` : "已记账 ✅");
    if($("#tab-ai").classList.contains("on")){   // AI 页入口（初始加载不走 showPage，按页面类判断）：聊天里展示这几笔流水卡片
      aiChatOn(); aiConfirmCard(items, ids);
    }
    afterTxWrite();
  }catch(e){
    msg(`已记入 ${e.ok || 0} 笔，第 ${(e.ok || 0)+1} 笔失败：${e.message}`, false);
  }finally{ btn.disabled = false; }
}

/* ===== 弹窗小票三态（null=未改 / ""=移除 / path=覆盖）===== */
function rcpDiscard(path){   // 取消记账/删空卡片/关弹窗时清未入库小票：服务端只删无流水引用的图
  if(!path) return;
  api(`/api/receipts/discard?path=${encodeURIComponent(path)}`, {method:"DELETE"}).catch(()=>{});
}
function editRcp(){ return eRcp === null ? (editTx && editTx.receipt_path || "") : eRcp; }
function editRcpSync(){   // 同步小票缩略图与「移除」按钮显隐
  const p = editRcp(), img = $("#e_rcp");
  img.src = p || ""; img.style.display = p ? "" : "none";
  $("#e_rcp_rm").style.display = p ? "" : "none";
}
async function editRcpUpload(){
  const f = $("#e_rcp_file").files[0]; if(!f) return;
  const fd = new FormData(); fd.append("file", f);
  try{
    const r = await api(`/api/ai/receipt?ledger=${lid()}`, {method:"POST", body:fd});
    if(r.error){ msg(r.error, false); $("#e_rcp_file").value=""; return; }
    eRcp = r.receipt_path;
    editRcpSync();
    msg("已上传新图，保存后生效");
  }catch(e){ msg("上传失败: "+e.message, false); $("#e_rcp_file").value=""; }
}
function editRcpClear(){
  if(eRcp) rcpDiscard(eRcp);   // 移除新上传且未保存的图即弃(本笔已有小票的被服务端按引用拦下)
  eRcp = "";
  $("#e_rcp_file").value = "";
  editRcpSync();
}

/* ===== 数据变更实时刷新（自 app.html 移入）=====
   长轮询 /api/rev/wait(方案B)——每个请求时长有限,可穿透会掐杀 SSE 无限流的缓冲网关;
   有变更立即返回,前端 300ms 内续上下一条,任意端记账其他端 1s 内刷新。
   回前台(focus/pageshow/visible)：掐掉可能半开的挂起请求 + 轻量探测——不带 since 的
   wait 立即返回当前 rev,与本地基线相同则什么都不做(旧版无条件刷:每次切回 6~8 个
   接口全重载,明细弹窗闪「加载中…」并丢选择态/滚动);真错过变更才刷,探测失败
   (网络抖)保守刷一次,兜底性与旧版持平。探测独立于长轮询环,环卡死也能发现变更。 */
(function(){
  let _rev = null, _lrev = null, _t = null, _lt = null, _ctrl = null;
  const cv = "cv=" + (window.CHAIMI_VER || "");
  const bump = ()=>{ clearTimeout(_t); _t = setTimeout(()=>{ if(typeof afterTxWrite === "function") afterTxWrite(); }, 800); };
  // lrev（账本列表代数）变化：防抖后重拉账本下拉（共享/移除/建删账本实时生效）
  const lsync = ()=>{ clearTimeout(_lt); _lt = setTimeout(()=>{ if(typeof syncLedgers === "function") syncLedgers(); }, 400); };
  // rev/lrev 基线统一收口：首轮只立基线不刷,此后偏离才刷（loop 与 probe 共用）
  const seen = r => {
    const rv = String(r.rev), lv = String(r.lrev);
    if(_rev !== null && rv !== _rev) bump();
    if(_lrev !== null && lv !== _lrev) lsync();
    _rev = rv; _lrev = lv;
  };
  const loop = async ()=>{
    _ctrl = new AbortController();
    const to = setTimeout(()=>_ctrl.abort(), 35000);   // 半开连接兜底:超 hold+10s 强制重开
    try{
      const q = (_rev === null) ? "?" : `?since=${_rev}&lsince=${_lrev}&`;
      const r = await fetch("api/rev/wait" + q + cv, {signal: _ctrl.signal});
      seen((await r.json()));
      setTimeout(loop, 300);
    }catch(_){ setTimeout(loop, 2000); }   // 网络故障/被掐:2s 退避重试
    finally{ clearTimeout(to); }
  };
  loop();
  const probe = async ()=>{
    try{
      const r = await fetch("api/rev/wait?" + cv);
      seen((await r.json()));
    }catch(_){ bump(); }
  };
  const wake = ()=>{ if(_ctrl) _ctrl.abort(); probe(); };
  document.addEventListener("visibilitychange", ()=>{ if(document.visibilityState === "visible") wake(); });
  addEventListener("focus", wake);
  addEventListener("pageshow", wake);
})();
/* P3-4:直连凭证过期横幅(打开应用查一次,点击跳设置页重新扫码) */
(async()=>{ try{
  const s = await fetch("api/bot/ilink/status").then(r=>r.ok?r.json():null);
  const exp = s && (s.bots||[]).filter(b=>b.state==="expired").map(b=>b.label||"成员");
  if(exp && exp.length){
    const b = document.createElement("div");
    b.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:999;background:#f59e0b;color:#fff;text-align:center;font-size:13px;padding:6px;cursor:pointer";
    b.textContent = "微信机器人凭证已过期（" + exp.join("、") + "），点击前往重新扫码";
    b.onclick = ()=>{ const nav = document.querySelector('[data-p="settings"]'); if(nav) nav.click(); };
    document.body.appendChild(b);
  }
}catch(_){} })();