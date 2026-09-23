"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
const icon = (name) => `<i data-lucide="${name}"></i>`;
const icons = () => window.lucide?.createIcons();
const localDate = () => window.BusinessTime.day();
const state = {
  user: null,
  view: "today",
  authMode: "login",
  authBusy: false,
  day: localDate(),
  profile: {},
  meals: [],
  workouts: [],
  summary: null,
  revision: 0,
  editing: null,
  deleting: null,
  drafts: [],
  trainingDrafts: [],
  mealTextStatus: "not_configured",
  photoStatus: "not_configured",
  speechStatus: "not_configured",
  nutritionStatus: "not_configured",
  workoutStatus: "not_configured",
  knowledgeQaStatus: "not_configured",
  mealPlanStatus: "not_configured",
  draftEditor: null,
};
const titles = {
  today: "今日总览",
  meals: "饮食记录",
  workouts: "训练记录",
  profile: "个人档案",
  assistant: "AI 助手",
  body: "体测回顾",
};
const meals = {
  breakfast: "早餐",
  lunch: "午餐",
  dinner: "晚餐",
  snack: "加餐",
};
let toastTimer;

function notify(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = error ? "error" : "";
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 4200);
}

function apiError(detail, status) {
  const message = typeof detail === "string" ? detail : detail?.message || "请求失败，请稍后重试";
  const machineCode = typeof detail?.code === "string" && /^[A-Z][A-Z0-9_]{1,63}$/.test(detail.code) ? detail.code : null;
  let code = machineCode;
  // Older saved assistant failures contain a message without its machine code.
  if (!code || code === "MEAL_REQUEST_FAILED") code = [
    ["MODEL_AUTH_ERROR", /^千问鉴权失败，/],
    ["MODEL_BILLING_ERROR", /^千问账户额度或计费状态限制了调用，/],
    ["MODEL_RATE_LIMITED", /^千问调用受限，/],
    ["MODEL_CONFIGURATION_ERROR", /^(千问型号或接口不可用，|千问接口地址格式不正确|请检查千问型号与百炼官方|当前文字解析仅支持千问，)/],
    ["MODEL_NOT_CONFIGURED", /^请在本机配置千问 API Key/],
    ["SPEECH_NOT_CONFIGURED", /^语音转写尚未配置，/],
    ["MODEL_TIMEOUT", /^千问响应超时，/],
    ["MODEL_UNAVAILABLE", /^暂时无法连接千问，/],
    ["MODEL_INVALID_OUTPUT", /^千问返回内容不完整或格式错误，/],
    ["MODEL_CONTENT_REJECTED", /^千问未接受本次内容，/],
    ["MODEL_UPSTREAM_ERROR", /^千问(服务暂时异常，|未接受本次请求参数或内容格式；|返回异常响应，)/],
  ].find(([, pattern]) => pattern.test(message))?.[0] || null;
  const messages = {
    MODEL_NOT_CONFIGURED: "AI 服务暂不可用，可继续手动记录；需要帮助请联系维护者。",
    SPEECH_NOT_CONFIGURED: "语音转写暂不可用，可直接输入文字；需要帮助请联系维护者。",
    MODEL_CONFIGURATION_ERROR: "AI 服务设置异常，请联系维护者。",
    MODEL_AUTH_ERROR: "AI 服务暂不可用，请联系维护者。",
    MODEL_BILLING_ERROR: "AI 服务额度暂不可用，请联系维护者。",
    MODEL_RATE_LIMITED: "AI 服务繁忙或使用次数受限，请稍后重试。",
    MODEL_TIMEOUT: "AI 响应超时，请稍后重试。",
    MODEL_UNAVAILABLE: "暂时无法连接 AI 服务，请稍后重试。",
    MODEL_INVALID_OUTPUT: "AI 返回内容不完整或格式不符合要求，请重试或手动填写。",
    MODEL_CONTENT_REJECTED: "AI 服务未接受本次内容，请核对描述后重试。",
    MODEL_UPSTREAM_ERROR: message.includes("请求参数或内容格式")
      ? "AI 服务未能处理本次请求格式，请稍后重试；持续出现时请联系维护者。"
      : "AI 服务暂时异常，请稍后重试；持续出现时请联系维护者。",
  };
  const incident = message.match(/故障编号\s+([a-f0-9]{12})(?=[，,）)\s]|$)/i)?.[1] || null;
  const mapped = messages[code];
  const error = new Error(mapped ? `${mapped}${incident ? `（故障编号 ${incident}）` : ""}` : message);
  error.status = status;
  error.code = machineCode || code;
  error.incident = incident;
  return error;
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", ...window.GuestSession?.headers(), ...options.headers },
  });
  const result = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/auth/")) showAuth();
    throw apiError(result?.detail, response.status);
  }
  return result;
}

