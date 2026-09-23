"use strict";
window.UsageView = (() => {
  let controller = null;
  const dialog = () => document.querySelector("#usage-dialog");
  function dispose() {
    controller?.abort();
    controller = null;
    dialog()?.close();
    document.querySelector("#usage-content")?.replaceChildren();
  }
  async function show() {
    controller?.abort();
    const request = controller = new AbortController(), user = state.user;
    if (!user) return;
    const target = document.querySelector("#usage-content");
    target.textContent = "正在读取额度…";
    if (!dialog().open) dialog().showModal();
    try {
      const data = await api("/usage", { signal: request.signal });
      if (request.signal.aborted || state.user !== user || controller !== request || !dialog().open) return;
      const reset = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(data.resets_at));
      target.innerHTML = `${data.scope === 'guest_network' ? '<p>游客额度由同一网络共享，重新进入体验不会重置。</p>' : ''}<dl class="usage-values"><div><dt>本期已用</dt><dd>${Number(data.used)} 次</dd></div><div><dt>${data.scope === 'guest_network' ? '体验剩余' : '个人剩余'}</dt><dd>${Number(data.remaining)} 次</dd></div><div><dt>每日上限</dt><dd>${Number(data.limit)} 次</dd></div></dl>
        <p>${data.enabled ? !data.shared_available ? "今日服务总使用次数已达上限，手动记录仍可使用" : Number(data.remaining)>0 ? "AI 功能可用" : "今日个人使用次数已用完，手动记录仍可使用" : "AI 功能暂不可用，手动记录仍可使用"}</p>
        <p class="muted">次数恢复时间：北京时间 ${escapeHtml(reset)}</p><p class="muted">每次 AI 请求都会计入，失败也计入。这里显示使用次数，不代表账户余额或费用。</p>`;
    } catch (error) {
      if (!request.signal.aborted && controller === request && state.user === user && dialog().open) target.textContent = error.message;
    }
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-usage]");
    if (!button) return;
    if (button.dataset.usage === "close") dispose();
    else show();
  });
  dialog().addEventListener("close", () => { controller?.abort(); controller = null; });
  return { dispose };
})();
