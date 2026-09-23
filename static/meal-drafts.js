"use strict";

function portionIssue(item) {
  const name = item.name.trim();
  if (!name || ["菜", "炒菜", "一道菜", "一盘菜", "一盘炒菜", "食物", "东西", "饭菜", "不知道", "不清楚", "未知"].includes(name))
    return "请填写具体食材或菜名，例如鸡蛋、麻婆豆腐";
  if (item.grams !== null && item.grams !== undefined)
    return item.grams > 0 && item.grams <= 10000 ? "" : "请检查克数范围";
  const amount = (item.amount_description || "").trim();
  if (!/不知道|不清楚|未知|随便|若干|[-负]\s*\d|(?:^|\D)0+(?:\.0+)?\s*(?:个|盘|份|碗|杯|克|g|ml)/.test(amount) && (
    /(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*|[一二两三四五六七八九十百半]+)\s*[大小中小半]*(?:个|只|枚|片|块|碗|杯|盘|份|人份|勺|袋|盒|瓶|根|串|把|锅|克|毫升|斤|两|g\b|ml\b)/i.test(amount) ||
    /\b(?:one|two|three|half|[1-9]\d*)\s+(?:eggs?|cups?|bowls?|plates?|servings?|pieces?)\b/i.test(amount) ||
    ["单人份", "双人份", "半份", "小份", "中份", "大份"].includes(amount))) return "";
  return "请补充基本份量，例如2个、半碗、一盘或单人份";
}

function draftList() {
  if (!state.drafts.length) return "";
  return `<section class="section draft-list"><div class="section-heading"><h2>${icon("file-pen-line")}待确认草稿<span class="count">${state.drafts.length} 份</span></h2></div>${state.drafts
    .map(
      (draft) => `
    <div class="record"><div class="record-main"><div class="record-name">${escapeHtml(draft.items.map((item) => item.name).join("、") || draft.text)}</div>
    <div class="record-meta">${escapeHtml(draft.day)} · ${meals[draft.meal_type]} · ${draft.status === "ready" ? "待核对" : "待补充"}</div></div>
    <button data-action="resume-draft" data-id="${escapeHtml(draft.id)}">${icon("pencil")}继续</button></div>`,
    )
    .join("")}</section>`;
}

function canLeaveDraft() {
  window.ManualNutrition?.cancel();
  window.FitnessSpeech?.cancel();
  if (window.WorkoutEditor && !window.WorkoutEditor.canLeave()) return false;
  const editor = state.draftEditor;
  if (!editor) return true;
  if (editor.busy) return false;
  return !editor.dirty || window.confirm("有尚未暂存的修改，仍要关闭吗？");
}

function closeRecordDialog() {
  if (canLeaveDraft()) $("#record-dialog").close();
}

$("#record-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeRecordDialog();
});
$("#record-dialog").addEventListener("close", () => {
  if ($("#record-dialog").open) return;
  window.FitnessSpeech?.dispose();
  window.ManualNutrition?.dispose();
  window.MealPhotos?.dispose();
  window.WorkoutEditor?.dispose();
  state.draftEditor = null;
  state.editing = null;
  $("#dialog-content").replaceChildren();
});

function draftItemFields(item = {}) {
  return `<section class="draft-item" role="group" aria-label="草稿食物">
    <div class="meal-item-heading"><h3>食物</h3><button type="button" class="icon-button" data-action="remove-draft-item" title="移除此食物" aria-label="移除此食物">${icon("x")}</button></div>
    <div class="meal-item-fields">${field("食物名称", "name", item.name, 'maxlength="120" required')}${field("食用份量（g）", "grams", item.grams, 'type="number" min="0.1" max="10000" step="0.1"')}</div>
    <label class="amount-label">份量描述<input name="amount_description" maxlength="120" value="${escapeHtml(item.amount_description)}" placeholder="例如：2个、半碗、一盘（单人份）"></label>
  </section>`;
}

function draftNutrition(draft) {
  const nutrition = draft?.nutrition;
  return `<section class="draft-nutrition" aria-label="营养估算">
    <div class="draft-toolbar"><h3>营养估算</h3><button type="button" data-action="estimate-nutrition">${icon("sparkles")}<span>估算全部营养</span></button></div>
    <p class="nutrition-status muted small" role="status"></p>
    <div class="nutrition-results">${nutrition ? `<p class="muted small">AI 营养估算 · 本次食用份量 · 非标签或食物库数值</p>${nutrition.items.map((item) => `<div class="nutrition-result" data-nutrition-index="${item.index}"><h3>${escapeHtml(draft.items[item.index].name)}</h3>${item.status === "estimated" ? nutritionDetails({...item, model: item.model || nutrition.model, generated_at: item.generated_at || nutrition.generated_at}) : `<p class="nutrition-question">营养未知：${escapeHtml(item.question)}</p>`}</div>`).join("")}<button type="button" data-action="clear-nutrition">${icon("x")}移除估算</button>` : ""}</div>
  </section>`;
}

