"use strict";

window.IntakeTargets = (() => {
  let active = null;
  const sourceUrl = 'https://www.canada.ca/en/health-canada/services/food-nutrition/healthy-eating/dietary-reference-intakes/tables/equations-estimate-energy-requirement.html';
  const activityLabels = {inactive:'日常活动为主',low_active:'较活跃',active:'活跃',very_active:'非常活跃'};
  const purposeLabels = {fat_loss:'减脂',muscle_gain:'增肌'};
  const messages = {
    unset:"未设置每日目标", paused:"该日期未启用目标", needs_review:"档案已变化，目标待复核",
    review_due:"已到约定复核日期，暂停差额；核对后保存新目标或停用",
    out_of_scope:"特殊饮食情况，暂停目标对比", no_records:"暂无饮食记录",
    unknown_intake:"有热量未知的记录，暂不计算差额",
  };
  function current(s, ticket) {
    return active === s && s.ticket === ticket && s.user === state.user && s.day === state.day && s.root.isConnected;
  }
  function dispose() { active?.controller?.abort(); active = null; }
  function canLeave() {
    return !active?.dirty || window.confirm("目标设置尚未保存，离开并放弃这次输入？");
  }
  function range(value) {
    if (!value) return "—";
    return value.lower === value.upper ? String(value.lower) : `${value.lower} ~ ${value.upper}`;
  }
  function difference(value) {
    if (!value) return {label:"与目标差额", value:"—"};
    if (value.upper < 0) return {label:"已记录高于目标", value:range({lower:-value.upper, upper:-value.lower})};
    if (value.lower < 0) return {label:"估算区间跨过目标", value:range(value)};
    return {label:"目标减已录入摄入", value:range(value)};
  }
  function estimateDetails(value) {
    if (!value) return '';
    const base = value.maintenance || value, p = base.inputs, m = base.measurements, a = value.adjustment;
    return `${a ? `<p class="small">维持参考约 ${escapeHtml(base.kcal)} ${a.purpose === 'fat_loss' ? '−' : '+'} 用户已有${escapeHtml(purposeLabels[a.purpose])}调整 ${escapeHtml(a.amount_kcal)} = 约 ${escapeHtml(value.kcal)} kcal / 天</p>
      <p class="small">用户填写调整来源：${escapeHtml(a.source)} · 复核日期 ${escapeHtml(a.review_on)}</p>
      <p class="small muted">调整量不是系统推荐，来源备注未经专业审核；${escapeHtml(value.method)}</p>` : ''}
      <p class="small">${escapeHtml(p.age)}岁 · ${p.equation_sex === 'male' ? '男性公式' : '女性公式'} · ${escapeHtml(activityLabels[p.activity])} · ${escapeHtml(m.height_cm)} cm / ${escapeHtml(m.weight_kg)} kg</p>
      <p class="small muted">${escapeHtml(base.source)} · 公式表${escapeHtml(base.source_version)} · ${escapeHtml(base.method)} · 维持参考约至${escapeHtml(base.rounding_kcal)} kcal</p>
      <a class="small" href="${sourceUrl}" target="_blank" rel="noopener noreferrer">查看计算依据 ${icon('external-link')}</a>`;
  }
  function render(s, summary) {
    s.summary = summary; s.dirty = false;
    const target = summary.intake_target, comparison = summary.intake_comparison;
    const saved = target.target, delta = difference(comparison.difference_kcal);
    s.root.innerHTML = `<div class="section-heading"><h2>${icon("target")}每日摄入对比</h2><div class="target-actions">
      <button type="button" class="icon-button target-calculate" title="测算维持参考" aria-label="测算维持参考" ${target.status === "out_of_scope" ? "disabled" : ""}>${icon("calculator")}</button>
      <button type="button" class="icon-button target-edit" title="设置每日目标" aria-label="设置每日目标" ${target.status === "out_of_scope" ? "disabled" : ""}>${icon("pencil")}</button>
      ${saved?.kcal ? `<button type="button" class="icon-button target-pause" title="停用目标" aria-label="停用目标">${icon("pause")}</button>` : ""}
      <button type="button" class="icon-button target-refresh" title="刷新目标对比" aria-label="刷新目标对比">${icon("refresh-cw")}</button></div></div>
      <div class="target-values"><div><span>已确认摄入目标</span><strong>${saved?.kcal ?? "—"}<small>kcal / 天</small></strong></div>
      <div><span>已录入摄入${comparison.estimated_count ? "（含估算）" : ""}</span><strong>${range(comparison.recorded_kcal)}<small>kcal</small></strong></div>
      <div><span>${delta.label}</span><strong>${delta.value}<small>kcal</small></strong></div></div>
      <p class="target-state small">${messages[comparison.status] || "仅与用户确认目标对比，不代表推荐食量"}</p>
      <p class="small muted">已录入${summary.meal_count}条 · ${comparison.estimated_count}条模型估算 · ${comparison.unknown_count}条热量未知</p>
      ${saved ? `<details class="target-details"><summary>目标记录${saved.estimate?.adjustment ? '（公式参考＋用户调整）' : saved.estimate ? '（公式估算）' : ''}</summary><p class="small">${escapeHtml(saved.effective_from)} 起${saved.kcal == null ? "停用" : "生效"}，${saved.estimate?.adjustment ? '至复核日期或下一次调整' : '至下一次调整'}${saved.source ? ` · ${saved.estimate ? '估算来源' : '用户填写来源'}：${escapeHtml(saved.source)}` : ""}</p>${estimateDetails(saved.estimate)}<p class="small muted">确认时间 ${escapeHtml(saved.confirmed_at)} UTC · 第${saved.version}版</p></details>` : ""}
      <p class="small muted">未录入饮食不在合计中。摄入差额不是实际能量缺口，训练消耗不抵扣饮食。</p>
      <div class="target-editor"></div><p class="target-status small" role="status"></p><p class="target-error form-error" role="alert" hidden></p>`;
    s.root.querySelector('.target-edit').addEventListener('click', () => edit(s));
    s.root.querySelector('.target-calculate').addEventListener('click', () => calculate(s));
    s.root.querySelector('.target-pause')?.addEventListener('click', () => pause(s));
    s.root.querySelector('.target-refresh').addEventListener('click', () => { if (canLeave()) reload(s); });
    icons();
  }
  async function operation(s, work, done) {
    if (active !== s || s.busy) return;
    const ticket = ++s.ticket; s.busy = true; s.controller = new AbortController();
    const timer = setTimeout(() => s.controller.abort(), 15000);
    s.root.querySelectorAll('button,fieldset').forEach(node => node.disabled = true);
    s.root.querySelector('.target-error').hidden = true;
    s.root.querySelector('.target-status').textContent = "正在处理…";
    try {
      const result = await work(s.controller.signal);
      if (current(s, ticket)) done(result);
    } catch (error) {
      if (current(s, ticket)) {
        const node = s.root.querySelector('.target-error'); node.hidden = false;
        node.textContent = error.name === 'AbortError' ? "等待超时，未自动重试；输入保留，可刷新核对是否已保存" : error.message;
      }
    } finally {
      clearTimeout(timer);
      if (current(s, ticket)) {
        s.busy = false;
        s.root.querySelectorAll('button,fieldset').forEach(node => node.disabled = false);
        s.root.querySelector('.target-edit').disabled = s.summary.intake_target.status === 'out_of_scope';
        s.root.querySelector('.target-calculate').disabled = s.summary.intake_target.status === 'out_of_scope';
        s.root.querySelector('.target-status').textContent = '';
      }
    }
  }
  function reload(s) {
    if (active === s && !s.busy) return refresh();
  }
  function calculate(s) {
    if (!canLeave()) return;
    operation(s, signal => api(`/intake-target?day=${s.day}`, {signal}), data => {
      s.dirty = false;
      const host = s.root.querySelector('.target-editor'), m = data.measurements;
      if (!m) throw new Error('服务尚未加载测算功能，请稍后刷新');
      host.innerHTML = `<form class="target-form energy-form"><fieldset>
        <h3>维持参考与目标调整</h3>
        <p class="small">档案身高 ${escapeHtml(m.height_cm ?? '未填写')} cm · 体重 ${escapeHtml(m.weight_kg ?? '未填写')} kg</p>
        <div class="form-grid"><label>测算年龄（周岁）<input type="number" name="age" min="19" max="100" step="1" required></label>
        <label>公式性别<select name="equation_sex" required><option value="">请选择</option><option value="male">男性公式</option><option value="female">女性公式</option></select></label>
        <label class="span-2">整体活动水平<select name="activity" required><option value="">请选择，不确定可先不测算</option>${Object.entries(activityLabels).map(([value,label]) => `<option value="${value}">${label}</option>`).join('')}</select></label>
        <label>目标生效日期<input type="date" name="effective_from" required value="${s.day}"></label>
        <label>本次用途<select name="purpose"><option value="maintain">维持参考</option><option value="fat_loss">使用已有减脂调整</option><option value="muscle_gain">使用已有增肌调整</option></select></label></div>
        <div class="energy-adjustment" hidden>
          <div class="form-grid"><label><span class="adjustment-label">每日减少量（kcal）</span><input type="number" name="amount_kcal" min="50" max="500" step="50" required disabled></label>
          <label>复核日期<input type="date" name="review_on" required disabled></label>
          <label class="span-2">已有调整量的来源备注<textarea name="adjustment_source" rows="2" maxlength="200" required disabled></textarea></label></div>
          <p class="small muted">仅使用已有且已核对的调整方案；不知道调整量时可先保留维持参考，不必猜数字。复核日暂停差额，不自动续期。</p>
          <details class="energy-scope small"><summary>调整范围与复核</summary><p>当前入口只接受50–500 kcal、50的整数倍，且至多为维持参考的20%；结果需大于1200且不超过5000。这些是产品防错限制，不代表其中任一数值都健康或适合你，低能量饮食需专业支持。</p>
          <p>复核日可选生效后1–28天，初始14天仅为可改的提醒安排，不是医学随访周期。调整不保证减脂或增肌效果。</p></details>
        </div>
        <details class="energy-scope small"><summary>活动分类与适用范围</summary>
          <p>看平时整天的活动，包含日常劳动和运动，不是只看每周健身次数。以下是官方日活动示例，不是训练建议：</p>
          <ul><li>日常活动为主：步行约30分钟及轻中度家务约90分钟。</li><li>较活跃：日常活动外，再快走约60–80分钟。</li><li>活跃：日常活动外，再快走约30–50分钟、骑车约45分钟、双打网球约40分钟。</li><li>非常活跃：日常活动外，再骑车约45分钟、慢跑约25分钟、双打网球约60分钟。</li></ul>
          <p>此简化入口仅支持19–100岁、身高140–210cm、体重40–200kg、BMI 18.5至小于40；这是产品收窄范围，不是诊断。公式分类无法对应自身情况时可使用已核对的手填目标。</p>
        </details>
        <label class="plan-check"><input type="checkbox" name="scope" required>已核对身高体重和活动；非孕哺期，无需疾病相关特殊饮食管理</label>
        <p class="small muted">公式仅估算维持参考，减脂/增肌调整由你提供；包含活动，不再加训练消耗。信息只在确认保存目标后留存，不发给模型。</p>
        <div class="dialog-actions"><button type="button" class="energy-cancel">取消</button><button type="submit">${icon('calculator')}计算参考值</button></div>
        <div class="energy-result" aria-live="polite"></div>
      </fieldset></form>`;
      const form = host.querySelector('form'), result = host.querySelector('.energy-result');
      const adjustmentHost = form.querySelector('.energy-adjustment'), reviewDate = form.elements.review_on;
      function updateDates() {
        const start = new Date(`${form.elements.effective_from.value}T12:00:00Z`);
        const offset = days => {const d = new Date(start); d.setUTCDate(d.getUTCDate() + days); return Number.isNaN(d.getTime()) ? '' : d.toISOString().slice(0,10);};
        reviewDate.min = offset(1); reviewDate.max = offset(28);
        if (!reviewDate.value) reviewDate.value = offset(14);
      }
      updateDates();
      form.elements.effective_from.addEventListener('input', updateDates);
      form.elements.purpose.addEventListener('change', () => {
        adjustmentHost.hidden = form.elements.purpose.value === 'maintain';
        adjustmentHost.querySelectorAll('input,textarea').forEach(input => input.disabled = adjustmentHost.hidden);
        form.querySelector('.adjustment-label').textContent = form.elements.purpose.value === 'muscle_gain' ? '每日增加量（kcal）' : '每日减少量（kcal）';
      });
      let preview = null, submitted = null, clientId = crypto.randomUUID();
      form.addEventListener('input', () => {s.dirty = true; preview = null; submitted = null; result.replaceChildren(); clientId = crypto.randomUUID();});
      form.querySelector('.energy-cancel').addEventListener('click', () => {if (canLeave()) {s.dirty = false; host.replaceChildren();}});
      form.addEventListener('submit', event => {
        event.preventDefault();
        const values = Object.fromEntries(new FormData(form));
        const inputs = {age:Number(values.age),equation_sex:values.equation_sex,activity:values.activity,general_adult:values.scope === 'on'};
        const adjustment = values.purpose === 'maintain' ? null : {purpose:values.purpose,amount_kcal:Number(values.amount_kcal),source:values.adjustment_source,review_on:values.review_on};
        const endpoint = adjustment ? 'adjustment' : 'estimate';
        preview = null; submitted = null; result.replaceChildren();
        operation(s, signal => api(`/intake-target/${endpoint}`,{method:'POST',signal,body:JSON.stringify({day:values.effective_from,context_hash:data.context_hash,inputs,...(adjustment ? {adjustment} : {})})}), response => {
          preview = response.estimate; s.dirty = true;
          submitted = {client_id:clientId,version:response.target_state.version,context_hash:response.target_state.context_hash,
            effective_from:values.effective_from,inputs,kcal:preview.kcal,method:preview.method,confirmed:true,...(adjustment ? {adjustment} : {})};
          result.innerHTML = `<h3>${adjustment ? '已有方案调整后' : '维持参考'}：约 ${escapeHtml(preview.kcal)} kcal / 天</h3>
            <p class="small">未保存。个体需求可能明显不同，不能当作实测或必须吃够的数值；${adjustment ? '这里只计算你提供的调整，不代表系统认定该方案适合你。' : '减脂/增肌调整尚未计算。'}</p>
            ${estimateDetails(preview)}<p class="small muted">确认后将保存这次年龄、公式性别、活动和体征快照，替换生效日期起的目标；不会修改个人档案或餐单。</p>
            <button type="button" class="primary energy-confirm">${icon('check')}${adjustment ? '确认保存调整目标' : '确认用作维持目标'}</button>`;
          result.querySelector('.energy-confirm').addEventListener('click', () => {
            if (!preview || !submitted) return;
            operation(s, signal => api(`/intake-target/confirm-${endpoint}`,{method:'POST',signal,body:JSON.stringify(submitted)}), () => {
              s.dirty = false; notify(adjustment ? '调整目标已保存' : '维持参考已确认为目标'); refresh();
            });
          });
          icons();
        });
      });
      icons(); form.querySelector('[name="age"]').focus();
    });
  }
  function edit(s) {
    if (!canLeave()) return;
    operation(s, signal => api(`/intake-target?day=${s.day}`, {signal}), data => {
      const host = s.root.querySelector('.target-editor');
      host.innerHTML = `<form class="target-form"><fieldset><div class="form-grid">
        <label>生效日期<input type="date" name="effective_from" required value="${s.day}"></label>
        <label>每日摄入目标（kcal）<input type="number" name="kcal" min="1000" max="5000" step="1" required value="${data.target?.kcal ?? ''}"></label>
        <label class="span-2">已有目标的来源备注<input name="source" maxlength="200" required value="${escapeHtml(data.target?.source || '')}"></label></div>
        <p class="small muted">这是你已有的目标，不是系统测算或推荐；从生效日期起使用，至下一次调整，不改更早日期。</p>
        <label class="plan-check"><input type="checkbox" name="reviewed" required>已核对目标；我已成年、非孕哺期，无需疾病相关特殊饮食管理</label>
        <div class="dialog-actions"><button type="button" class="target-cancel">取消</button><button type="submit" class="primary">${icon("save")}确认保存目标</button></div></fieldset></form>`;
      const form = host.querySelector('form');
      let clientId = crypto.randomUUID();
      form.addEventListener('input', () => {s.dirty = true; clientId = crypto.randomUUID();});
      form.querySelector('.target-cancel').addEventListener('click', () => {
        if (canLeave()) {s.dirty = false; host.replaceChildren();}
      });
      form.addEventListener('submit', event => {
        event.preventDefault();
        const fields = Object.fromEntries(new FormData(form));
        const body = {client_id:clientId, version:data.version, context_hash:data.context_hash,
          effective_from:fields.effective_from, kcal:Number(fields.kcal), source:fields.source,
          confirmed:fields.reviewed === 'on', general_adult:fields.reviewed === 'on'};
        operation(s, async signal => {
          await api('/intake-target', {method:'POST',signal,body:JSON.stringify(body)});
          return api(`/summary?day=${s.day}`, {signal});
        }, () => {s.dirty = false; notify('目标已保存'); refresh();});
      });
      icons(); form.querySelector('[name="kcal"]').focus();
    });
  }
  function pause(s) {
    if (!canLeave() || !window.confirm(`从${s.day}起停用摄入目标？更早日期和饮食记录不变。`)) return;
    const data = s.summary.intake_target;
    operation(s, async signal => {
      await api('/intake-target', {method:'POST',signal,body:JSON.stringify({client_id:crypto.randomUUID(),version:data.version,
        context_hash:data.context_hash,effective_from:s.day,kcal:null,confirmed:true})});
      return api(`/summary?day=${s.day}`, {signal});
    }, () => {s.dirty = false; notify('目标已停用'); refresh();});
  }
  function mount(root, summary) {
    dispose();
    if (!summary?.intake_target || !summary?.intake_comparison) {
      root.innerHTML = '<p class="small muted">目标信息暂不可用，请稍后刷新</p>';
      return;
    }
    const s = active = {root, user:state.user, day:state.day, ticket:0, busy:false, dirty:false};
    render(s, summary);
  }
  return {mount, dispose, canLeave, difference};
})();