function showAuth() {
  window.GuestSession?.clear();
  window.UsageView?.dispose();
  window.BodyMeasurements?.dispose();
  window.NutritionTargets?.dispose();
  window.TrainingWeek?.dispose();
  window.MealPhotos?.dispose();
  window.IntakeTargets?.dispose();
  window.CoachView?.dispose();
  window.KnowledgeView?.dispose();
  window.WorkoutEditor?.dispose();
  window.ManualNutrition?.dispose();
  window.FitnessSpeech?.dispose();
  state.user = null;
  state.profile = {};
  state.meals = [];
  state.workouts = [];
  state.summary = null;
  state.editing = null;
  state.deleting = null;
  state.drafts = [];
  state.trainingDrafts = [];
  state.draftEditor = null;
  state.revision++;
  $("#workspace").hidden = true;
  $("#auth-screen").hidden = false;
  $("#content").replaceChildren();
  $("#record-dialog").close();
  $("#confirm-dialog").close();
  $("#dialog-content").replaceChildren();
  $("#confirm-name").textContent = "";
  $("#account-name").textContent = "";
  icons();
}

async function enterWorkspace(user) {
  if (!user.is_guest) window.GuestSession?.clear();
  state.user = user;
  state.view = "today";
  $("#auth-screen").hidden = true;
  $("#workspace").hidden = false;
  $("#account-name").textContent = user.username;
  $("#guest-banner").hidden = !user.is_guest;
  const logout = $('[data-action="logout"]');
  logout.title = user.is_guest ? "退出体验" : "退出登录";
  logout.setAttribute("aria-label", logout.title);
  await refresh();
}

async function refresh() {
  window.BodyMeasurements?.dispose();
  window.NutritionTargets?.dispose();
  window.TrainingWeek?.dispose();
  window.IntakeTargets?.dispose();
  window.CoachView?.dispose();
  window.KnowledgeView?.dispose();
  const revision = ++state.revision;
  const day = state.day;
  $("#page-error").hidden = true;
  $("#content").innerHTML = '<p class="loading">正在读取记录…</p>';
  updateHeading();
  try {
    const [profile, mealRows, workoutRows, summary, drafts, health, trainingDrafts] =
      await Promise.all([
        api("/profile"),
        api(`/meals?day=${day}`),
        api(`/workouts?day=${day}`),
        api(`/summary?day=${day}`),
        api("/meal-drafts"),
        api("/health"),
        api("/training-plans/executions"),
      ]);
    if (revision !== state.revision || !state.user) return;
    Object.assign(state, {
      profile,
      meals: mealRows,
      workouts: workoutRows,
      summary,
      drafts,
      trainingDrafts,
      mealTextStatus: health.meal_text,
      photoStatus: health.photo,
      speechStatus: health.speech,
      nutritionStatus: health.nutrition,
      workoutStatus: health.workout,
      knowledgeQaStatus: health.knowledge_qa,
      mealPlanStatus: health.meal_plan,
      trainingPlanStatus: health.training_plan,
      coachStatus: health.coach_understanding,
    });
    $("#account-name").textContent =
      profile.display_name || state.user.username;
    render();
  } catch (error) {
    if (revision !== state.revision || !state.user) return;
    $("#page-error").textContent = error.message;
    $("#page-error").hidden = false;
    $("#content").innerHTML = '<button data-action="retry">重新加载</button>';
  }
}

function updateHeading() {
  $("#page-title").textContent = titles[state.view];
  $("#page-context").textContent =
    state.view === "profile"
      ? "身体与目标"
      : state.view === "body" ? "测量与记录"
      : state.view === "assistant"
        ? "个人助手"
        : "每日记录";
  $("#date-control").hidden = ["profile", "assistant"].includes(state.view);
  $("#day").value = state.day;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === state.view);
    if (button.dataset.view === state.view)
      button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