function openTextDraft(draft = null, mode = null) {
  const manual = $("#record-form");
  if (
    manual &&
    [...manual.querySelectorAll('[name="name"], [name="notes"]')].some((el) =>
      el.value.trim(),
    ) &&
    !window.confirm("切换后，尚未保存的手动记录会丢失。继续吗？")
  )
    return;
  if (!canLeaveDraft()) return;
  const day =
    draft?.day || (manual && $('[name="day"]', manual).value) || state.day;
  const mealType =
    draft?.meal_type ||
    (manual && $('[name="meal_type"]', manual).value) ||
    "breakfast";
  state.editing = null;
  state.draftEditor = {
    draft,
    day,
    mealType,
    clientId: crypto.randomUUID(),
    dirty: false,
    busy: false,
    confirmation: null,
    inputMode: mode || (draft?.input_type === 'photo' ? 'photo' : 'text'),
  };
  renderDraft();
  if (!$("#record-dialog").open) $("#record-dialog").showModal();
}

function renderDraft() {
  window.WorkoutEditor?.dispose();
  window.ManualNutrition?.dispose();
  window.FitnessSpeech?.dispose();
  const editor = state.draftEditor;
  const draft = editor.draft;
  const connected = state.mealTextStatus === "configured_unverified";
  const photo = editor.inputMode === 'photo', photoEnabled = state.photoStatus === 'configured_unverified';
  window.MealPhotos?.dispose();
  $("#dialog-title").textContent = draft ? "核对饮食草稿" : photo ? "照片记录饮食" : "文字记录饮食";
  $("#dialog-content").innerHTML = `<form id="draft-form">
    <div class="segmented" aria-label="记录方式"><button type="button" data-action="draft-manual" aria-pressed="false">手动填写</button><button type="button" data-action="draft-text-mode" class="${photo?'':'selected'}" aria-pressed="${!photo}">文字描述</button><button type="button" data-action="draft-photo-mode" class="${photo?'selected':''}" aria-pressed="${photo}">${icon("camera")}照片</button></div>
    <div class="form-grid">${field("日期", "day", draft?.day || editor.day, 'type="date" required')}<label>餐次<select name="meal_type">${options(meals, draft?.meal_type || editor.mealType)}</select></label></div>
    ${photo?'<div id="photo-input"></div>':''}
    <label for="meal-description">${photo?'本餐补充说明（选填）':"本次饮食描述（仅所选餐次）"}</label><textarea id="meal-description" name="text" maxlength="2000" ${photo?'':'required'} placeholder="例如：午餐吃火锅，我吃了半盘羊肉和一盘豆腐">${escapeHtml(draft?.text)}</textarea>
    <div id="speech-input"></div>
    <div class="draft-toolbar"><span class="muted small">${photo?(photoEnabled?'AI 照片识别':'照片识别暂不可用，可手动填写'):connected ? "AI 识别食物与份量" : "文字识别暂不可用，可手动填写"}</span><button type="button" data-action="${photo?'parse-photo':'parse-draft'}" ${(photo?photoEnabled:connected) ? "" : 'disabled title="识别暂不可用，请使用手动填写"'}>${icon("sparkles")}${photo?'识别照片':draft?.items.length ? "重新解析" : "解析食物"}</button></div>
    ${draft?.questions.length ? `<div class="draft-questions"><h3>解析提示</h3><ul>${draft.questions.map((question) => `<li>${escapeHtml(question)}</li>`).join("")}</ul></div>` : ""}
    <div id="draft-items">${(draft?.items || []).map(draftItemFields).join("")}</div>
    <button type="button" class="add-food" data-action="add-draft-item">${icon("plus")}添加食物</button>
    ${draftNutrition(draft)}
    <label>备注<textarea name="notes" maxlength="500">${escapeHtml(draft?.notes)}</textarea></label>
    <p class="draft-state muted small" role="status"></p>
    <label class="draft-review"><input type="checkbox" name="reviewed">${draft?.nutrition ? "我已核对食物、份量和营养估算" : "我已核对食物和份量"}</label>
    <p class="form-error" role="alert"></p>
    <button type="button" data-action="reload-draft" hidden>${icon("refresh-cw")}读取最新草稿</button>
    <div class="draft-actions"><button type="button" class="danger" data-action="cancel-draft">${icon("trash-2")}放弃</button><button type="submit">${icon("save")}暂存草稿</button><button type="button" class="primary" data-action="confirm-draft">${icon("check")}确认入账</button></div>
  </form>`;
  editor.dirty = false;
  if(photo)window.MealPhotos.mount($('#photo-input'),photoEnabled,()=>{editor.dirty=true;editor.photoRequest=null;$('[name="reviewed"]',$('#draft-form')).checked=false;updateDraftControls();});
  updateDraftControls();
  window.FitnessSpeech?.mount($("#speech-input"), $('#draft-form [name="text"]'), state.speechStatus === "configured_unverified", (busy) => {
    editor.busy = busy;
    updateDraftControls();
  });
  icons();
}

