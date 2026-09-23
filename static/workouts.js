"use strict";

window.WorkoutEditor = (() => {
  const entries = new WeakMap();
  const statusOptions = {unknown:"待确认",planned:"计划中",completed:"已完成"};
  const factors = ["name", "minutes"];
  let active = null;
  const rows = (s=active) => [...s.form.querySelectorAll(".workout-item")];
  const value = (name, root=active.form) => $(`[name="${name}"]`, root).value;
  const number = (name, root) => value(name,root)==="" ? null : Number(value(name,root));
  const identity = (row) => ({name:value("name",row).trim(), minutes:number("minutes",row),
    intensity:entries.get(row).intensity, details:entries.get(row).details, weight_kg:number("weight_kg")});
  const ready = (row) => {
    const item=identity(row);
    return item.name && Number.isInteger(item.minutes) && item.minutes>=1 && item.minutes<=600 && value("status",row)!=="unknown";
  };
  const needsEstimate = row => {
    const entry=entries.get(row);
    return !entry.preview && !entry.estimate && !entry.skipEstimate;
  };

  function calorieDetails(estimate, compact=false) {
    const assumptions=`<ul class="nutrition-assumptions">${estimate.assumptions.map(text=>`<li>${escapeHtml(text)}</li>`).join("")}</ul>
      <p class="muted small">AI 估算（阿里云千问） · ${escapeHtml(estimate.generated_at.slice(0,10))}</p>`;
    return `<strong>${nutritionRange(estimate.kcal)} kcal</strong><p class="muted small">模型估算 · 运动期间总消耗（含静息部分）</p>
      ${compact ? `<details class="workout-options"><summary>估算依据</summary>${assumptions}</details>` : assumptions}`;
  }

  function fields(item={}) {
    return `<section class="workout-item" role="group" aria-label="训练项目">
      <div class="meal-item-heading"><h3>项目</h3><button type="button" class="icon-button" data-workout="remove" title="移除此训练" aria-label="移除此训练">${icon("x")}</button></div>
      <label>训练项目或部位<input name="name" required maxlength="120" value="${escapeHtml(item.name)}" placeholder="例如：游泳、胸和三头力量训练"></label>
      <div class="form-grid">${field("总时长（分钟）","minutes",item.minutes,'type="number" min="1" max="600" step="1" required')}
      <label>完成状态<select name="status">${options(active?.row || item.status === 'planned' ? statusOptions : {unknown:'待确认',completed:'已完成'},item.status || active?.defaultStatus || "completed")}</select></label></div>
      <p class="workout-item-status muted small" role="status"></p><div class="workout-calories"></div>
    </section>`;
  }

  function add(item={}) {
    const list=$("#workout-items",active.form);
    list.insertAdjacentHTML("beforeend",fields(item));
    const row=list.lastElementChild;
    entries.set(row,{clientId:crypto.randomUUID(), requestId:crypto.randomUUID(), estimate:item.calorie_estimate || null,
      preview:item.calorie_preview_id || null, clear:false, stale:false, skipEstimate:false, question:"",
      intensity:item.intensity || "normal_assumed",details:item.details || ""});
    if(active.execution) $('[name="status"]',row).disabled=true;
    return row;
  }

  function update() {
    const s=active;
    if(!s) return;
    const items=rows(), locked=s.busy || s.speechBusy;
    const pending=items.filter(needsEstimate);
    const needsParse=s.mode==="text" && value("text").trim()!==s.parsedText;
    const valid=items.length>0 && items.every(ready);
    const weight=number("weight_kg");
    $(".workout-weight-state",s.form).textContent=weight!==null && weight!==s.initialWeight
      ? "保存已完成训练后，按记录日期同步档案体重；旧日补录不覆盖较新值。" : "";
    items.forEach((row,index)=>{
      const entry=entries.get(row);
      $("h3",row).textContent=`项目 ${index+1}`;
      row.setAttribute("aria-label",`训练项目 ${index+1}`);
      $('[data-workout="remove"]',row).disabled=Boolean(locked || s.row || items.length===1);
      const energy=entry.stale ? "训练参数已变更，消耗待重估" : entry.estimate ? (value("status",row)==="completed" ? "估算消耗" : "预计消耗 · 尚未完成") : entry.question || "消耗未知";
      $(".workout-item-status",row).textContent=energy;
      $(".workout-calories",row).innerHTML=entry.estimate ? calorieDetails(entry.estimate,true) : "";
    });
    $('[data-workout="add"]',s.form).disabled=Boolean(locked || items.length>=30);
    const estimate=$('[data-workout="estimate"]',s.form);
    const retryable=items.filter(row=>!entries.get(row).estimate);
    estimate.hidden=!s.reviewed || !retryable.length;
    estimate.disabled=Boolean(locked || state.workoutStatus!=="configured_unverified" || !valid || needsParse || !retryable.length || weight===null || weight<20 || weight>400);
    $('[data-workout="clear"]',s.form).hidden=!items.some(row=>entries.get(row).preview || entries.get(row).estimate);
    $('[data-workout="parse"]',s.form).disabled=Boolean(locked || state.workoutStatus!=="configured_unverified" || !value("text").trim());
    $('[type="submit"]',s.form).disabled=Boolean(locked || !valid || needsParse);
    $("span",$('[type="submit"]',s.form)).textContent=s.reviewed ? "保存训练" : "核对训练";
    $(".workout-state",s.form).textContent=locked ? (s.busy || "语音处理中") : needsParse ? "描述待解析" : !valid ? "请核对项目、总时长和完成状态" : state.workoutStatus!=="configured_unverified" ? "消耗估算暂不可用，仍可保存训练" : weight===null ? "缺少本次体重，消耗未知，仍可保存" : !s.reviewed ? "待核对" : pending.length ? `${pending.length}项消耗未知，仍可保存` : "待确认保存";
    icons();
  }

  function unlock(s) {
    s.locked?.forEach(([node,disabled])=>{if(node.isConnected) node.disabled=disabled;});
    s.locked=null;
    s.busy=false;
  }

  function cancel() {
    if(!active?.busy || active.saving) return;
    active.ticket++;
    active.controller?.abort();
    if(active.operation!=="parse") $(".form-error",active.form).textContent="估算已停止，训练未保存。";
    unlock(active);
    update();
  }

  function dispose() { cancel(); active=null; }

  function canLeave() {
    if(!active) return true;
    if(active.saving) return false;
    cancel();
    return !active.dirty || window.confirm("训练尚未保存，仍要关闭吗？");
  }

  function open(row=null,execution=null,defaults={}) {
    if(!canLeaveDraft()) return;
    dispose();window.ManualNutrition?.dispose();window.FitnessSpeech?.dispose();
    state.draftEditor=null;
    state.editing={kind:"workouts",row};
    $("#dialog-title").textContent=execution ? "记录实际训练" : row ? "编辑训练记录" : "记录训练";
    $("#dialog-content").innerHTML=`<form id="workout-form">
      <div class="segmented" aria-label="训练记录方式"><button type="button" data-workout="manual" class="selected" aria-pressed="true">手动填写</button>${row || execution ? "" : '<button type="button" data-workout="text" aria-pressed="false">文字描述</button>'}</div>
      <div class="form-grid">${field("日期","day",execution?.day || row?.day || state.day,'type="date" required')}${field("本次体重（kg）","weight_kg",execution ? execution.weight_kg : row?.weight_kg ?? state.profile.weight_kg,'type="number" min="20" max="400" step="0.1"')}</div>
      <p class="muted small workout-weight-state" role="status"></p>
      <div id="workout-text" hidden><label>训练描述<textarea name="text" maxlength="2000" placeholder="例如：今天游泳30分钟，胸和三头一共练了40分钟"></textarea></label><div id="workout-speech"></div>
      <div class="draft-toolbar"><span class="muted small">AI 识别训练项目与时长</span><button type="button" data-workout="parse">${icon("sparkles")}解析训练</button></div></div>
      <div class="workout-questions"></div><div id="workout-items"></div>
      <button type="button" class="add-food" data-workout="add" ${row ? "hidden" : ""}>${icon("plus")}再加一个项目</button>
      <label>备注<textarea name="notes" maxlength="500">${escapeHtml(execution?.notes ?? row?.notes)}</textarea></label>
      <p class="workout-state muted small" role="status"></p>
      <div class="draft-toolbar"><button type="button" data-workout="estimate" hidden>${icon("refresh-cw")}<span>重试消耗估算</span></button><button type="button" class="icon-button" data-workout="clear" title="移除消耗估算" aria-label="移除消耗估算">${icon("x")}</button></div>
      <p class="form-error" role="alert"></p><div class="dialog-actions"><button type="button" data-action="close-dialog">取消</button>${execution ? `<button type="button" data-workout="stash">${icon("save")}暂存草稿</button>` : ""}<button type="submit" class="primary">${icon("check")}<span>核对训练</span></button></div></form>`;
    active={form:$("#workout-form"),row,execution,defaultStatus:"completed",user:state.user,mode:"manual",parsedText:null,dirty:false,reviewed:false,busy:false,speechBusy:false,ticket:0};
    active.initialWeight=number("weight_kg");
    if(execution) execution.items.forEach(item=>add({...item,status:"completed"}));
    else add(row ? {...row,intensity:row.intensity || "unknown"} : {});
    update();
    $("#record-dialog").showModal();
  }

  function remember(execution) {
    state.trainingDrafts=[execution,...(state.trainingDrafts || []).filter(item=>item.plan_id!==execution.plan_id)];
  }

  function openExecution(execution) {remember(execution);open(null,execution);}

  function draftList() {
    if(!state.trainingDrafts?.length) return "";
    return `<section class="section training-draft-list"><div class="section-heading"><h2>${icon("file-pen-line")}待核对实际训练<span class="count">${state.trainingDrafts.length} 份</span></h2></div>${state.trainingDrafts.map(draft=>`<div class="record"><div class="record-main"><div class="record-name">${escapeHtml(draft.items.map(item=>item.name).join("、") || "实际训练草稿")}</div><div class="record-meta">${escapeHtml(draft.day)} · 尚未入账</div></div><div class="record-actions"><button type="button" data-workout-resume="${escapeHtml(draft.plan_id)}">${icon("pencil")}继续</button><button type="button" class="icon-button" data-workout-resume="${escapeHtml(draft.plan_id)}" data-discard-version="${draft.version}" title="删除训练草稿" aria-label="删除训练草稿">${icon("trash-2")}</button></div></div>`).join("")}</section>`;
  }

  document.addEventListener("click",async event=>{
    const button=event.target.closest("[data-workout-resume]");
    if(!button || button.disabled) return;
    const discard=button.dataset.discardVersion;
    if(discard && !window.confirm("删除这份未入账的训练草稿？实际训练记录不受影响。")) return;
    const user=state.user;button.disabled=true;
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),30000);
    try {
      const result=await api(`/training-plans/${button.dataset.workoutResume}/execution`,{method:discard?"DELETE":"POST",signal:controller.signal,body:JSON.stringify({reviewed:true,...(discard?{version:Number(discard)}:{})})});
      if(state.user!==user || !button.isConnected) return;
      if(discard) {notify("训练草稿已删除");await refresh();}
      else if(result.status==="committed") {notify("这份训练已确认入账，请到训练记录查看或修改");await refresh();}
      else openExecution(result);
    } catch(error) {if(state.user===user && button.isConnected) notify(error.name==="AbortError"?"读取超时，请重新打开":error.message,true);}
    finally {clearTimeout(timeout);if(button.isConnected)button.disabled=false;}
  });

  function invalidate(row) {
    const entry=entries.get(row);
    entry.stale ||= Boolean(entry.estimate || entry.preview);
    entry.clear ||= Boolean(entry.estimate || entry.preview);
    entry.preview=entry.estimate=null;entry.question="";entry.skipEstimate=false;entry.requestId=crypto.randomUUID();
  }

  async function run(action) {
    const s=active;
    if(!s || s.busy || s.speechBusy) return;
    if(action==="stash" && s.pendingCommit) {notify("上次入账结果尚未确认，请重试保存或重新打开核对",true);return;}
    const items=rows();
    if(!["parse","stash"].includes(action) && !s.form.reportValidity()) return;
    if(!["parse","stash"].includes(action) && (!items.length || !items.every(ready) || (s.mode==="text" && value("text").trim()!==s.parsedText))) return;
    if(action==="parse" && !value("text").trim()) return;
    if(action==="parse" && items.some(row=>value("name",row).trim()) && !window.confirm("解析将替换当前训练项目，继续吗？")) return;
    if(action==="save" && !s.reviewed) return;
    if(action==="estimate") items.filter(row=>!entries.get(row).estimate).forEach(row=>{
      const entry=entries.get(row);
      if(entry.preview || entry.skipEstimate) invalidate(row);
    });
    const pending=items.filter(needsEstimate);
    const estimating=action==="review" || action==="estimate";
    if(estimating) {
      s.reviewed=true;
      $(".form-error",s.form).textContent="";
      const weight=number("weight_kg");
      if(!pending.length || state.workoutStatus!=="configured_unverified" || weight===null || weight<20 || weight>400) {update();return;}
    }
    if(action==="parse") s.reviewed=false;
    const text=value("text").trim();
    const ticket=++s.ticket;
    const current=()=>active===s && s.ticket===ticket && state.user===s.user && s.form.isConnected;
    s.locked=[...s.form.querySelectorAll("input,textarea,select,button")].map(node=>[node,node.disabled]);
    s.locked.forEach(([node])=>{node.disabled=true;});
    s.busy=action==="parse" ? "正在解析训练…" : estimating ? "正在估算消耗…" : action==="stash" ? "正在暂存草稿…" : "正在保存训练…";
    s.operation=action;
    s.saving=["save","stash"].includes(action);
    s.controller=new AbortController();
    const timeout=setTimeout(()=>s.controller.abort(),estimating ? Math.ceil(pending.length/5)*25000+10000 : 35000);
    let followUp=false;
    $(".form-error",s.form).textContent="";update();
    try {
      if(action==="parse") {
        const result=await api("/workouts/parse-text",{method:"POST",body:JSON.stringify({text}),signal:s.controller.signal});
        if(!current()) return;
        $("#workout-items",s.form).replaceChildren();
        result.items.forEach(add);
        $(".workout-questions",s.form).innerHTML=result.questions.length ? `<ul>${result.questions.map(q=>`<li>${escapeHtml(q)}</li>`).join("")}</ul>` : "";
        s.parsedText=text;s.dirty=true;
        followUp=result.items.length>0 && rows().every(ready);
      } else if(estimating) {
        const result=await api("/workouts/calories/preview-batch",{method:"POST",signal:s.controller.signal,
          body:JSON.stringify({items:pending.map(row=>({...identity(row),client_id:entries.get(row).requestId}))})});
        if(!current()) return;
        if(result.items.length!==pending.length) throw new Error("消耗结果不完整，请重试");
        result.items.forEach((preview,index)=>{
          const entry=entries.get(pending[index]);
          entry.preview=preview.id;entry.estimate=preview.estimate.status==="estimated" ? preview.estimate : null;
          entry.question=preview.estimate.question;entry.stale=false;
        });
        s.dirty=true;
      } else if(s.execution) {
        const url=`/training-plans/${s.execution.plan_id}/execution`;
        if(!s.pendingCommit) {
          const body={version:s.execution.version,day:value("day"),weight_kg:number("weight_kg"),notes:value("notes"),
            sync_weight:number("weight_kg")!==null && (number("weight_kg")!==s.initialWeight || Boolean(s.execution.sync_weight)),
            items:items.map(row=>{const {weight_kg,...item}=identity(row);return {...item,calorie_preview_id:entries.get(row).preview};})};
          const saved=await api(url,{method:"PUT",signal:s.controller.signal,body:JSON.stringify(body)});
          if(!current()) return;
          s.execution=saved;s.dirty=false;remember(saved);
        }
        if(action==="stash") {notify("实际训练草稿已暂存，尚未入账");return;}
        s.pendingCommit=s.execution.version;
        await api(`${url}/commit`,{method:"POST",signal:s.controller.signal,body:JSON.stringify({version:s.pendingCommit,reviewed:true})});
        if(!current()) return;
        state.day=value("day");s.dirty=false;s.saving=false;$("#record-dialog").close();
        notify(`已保存 ${items.length} 项实际训练`);await refresh();
      } else {
        const payload=items.map(row=>{
          const entry=entries.get(row);
          return {...identity(row),day:value("day"),status:value("status",row),notes:value("notes"),
            sync_weight:number("weight_kg")!==null && number("weight_kg")!==s.initialWeight && value("status",row)==="completed",
            ...(s.row ? {} : {client_id:entry.clientId}),
            ...(entry.preview ? {calorie_preview_id:entry.preview} : entry.clear ? {clear_calorie_estimate:true} : {})};
        });
        await api(s.row ? `/workouts/${s.row.id}` : "/workouts/batch",{method:s.row ? "PUT" : "POST",signal:s.controller.signal,
          body:JSON.stringify(s.row ? payload[0] : {items:payload})});
        if(!current()) return;
        s.dirty=false;s.saving=false;$("#record-dialog").close();
        notify(`已保存 ${payload.length} 项训练`);await refresh();
      }
    } catch(error) {
      if(current()) {
        $(".form-error",s.form).textContent=(error.name==="AbortError" ? "请求超时，输入已保留；保存超时可原样重试" : error.message) + (estimating ? "；可重试估算，或按消耗未知保存训练。" : "");
        if(estimating && error.status===409) pending.forEach(row=>{entries.get(row).requestId=crypto.randomUUID();});
        if(["save","stash"].includes(action) && [404,409].includes(error.status) && /预览/.test(error.message)) {
          items.filter(row=>entries.get(row).preview).forEach(invalidate);
          s.reviewed=false;s.pendingCommit=null;s.dirty=true;
        }
      }
    } finally {
      clearTimeout(timeout);
      if(current()) {s.saving=false;unlock(s);update();if(followUp) await run("review");}
    }
  }

  document.addEventListener("input",event=>{
    if(!active || !event.target.closest("#workout-form") || event.target.closest("#workout-speech")) return;
    active.dirty=true;active.reviewed=false;
    active.pendingCommit=null;
    const row=event.target.closest(".workout-item");
    if(row && event.target.name==="name") {entries.get(row).intensity="normal_assumed";entries.get(row).details="";}
    if(row && factors.includes(event.target.name)) invalidate(row);
    if(event.target.name==="weight_kg") rows().forEach(invalidate);
    if(row) $(".workout-questions",active.form).innerHTML="";
    update();
  });

  document.addEventListener("click",event=>{
    const button=event.target.closest("button[data-workout]");
    if(!button || button.disabled || !active || active.busy || active.speechBusy) return;
    const action=button.dataset.workout;
    if(["parse","estimate","stash"].includes(action)) {run(action);return;}
    if(["manual","text"].includes(action)) {
      active.reviewed=false;
      active.mode=action;
      $("#workout-text",active.form).hidden=action!=="text";
      for(const mode of ["manual","text"]) {
        const node=$(`[data-workout="${mode}"]`,active.form);
        node?.setAttribute("aria-pressed",String(mode===action));node?.classList.toggle("selected",mode===action);
      }
      window.FitnessSpeech?.dispose();
      if(action==="text") {
        const s=active;
        window.FitnessSpeech?.mount($("#workout-speech",s.form),$('[name="text"]',s.form),state.speechStatus==="configured_unverified",busy=>{if(active===s){s.speechBusy=busy;update();}});
      }
    }
    if(action==="add" && rows().length<30) {const row=add();active.dirty=true;active.reviewed=false;active.pendingCommit=null;$('[name="name"]',row).focus();}
    if(action==="remove" && rows().length>1) {button.closest(".workout-item").remove();active.dirty=true;active.reviewed=false;active.pendingCommit=null;}
    if(action==="clear") {
      rows().forEach(row=>{invalidate(row);const entry=entries.get(row);entry.clear=true;entry.stale=false;entry.skipEstimate=true;});active.dirty=true;active.pendingCommit=null;
    }
    update();
  });

  document.addEventListener("submit",event=>{
    if(event.target.id!=="workout-form") return;
    event.preventDefault();
    if(!$('[type="submit"]',event.target).disabled) run(active.reviewed ? "save" : "review");
  });
  window.addEventListener?.("beforeunload",event=>{
    if(active?.dirty || active?.saving){event.preventDefault();event.returnValue="";}
  });
  return {open,openExecution,draftList,dispose,canLeave,calorieDetails};
})();