function actionButton(action, kind, row, symbol, title) {
  return `<button class="icon-button" data-action="${action}" data-kind="${kind}" data-id="${row.id}" title="${title}" aria-label="${title}">${icon(symbol)}</button>`;
}

function emptyState(symbol, text) {
  return `<div class="empty">${icon(symbol)}<p>${text}</p></div>`;
}

function mealList() {
  if (!state.meals.length)
    return emptyState("utensils", "这一天还没有饮食记录");
  return state.meals
    .map((row) => {
      const kcal =
        row.kcal_per_100g === null || row.grams == null
          ? row.nutrition_estimate ? `估算 ${nutritionRange(row.nutrition_estimate.kcal)} kcal` : "热量未知"
          : `${Number(((row.kcal_per_100g * row.grams) / 100).toFixed(1))} kcal`;
      return `<div class="record"><div class="record-marker">${icon("utensils")}</div>
      <div class="record-main"><div class="record-name">${escapeHtml(row.name)}<span class="pill">${meals[row.meal_type]}</span></div>
      <div class="record-meta">${row.grams == null ? escapeHtml(row.amount_description) : `${row.grams} g`}${row.source ? ` · 来源：${escapeHtml(row.source)}` : ""}${row.notes ? `<br>${escapeHtml(row.notes)}` : ""}</div>${row.nutrition_estimate ? `<details class="nutrition-details"><summary>营养估算依据</summary>${nutritionDetails(row.nutrition_estimate)}</details>` : ""}</div>
      <div class="record-value">${kcal}</div><div class="record-actions">${actionButton("edit", "meals", row, "pencil", "编辑饮食")}${actionButton("delete", "meals", row, "trash-2", "删除饮食")}</div></div>`;
    })
    .join("");
}

function workoutList() {
  if (!state.workouts.length)
    return emptyState("dumbbell", "这一天还没有训练记录");
  return state.workouts
    .map(
      (
        row,
      ) => `<div class="record"><div class="record-marker training">${icon("dumbbell")}</div>
    <div class="record-main"><div class="record-name">${escapeHtml(row.name)}<span class="pill ${row.status}">${row.status === "completed" ? "已完成" : "计划中"}</span></div><div class="record-meta">${row.minutes} 分钟${row.details ? ` · ${escapeHtml(row.details)}` : ""}${row.notes ? ` · ${escapeHtml(row.notes)}` : ""}</div>${row.calorie_estimate ? `<details class="nutrition-details"><summary>消耗估算依据</summary>${window.WorkoutEditor.calorieDetails(row.calorie_estimate)}</details>` : ""}</div>
    <div class="record-value">${row.calorie_estimate ? `${row.status === "completed" ? "估算" : "预计"} ${nutritionRange(row.calorie_estimate.kcal)} kcal` : "消耗未知"}</div>
    <div class="record-actions">${actionButton("toggle-workout", "workouts", row, row.status === "completed" ? "circle-check" : "circle", row.status === "completed" ? "改为计划中" : "标记完成")}${actionButton("edit", "workouts", row, "pencil", "编辑训练")}${actionButton("delete", "workouts", row, "trash-2", "删除训练")}</div></div>`,
    )
    .join("");
}

function section(kind) {
  const isMeal = kind === "meals";
  return `<section class="section record-section"><div class="section-heading"><h2>${icon(isMeal ? "utensils" : "dumbbell")}${isMeal ? "饮食" : "训练"}<span class="count">${state[kind].length} 条</span></h2><div class="section-commands"><button class="${state.view === "today" ? "" : "primary"}" data-action="add" data-kind="${kind}">${icon("plus")}${isMeal ? "记录饮食" : "记录训练"}</button></div></div>${isMeal ? mealList() : workoutList()}</section>`;
}

