/* 主题先行（app.html / home.html 共用，原为两份相同的内联脚本）：
   按偏好/上次解析明暗在首帧前落到 body（防刷新闪白/闪黑）；宿主/系统明暗
   由 theme.js（ESM）异步取回后校正。
   必须以经典脚本紧跟 <body> 开标签引入——早于首帧渲染与 theme.js 执行。 */
(function(){
  var dark;
  try{
    var m = localStorage.getItem("chaimi_theme") || "auto";
    dark = m === "dark" ? true : m === "light" ? false
      : (localStorage.getItem("chaimi_theme_dark") || (matchMedia("(prefers-color-scheme: dark)").matches ? "1" : "0")) === "1";
  }catch(_){ dark = matchMedia("(prefers-color-scheme: dark)").matches; }
  document.body.dataset.theme = dark ? "dark" : "light";
})();

/* 揭掉启动画面（两页共用，样式见 base.css 的 gate-hold）：加 gate-fade 让底和图标
   一起渐隐、页面从下面透出，260ms 后移除两个类（过渡 250ms，留 10ms 余量）。
   系统开了「减少动态效果」时过渡在 CSS 里被关掉，直接揭，这里不用再判断 */
function gateLift(){
  const root = document.documentElement;
  if(!root.classList.contains("gate-hold")) return;   // 没盖着就不动（重复调用无害）
  root.classList.add("gate-fade");
  setTimeout(() => root.classList.remove("gate-hold", "gate-fade"), 260);
}