function draftValues() {
  const form = $("#draft-form");
  return {
    text: $('[name="text"]', form).value || (state.draftEditor?.inputMode==='photo'?'照片饮食，请核对实际食用份量':''),
    day: $('[name="day"]', form).value,
    meal_type: $('[name="meal_type"]', form).value,
    notes: $('[name="notes"]', form).value,
    items: [...form.querySelectorAll(".draft-item")].map((item) => ({
      name: $('[name="name"]', item).value,
      grams:
        $('[name="grams"]', item).value === ""
          ? null
          : Number($('[name="grams"]', item).value),
      amount_description: $('[name="amount_description"]', item).value,
      confidence: "needs_confirmation",
    })),
  };
}

function updateDraftControls() {
  const editor = state.draftEditor;
  const form = $("#draft-form");
  if (!editor || !form) return;
  const values = draftValues();
  const photoButton=form.querySelector('[data-action="parse-photo"]');
  form.querySelectorAll('[data-action="cancel-draft"],[data-action="reload-draft"],[type="submit"]')
    .forEach(button=>{button.disabled=editor.busy;});
  if(photoButton)photoButton.disabled=editor.busy||state.photoStatus!=='configured_unverified'||!window.MealPhotos.file();
  const items = values.items;
  form.querySelectorAll(".draft-item").forEach((element, index) => {
    $("h3", element).textContent = `食物 ${index + 1}`;
    element.setAttribute("aria-label", `草稿食物 ${index + 1}`);
  });
  const ready =
    items.length > 0 &&
    items.every(
      (item) => !portionIssue(item),
    );
  $('[data-action="confirm-draft"]', form).disabled =
    editor.busy || !ready || !$('[name="reviewed"]', form).checked;
  $('[data-action="add-draft-item"]', form).disabled =
    editor.busy || items.length >= 30;
  const nutrition = editor.draft?.nutrition;
  const current = currentNutritionIndices(editor.draft, values);
  const estimated = (nutrition?.items || []).filter(item=>current.has(item.index) && item.status==='estimated').length;
  const pending = items.length - estimated;
  const enabled = state.nutritionStatus === "configured_unverified";
  const estimateButton = $('[data-action="estimate-nutrition"]', form);
  estimateButton.disabled = editor.busy || !enabled || !ready || items.length > 30 || !pending;
  $("span", estimateButton).textContent = nutrition ? "重新估算营养" : "估算全部营养";
  form.querySelectorAll("[data-nutrition-index]").forEach((element) => {
    element.hidden = !current.has(Number(element.dataset.nutritionIndex));
  });
  $(".nutrition-results", form).hidden = current.size === 0;
  $(".nutrition-status", form).textContent = !enabled ? "营养估算未启用" : !ready ? "食物或基本份量待补充" : pending ? `${pending}项待估算或仍未知${estimated ? ` · ${estimated}项估算已保留` : " · 可跳过"}` : "估算已暂存，待核对";
  $(".draft-state", form).textContent =
    `${editor.dirty || !editor.draft ? "尚未暂存" : "已暂存"} · ${ready ? "待核对" : "食物或基本份量待补充"}`;
}

function currentNutritionIndices(draft, values) {
  const indices = new Set();
  if (!draft?.nutrition || draft.text !== values.text) return indices;
  const identity = (item) => item && JSON.stringify([item.name.trim(), item.grams ?? null, (item.amount_description || "").trim()]);
  draft.nutrition.items.forEach(({index}) => {
    if (values.items[index] && identity(draft.items[index]) === identity(values.items[index])) indices.add(index);
  });
  return indices;
}