function metrics() {
  const s = state.summary;
  const nutrient = (name) =>
    s.nutrition[name].known_total === null
      ? "—"
      : Number(s.nutrition[name].known_total.toFixed(1));
  const note = (name) =>
    s.nutrition[name].missing_count
      ? `${s.nutrition[name].missing_count} 条记录缺少数值`
      : s.meal_count
        ? "按已记录份量合计"
        : "暂无记录";
  const nutritionMetric = (name, label, symbol, unit) => {
    const estimated = s.estimated_nutrition?.[name];
    const hasEstimate = estimated?.count > 0;
    const onlyEstimated = hasEstimate && s.nutrition[name].known_total === null;
    const range = hasEstimate ? nutritionRange({lower: estimated.lower_total, upper: estimated.upper_total}) : "";
    const details = hasEstimate
      ? `${onlyEstimated ? `${estimated.count}条模型估算` : `另有估算 ${range} ${unit}`} · ${estimated.unknown_count ? `${estimated.unknown_count}条营养未知` : "无待补全项"}`
      : note(name);
    return `<div class="metric"><div class="metric-label">${icon(symbol)}${onlyEstimated ? "估算" : "已知"}${label}</div><div class="metric-value ${onlyEstimated ? "range-value" : ""}">${onlyEstimated ? range : nutrient(name)}<small>${unit}</small></div><p class="metric-note">${details}</p></div>`;
  };
  return `<div class="metrics">
    ${nutritionMetric("kcal", "摄入", "flame", "kcal")}
    ${nutritionMetric("protein", "蛋白质", "egg", "g")}
    <div class="metric"><div class="metric-label">${icon("timer")}完成训练</div><div class="metric-value">${s.completed_minutes}<small>分钟</small></div><p class="metric-note">${s.completed_count} / ${s.workout_count} 项已完成</p></div>
    <div class="metric"><div class="metric-label">${icon("activity")}训练估算消耗</div><div class="metric-value range-value">${s.estimated_workout_calories?.count ? nutritionRange({lower:s.estimated_workout_calories.lower_total,upper:s.estimated_workout_calories.upper_total}) : "—"}<small>kcal</small></div><p class="metric-note">仅已完成 · 含静息部分 · ${s.estimated_workout_calories?.unknown_count || 0}项未知</p></div>
  </div>`;
}

function nutritionRange(value) {
  return `${Number(value.lower.toFixed(1))} ~ ${Number(value.upper.toFixed(1))}`;
}

function nutritionDetails(estimate) {
  const values = [["kcal", "热量", "kcal"], ["protein", "蛋白质", "g"], ["carbs", "碳水", "g"], ["fat", "脂肪", "g"]];
  return `<div class="nutrition-values">${values.map(([key, label, unit]) => `<div><span>${label}</span><strong>${nutritionRange(estimate[key])} ${unit}</strong></div>`).join("")}</div>
    <ul class="nutrition-assumptions">${estimate.assumptions.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul>
    ${estimate.model ? `<p class="muted small">AI 估算（阿里云千问） · ${escapeHtml(estimate.generated_at.slice(0, 10))}</p>` : ""}`;
}

function options(values, selected) {
  return Object.entries(values)
    .map(
      ([value, label]) =>
        `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`,
    )
    .join("");
}

function field(label, name, value, attributes = "") {
  return `<label>${label}<input name="${name}" value="${escapeHtml(value)}" ${attributes}></label>`;
}

