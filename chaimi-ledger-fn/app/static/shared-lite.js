/* ===== 基础工具单源（app.html 与 home.html 共载本文件）=====
   $/esc/qEsc/api/apiSend/makeSeq/ledgerScope/avatarUrl/pad2/nextDayStr/mkEditToggle，以及
   设备记忆 Cookie 读写 ckGet/ckSet/ckDel（见下方设备记忆节），全部在此声明一次，
   core.js 不再重复声明（此前 home 内联一份、catmgr 内置一份 qEsc 回退，三份漂移过
   esc 写法）。makeSeq/ledgerScope 的完整语义注释见下方迟到响应守卫节。 */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
/* onclick 内联字符串转义：先 JS 字符串层（\ ' 换行），再 HTML 属性层 */
const qEsc = t => String(t == null ? "" : t).replace(/[\\']/g, c => "\\" + c).replace(/[\n\r\u2028\u2029]/g, "\\n")
  .replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

/* ===== 迟到响应守卫（全站唯一实现，app/home 两页经本文件共载）=====
   makeSeq()   序号型：同 UI 重复发起时任何新 next() 作废旧响应；实例可显式共享
               （openEdit/refundRowSync 共 rfGen；now() 供「快照当前代数不推进」场景，
               如 core.editRcpUpload 的弹窗世代快照）。
   ledgerScope() 账本身份型：捕获发起时账本 id，回填前 isCurrent() 核对；.id 供后续
               请求沿用同一账本。home 页无 lid()，身份 getter 须显式注入。 */
function makeSeq(){
  let n = 0;
  return { next: () => ++n, now: () => n, isCurrent: k => k === n };
}
function ledgerScope(getId = null){
  // home 无 lid()：身份 getter 必须显式注入（switchLedger 传 () => LEDGER）。
  // 不留 LEDGER 兜底——无 getter 调用直接 ReferenceError 暴露，胜过静默用错身份源
  const get = getId || lid;
  const id = get();
  return { id, isCurrent: () => id === get() };
}

function avatarUrl(av){   // 牛牛头像地址（'m' 男牛，其余一律兜底女牛）——core/settings/home 三方单源
  return `images/avatar-cow-${av === "m" ? "m" : "f"}.png`;
}
const pad2 = n => String(n).padStart(2, "0");   // 两位补零（app 页 today/时间戳、两页日期拼接共用）
function nextDayStr(day){   // 裸日期 +1 天：作排他上界（to 为「date < 次日」，等效「≤ day 含当天」）
                            // （本地拼接，避免 toISOString 的 UTC 偏移）。全站唯一「含当天→排他」转换，
                            // stats(rangeDto)/ai/ledger/expRangeParams 共用
  const nd = new Date(day + "T00:00:00"); nd.setDate(nd.getDate()+1);
  return `${nd.getFullYear()}-${pad2(nd.getMonth()+1)}-${pad2(nd.getDate())}`;
}
const ymLabel = ym => /^\d{4}-\d{2}$/.test(ym) ? ym.replace("-","年")+"月" : ym;   // 'YYYY-MM' → 中文年月（月历标题/范围润色共用单源）
function mkEditToggle(btnId, rerender){   // 「编辑 ⇄ 完成」状态机（settings 页账户/用户表、catmgr 抽屉共用单源）
  const st = {on: false};
  st.toggle = () => { st.on = !st.on; const b = document.getElementById(btnId); if(b) b.textContent = st.on ? "完成" : "编辑"; rerender(); };
  return st;
}
async function api(path, opts){
  const r = await fetch(path.replace(/^\//, ""), opts);   // 相对路径，经 <base> 兼容网关前缀/直连
  if(!r.ok){ let e = "请求失败"; try{ e = (await r.json()).detail || e }catch(_){} throw new Error(e); }
  return r.json();
}
const apiSend = (path, method, body) => api(path, {method, headers:{"Content-Type":"application/json"},
  body: JSON.stringify(body)});

/* 在途请求去重（并发共享同一 Promise）：设置页三连读 / whoami 等热点单源——
   此前 loadMe/getLedgers/getCfg 三处各抄一份「if(!_p) _p = fetch().then(清null)」同构块。
   Promise 落定即清空（成败同）：调用方下次再调会重新请求，缓存语义由调用方自理。 */
function singleFlight(fn){
  let p = null;
  return (...args) => p || (p = Promise.resolve(fn(...args)).finally(() => { p = null; }));
}

/* ===== 设备记忆：上次用的账本、每个账本上次用的用户（app/home 共用）=====
   存在 Cookie 里：chaimi_ledger=账本id，chaimi_user_{账本id}=用户id。Cookie 会随每次
   请求发给服务端，/ 入口据此直接决定给主界面还是选人页（server.py 的 index），老用户
   打开应用不用再先过一遍 home。
   Path 取应用根路径：飞牛网关下各应用同域，靠路径隔开；服务端续期时用的是同一个
   Path（server.py 的 _app_page），两边必须一致，否则会变成两份同名 Cookie。 */
function _ckPath(){
  // <base> 指向应用根（app 页由服务端注入，home 页自带 ../）。去掉末尾斜杠：入口地址
  // 带不带斜杠，浏览器都会把 Cookie 带上
  try{ return new URL(document.baseURI).pathname.replace(/[^/]*$/, "").replace(/\/+$/, "") || "/"; }
  catch(_){ return "/"; }
}
function ckGet(name){
  try{ return (document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)")) || [])[1] || ""; }
  catch(_){ return ""; }
}
function ckSet(name, val){
  // 值没变就不写：页面脚本写的 Cookie 在 Safari/iOS 上最多活 7 天，服务端下发的不受
  // 这个限制（每次给出主界面都会重发一遍续期），别拿同一个值把服务端那份覆盖掉
  if(ckGet(name) === String(val)) return;
  try{ document.cookie = `${name}=${val}; path=${_ckPath()}; max-age=31536000; SameSite=Lax`; }catch(_){}
}
function ckDel(name){
  try{ document.cookie = `${name}=; path=${_ckPath()}; max-age=0`; }catch(_){}
}
