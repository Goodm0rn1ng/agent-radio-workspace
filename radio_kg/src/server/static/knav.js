/* 回声工作台 · 全局导航注入。
   用法：<nav data-knav="chat"></nav><script src="/static/knav.js"></script>
   data-knav 取值: home | chat | radio | clipper | dashboard */
(function () {
  const el = document.querySelector("[data-knav]");
  if (!el) return;
  const cur = el.dataset.knav || "";
  const items = [
    ["home", "/", "主页"],
    ["chat", "/chat", "对话"],
    ["radio", "/radio", "Radio 录制"],
    ["clipper", "/clipper", "直播切片"],
    ["databoard", "/clipper/dashboard", "数据看板"],
    ["dashboard", "/dashboard", "入库看板"],
  ];
  el.classList.add("knav");
  el.innerHTML =
    '<a class="knav-brand" href="/"><span class="leaf">❋</span>回声工作台</a>' +
    items.map(([id, href, label]) =>
      `<a class="knav-item${id === cur ? " on" : ""}" href="${href}">${label}</a>`
    ).join("") +
    '<span class="knav-health" title="系统健康状态"><span class="kdot off" id="knavDot"></span><span id="knavTxt">检查中…</span></span>';
  fetch("/api/health").then(r => r.json()).then(d => {
    const ok = d.status === "ok";
    const dot = document.getElementById("knavDot"), txt = document.getElementById("knavTxt");
    dot.className = "kdot " + (ok ? "on" : "bad");
    txt.textContent = ok ? "系统正常" : "组件异常";
    if (!ok) {
      const bad = Object.entries(d.components || {}).filter(([, v]) => !v.ok).map(([k]) => k);
      dot.parentElement.title = "异常组件: " + bad.join(", ");
    }
  }).catch(() => {
    document.getElementById("knavTxt").textContent = "无法连接";
    document.getElementById("knavDot").className = "kdot bad";
  });
})();