function profileForm() {
  const p = state.profile;
  return `<form id="profile-form" class="profile-form">
    <section class="profile-section"><h2>个人信息</h2><div class="form-grid">
      ${field("称呼", "display_name", p.display_name, 'maxlength="40"')}
      <label>健身目标<select name="goal">${options({ fat_loss: "减脂", muscle_gain: "增肌", maintain: "维持" }, p.goal)}</select></label>
      ${field("身高（cm，可选）", "height_cm", p.height_cm, 'type="number" min="50" max="250" step="0.1"')}
      ${field("体重（kg，可选）", "weight_kg", p.weight_kg, 'type="number" min="20" max="400" step="0.1"')}
      ${field("体脂率（%，可选）", "body_fat_percent", p.body_fat_percent, 'type="number" min="1" max="75" step="0.1"')}
      ${field("年龄（周岁）", "age", p.age, 'type="number" min="19" max="100" step="1"')}
      <label>生理性别（用于热量计算）<select name="equation_sex">${options({"":"未填写",male:"男",female:"女"},p.equation_sex || "")}</select></label>
      <label>训练经验<select name="experience">${options({ beginner: "刚开始健身", experienced: "已有训练习惯" }, p.experience)}</select></label>
      <label>平时整体活动水平<select name="activity">${options({"":"未填写",inactive:"日常活动为主",low_active:"较活跃",active:"活跃",very_active:"非常活跃"},p.activity || "")}</select></label>
      <label class="plan-check"><input type="checkbox" name="regular_training" ${p.nutrition_reference === 'regular_training' ? 'checked' : ''}>目前规律训练（采用运动营养参考）</label>
      <details class="small energy-scope span-2"><summary>活动分类与营养参考</summary><p>整体活动包含工作、通勤和运动，用于估算能量；是否规律训练用于选择蛋白质等营养参考，两者不等同。</p><p>官方活动举例：“日常活动为主”包含约30分钟步行及90分钟轻中度家务；“较活跃”额外约60–80分钟快走；“活跃”包含更多步行、骑行和球类；“非常活跃”则更多。不按健身经验或可训练天数自动猜测。</p><p>当前公式支持19–100岁、140–210cm、40–200kg、BMI 18.5至小于40；这是入口范围，不是健康评估。活动已包含在测算中，不额外加回训练消耗。</p></details>
    </div></section>
    <section class="profile-section"><h2>训练条件与偏好</h2><div class="form-grid">
      ${field("每次可用时间（分钟）", "minutes_per_session", p.minutes_per_session, 'type="number" min="5" max="300" required')}
      <label>训练分化<select name="training_split">${options({ppl:"三分化",four:"四分化",five:"五分化"},p.training_split || "ppl")}</select></label>
      <label class="span-2">器械与场地<input name="equipment" value="${escapeHtml(p.equipment)}" maxlength="300" placeholder="例如：哑铃、平板训练凳；龙门架、双侧可调滑轮、双把手"></label>
      <div class="span-2"><label for="profile-preferences">口味与就餐习惯</label><textarea id="profile-preferences" name="preferences" maxlength="1000" placeholder="例如：口味偏甜；在外饮食">${escapeHtml(p.preferences)}</textarea></div>
      <div class="span-2"><label for="profile-allergies">食物过敏与禁忌</label><textarea id="profile-allergies" name="food_allergies" maxlength="1000" placeholder="例如：西兰花过敏；需避免的食材">${escapeHtml(p.food_allergies)}</textarea></div>
    </div></section>
    <p class="small muted">仅适用于无伤病的一般成人健身，不提供伤病或康复训练方案。</p>
    <p class="form-error" role="alert"></p><div class="form-actions"><button type="submit" class="primary">${icon("save")}保存档案</button></div>
  </form>`;
}

function render() {
  window.BodyMeasurements?.dispose();
  window.NutritionTargets?.dispose();
  window.TrainingWeek?.dispose();
  window.IntakeTargets?.dispose();
  window.CoachView?.dispose();
  window.KnowledgeView?.dispose();
  updateHeading();
  const content = $("#content");
  if (state.view === "today") {
    content.innerHTML =
      metrics() + '<section class="section intake-targets"></section>' + draftList() + window.WorkoutEditor.draftList() + section("meals") + section("workouts");
    window.NutritionTargets.mount(content.querySelector('.intake-targets'), 'day', state.summary);
  }
  if (state.view === "meals") {
    content.innerHTML = '<section class="section meal-week" aria-label="饮食周总览"></section>' + draftList() + section("meals");
    window.TrainingWeek.mount(content.querySelector('.meal-week'), 'meals');
  }
  if (state.view === "workouts") {
    content.innerHTML = '<section class="section training-week" aria-label="训练周总览"></section>' + window.WorkoutEditor.draftList() + section("workouts");
    window.TrainingWeek.mount(content.querySelector('.training-week'));
  }
  if (state.view === "profile") {
    content.innerHTML = profileForm() + '<section class="profile-section nutrition-standard"></section>';
    window.NutritionTargets.mount(content.querySelector('.nutrition-standard'), 'standard');
  }
  if (state.view === "assistant") window.CoachView.mount(content);
  if (state.view === "body") window.BodyMeasurements.mount(content);
  icons();
}

document.addEventListener("input", event => {
  const form = event.target.closest('#profile-form');
  if (form) form.dataset.dirty = 'true';
});

