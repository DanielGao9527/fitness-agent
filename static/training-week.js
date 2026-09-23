"use strict";

window.TrainingWeek = (() => {
  let active = null;
  const weekdays = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

  function shiftDay(day, amount) {
    const value = new Date(`${day}T12:00:00Z`);
    value.setUTCDate(value.getUTCDate() + amount);
    if (value.getUTCFullYear() < 1 || value.getUTCFullYear() > 9999) return day;
    return value.toISOString().slice(0, 10);
  }

  function dispose() {
    active?.controller?.abort();
    active = null;
  }

  function renderWeek(s, data) {
    if (s.kind === 'meals') return renderMeals(s, data);
    const totals = data.totals, energy = totals.estimated_workout_calories;
    const energyText = energy.count
      ? `已记录消耗 ${nutritionRange({lower:energy.lower_total, upper:energy.upper_total})} kcal${energy.unknown_count ? ` · 另有${energy.unknown_count}项未知` : ""}`
      : energy.unknown_count ? `${energy.unknown_count}项已完成训练消耗未知` : "暂无已完成消耗记录";
    s.root.innerHTML = `<div class="section-heading training-week-heading">
      <div><h2>${icon("calendar-days")}本周训练</h2><p class="muted small week-range">${escapeHtml(data.start)} 至 ${escapeHtml(data.end)}</p></div>
      <div class="week-nav"><button class="icon-button" data-week-shift="-7" title="上一周" aria-label="上一周" ${shiftDay(s.day,-7) === s.day ? "disabled" : ""}>${icon("chevron-left")}</button>
      <button class="icon-button" data-week-today title="回到今天" aria-label="回到今天">${icon("calendar-check")}</button>
      <button class="icon-button" data-week-shift="7" title="下一周" aria-label="下一周" ${shiftDay(s.day,7) === s.day ? "disabled" : ""}>${icon("chevron-right")}</button></div>
      </div><div class="training-week-grid" role="group" aria-label="训练日期">${data.days.map(row => {
        const label = `${row.day}，${row.workout_count ? `已完成${row.completed_count}项共${row.completed_minutes}分钟，待完成${row.planned_count}项共${row.planned_minutes}分钟` : "暂无记录"}`;
        const shortCount = count => count > 9 ? "9+" : `${count}项`;
        return `<button class="week-day" data-training-day="${escapeHtml(row.day)}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}" aria-pressed="${row.day === s.day}" ${row.day === localDate() ? 'aria-current="date"' : ""}>
          <span class="week-weekday">${weekdays[new Date(`${row.day}T12:00:00Z`).getUTCDay()]}</span><strong>${Number(row.day.slice(8))}</strong>
          <span class="week-day-state week-completed">${row.completed_count ? `<span class="week-day-volume">已练 ${row.completed_minutes} 分钟</span><span class="week-day-count">已${shortCount(row.completed_count)}</span>` : ""}</span>
          <span class="week-day-state week-planned">${row.planned_count ? `<span class="week-day-volume">待练 ${row.planned_minutes} 分钟</span><span class="week-day-count">待${shortCount(row.planned_count)}</span>` : !row.workout_count ? '<span class="week-empty"><span class="week-day-volume">无记录</span><span class="week-day-count">暂无</span></span>' : ""}</span></button>`;
      }).join("")}</div>
      <dl class="training-week-totals"><div><dt>已记录完成</dt><dd>${totals.completed_count} 项 · ${totals.completed_minutes} 分钟</dd></div>
      <div><dt>待完成安排</dt><dd>${totals.planned_count} 项 · ${totals.planned_minutes} 分钟</dd></div>
      <div><dt>有完成记录</dt><dd>${totals.completed_days} 天</dd></div></dl>
      <p class="muted small week-energy">${energyText}${energy.count ? " · 含静息部分" : ""}</p>`;
    icons();
  }

  function mealEnergy(row) {
    const known = row.nutrition.kcal.known_total, estimate = row.estimated_nutrition.kcal;
    if (known === null && !estimate.count) return row.meal_count ? '热量未知' : '无记录';
    const lower = (known || 0) + (estimate.lower_total || 0), upper = (known || 0) + (estimate.upper_total || 0);
    return `${nutritionRange({lower,upper})} kcal${estimate.count ? '（含估算）' : ''}${estimate.unknown_count ? ` · ${estimate.unknown_count}项未知` : ''}`;
  }

  function renderMeals(s, data) {
    const totals = data.totals;
    s.root.innerHTML = `<div class="section-heading training-week-heading"><div><h2>${icon('calendar-days')}本周饮食</h2><p class="muted small week-range">${escapeHtml(data.start)} 至 ${escapeHtml(data.end)}</p></div>
      <div class="week-nav"><button class="icon-button" data-week-shift="-7" title="上一周" aria-label="上一周" ${shiftDay(s.day,-7)===s.day?'disabled':''}>${icon('chevron-left')}</button><button class="icon-button" data-week-today title="回到今天" aria-label="回到今天">${icon('calendar-check')}</button><button class="icon-button" data-week-shift="7" title="下一周" aria-label="下一周" ${shiftDay(s.day,7)===s.day?'disabled':''}>${icon('chevron-right')}</button></div></div>
      <div class="training-week-grid" role="group" aria-label="饮食日期">${data.days.map(row=>{
        const label=`${row.day}，${row.meal_count}项饮食，${mealEnergy(row)}`;
        return `<button class="week-day" data-meal-day="${escapeHtml(row.day)}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}" aria-pressed="${row.day===s.day}" ${row.day===localDate()?'aria-current="date"':''}><span class="week-weekday">${weekdays[new Date(`${row.day}T12:00:00Z`).getUTCDay()]}</span><strong>${Number(row.day.slice(8))}</strong><span class="week-day-state week-completed">${row.meal_count?`${row.meal_count>9?'9+':row.meal_count}项`:''}</span><span class="week-day-state week-planned">${row.meal_count?`${row.meal_types.length}餐`:'无记录'}</span></button>`;
      }).join('')}</div><dl class="training-week-totals"><div><dt>饮食记录</dt><dd>${totals.meal_count} 项</dd></div><div><dt>有记录</dt><dd>${totals.recorded_days} 天</dd></div><div><dt>热量未知</dt><dd>${totals.estimated_nutrition.kcal.unknown_count} 项</dd></div></dl><p class="muted small week-energy">已记录合计：${mealEnergy(totals)}</p>`;
    icons();
  }

  async function load(s) {
    s.controller?.abort();
    const controller = new AbortController();
    s.controller = controller;
    const current = () => active === s && s.controller === controller && state.user === s.user && state.day === s.day && s.root.isConnected;
    s.root.innerHTML = `<p class="loading" role="status">正在读取本周${s.kind==='meals'?'饮食':'训练'}…</p>`;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const data = await api(`/${s.kind}/week?day=${s.day}`, {signal:controller.signal});
      if (current()) renderWeek(s, data);
    } catch (error) {
      if (!current()) return;
      s.root.innerHTML = `<p class="form-error" role="alert">${escapeHtml(error.name === "AbortError" ? "周总览读取超时，当天记录仍可使用。" : error.message)}</p><button data-week-retry>${icon("refresh-cw")}重新加载周总览</button>`;
      icons();
    } finally {
      clearTimeout(timeout);
    }
  }

  function mount(root, kind='workouts') {
    dispose();
    const s = {root, kind, day:state.day, user:state.user, controller:null};
    active = s;
    root.addEventListener("click", async event => {
      const button = event.target.closest("button");
      if (!button || button.disabled || active !== s || state.user !== s.user) return;
      if (button.hasAttribute("data-week-retry")) return load(s);
      const day = button.dataset.trainingDay || button.dataset.mealDay || (button.hasAttribute("data-week-today") ? localDate() : shiftDay(s.day,Number(button.dataset.weekShift)));
      if (day === state.day || !window.IntakeTargets?.canLeave() || (window.MealPlanActions && !window.MealPlanActions.canLeave())) return;
      state.day = day;
      await refresh();
    });
    load(s);
  }

  return {mount, dispose};
})();