function sameDraftValues(draft, values) {
  return (
    draft.text === values.text &&
    draft.day === values.day &&
    draft.meal_type === values.meal_type &&
    draft.notes === values.notes &&
    JSON.stringify(
      draft.items.map(({ name, grams, amount_description }) => ({
        name,
        grams,
        amount_description,
      })),
    ) ===
      JSON.stringify(
        values.items.map(({ name, grams, amount_description }) => ({
          name,
          grams,
          amount_description,
        })),
      )
  );
}

async function persistDraft(editor, values, text) {
  if (!editor.draft) {
    const initial = JSON.stringify({
      text,
      day: values.day,
      meal_type: values.meal_type,
    });
    if (editor.initial && editor.initial !== initial)
      editor.clientId = crypto.randomUUID();
    editor.initial = initial;
    const created = await api("/meal-drafts", {
      method: "POST",
      body: JSON.stringify({
        ...JSON.parse(initial),
        client_id: editor.clientId,
      }),
    });
    if (state.draftEditor !== editor) return null;
    editor.draft = created;
  }
  if (!sameDraftValues(editor.draft, values)) {
    const updated = await api(`/meal-drafts/${editor.draft.id}`, {
      method: "PUT",
      body: JSON.stringify({ ...values, version: editor.draft.version }),
    });
    if (state.draftEditor !== editor) return null;
    editor.draft = updated;
    editor.confirmation = null;
  }
  editor.dirty = false;
  return editor.draft;
}

async function runDraftAction(action) {
  const editor = state.draftEditor;
  const form = $("#draft-form");
  if (!editor || editor.busy || !form) return;
  if (
    !["cancel-draft", "reload-draft"].includes(action) &&
    !form.reportValidity()
  )
    return;
  if (
    action === "confirm-draft" &&
    $('[data-action="confirm-draft"]', form).disabled
  )
    return;
  if (action === "estimate-nutrition" && $('[data-action="estimate-nutrition"]', form).disabled) return;
  if (
    ["parse-draft","parse-photo"].includes(action) &&
    draftValues().items.length &&
    !window.confirm("重新解析会替换当前食物条目，继续吗？")
  )
    return;
  if (
    action === "cancel-draft" &&
    !window.confirm("放弃这份草稿？不会删除已保存的饮食记录。")
  )
    return;
  if (
    action === "reload-draft" &&
    editor.dirty &&
    !window.confirm("读取最新草稿会丢弃尚未暂存的修改，继续吗？")
  )
    return;
  const values = draftValues();
  const text = values.text;
  const photoFile=action==='parse-photo'?window.MealPhotos.file():null;
  if(action==='parse-photo'&&!photoFile)return;
  const disabled = [
    ...form.querySelectorAll("input,select,textarea,button"),
  ].map((element) => [element, element.disabled]);
  editor.busy = true;
  disabled.forEach(([element]) => {
    element.disabled = true;
  });
  $(".form-error", form).textContent = "";
  $(".draft-state", form).textContent =
    ["parse-draft","parse-photo"].includes(action) ? "正在解析…" : action === "estimate-nutrition" ? "正在估算营养…" : "正在保存…";
  try {
    if (action === "cancel-draft") {
      if (editor.draft)
        await api(`/meal-drafts/${editor.draft.id}/cancel`, {
          method: "POST",
          body: JSON.stringify({ version: editor.draft.version }),
        });
    } else if (action === "reload-draft") {
      editor.draft = await api(`/meal-drafts/${editor.draft.id}`);
    } else {
      const draft = await persistDraft(editor, values, text);
      if (!draft || state.draftEditor !== editor) return;
      if (action === "parse-draft") {
        editor.draft = await api(`/meal-drafts/${draft.id}/parse`, {
          method: "POST",
          body: JSON.stringify({ version: draft.version }),
        });
        editor.confirmation = null;
      }
      if(action==='parse-photo'){
        editor.photoRequest ||= {id:crypto.randomUUID(),version:draft.version};
        const request=editor.photoRequest,controller=new AbortController(),timer=setTimeout(()=>controller.abort(),35000);
        try {editor.draft=await api(`/meal-drafts/${draft.id}/photo?version=${request.version}&client_id=${request.id}`,{
          method:'POST',headers:{'Content-Type':photoFile.type},body:photoFile,signal:controller.signal});editor.confirmation=null;
        }finally{clearTimeout(timer);}
      }
      if (["estimate-nutrition", "clear-nutrition"].includes(action)) {
        const controller = new AbortController();
        const pending = draft.items.length - (draft.nutrition?.items.length || 0);
        const timeout = setTimeout(() => controller.abort(), action === "clear-nutrition" ? 35000 : Math.max(1, Math.ceil(pending / 5)) * 25000 + 10000);
        try {
          editor.draft = await api(`/meal-drafts/${draft.id}/nutrition${action === "clear-nutrition" ? "/clear" : ""}`, {
            method: "POST", body: JSON.stringify({version: draft.version}), signal: controller.signal,
          });
          editor.confirmation = null;
        } finally { clearTimeout(timeout); }
      }
      if (action === "confirm-draft") {
        editor.confirmation ||= {
          version: draft.version,
          confirmation_id: crypto.randomUUID(),
        };
        editor.draft = await api(`/meal-drafts/${draft.id}/confirm`, {
          method: "POST",
          body: JSON.stringify(editor.confirmation),
        });
      }
    }
    if (state.draftEditor !== editor || !state.user) return;
    if (
      action === "cancel-draft" ||
      ["committed", "cancelled"].includes(editor.draft.status)
    ) {
      editor.dirty = false;
      $("#record-dialog").close();
      notify(action === "confirm-draft" ? "饮食已确认入账" : "草稿已结束");
    } else {
      renderDraft();
      if (action === "save-draft") notify("草稿已暂存");
    }
    await refresh();
  } catch (error) {
    if (state.draftEditor !== editor || !state.user) return;
    if(action==='parse-photo'&&error.status)editor.photoRequest=null;
    if (["parse-draft", "estimate-nutrition", "clear-nutrition"].includes(action) && editor.draft && !editor.dirty)
      renderDraft();
    $("#draft-form .form-error").textContent = error.name === "AbortError" ? "请求超时，草稿已保留；请读取最新草稿核对结果" : error.message;
    $('[data-action="reload-draft"]', $("#draft-form")).hidden = !(
      (error.status === 409 || error.name === "AbortError") && editor.draft
    );
  } finally {
    editor.busy = false;
    disabled.forEach(([element, wasDisabled]) => {
      if (element.isConnected) element.disabled = wasDisabled;
    });
    updateDraftControls();
  }
}