function mealItemFields(value = {}, removable = true) {
  return `<section class="meal-item" role="group" aria-label="食物" data-client-id="${crypto.randomUUID()}">
    <div class="meal-item-heading"><h3>食物</h3>${removable ? `<button type="button" class="icon-button" data-action="remove-meal-item" title="移除此食物" aria-label="移除此食物">${icon("x")}</button>` : ""}</div>
    <div class="meal-item-fields">
      <label>食物名称<input name="name" value="${escapeHtml(value.name)}" maxlength="120" required placeholder="例如：鸡蛋"></label>
      ${field("食用份量（g）", "grams", value.grams, 'type="number" min="0.1" max="10000" step="0.1"')}
    </div>
    <label>份量描述<input name="amount_description" maxlength="120" value="${escapeHtml(value.amount_description)}" placeholder="例如：2个、半碗、一盘（单人份）"></label>
    ${window.ManualNutrition.markup(value)}
    <details><summary>营养信息（每 100 g，选填）</summary><div class="form-grid">
      ${field("热量（kcal）", "kcal_per_100g", value.kcal_per_100g, 'type="number" min="0" max="1000" step="0.1"')}
      ${field("蛋白质（g）", "protein_per_100g", value.protein_per_100g, 'type="number" min="0" max="100" step="0.1"')}
      ${field("碳水化合物（g）", "carbs_per_100g", value.carbs_per_100g, 'type="number" min="0" max="100" step="0.1"')}
      ${field("脂肪（g）", "fat_per_100g", value.fat_per_100g, 'type="number" min="0" max="100" step="0.1"')}
      <label class="span-2">数值来源<input name="source" maxlength="300" value="${escapeHtml(value.source)}" placeholder="例如：包装标签；自行估算"></label>
    </div></details>
  </section>`;
}

function updateMealItems() {
  const items = [...document.querySelectorAll(".meal-item")];
  items.forEach((item, index) => {
    window.ManualNutrition?.mountItem(item);
    const title = `食物 ${index + 1}`;
    $("h3", item).textContent = title;
    item.setAttribute("aria-label", title);
    const remove = $('[data-action="remove-meal-item"]', item);
    if (remove) remove.disabled = items.length === 1;
  });
  const add = $('[data-action="add-meal-item"]');
  if (add) add.disabled = items.length >= 30;
  window.ManualNutrition?.updateControls();
}

function readMealItem(element, shared, includeClientId) {
  const data = { ...shared };
  for (const name of ["name", "source", "amount_description"])
    data[name] = $(`[name="${name}"]`, element).value;
  const nutrients = [
    "kcal_per_100g",
    "protein_per_100g",
    "carbs_per_100g",
    "fat_per_100g",
  ];
  for (const name of ["grams", ...nutrients]) {
    const value = $(`[name="${name}"]`, element).value;
    data[name] = value === "" ? null : Number(value);
  }
  const issue = portionIssue(data);
  if (issue) throw new Error(issue);
  if (nutrients.some((name) => data[name] !== null) && !data.source.trim()) {
    $("details", element).open = true;
    $('[name="source"]', element).focus();
    throw new Error("填写营养数值时，请注明来源");
  }
  if (includeClientId) data.client_id = element.dataset.clientId;
  return {...data, ...window.ManualNutrition.payload(element)};
}

