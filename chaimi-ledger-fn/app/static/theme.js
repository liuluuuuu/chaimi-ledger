/* ===== 主题三态：浅色 / 深色 / 自动（app.html 与 home.html 共用，ESM）=====
   自动 = 飞牛宿主主题优先（桌面 iframe 与安卓/iOS App 均有桥），无桥环境（鸿蒙/
   独立浏览器/旧系统）回落浏览器系统偏好 prefers-color-scheme。
   变化感知：桌面订阅 $on('os/theme') 实时事件；手机无事件，回前台/系统偏好变化时重读。
   偏好持久化 chaimi_theme（默认 auto），解析结果存 chaimi_theme_dark 供首帧防闪。 */
import { TrimApp } from "./trim-sdk.js";

const themeMQ = matchMedia("(prefers-color-scheme: dark)");
let hostDark = null;                 // 飞牛宿主明暗；null = 宿主不可用，回落系统偏好
let _sdk = null, hostLive = false;   // 桥单例（重读共用握手）；已订阅实时事件（仅桌面）后停轮询

function themeMode(){ try{ return localStorage.getItem("chaimi_theme") || "auto"; }catch(_){ return "auto"; } }
function applyTheme(){
  const m = themeMode();
  const dark = m === "dark" || (m === "auto" && (hostDark !== null ? hostDark : themeMQ.matches));
  const flip = (document.body.dataset.theme === "dark") !== dark;   // 明暗实际翻转才重绘图表
  document.body.dataset.theme = dark ? "dark" : "light";
  const icon = (id, on) => { const el = document.getElementById(id); if(el) el.style.display = on ? "block" : "none"; };
  icon("ic-sun-ai", m === "light"); icon("ic-moon-ai", m === "dark"); icon("ic-auto-ai", m === "auto");
  try{ localStorage.setItem("chaimi_theme_dark", dark ? "1" : "0"); }catch(_){}
  if(flip && typeof dropCharts === "function") dropCharts();   // stats.js 定义；本模块（defer）晚于它执行
}
window.toggleTheme = function(){   // 侧栏按钮 onclick 直调：浅色 → 深色 → 自动 循环（home.html 无按钮）
  const next = { light: "dark", dark: "auto", auto: "light" }[themeMode()] || "auto";
  try{ localStorage.setItem("chaimi_theme", next); }catch(_){}
  applyTheme();
  if(window.msg) msg("主题：" + ({ light: "浅色", dark: "深色", auto: "自动" })[next], true, "info");   // home.html 无 msg 则静默
};
themeMQ.addEventListener("change", () => { applyTheme(); if(!hostLive) syncHost(); });   // 系统偏好变化：无桥环境实时跟随；有桥手机顺带重读宿主

const adopt = t => { if(t === "dark" || t === "light"){ hostDark = t === "dark"; applyTheme(); } };   // 值非法则不动
async function syncHost(){   // 读宿主主题（幂等；任何失败静默回落系统偏好）
  try{
    if(!_sdk) _sdk = new TrimApp();
    const cfg = await _sdk.getPlatformConfig();
    if(!_sdk.isStandaloneWeb) adopt(cfg && cfg.theme);   // 独立浏览器里是浏览器推导值，不采纳
    if(_sdk.isWeb && !hostLive){   // 桌面 Web 宿主：订阅实时事件，此后轮询全部短路
      hostLive = true;
      _sdk.$on("os/theme", (...a) => adopt(typeof a[0] === "string" ? a[0] : a[0] && a[0].theme)).catch(() => { hostLive = false; });   // payload 兼容字符串与 {theme} 对象
    }
  }catch(_){ }
}
syncHost();
/* 手机端感知主题变化（无定时器，全事件驱动）：切深色一般要离开 App——回来即触发
   visibilitychange 重读；不离开 App 的快捷切换由上面 prefers-color-scheme 变化
   事件兜住（webview 透传系统深色的机型，iOS 即如此） */
document.addEventListener("visibilitychange", () => { if(document.visibilityState === "visible" && !hostLive) syncHost(); });
applyTheme();   // 初始同步图标（body 明暗已由内联脚本先行设置，此处幂等）