document.addEventListener("input", (event) => {
  if (event.target.closest("#speech-input")) return;
  if (!event.target.closest("#draft-form") || !state.draftEditor) return;
  if (event.target.name !== "reviewed") {
    state.draftEditor.dirty = true;
    state.draftEditor.photoRequest = null;
    $('[name="reviewed"]', $("#draft-form")).checked = false;
  }
  updateDraftControls();
});

document.addEventListener("submit", (event) => {
  if (event.target.id !== "draft-form") return;
  event.preventDefault();
  runDraftAction("save-draft");
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  try {
    if (action === "text-meal") openTextDraft();
    if (action === "photo-meal") openTextDraft(null,'photo');
    if(['draft-photo-mode','draft-text-mode'].includes(action)&&state.draftEditor&&!state.draftEditor.busy){
      const editor=state.draftEditor,mode=action==='draft-photo-mode'?'photo':'text';
      if(editor.inputMode===mode)return;
      if(editor.dirty&&!window.confirm('切换方式会丢弃尚未暂存的修改，继续吗？'))return;
      editor.inputMode=mode;renderDraft();
    }
    if (action === "draft-manual") openRecord("meals");
    if (action === "resume-draft") {
      const user = state.user;
      const draft = await api(`/meal-drafts/${button.dataset.id}`);
      if (
        state.user === user &&
        user &&
        ["needs_input", "ready"].includes(draft.status)
      )
        openTextDraft(draft);
    }
    if (
      [
        "save-draft",
        "parse-draft",
        "parse-photo",
        "confirm-draft",
        "cancel-draft",
        "reload-draft",
        "estimate-nutrition",
        "clear-nutrition",
      ].includes(action)
    )
      await runDraftAction(action);
    if (
      ["add-draft-item", "remove-draft-item"].includes(action) &&
      state.draftEditor &&
      !state.draftEditor.busy
    ) {
      const items = $("#draft-items");
      if (action === "add-draft-item" && items.children.length < 30) {
        items.insertAdjacentHTML("beforeend", draftItemFields());
        $('[name="name"]', items.lastElementChild).focus();
      }
      if (action === "remove-draft-item")
        button.closest(".draft-item").remove();
      state.draftEditor.dirty = true;
      $('[name="reviewed"]', $("#draft-form")).checked = false;
      updateDraftControls();
      icons();
    }
  } catch (error) {
    notify(error.message, true);
  }
});
