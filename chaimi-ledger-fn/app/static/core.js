/* 基础工具（$/esc/qEsc/api/apiSend/makeSeq/ledgerScope/avatarUrl/nextDayStr/pad2/mkEditToggle）
   单源在 shared-lite.js（app.html 与 home.html 共载，core.js 不再重复声明——双份实现
   曾出现 esc 写法漂移）；fmt/toCents 为本文件声明的 app 专属变体。 */
const fmt = c => (c/100).toFixed(2);            // 分 → 元字符串
/* 元输入 → 分（记账/预算共用）：先 toPrecision(12) 再取整——裸 Math.round(parseFloat*100)
   在 *.005 边界因二进制误差丢 1 分（1.005*100=100.4999…→100） */
const toCents = y => Math.round((parseFloat(y || "0") * 100).toPrecision(12));
const MAX_UPLOAD_B = (window.CHAIMI_MAX_MB || 20) * 1048576;   // 上传上限：服务端 _page 注入（server.py MAX_UPLOAD 单源）；直开静态页无注入时回落 20MB
const mobileMQ = matchMedia("(max-width:768px)");   // 抽屉断点，与 views.css @media 同阈值（769 起为桌面）；ledger.js 复用 !mobileMQ.matches

/* 当前飞牛用户（/api/whoami 一次拉取；uid=null=本地开发无网关身份）：
   身份徽标/共享标记/默认归属选中/设置页 admin 门共用 */
let ME = {uid:null, username:"", isadmin:false};
const loadMe = singleFlight(async () => {
  try{ ME = (await api("api/whoami")) || ME; }catch(_){ return ME; }   // 拉不到按当前值用（原口径：失败不阻断）
  const b = $("#meBadge");
  if(b){
    b.style.display = ME.uid ? "" : "none";
    b.textContent = "👤 " + (ME.username || ("uid " + ME.uid)) + (ME.isadmin ? " · 管理员" : "");
  }
  return ME;
});

