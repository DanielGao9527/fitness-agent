"use strict";

window.BodyMeasurements = (() => {
  let active = null;
  const labels = {weight_kg: "体重", body_fat_percent: "体脂率"};
  const units = {weight_kg: "kg", body_fat_percent: "%"};
  const number = value => value == null ? "未测" : String(Number(value.toFixed(1)));
  const node = (s, selector) => s.root.querySelector(selector);
  const current = s => active === s && state.user === s.user && s.root.isConnected;

  function dispose() {
    active?.controller?.abort();
    active?.observer?.disconnect();
    active = null;
  }
  function canLeave() {
    if (active?.busy) return window.confirm("请求仍在处理中，离开后请核对是否已保存。确定离开？");
    return !active?.dirty || window.confirm("体测输入尚未保存，离开并放弃这次输入？");
  }
  window.addEventListener("beforeunload", event => {
    if (active?.dirty || active?.busy) { event.preventDefault(); event.returnValue = ""; }
  });

  async function operation(s, request, success) {
    if (!current(s) || s.busy) return false;
    s.busy = true;
    s.controller = new AbortController();
    const timer = setTimeout(() => s.controller.abort(), 15000);
    const disabled = new Map();
    s.root.querySelectorAll("button,fieldset").forEach(el => {disabled.set(el, el.disabled); el.disabled = true;});
    node(s, ".body-error").hidden = true;
    node(s, ".body-status").textContent = "正在处理…";
    let ok = false;
    try {
      const result = await request(s.controller.signal);
      if (current(s)) { success(result); ok = true; }
    } catch (error) {
      if (current(s)) {
        node(s, ".body-error").textContent = error.name === "AbortError"
          ? "等待超时，输入已保留。保存请求可能已完成，可用原内容重试或刷新核对。" : error.message;
        node(s, ".body-error").hidden = false;
      }
    } finally {
      clearTimeout(timer);
      if (current(s)) {
        s.busy = false;
        for (const [el, value] of disabled) if (el.isConnected) el.disabled = value;
        node(s, ".body-status").textContent = "";
        icons();
      }
    }
    return ok;
  }

  function mount(root) {
    dispose();
    const s = active = {root, user: state.user, day: state.day, days: 30, metric: "weight_kg", data: null,
      dirty: false, busy: false, observer: null, controller: null};
    root.innerHTML = `<div class="body-toolbar"><div class="segmented" role="group" aria-label="回顾范围">
      <button type="button" data-body-days="30" aria-pressed="true" class="selected">近30天</button>
      <button type="button" data-body-days="90" aria-pressed="false">近90天</button></div>
      <div class="body-commands"><button type="button" class="primary body-add">${icon("plus")}记录体测</button>
      <button type="button" class="icon-button body-refresh" title="刷新体测回顾" aria-label="刷新体测回顾">${icon("refresh-cw")}</button></div></div>
      <p class="body-status small" role="status"></p><p class="body-error form-error" role="alert" hidden></p>
      <div class="body-editor"></div><div class="body-results"></div>`;
    root.addEventListener("click", event => {
      const button = event.target.closest("button");
      if (!button || button.disabled || !current(s) || s.busy) return;
      if (button.matches(".body-add") && canLeave()) edit(s);
      if (button.matches(".body-refresh") && canLeave()) {clearEditor(s); load(s);}
      if (button.dataset.bodyDays && canLeave()) {
        clearEditor(s); s.days = Number(button.dataset.bodyDays); load(s);
      }
      if (button.dataset.bodyMetric) {s.metric = button.dataset.bodyMetric; renderChart(s);}
      if (button.dataset.bodyEdit && canLeave()) edit(s, s.data.records.find(row => row.id === Number(button.dataset.bodyEdit)));
      if (button.dataset.bodyDelete) remove(s, s.data.records.find(row => row.id === Number(button.dataset.bodyDelete)));
    });
    load(s);
  }

  async function load(s) {
    s.observer?.disconnect();
    node(s, ".body-results").innerHTML = '<p class="loading">正在读取本人的体测与记录…</p>';
    s.root.querySelectorAll("[data-body-days]").forEach(button => {
      const selected = Number(button.dataset.bodyDays) === s.days;
      button.classList.toggle("selected", selected); button.setAttribute("aria-pressed", selected);
    });
    const ok = await operation(s, signal => api(`/body-measurements?day=${s.day}&days=${s.days}`, {signal}), data => {
      s.data = data; render(s);
    });
    if (!ok && current(s)) node(s, ".body-results").innerHTML = '<p class="muted">回顾暂未读取成功</p>';
  }

  function metricInfo(data, key) {
    const metric = data.metrics[key];
    const carried = data.current_body?.[key];
    const value = carried?.value;
    const source = carried?.source === 'measurement' ? `沿用 ${carried.day} 测量值`
      : carried?.source === 'workout' ? `沿用 ${carried.day} 训练时填写的体重`
      : value != null ? `沿用档案值${carried.day ? ` · ${carried.day} 更新` : ' · 测量日期未注明'}` : '尚未填写身体数据';
    const change = metric.change == null ? "至少两次测量后显示差值"
      : `首末记录相差 ${metric.change > 0 ? "+" : ""}${number(metric.change)} ${key === "body_fat_percent" ? "个百分点" : "kg"}`;
    return `<div><h3>当前${labels[key]}</h3><p class="body-value">${number(value)}${value != null ? `<small>${units[key]}</small>` : ""}</p>
      <p class="small muted">${source}</p>
      <p class="small">所选范围：${change}</p>${metric.count >= 2 ? `<p class="small muted">${metric.first.day} 至 ${metric.last.day} · ${metric.count} 次测量</p>` : ""}</div>`;
  }

  function render(s) {
    const data = s.data, recap = data.review;
    const estimated = recap.estimated_nutrition.kcal;
    const known = recap.nutrition.kcal.known_total;
    const recorded = known == null && !estimated.count ? "未知"
      : `${number((known || 0) + (estimated.lower_total || 0))}${estimated.count ? `–${number((known || 0) + estimated.upper_total)}` : ""} kcal`;
    node(s, ".body-results").innerHTML = `<p class="small muted body-period">${data.start} 至 ${data.end} · ${data.days}天</p>
      <section class="section body-trends"><div class="section-heading"><h2>${icon("chart-no-axes-combined")}体测变化</h2></div>
        <div class="body-metrics">${metricInfo(data, "weight_kg")}${metricInfo(data, "body_fat_percent")}</div>
        <div class="segmented body-metric-tabs" role="group" aria-label="测量指标">${Object.keys(labels).map(key => `<button type="button" data-body-metric="${key}">${labels[key]}</button>`).join("")}</div>
        <div class="body-chart"><canvas role="img"></canvas></div>
        <p class="small muted">仅显示测量值，空白日期不补值。首末差值不代表持续变化或减脂效果。</p>
      </section>
      <section class="section body-recap"><div class="section-heading"><h2>${icon("calendar-range")}同期记录</h2></div>
        <dl class="body-recap-grid"><div><dt>体测有记录</dt><dd>${recap.measurement_days}<small>天 / ${data.days}天</small></dd></div>
        <div><dt>饮食有记录</dt><dd>${recap.meal_days}<small>天 / ${data.days}天</small></dd></div>
        <div><dt>已完成训练</dt><dd>${recap.completed_workout_days}<small>天 · ${recap.completed_minutes}分钟</small></dd></div></dl>
        <p class="small">已录入食物 ${recap.meal_count}项 · 可计算热量合计 ${recorded}${estimated.count ? `（含${estimated.count}项估算）` : ""} · 热量未知 ${estimated.unknown_count}项</p>
        <p class="small muted">有记录不代表全天完整；空白不代表没吃或没练。未完成训练、助手建议不计入完成统计，不计算实际热量缺口。</p>
      </section>
      <section class="section"><div class="section-heading"><h2>${icon("list")}体测明细<span class="count">${data.records.length}条</span></h2></div>
        ${data.records.length ? `<div class="body-table"><table><thead><tr><th scope="col">日期</th><th scope="col">体重<small>kg</small></th><th scope="col">体脂<small>%</small></th><th scope="col">操作</th></tr></thead><tbody>
        ${[...data.records].reverse().map(row => `<tr><th scope="row">${row.day}${row.notes ? `<span class="body-note">${escapeHtml(row.notes)}</span>` : ""}</th><td>${number(row.weight_kg)}</td><td>${number(row.body_fat_percent)}</td><td><div class="body-row-actions"><button type="button" class="icon-button" data-body-edit="${row.id}" aria-label="编辑${row.day}体测" title="编辑体测">${icon("pencil")}</button><button type="button" class="icon-button" data-body-delete="${row.id}" aria-label="删除${row.day}体测" title="删除体测">${icon("trash-2")}</button></div></td></tr>`).join("")}</tbody></table></div>`
          : '<p class="empty">所选范围内还没有体测记录</p>'}
      </section>`;
    renderChart(s);
    s.observer?.disconnect();
    s.observer = new ResizeObserver(() => {if (current(s)) drawChart(s);});
    s.observer.observe(node(s, ".body-chart"));
    icons();
  }

  function renderChart(s) {
    s.root.querySelectorAll("[data-body-metric]").forEach(button => {
      const selected = button.dataset.bodyMetric === s.metric;
      button.classList.toggle("selected", selected); button.setAttribute("aria-pressed", selected);
    });
    drawChart(s);
  }

  function drawChart(s) {
    const canvas = node(s, "canvas");
    if (!canvas || !s.data) return;
    const width = canvas.parentElement.clientWidth, height = 240, ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, width, height);
    const points = s.data.records.filter(row => row[s.metric] != null);
    canvas.setAttribute("aria-label", `${labels[s.metric]}测量图，${s.data.start}至${s.data.end}，${points.length}次测量。具体数值见体测明细。`);
    ctx.font = '12px system-ui'; ctx.fillStyle = '#68756f';
    if (!points.length) {ctx.textAlign = 'center'; ctx.fillText('所选范围内暂无测量', width / 2, height / 2); return;}
    const values = points.map(row => row[s.metric]);
    const low = Math.floor(Math.min(...values) - 1), high = Math.ceil(Math.max(...values) + 1);
    const left = 42, right = Math.max(left + 1, width - 15), top = 16, bottom = height - 32;
    const ordinal = day => Date.parse(`${day}T00:00:00Z`) / 86400000;
    const start = ordinal(s.data.start);
    const x = row => left + (ordinal(row.day) - start) / Math.max(1, s.data.days - 1) * (right - left);
    const y = row => bottom - (row[s.metric] - low) / (high - low) * (bottom - top);
    ctx.textAlign = 'right';
    for (let i = 0; i < 3; i++) {
      const heightAt = bottom - i / 2 * (bottom - top);
      ctx.fillText(number(low + i / 2 * (high - low)), left - 8, heightAt + 4);
      ctx.strokeStyle = '#e4e9e5'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, heightAt); ctx.lineTo(right, heightAt); ctx.stroke();
    }
    ctx.textAlign = 'left'; ctx.fillText(s.data.start.slice(5), left, height - 9);
    ctx.textAlign = 'right'; ctx.fillText(s.data.end.slice(5), right, height - 9);
    ctx.strokeStyle = ctx.fillStyle = s.metric === 'weight_kg' ? '#18765c' : '#98612b'; ctx.lineWidth = 2;
    points.forEach((row, index) => {
      const previous = points[index - 1];
      if (previous && ordinal(row.day) - ordinal(previous.day) === 1) {
        ctx.beginPath(); ctx.moveTo(x(previous), y(previous)); ctx.lineTo(x(row), y(row)); ctx.stroke();
      }
      ctx.beginPath(); ctx.arc(x(row), y(row), 3.5, 0, 2 * Math.PI); ctx.fill();
    });
  }

  function clearEditor(s) {s.dirty = false; node(s, ".body-editor").replaceChildren();}
  function edit(s, row = null) {
    s.dirty = false;
    const host = node(s, ".body-editor");
    host.innerHTML = `<form class="body-form section"><h2>${row ? "修改体测" : "记录体测"}</h2><fieldset><div class="form-grid">
      ${field("测量日期", "day", row?.day || (s.day > localDate() ? localDate() : s.day), `type="date" required max="${localDate()}"`)}
      ${field("体重（kg）", "weight_kg", row?.weight_kg, 'type="number" min="20" max="400" step="0.1"')}
      ${field("体脂率（%）", "body_fat_percent", row?.body_fat_percent, 'type="number" min="1" max="75" step="0.1"')}
      <label class="span-2">测量备注（可选）<input name="notes" maxlength="300" value="${escapeHtml(row?.notes)}" placeholder="例如：早起空腹，同一台秤"></label>
      </div><p class="small muted">只填本次实际测量项。最新值同步档案与今后的公式目标；旧日补录不覆盖较新值，未测项继续沿用。</p>
      <p class="form-error body-input-error" role="alert"></p>
      <div class="form-actions"><button type="button" class="body-cancel">取消</button><button type="submit" class="primary">${icon("save")}保存体测</button></div></fieldset></form>`;
    let clientId = crypto.randomUUID(), lastPayload = null;
    const form = host.querySelector("form");
    form.addEventListener("input", () => {s.dirty = true;});
    host.querySelector(".body-cancel").onclick = () => {if (canLeave()) clearEditor(s);};
    form.onsubmit = async event => {
      event.preventDefault();
      if (!current(s) || s.busy || !form.reportValidity()) return;
      const data = new FormData(form);
      const payload = {day: data.get("day"), notes: data.get("notes").trim(),
        weight_kg: data.get("weight_kg") === "" ? null : Number(data.get("weight_kg")),
        body_fat_percent: data.get("body_fat_percent") === "" ? null : Number(data.get("body_fat_percent"))};
      if (payload.weight_kg == null && payload.body_fat_percent == null) {
        host.querySelector(".body-input-error").textContent = "体重或体脂率至少填写一项"; return;
      }
      host.querySelector(".body-input-error").textContent = "";
      const encoded = JSON.stringify(payload);
      if (lastPayload !== null && encoded !== lastPayload) clientId = crypto.randomUUID();
      lastPayload = encoded;
      const ok = await operation(s, async signal => {await api(`/body-measurements${row ? `/${row.id}` : ""}`, {
        method: row ? "PUT" : "POST", signal,
        body: JSON.stringify({...payload, ...(row ? {version: row.version} : {client_id: clientId})}),
      }); return api('/profile', {signal});}, profile => {state.profile=profile; clearEditor(s); notify("体测已保存，当前身体数据已核对更新");});
      if (ok && current(s)) await load(s);
    };
    icons(); form.querySelector('[name="weight_kg"]').focus();
  }

  async function remove(s, row) {
    if (!row || !canLeave() || !window.confirm(`删除 ${row.day} 的体测记录？`)) return;
    const ok = await operation(s, async signal => {await api(`/body-measurements/${row.id}?version=${row.version}`, {method: "DELETE", signal});return api('/profile',{signal});},
      profile => {state.profile=profile;clearEditor(s); notify("体测已删除，当前值已重新核对");});
    if (ok && current(s)) await load(s);
  }
  return {mount, dispose, canLeave};
})();