function openRecord(kind, row = null) {
  if (kind === "workouts") return window.WorkoutEditor.open(row);
  if (kind !== "meals") return;
  if (!canLeaveDraft()) return;
  window.MealPhotos?.dispose();
  window.WorkoutEditor?.dispose();
  window.FitnessSpeech?.dispose();
  window.ManualNutrition?.dispose();
  state.draftEditor = null;
  state.editing = { kind, row };
  const value = row || {
    day: state.day,
    meal_type: "breakfast",
    grams: "",
  };
  $("#dialog-title").textContent =
    `${row ? "编辑" : "添加"}饮食记录`;
  const mealFields = `<div class="segmented" aria-label="记录方式"><button type="button" class="selected" aria-pressed="true">手动填写</button>${row ? "" : `<button type="button" data-action="text-meal" aria-pressed="false">${icon("text-cursor-input")}文字描述</button><button type="button" data-action="photo-meal" aria-pressed="false">${icon("camera")}照片</button>`}</div>
    <div class="form-grid">${field("日期", "day", value.day, 'type="date" required')}<label>餐次<select name="meal_type">${options(meals, value.meal_type)}</select></label></div>
    <div id="meal-items">${mealItemFields(value, !row)}</div>
    ${row ? "" : `<button type="button" class="add-food" data-action="add-meal-item">${icon("plus")}再加一种食物</button>`}`;
  $("#dialog-content").innerHTML =
    `<form id="record-form">${mealFields}<label>备注<textarea name="notes" maxlength="500">${escapeHtml(value.notes)}</textarea></label>${window.ManualNutrition.toolbar(Boolean(row))}<p class="form-error" role="alert"></p><div class="dialog-actions"><button type="button" data-action="close-dialog">取消</button><button type="submit" class="primary">${icon("save")}保存记录</button></div></form>`;
  icons();
  window.ManualNutrition.mount($("#record-form"), row);
  updateMealItems();
  $("#record-dialog").showModal();
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button, a[data-view]");
  if (!button || button.disabled) return;
  try {
    if (button.dataset.auth) {
      if (state.authBusy) return;
      state.authMode = button.dataset.auth;
      document
        .querySelectorAll("[data-auth]")
        .forEach((item) => item.classList.toggle("selected", item === button));
      $("#auth-title").textContent =
        state.authMode === "login" ? "登录你的账户" : "创建你的账户";
      $("#auth-submit").textContent =
        state.authMode === "login" ? "登录" : "注册并开始";
      $('#auth-form [name="password"]').autocomplete =
        state.authMode === "login" ? "current-password" : "new-password";
      $("#auth-error").textContent = "";
    }
    if (button.dataset.view && state.user) {
      event.preventDefault();
      if (window.BodyMeasurements && !window.BodyMeasurements.canLeave()) return;
      if (!window.IntakeTargets?.canLeave()) return;
      if (!window.NutritionTargets?.canLeave()) return;
      if (window.MealPlanActions && !window.MealPlanActions.canLeave()) return;
      state.view = button.dataset.view;
      if (state.summary && state.view !== "today") render();
      else await refresh();
    }
    const action = button.dataset.action;
    if (action === "guest-start") {
      if (state.authBusy) return;
      setAuthBusy(true);
      $("#auth-error").textContent = "";
      try {
        window.GuestSession.start();
        const user = await api("/auth/guest", { method: "POST", body: "{}" });
        $("#auth-form").reset();
        await enterWorkspace(user);
      } catch (error) {
        window.GuestSession.clear();
        $("#auth-error").textContent = error.message;
      } finally {
        setAuthBusy(false);
      }
    }
    const kind = button.dataset.kind;
    const row = kind
      ? state[kind].find((item) => item.id === Number(button.dataset.id))
      : null;
    if (action === "logout") {
      if (window.BodyMeasurements && !window.BodyMeasurements.canLeave()) return;
      if (window.MealPlanActions && !window.MealPlanActions.canLeave()) return;
      if (state.user?.is_guest && !window.confirm("退出体验将清空临时记录，确定退出？")) return;
      await api("/auth/logout", { method: "POST", body: "{}" });
      $("#auth-form").reset();
      showAuth();
    }
    if (action === "retry") await refresh();
    if (action === "add") await openRecord(kind);
    if (action === "edit" && row) await openRecord(kind, row);
    if (action === "close-dialog") closeRecordDialog();
    if (action === "delete" && row) {
      state.deleting = { kind, row };
      $("#confirm-name").textContent = row.name;
      $("#confirm-dialog").showModal();
    }
    if (action === "cancel-delete") $("#confirm-dialog").close();
    if (action === "toggle-workout" && row) {
      button.disabled = true;
      const { id, calorie_estimate, ...payload } = row;
      payload.status = row.status === "completed" ? "planned" : "completed";
      await api(`/workouts/${id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      await refresh();
    }
    if (action === "add-meal-item" || action === "remove-meal-item") {
      if ($('#record-form [type="submit"]').disabled) return;
      const items = $("#meal-items");
      if (action === "add-meal-item" && items.children.length < 30) {
        items.insertAdjacentHTML("beforeend", mealItemFields());
        $('[name="name"]', items.lastElementChild).focus();
      } else if (action === "remove-meal-item" && items.children.length > 1) {
        button.closest(".meal-item").remove();
      }
      updateMealItems();
      icons();
    }
  } catch (error) {
    notify(error.message, true);
  } finally {
    if (button.isConnected && button.dataset.action === "toggle-workout")
      button.disabled = false;
  }
});

$("#day").addEventListener("change", () => {
  if (!$("#day").value || (window.BodyMeasurements && !window.BodyMeasurements.canLeave()) || !window.IntakeTargets?.canLeave() || !window.NutritionTargets?.canLeave() || (window.MealPlanActions && !window.MealPlanActions.canLeave())) {
    $("#day").value = state.day;
    return;
  }
  state.day = $("#day").value;
  refresh();
});

$("#confirm-delete").addEventListener("click", async () => {
  const button = $("#confirm-delete");
  if (!state.deleting || button.disabled) return;
  button.disabled = true;
  try {
    await api(`/${state.deleting.kind}/${state.deleting.row.id}`, {
      method: "DELETE",
    });
    $("#confirm-dialog").close();
    state.deleting = null;
    notify("记录已删除");
    await refresh();
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!["auth-form", "profile-form", "record-form"].includes(form.id)) return;
  event.preventDefault();
  const button = $('[type="submit"]', form);
  if (button.disabled) return;
  if (form.id === "auth-form" && state.authBusy) return;
  const errorElement = $(".form-error", form);
  errorElement.textContent = "";
  const data = Object.fromEntries(new FormData(form));
  button.disabled = true;
  if (form.id === "auth-form") setAuthBusy(true);
  try {
    if (form.id === "auth-form") {
      const user = await api(`/auth/${state.authMode}`, {
        method: "POST",
        body: JSON.stringify(data),
      });
      form.reset();
      await enterWorkspace(user);
    }
    if (form.id === "profile-form") {
      if (!window.NutritionTargets?.canLeave()) return;
      for (const name of [
        "height_cm",
        "weight_kg",
        "body_fat_percent",
        "minutes_per_session",
        "age",
      ])
        data[name] = data[name] === "" ? null : Number(data[name]);
      data.equation_sex = data.equation_sex || null;
      data.activity = data.activity || null;
      data.nutrition_reference = data.regular_training ? 'regular_training' : 'general';
      delete data.regular_training;
      state.profile = await api("/profile", {
        method: "PUT",
        body: JSON.stringify(data),
      });
      form.dataset.dirty = 'false';
      for (const name of ['weight_kg', 'body_fat_percent']) form.elements[name].value = state.profile[name] ?? '';
      $("#account-name").textContent =
        state.profile.display_name || state.user.username;
      notify("档案已保存");
      window.NutritionTargets?.profileSaved();
    }
    if (form.id === "record-form") {
      const { row } = state.editing;
      if (!window.ManualNutrition.allowSave(form)) return;
      const shared = {
        day: data.day,
        meal_type: data.meal_type,
        notes: data.notes,
      };
      const items = [...form.querySelectorAll(".meal-item")].map((element) =>
        readMealItem(element, shared, !row),
      );
      await api(row ? `/meals/${row.id}` : "/meals/batch", {
        method: row ? "PUT" : "POST",
        body: JSON.stringify(row ? items[0] : { items }),
      });
      const savedCount = items.length;
      $("#record-dialog").close();
      notify(savedCount > 1 ? `已保存 ${savedCount} 种食物` : "记录已保存");
      await refresh();
    }
  } catch (error) {
    errorElement.textContent = error.message;
  } finally {
    if (form.id === "auth-form") setAuthBusy(false);
    if (button.isConnected) button.disabled = false;
  }
});

function setAuthBusy(busy) {
  state.authBusy = busy;
  document.querySelectorAll('#auth-screen button').forEach(button => { button.disabled = busy; });
}

async function initialize() {
  icons();
  $("#day").value = state.day;
  try {
    const health = await api("/health");
    $('[data-auth="register"]').hidden = health.registration_enabled === false;
    $('[data-action="guest-start"]').hidden = health.guest_enabled !== true;
    $('#guest-notice').hidden = health.guest_enabled !== true;
  } catch (_) {
    // Authentication below retains the existing unavailable-service state.
  }
  try {
    await enterWorkspace(await api("/auth/me"));
  } catch (error) {
    showAuth();
    if (error.status !== 401)
      $("#auth-error").textContent = "服务暂不可用，请稍后刷新";
  }
}

initialize();