/* 币种单源（curSym 与设置页币种下拉 LEDGER_CURS 共用）：[中文名, 符号]，新增币种只改这里 */
const CUR_INFO = {CNY:["人民币","¥"], USD:["美元","$"], EUR:["欧元","€"], JPY:["日元","¥"], HKD:["港币","HK$"], GBP:["英镑","£"]};
const CUR_SYMBOLS = Object.fromEntries(Object.entries(CUR_INFO).map(([c, [t, s]]) => [c, s]));
let ledgersCache = [];   // loadAll 时刷新，供 curSym 反查当前账本币种
function curSym(){
  const cur = ledgersCache.find(l => l.id === +($("#ledgerSel")?.value || 0));
  return CUR_SYMBOLS[cur && cur.currency] || "¥";
}
// 日历单元专用：整数 + 千分位（如 "1,234"），不显示小数——缩小占用避免溢出；详情弹窗仍走 fmt 完整精度
const fmtI = c => { const n = Math.round(c/100); return n.toLocaleString("en-US"); };
// 本地时区取今日/本月（toISOString 是 UTC：东八区早 8 点前会差一天，新记账默认日期/今日卡片/月份选择器全受影响）
const today = () => { const d = new Date(); return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`; };
const thisMonth = () => today().slice(0,7);
/* 退款对「吸收」过滤：已被退款行引用的原交易 id 集合（流水/详情共用） */
function absorbedIds(items){ return new Set(items.filter(x=>x.refund_for).map(x=>x.refund_for)); }
/* AI 是否已配置；接口异常时按已配置处理（产品口径：失败不阻断，让用户走到具体
   功能处收到真实报错，而不是被"未配置 AI"误导） */
async function aiConfigured(){
  try{ const cfg = await getCfg(); return !!(cfg.llm && cfg.llm.api_key); }
  catch(e){ console.warn("配置读取失败，按已配置放行", e); return true; }
}
const lid = () => +$("#ledgerSel").value || 1;
const TYPE_CN = {expense:"支出", income:"收入", transfer:"转账"};
const TX_SUB_DEFAULT = "账户转账";   // 转账兜底子类单点（与后端 db.DEFAULT_TRANSFER_SUB 同值；import.js/ai.js 展示共用这里，不再各抄一份）
const TX_TYPES = [{v:"expense",t:"支出"},{v:"income",t:"收入"}];
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
/* id → 名称（saveEditTx 卡片重渲 / txSelApply 批量同步 / aiConfirmCard 假交易三处共用，
   此前各写一份 find+回退） */
const catNameOf = id => (categories.find(c=>c.id===id) || {}).name || "";
const accNameOf = id => (accounts.find(a=>a.id===id) || {}).name || "";

/* ===== 基础 ===== */
/* Toast：下滑入场 + 3 秒自动消失。msg(t) 绿 / msg(t,false) 红 / msg(t,true,"info") 蓝；
   先清 class 强制 reflow 再加 .on，动画才可靠重放 */
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
/* 全局兜底：onclick 直调 async 函数失败时 toast 出 e.reason（有本地 catch 的路径不会重复提示） */
window.addEventListener("unhandledrejection", e => {
  const r = e.reason;
  const m = r && r.message ? r.message : (typeof r === "string" && r ? r : "");
  if(m) msg(m, false);
});

let _cfmRes = null;
function uiConfirm(text, okLabel){
  if(_cfmRes) cfmClose(false);   // 重入守卫：先决掉旧 Promise（同 uiPrompt），防第一次 await 永久悬挂
  $("#cfmText").textContent = text;
  $("#cfmOk").textContent = okLabel || "确定";
  $("#cfmMask").style.display = "flex";
  return new Promise(r => _cfmRes = r);
}

let _prmRes = null;
function uiPrompt(text, def){
  // 重入守卫：单槽 resolver 被第二次调用覆盖后，第一次的 await 永远悬挂（调用链泄漏
  // 且无法取消）——先按取消决掉旧 Promise 再开新框
  if(_prmRes) prmClose(null);
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
/* api/apiSend 定义在 shared-lite.js（全页单源） */
/* 接口缓存（写操作后由调用方置空失效）：/api/ledgers 设置页一次打开原会重复 GET；
   /api/config 的在途去重由 singleFlight 承担（shared-lite 单源，不再三处各抄一块） */
const _fetchLedgers = singleFlight(async () => (ledgersCache = await api("api/ledgers")));
async function getLedgers(force){
  if(!force && ledgersCache.length) return ledgersCache;
  return _fetchLedgers();
}
let _cfgCache = null;
const _fetchCfg = singleFlight(async () => (_cfgCache = await api("api/config")));
async function getCfg(force){
  if(_cfgCache && !force) return _cfgCache;
  return _fetchCfg();
}
/* /api/config PUT 单源：写后缓存失效一处收口（settings 页 saveCfg/saveBkCfg/saveBotCfg
   共用；restore/扫码确认/解绑等非 PUT 写路径各自显式失效并注释原因） */
async function putConfig(section, body){
  await apiSend("api/config", "PUT", {[section]: body});
  _cfgCache = null;
}
const curLedger = () => getLedgers().then(ls => ls.find(l => l.id === lid()));   // 当前账本对象（预算/币种/导出文件名共用）
function toggleSidebar(){
  const open = $("#sidebar").classList.toggle("open");
  // 遮罩/推挤按「当前视口 + 展开态」联动判定：CSS 按 <768px 判定抽屉态，这里用同阈值，
  // 否则平板（CSS 抽屉、JS 误判桌面）时遮罩不出现、收起逻辑失灵
  const mobile = mobileMQ.matches;
  $("#sideMask").classList.toggle("on", open && mobile);
  $(".main").classList.toggle("pushed", open && mobile);
}
/* 主题三态（浅色/深色/自动·含飞牛宿主跟随）在 static/theme.js（ESM，本文件之后执行）：
   toggleTheme 挂在 window，侧栏按钮 onclick 直调；dropCharts 由其在明暗翻转时调用 */
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
/* 悬浮小层（fixed ui-pop：统计日期/年份 dtPop/ddPop、头像选择 avPop；记账弹窗内退款
   下拉 e_refund_dd）：Esc 与物理返回键都要先于大弹窗收。此前判断只写在 Esc 先行块、
   popstate 路径没有——弹层开着按返回页面切走、fixed 浮层残留新页；退款下拉开着按返回
   连编辑中的整窗一起关。单源于此，两路入口共用 */
function topFloatLayer(){
  if(["dtPop","ddPop","avPop"].some(id => document.getElementById(id)?.style.display === "block"))
    return () => _popHide();
  if($("#e_refund_dd")?.classList.contains("open")) return () => closeRefundDD();   // 叠在记账弹窗内：先收它，别把整窗关掉
  return null;
}
/* 最顶层模态及其关闭动作（物理键优先关它；顺序≈叠放层级：确认/输入框最上，抽屉最下）。
   弹窗开启不 pushState，返回键先 pop 一条导航历史——未登记的弹窗会漏关（返回键
   表现为"没反应"，连按直接退出应用），新弹窗必须在这里登记 */
function navTopModal(){
  const f = topFloatLayer();
  if(f) return f;
  if($("#cfmMask").style.display === "flex") return () => cfmClose(false);    // 通用确认框
  if($("#prmMask").style.display === "flex") return () => prmClose(null);     // 通用输入框
  if($("#cropMask").style.display === "flex") return () => closeCrop();       // 小票裁剪（叠在小票预览之上，先于它关闭）
  if($("#rcpMask").classList.contains("on")) return () => closeReceipt();      // 小票预览（叠在记账弹窗上）
  if($("#editMask").classList.contains("on")) return () => txClose();          // 统一记账弹窗
  if($("#txSelMask").classList.contains("on")) return () => txSelCloseModal();
  if($("#pvMask").style.display === "flex") return () => closePv();            // 导入预览
  if($("#wizMask").style.display === "flex") return () => wizClose();          // 模板向导（预览叠其上）
  if($("#dayMask").classList.contains("on")) return () => closeDay();          // 日历详情
  if($("#llmModelsMask").style.display === "flex") return () => llmModelsClose();   // 模型列表
  const cn = catNewLayer();   // 新建分类弹窗（catmgr 单源：懒构建判空+关闭配对）
  if(cn) return cn;
  if($("#shareMask").style.display === "flex") return () => shareClose();      // 账本共享（多用户）
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
/* ===== Esc 关闭顶层浮层/弹窗（物理返回键同款栈）与弹窗焦点圈禁 ===== */
document.addEventListener("keydown", e=>{
  if(e.key !== "Escape") return;
  const f = topFloatLayer();   // 悬浮小层先收（输入态不豁免：日期弹层就锚在只读输入上）
  if(f){ f(); return; }
  // 正在表单里输入：Esc 仍关弹窗（弹窗内按 Esc 关窗是标准行为），只豁免
  // 聊天/抽屉等界面层（退出输入态不该顺手折叠界面）
  const ae = document.activeElement;
  if(ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)){
    const maskOpen = [...document.querySelectorAll(".modal-mask")]
      .some(el => getComputedStyle(el).display === "flex");
    if(!maskOpen) return;
  }
  const m = navTopModal();
  if(m) m();
});
/* 焦点圈禁：顶层可见弹窗（含退款全屏选择）内 Tab 循环、焦点不逃到背景。
   取「最后一个可见者」为顶层——cfm/prm 已由 --z-confirm 保证最上，与视觉栈顶一致 */
document.addEventListener("keydown", e=>{
  if(e.key !== "Tab") return;
  const masks = [...document.querySelectorAll(".modal-mask, .rf-dd")].filter(el => {
    if(el.classList.contains("rf-dd")) return el.classList.contains("open");
    return getComputedStyle(el).display === "flex";
  });
  const top = masks[masks.length - 1];
  if(!top) return;
  const f = [...top.querySelectorAll('button:not(:disabled),input:not([type="hidden"]):not(:disabled),select,textarea,[href],[tabindex]:not([tabindex="-1"])')]
    .filter(el => el.getClientRects().length);
  if(!f.length) return;
  const first = f[0], last = f[f.length-1];
  if(!top.contains(document.activeElement)){ e.preventDefault(); first.focus(); }
  else if(e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
  else if(!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
});
function applyPageState(p){
  /* 页面态落地（showPage 与 loadAll 的刷新恢复共用）：page 标记 + 高亮 + 侧栏选中 */
  document.body.dataset.page = p;
  document.querySelectorAll(".page").forEach(x=>x.classList.toggle("on", x.id==="tab-"+p));
  document.querySelectorAll(".sidebar .nav-item[data-p]").forEach(b=>b.classList.toggle("on", b.dataset.p===p));
  moveScopeBar(p);
}
function showPage(p){
  if(_navSkip) _navSkip = false;                                  // popstate 恢复调用：历史已 pop，不再 push
  else if(document.body.dataset.page !== p){ _navStack.push(p); history.pushState({chaimiNav:p}, ""); }   // 用户切换：双栈同步 push
  try{ localStorage.setItem("chaimi_page", p); }catch(_){}        // 刷新恢复停留页（loadAll 首载读取）
  applyPageState(p);
  // 离开 AI 页收起对话态与「+」菜单
  if(p!=="ai"){ $("#tab-ai").classList.remove("chat-on"); const pw=$(".ai-plus-wrap"); if(pw) pw.classList.remove("open"); }
  if(p!=="list" && txSelMode) txSelExit();
  sumbarCollapseReset();   // 页切复位汇总收起态（gsum 全页共享）；下方按保留滚动位对齐
  sumbarCollapseApply();
  if(p==="list") { loadTx(); loadSumbarList(); }
  if(p==="renqing") loadRenqing();
  if(p==="cal"||p==="stats") refreshStats();
  if(p==="io") { loadBatches(); expInitRange(); }
  if(p==="ai"){ loadSumbarAi(); aiToday(); }
  if(p==="settings") stLoad();
  // 离开设置页：机器人扫码轮询与二维码倒计时只属于设置页场景——清定时器并把二维码框
  // 一并收起（只停轮询不收框，回来时框挂着冻结的倒计时、轮询死亡且无人恢复）。
  // _qrTimer/_ilinkTimer 声明在 settings.js（defer 序在本文件后），showPage 只在
  // 脚本全部就绪后才会被调用，直接引用（上面的 aiToday 同理）
  if(p!=="settings"){
    if(_qrTimer){ clearInterval(_qrTimer); _qrTimer = null; }
    if(_ilinkTimer){ clearTimeout(_ilinkTimer); _ilinkTimer = null; }
    const qr = document.getElementById("bot_qrbox");
    if(qr) qr.style.display = "none";
  }
  // 离开 AI 页：关闭进行中的 SSE 流（服务端 LLM 继续烧 token、文本打进隐藏气泡）
  if(p!=="ai") aiSseStop();
}
document.querySelectorAll(".sidebar .nav-item[data-p]").forEach(b=>b.onclick=()=>{
  showPage(b.dataset.p);
  // 抽屉开着就收（桌面态侧栏常驻无 open，不受影响）——不再按 innerWidth 判断（见 toggleSidebar 注释）
  if($("#sidebar").classList.contains("open")) toggleSidebar();
});

/* ===== 数据加载 ===== */
const ledgerOptions = (ls, sel) => ls.map(l=>   // 账本下拉 option（顶栏/共享标记；有图标时图标与名字间自动空格；mine=false=别人共享来的）
  `<option value="${l.id}"${sel && l.id===sel ? " selected" : ""}>${l.icon ? esc(l.icon)+" " : ""}${esc(l.name)}${l.mine===false ? "（共享）" : ""}</option>`).join("");
const _loadAllGen = makeSeq();   // loadAll 自己的世代序号：快速连切账本时（change 事件/建删账本/
                                 // 恢复备份多入口并发），慢的旧 loadAll 不得覆盖新的选择器/月份/预算框——
                                 // 这里不能用 ledgerScope（#ledgerSel 的值正是被 loadAll 改写的，
                                 // lid() 在自身 await 期间变化是它自己的产物）
async function loadAll(selectId){
  const gen = _loadAllGen.next();
  await loadMe();   // 身份先行：默认归属选中/账本过滤共享标记都依赖 ME
  if(!_loadAllGen.isCurrent(gen)) return;
  txCache = {};   // 切账本/初始加载：旧缓存作废（openEdit 取不到即静默返回，挡住跨账本误编辑）
  if(txSelMode) txSelExit();   // 批量选择集是旧账本的 id：restore 同款清理（后端 _tx_row_in_ledger
                               // 会跳过跨账本行，但工具条仍显示旧账本「已选 N 笔」误导操作）
  const ls = await getLedgers(true);   // 强制刷新：账本切换/增删入口都走这里，缓存随之换血
  if(!_loadAllGen.isCurrent(gen)) return;
  // 设备记忆里的账本（Cookie，见 shared-lite.js）：整页重载后 select 为空须避免回落第一个账本
  let keep = selectId || +$("#ledgerSel").value || 0;
  if(!keep) keep = +ckGet("chaimi_ledger") || 0;
  $("#ledgerSel").innerHTML = ledgerOptions(ls, keep);
  if(!ls.length){ location.replace("static/home.html"); return; }   // 无账本（新飞牛用户/全新安装）→ home 页建账本，不再停在空白应用
  if(keep && ls.some(l=>l.id===+keep)) $("#ledgerSel").value = keep;
  ckSet("chaimi_ledger", lid());   // 记住当前账本（被删/失效时下次自然回落）
  // 月份选择器先归位本月再并发取数：refreshStats/loadSumbarList/loadTx 都经
  // dtGet(#monthSel)||thisMonth() 回退取月，若先取数后复位，切账本时会按用户上月
  // 选的月份拉数据、选择器却显示本月（数据与选择器不一致）。dtSet 只写值不派发
  // change（不触发 onTimeChange），不会引发二次加载
  dtSet($("#monthSel"), thisMonth());
  // 刷新恢复上次停留页：在并发取数**之前**落地（applyPageState 只操作 DOM class，
  // 不依赖任何数据）——refreshStats 等渲染器的「页面可见性」门控此后都能命中，
  // 不再需要取数后按页补拉（此前 stats 页为此专门在尾部再发一次请求）
  if(!document.body.dataset.page){
    let _rst = "ai";
    try{ const _sv = localStorage.getItem("chaimi_page"); if(_sv && document.getElementById("tab-"+_sv)) _rst = _sv; }catch(_){}
    applyPageState(_rst);
  }
  await Promise.all([loadCats(), loadAccs(), loadAttrs(), loadTx(), refreshStats(), loadPendingBadge(), loadSumbarAi(), loadSumbarList(), aiToday()]);
  if(!_loadAllGen.isCurrent(gen)) return;   // 子加载器各有守卫（数据不脏），这里只挡同步写的三个控件
  await fillYearSel();
  syncScopeChips();
  $("#bdg_amt").value = ""; bdgDirty = false;   // 切账本：预算框退出编辑态，stAccs 按新账本回填（未手改才回填，防覆盖输入）
  // 停留页随新账本重拉（showPage 只在进页时触发；settings/renqing/io 的表不在上面的
  // 并发取数里，stats 的 refreshStats 已在并发取数中按落地后的页面态渲染）
  { const _p = document.body.dataset.page;
    if(_p === "settings") stLoad();
    if(_p === "renqing") loadRenqing();
    if(_p === "io") loadBatches(); }

  // 仅首载入导航栈：loadAll 还被建/删账本、改币种、恢复备份等复用，重复 push 会
  // 占满物理返回键历史、「再按一次退出」判定被稀释
  if(!_navStack.length){
    _navStack.push(document.body.dataset.page);
    history.pushState({chaimiNav: document.body.dataset.page}, "");
  }
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
  }catch(e){ console.warn("账本列表同步失败", e); }   // 下拉可能停在旧列表，留痕便于排查
}
/* 以下全局加载按「账本身份」守卫（ledgerScope 单源；loadTx 那类是序号守卫，但这些函数
   写的是跨页全局数组，账本切换后旧账本的迟到响应写进来会污染下拉/弹窗）：
   await 回来后账本已变即整包丢弃，赋值必须发生在任何全局数组写入之前 */
async function loadCats(){
  const sc = ledgerScope();
  const cs = await api(`/api/categories?ledger=${sc.id}`);
  if(!sc.isCurrent()) return;   // 期间切了账本：旧账本分类表不写全局
  categories = cs;
}
async function loadAccs(){
  const sc = ledgerScope();
  const r = await api(`/api/accounts?ledger=${sc.id}`);
  if(!sc.isCurrent()) return;   // 期间切了账本：旧账本账户表不写全局
  accounts = r.items;
}
async function loadPendingBadge(){
  const sc = ledgerScope();   // 账本身份守卫（ledgerScope 单源）：旧账本徽标的迟到响应不回填
  try{
    const r = await api(`/api/transactions?ledger=${sc.id}&need_confirm=1&page=1&page_size=1`);
    if(!sc.isCurrent()) return;
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
  }catch(e){ console.warn("待确认徽标加载失败", e); if(sc.isCurrent()) $("#pendBdg").style.display="none"; }   // 过期失败不误藏新账本徽标
}

function txCatOptions(type, selId){   // 一级分类 option；收支两类末尾挂「不计收支」分组（退款/不计收支），
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
  if(id) ckSet("chaimi_user_"+lid(), id);   // 0=无选择，不落盘（与「没记过」同义）
  renderGreet();
}
function restoreCurUser(){
  const saved = +ckGet("chaimi_user_"+lid()) || 0;
  // 同名组兜底（多用户：被共享者默认归属自己，同名归属组由共享时自动创建）也走
  // setCurUser 落盘——只改内存不写记忆的话，切到无记忆账本的设备每次冷启动都还得再点一次用户
  const same = ME.username ? attrGroups.find(g=>g.name===ME.username) : null;
  setCurUser(attrGroups.some(g=>g.id===saved) ? saved : (same ? same.id : 0));
  if(curUserId) gateLift();   // 闸门放行：渐隐揭掉启动画面（无记录时不经过=画面盖着直到跳 home）
  userGateOpen();
}
function greetWord(){ const h = new Date().getHours();
  return h<5?"夜深了":h<9?"早上好":h<12?"上午好":h<14?"中午好":h<18?"下午好":"晚上好"; }
function renderGreet(){
  const b = $("#gbUser"), u = curUser();
  b.style.display = u ? "inline-flex" : "none";
  if(u){
    const av = `<img src="${avatarUrl(u.avatar)}" alt="">`;
    b.innerHTML = av + `<b>${esc(u.name)}</b><span class="gw">${greetWord()} ▾</span>`;
  }
}
function goUserHome(){
  location.replace("static/home.html?ledger=" + lid() + "&pick=1");   // pick=1：显式选人，跳过 home 自动进入（否则又被弹回原用户）；replace：回退键不回选择页
}
function userGateOpen(){
  if(curUser()) return;
  location.replace("static/home.html?ledger=" + lid());   // 走 /static 路径（网关已验证可路由）；replace：回退键不回登录页
}
async function loadAttrs(){
  const sc = ledgerScope();   // 发请求时的账本身份：await 期间切账本，响应不得进全局（见 loadCats 注释）
  try{
    const [ats, gs] = await Promise.all([
      api(`/api/attributions?ledger=${sc.id}`),
      api(`/api/attribution-groups?ledger=${sc.id}`)
    ]);
    // 丢弃必须发生在 setAttrState 之前：它一进来就写 attrGroups 等 4 个全局数组并连带
    // restoreCurUser 改当前用户/问候栏，旧账本的归属会把新账本的用户态整体顶掉
    if(!sc.isCurrent()) return [[], []];
    setAttrState(ats, gs);
    return [ats, gs];   // 原始名单返回：设置页 stAttrs 渲染表格复用（拉取/解析单源）
  }catch(e){   // 失败清空并留痕（静默清空会让归属下拉/用户页空得莫名其妙，与全站 loader 口径一致）
    console.warn("归属名单加载失败", e);
    if(sc.isCurrent()){ attributions = []; attrGroupNames = []; raw2group = {}; }
    return [[], []];
  }
}
async function txItemAddAttr(btn){   // 条目卡内新建用户并选中（与「＋新建」分类同款交互）
  const sc = ledgerScope();
  const name = await uiPrompt("新建用户名：");
  if(!name) return;
  try{ await apiSend(`/api/attribution-groups?ledger=${sc.id}`, "POST", {name: name.trim()}); }
  catch(e){ return msg(e.message, false); }
  if(!sc.isCurrent()) return;   // POST 期间切账本：用户名不并入新账本的用户列表（loadAttrs 会拉到）
  attrGroupNames = [...new Set([...attrGroupNames, name.trim()])];
  const sel = btn.closest(".txf-item").querySelector(".txf-attr");
  if(sel) sel.innerHTML = txAttrOptionsHtml(name.trim());
  msg("用户已创建并选中 ✅");
}

function txAccOptions(selId){   // 账户 option；始终含「未指定」占位
  return `<option value="">未指定</option>` + accounts
    .map(a=>`<option value="${a.id}" ${a.id===selId?"selected":""}>${esc(a.name)}</option>`).join("");
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
  // 编辑已有转账行不显示收支切换（否则改成 expense/income 后两个 chip 都不选中、
  // 分类还被禁用，成死卡），改为只读标注后仍按 transfer 入库
  const typeRow = !showTypes ? ""
    : (transfer && mode === "create") ? `<div class="txf-ro txf-types">转账</div>`
    : `<div class="filters txf-types">${transfer   // 编辑/确认的转账：给「转账 · 不计收支」chip 可切回
        ? `<button class="fchip${type==="transfer"?" on":""}" data-t="transfer" onclick="txItemSetType(this)">转账 · 不计收支</button>` : ""}${TX_TYPES
      .map(x=>`<button class="fchip${x.v===type?" on":""}" data-t="${x.v}" onclick="txItemSetType(this)">${x.t}</button>`).join("")}</div>`;
  const catHtml = txCatOptions(type, it.category_id);
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
        <div><label>金额(元)</label><input class="txf-amt" type="number" step="0.01" min="0" inputmode="decimal" style="font-size:20px" value="${it.amount ? fmt(it.amount) : ""}"></div>
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
   返回新分类 {id,name}；用户取消返回 null。DOM 侧（插 option/选中）由调用方自理。
   重拉走 loadCats 单源（此前裸 GET 直写 categories 全局，是三处共写里唯一无守卫的）。 */
async function newCategoryAndPick(type, promptText){
  const sc = ledgerScope();
  const name = await uiPrompt(promptText);
  if(!name) return null;
  const r = await apiSend(`/api/categories?ledger=${sc.id}`, "POST", {name: name.trim(), type});
  await loadCats();
  // 单点守卫（txItemAddCat/impSetCat 两个调用方共用）：prompt/POST 期间切账本时，
  // 旧账本的新分类 id 不得交给调用方插下拉/写预览（同族 txItemAddAttr 的 isCurrent）
  if(!sc.isCurrent()) return null;
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
const _txModalGen = makeSeq();   // 记账弹窗世代（txOpen 开启、txClose 关闭各 next() 一次）：
                                 // editRcpUpload 等慢异步回来时弹窗可能已关（旧图成孤儿）
                                 // 或已换另一笔（小票挂错行），过代响应一律作废
function txOpen(mode, opts){   // 打开统一记账弹窗
  opts = opts || {};
  _txModalGen.next();   // 新一代弹窗：作废在途的弹窗级异步（editRcpUpload 等，见其守卫）
  txMode = mode;
  editTx = opts.tx || null;                      // 编辑/确认态的原始流水（editRcp 依赖）
  eRcp = opts.receipt ? opts.receipt : null;     // null=未改，字符串=已有/新上传小票
  editRefundSel = 0;   // 退款选中态按弹窗复位：非退款流水编辑中把分类切到「退款」时，
                       // refundRowSync 兜底读它——残留上一笔的旧链会把本笔错链到别人的原消费
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
  _txModalGen.next();   // 关闭也推进世代：editRcpUpload 在途时（上传中关弹窗未再开），
                        // 完成响应不得把旧图写进已关闭弹窗的 eRcp（成孤儿文件）
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
    amount: toCents(el.querySelector(".txf-amt").value),
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
  const l = lid();   // 整批一次定账本：循环内逐笔现取 lid() 时，中途切账本会把剩余几笔记进另一本账
  const ids = [];   // 各笔入库后的 id（供 AI 聊天里渲染确认卡片）
  try{
    for(let i=0;i<items.length;i++){
      const it = items[i];
      // 归属随条目（卡片内逐笔选择）；条目已有的其他 tags 原样保留（调用方带什么留什么）
      const tags = Object.assign({}, it.tags);
      if(it.attr) tags.attribution = it.attr; else delete tags.attribution;
      it.tags = tags;   // 供确认卡回显归属
      // date 原样 10 位：服务端 db.norm_date 补「记账时刻」，不再固定 12:00:00
      const r = await apiSend(`/api/transactions?ledger=${l}`, "POST", {
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
/* 一张条目卡是否会被提交：与 txSubmitCreate/aiBookingSubmit 里 txCollect().filter(x=>x.amount>0)
   同判据（单源于此，调用方勿再抄一份）。重试裁卡必须与提交过滤同一口径——否则零金额卡
   混在中间时按 DOM 序裁会裁错卡，已成功笔重试即重复入库 */
function txSubmittable(el){
  return toCents(el.querySelector(".txf-amt").value) > 0;
}
/* 部分成功后裁掉已入库的条目卡：取 container 内全部 .txf-item，按 txSubmittable 过滤
   （DOM 序=提交序：txCollect 按容器顺序收集）后移除前 doneN 张。
   doneN 覆盖全部可提交卡时不裁（全部成功语义保持现状，由调用方走成功路径）。
   返回剩余待重试的可提交卡数（0=没裁，调用方无需重编号） */
function trimPostedCards(container, doneN){
  if(!container || !(doneN > 0)) return 0;
  const submittable = [...container.querySelectorAll(".txf-item")].filter(txSubmittable);
  if(doneN >= submittable.length) return 0;
  submittable.slice(0, doneN).forEach(el=>el.remove());
  return submittable.length - doneN;
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
    // 部分成功：先把已入库的笔从弹窗裁掉（判据/顺序与提交同源），用户修正后重试才不会重复入库
    if(trimPostedCards($("#txItems"), e.ok || 0)) txRenumber($("#txItems"));
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
/* 上传小票公共链路（记账弹窗 editRcpUpload 与 AI 页 aiReceipt 共用）：
   客户端大小预检（后端同上限，免上传 20MB 后才被拒）→ POST /api/ai/receipt。
   返回识别结果（含 receipt_path；失败为 {error}）；超限/网络错误抛 Error，UI 收尾由调用方自理 */
async function uploadReceipt(file){
  if(file.size > MAX_UPLOAD_B) throw new Error(`图片超过 ${MAX_UPLOAD_B/1048576}MB 上限`);
  const fd = new FormData(); fd.append("file", file);
  return api(`/api/ai/receipt?ledger=${lid()}`, {method:"POST", body:fd});
}
async function editRcpUpload(){
  const f = $("#e_rcp_file").files[0]; if(!f) return;
  const gen = _txModalGen.now();
  try{
    const r = await uploadReceipt(f);
    if(!_txModalGen.isCurrent(gen)) return;   // 上传期间弹窗被关/重开：响应作废
    if(r.error){ msg(r.error, false); $("#e_rcp_file").value=""; return; }
    eRcp = r.receipt_path;
    $("#e_rcp_file").value="";   // 成功也清空（错误路径已清）：否则换回同一文件不触发 change
    editRcpSync();
    msg("已上传新图，保存后生效");
  }catch(e){ if(_txModalGen.isCurrent(gen)) msg("上传失败: "+e.message, false); $("#e_rcp_file").value=""; }
}
function editRcpClear(){
  if(eRcp) rcpDiscard(eRcp);   // 移除新上传且未保存的图即弃(本笔已有小票的被服务端按引用拦下)
  eRcp = "";
  $("#e_rcp_file").value = "";
  editRcpSync();
}

/* ===== 数据变更实时刷新 =====
   长轮询 /api/rev/wait：有变更立即返回，前端 300ms 续下一条；回前台掐掉半开请求并
   轻量探测——rev 与基线相同则不刷（避免每次切回全量重载丢弹窗状态），错过才刷。 */
(function(){
  let _rev = null, _lrev = null, _t = null, _lt = null, _ctrl = null;
  const cv = "cv=" + (window.CHAIMI_VER || "");
  // afterTxWrite/syncLedgers/loadAll 均为本文件函数声明（脚本作用域提升），直接调用
  const bump = ()=>{ clearTimeout(_t); _t = setTimeout(afterTxWrite, 800); };
  // lrev（账本列表代数）变化：防抖后重拉账本下拉（共享/移除/建删账本实时生效）
  const lsync = ()=>{ clearTimeout(_lt); _lt = setTimeout(syncLedgers, 400); };
  // rev/lrev 基线统一收口：首轮只立基线不刷,此后偏离才刷（loop 与 probe 共用）
  const seen = r => {
    const rv = String(r.rev), lv = String(r.lrev);
    if(_rev !== null && rv !== _rev) bump();
    if(_lrev !== null && lv !== _lrev) lsync();
    _rev = rv; _lrev = lv;
  };
  // hold 契约（服务端 /api/rev/wait 钳 1..30s）：显式下发，超时返回本身即心跳；
  // 半开检测 = hold+10s（正常请求必在 ≤hold 完成，超时必是连接已死），不再拍脑袋 35s
  const HOLD = 20;
  const loop = async ()=>{
    _ctrl = new AbortController();
    const to = setTimeout(()=>_ctrl.abort(), (HOLD+10)*1000);
    try{
      const q = (_rev === null) ? "?" : `?since=${_rev}&lsince=${_lrev}&hold=${HOLD}&`;
      const r = await fetch("api/rev/wait" + q + cv, {signal: _ctrl.signal});
      seen((await r.json()));
      setTimeout(loop, 300);
    }catch(_){ setTimeout(loop, 2000); }   // 网络故障/被掐/半开强断:2s 退避重连，基线比对补拉
    finally{ clearTimeout(to); }
  };
  loop();
  const probe = async ()=>{
    try{
      const r = await fetch("api/rev/wait?" + cv);
      seen((await r.json()));
    }catch(_){ /* 探测失败不盲刷（旧版 bump 一次网络抖动就整页重载丢弹窗状态）：回环重连后按基线比对补拉 */ }
  };
  const wake = ()=>{ if(_ctrl) _ctrl.abort(); probe(); };
  // 跨午夜陈旧：seen() 只比对 rev，0 点后没有写库时回前台也不重拉，「今天」卡/汇总条
  // 会一直显示昨天的全日数据。回前台顺带核对日期，变了就整页重载
  let _day = today();
  const wakeDay = ()=>{ if(today() !== _day){ _day = today(); loadAll(); } };
  document.addEventListener("visibilitychange", ()=>{ if(document.visibilityState === "visible"){ wake(); wakeDay(); } });
  addEventListener("focus", ()=>{ wake(); wakeDay(); });
  addEventListener("pageshow", ()=>{ wake(); wakeDay(); });
})();
/* P3-4:直连凭证过期横幅(打开应用查一次,点击跳设置页重新扫码) */
(async()=>{ try{
  const s = await fetch("api/bot/ilink/status").then(r=>r.ok?r.json():null);
  const exp = s && (s.bots||[]).filter(b=>b.state==="expired").map(b=>b.label||"成员");
  if(exp && exp.length){
    const b = document.createElement("div");
    b.className = "bot-banner";   // 样式与 z 阶梯收进 base.css（原内联 z-index:999 脱离分层体系）
    b.textContent = "微信机器人凭证已过期（" + exp.join("、") + "），点击前往重新扫码";
    b.onclick = ()=>{ const nav = document.querySelector('[data-p="settings"]'); if(nav) nav.click(); };
    document.body.appendChild(b);
  }
}catch(e){ console.warn("凭证过期探测失败", e); }   // 探测挂了横幅永远不亮，留痕控制台
})();
/* ===== 模板实例化（app.html 的 tpl-search-box / tpl-sel-bar）=====
   搜索框三件套与选择态工具条此前在 HTML 里逐字拷贝四份（流水/人情页、流水页/日弹窗），
   改结构须同步四处；现 HTML 只留壳（id+placeholder），启动时按 data-part 挂 id 实例化。
   本段在 core.js 顶层执行——defer 序先于 ledger.js 的 bindSearch 取用 #q_text/#rq_q。 */
function mountTpl(tplId, hostId, ids){
  const host = document.getElementById(hostId);
  const tpl = document.getElementById(tplId);
  if(!host || !tpl || host.childElementCount) return;
  host.appendChild(tpl.content.cloneNode(true));
  for(const [part, id] of Object.entries(ids || {})){
    const el = host.querySelector(`[data-part="${part}"]`);
    if(el) el.id = id;
  }
  const inp = host.querySelector('[data-part="input"]');
  if(inp){
    if(host.dataset.placeholder) inp.placeholder = host.dataset.placeholder;
    if(host.dataset.name) inp.name = host.dataset.name;
  }
}
mountTpl("tpl-search-box", "txSearchBox", {input: "q_text", clear: "q_clear"});
mountTpl("tpl-search-box", "rqSearchBox", {input: "rq_q", clear: "rq_q_clear"});
mountTpl("tpl-sel-bar", "txSelBar", {all: "txSelAll", count: "txSelCount", mod: "txSelModBtn", del: "txSelDelBtn"});
mountTpl("tpl-sel-bar", "daySelBar", {all: "daySelAll", count: "daySelCount", mod: "daySelModBtn", del: "daySelDelBtn"});
