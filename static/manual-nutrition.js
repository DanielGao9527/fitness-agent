"use strict";

window.ManualNutrition = (() => {
  const entries = new WeakMap();
  const fields = ["name", "grams", "amount_description", "kcal_per_100g", "protein_per_100g", "carbs_per_100g", "fat_per_100g"];
  let active = null;
  const food = (item) => ({name: $('[name="name"]', item).value.trim(),
    grams: $('[name="grams"]', item).value === "" ? null : Number($('[name="grams"]', item).value),
    amount_description: $('[name="amount_description"]', item).value.trim()});
  const manualValues = (item) => fields.slice(3).some((name) => $(`[name="${name}"]`, item).value !== "");

  function markup(value = {}) {
    return `<div class="manual-nutrition"><p class="manual-nutrition-status small muted" role="status"></p><div class="manual-nutrition-result"></div></div>`;
  }

  function toolbar(isEdit) {
    return `<div class="manual-nutrition-toolbar"><p class="manual-batch-status small muted" role="status"></p><div class="draft-toolbar"><button type="button" data-action="estimate-manual-nutrition">${icon("sparkles")}<span>${isEdit ? "估算营养" : "估算全部营养"}</span></button><button type="button" class="icon-button" data-action="clear-manual-nutrition" title="移除营养估算" aria-label="移除营养估算" hidden>${icon("x")}</button></div></div>`;
  }

  function updateControls() {
    if (!active) return;
    const items = [...active.form.querySelectorAll(".meal-item")];
    const eligible = items.filter(item => !manualValues(item) && (!entries.get(item)?.preview || entries.get(item)?.question));
    const ever = items.some(item => entries.get(item)?.everEstimated);
    const ready = items.length && items.every(item => !portionIssue(food(item)));
    const enabled = state.nutritionStatus === "configured_unverified";
    const button = $('[data-action="estimate-manual-nutrition"]', active.form);
    button.disabled = Boolean(active.busy || !enabled || !ready || !eligible.length);
    $("span", button).textContent = ever ? "重新估算营养" : active.editing.row ? "估算营养" : "估算全部营养";
    $(".manual-batch-status", active.form).textContent = active.busy ? `正在估算 ${active.pendingCount} 项…` : !enabled ? "营养估算未启用" : !ready ? "请补充食物和基本份量" : eligible.length ? `${eligible.length} 项待估算` : "营养结果已就绪";
    $('[data-action="clear-manual-nutrition"]', active.form).hidden = !items.some(item => entries.get(item)?.estimate || entries.get(item)?.preview);
  }

  function update(item) {
    const entry = entries.get(item);
    if (!entry) return;
    const issue = portionIssue(food(item));
    const enabled = state.nutritionStatus === "configured_unverified";
    const hasManual = manualValues(item);
    $(".manual-nutrition-status", item).textContent = hasManual ? "已填写每百克营养值" : entry.error ||
      (entry.busy ? "正在估算…" : entry.stale ? "份量或食物已变更，营养待重估" : entry.estimate ? "模型估算 · 本次食用份量" : entry.question ||
      (!enabled ? "营养估算未启用" : issue ? "食物或基本份量待补充" : "营养未知 · 可跳过"));
    $(".manual-nutrition-result", item).innerHTML = entry.estimate ? nutritionDetails(entry.estimate) : "";
    updateControls();
    icons();
  }

  function mountItem(item, value = {}) {
    if (!item || entries.has(item)) return;
    entries.set(item, {estimate: value.nutrition_estimate || null, preview: null, clear: false,
      stale: false, everEstimated: Boolean(value.nutrition_estimate), question: "", error: "", requestId: crypto.randomUUID()});
    update(item);
  }

  function unlock(session) {
    session.locked?.forEach(([element, disabled]) => { if (element.isConnected) element.disabled = disabled; });
    session.locked = null;
    session.busy = false;
  }

  function cancel() {
    if (!active?.busy) return;
    active.ticket++;
    active.controller?.abort();
    unlock(active);
    active.form.querySelectorAll(".meal-item").forEach((item) => { const entry=entries.get(item); if (entry) entry.busy=false; update(item); });
  }

  function dispose() { cancel(); active = null; }

  function mount(form, value) {
    dispose();
    active = {form, editing: state.editing, busy: false, ticket: 0};
    form.querySelectorAll(".meal-item").forEach((item) => mountItem(item, value || {}));
  }

  function payload(item) {
    const entry = entries.get(item);
    return entry?.preview ? {nutrition_preview_id: entry.preview} : entry?.clear ? {clear_nutrition_estimate: true} : {};
  }

  function allowSave(form) {
    if (active?.busy) return false;
    const stale = [...form.querySelectorAll(".meal-item")].some((item) => entries.get(item)?.stale && !manualValues(item));
    return !stale || window.confirm("食物或份量已修改，尚未重新估算。仍按营养未知保存吗？");
  }

  async function estimate() {
    const session = active;
    if (!session || session.busy || $('[data-action="estimate-manual-nutrition"]', session.form).disabled) return;
    const items = [...session.form.querySelectorAll(".meal-item")].filter(item => !manualValues(item) && (!entries.get(item).preview || entries.get(item).question));
    if (!items.length || items.some(item => portionIssue(food(item)))) return;
    const ticket = ++session.ticket;
    const current = () => active === session && session.ticket === ticket && state.editing === session.editing && session.form.isConnected;
    session.busy = true;
    session.pendingCount = items.length;
    items.forEach(item => { const entry=entries.get(item); entry.busy=true; entry.error=""; update(item); });
    session.controller = new AbortController();
    session.locked = [...session.form.querySelectorAll("input,textarea,select,button")].map((element) => [element, element.disabled]);
    session.locked.forEach(([element]) => { element.disabled = true; });
    const timeout = setTimeout(() => session.controller.abort(), Math.ceil(items.length / 5) * 25000 + 10000);
    try {
      const response = await api("/nutrition/preview-batch", {method: "POST", signal: session.controller.signal,
        body: JSON.stringify({items: items.map(item => ({...food(item), client_id: entries.get(item).requestId}))})});
      if (!current()) return;
      if (response.items.length !== items.length) throw new Error("估算结果不完整，请重试");
      response.items.forEach((preview, index) => {
        const entry = entries.get(items[index]), result = preview.nutrition.items[0];
        entry.preview = preview.id;
        entry.estimate = result.status === "estimated" ? {...result, model: preview.nutrition.model, generated_at: preview.nutrition.generated_at} : null;
        entry.question = result.status === "unknown" ? `营养未知：${result.question}` : "";
        if (entry.question) entry.requestId = crypto.randomUUID();
        entry.stale = false;
        entry.everEstimated = true;
      });
    } catch (error) {
      if (current()) {
        items.forEach(item => {
          const entry=entries.get(item);
          entry.error = error.name === "AbortError" ? "估算超时，输入已保留；可重试" : error.message;
          if (error.status === 409) entry.requestId = crypto.randomUUID();
        });
      }
    } finally {
      clearTimeout(timeout);
      if (current()) {
        items.forEach(item => { entries.get(item).busy=false; });
        unlock(session);
        session.form.querySelectorAll(".meal-item").forEach(update);
      }
    }
  }

  document.addEventListener("input", (event) => {
    const item = event.target.closest("#record-form .meal-item");
    if (!item || !fields.includes(event.target.name)) return;
    const entry = entries.get(item);
    if (!entry) return;
    entry.stale ||= Boolean(entry.estimate || entry.preview);
    entry.clear ||= Boolean(entry.estimate || entry.preview);
    entry.estimate = entry.preview = null;
    entry.question = entry.error = "";
    entry.requestId = crypto.randomUUID();
    update(item);
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    if (!active) return;
    if (button.dataset.action === "estimate-manual-nutrition") estimate();
    if (button.dataset.action === "clear-manual-nutrition") {
      active.form.querySelectorAll(".meal-item").forEach(item => {
        const entry = entries.get(item);
        entry.estimate = entry.preview = null;
        entry.clear = true;
        entry.stale = false;
        entry.error = entry.question = "";
        entry.requestId = crypto.randomUUID();
        update(item);
      });
    }
  });
  return {markup, toolbar, mount, mountItem, cancel, dispose, payload, allowSave, updateControls};
})();
