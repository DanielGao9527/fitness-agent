"use strict";

window.TrainingPlans = (() => {
  let active=null;
  function current(s,ticket) {return active===s && s.ticket===ticket && state.user===s.user && s.root.isConnected;}
  function controls(s,busy) {
    s.root.querySelector("fieldset").disabled=busy;
    s.root.querySelector(".training-stop").hidden=!busy;
    s.root.querySelector(".training-generate").disabled=busy || state.trainingPlanStatus!=="configured_unverified";
    s.root.querySelectorAll(".training-accept, .training-record").forEach(button=>button.disabled=busy);
  }
  function cancel() {
    const s=active;
    if(!s || !s.busy) return;
    ++s.ticket;s.controller?.abort();s.busy=false;controls(s,false);
    s.root.querySelector(".training-status").textContent="已停止等待，已提交的 AI 请求仍计入使用次数；可刷新查看已生成草稿";
  }
  function dispose() {cancel();active=null;}
  function summary(request) {
    if(!request || !Object.keys(request).length)return "";
    const basis={daily:"全天总共",session:"本次可用",unspecified:"时间口径待核对"};
    return `<p class="small training-request">${[
      request.kind==="strength"?"力量需求":request.kind==="aerobic"?"基础有氧需求":"训练需求",
      request.minutes?`${basis[request.time_basis] || basis.unspecified} ${request.minutes} 分钟`:"时间未确认",
      request.activity?({walk:"平地步行",cycle:"平地骑行",either:"步行或骑行",swim:"休闲游泳"}[request.activity]):"",
      request.focus?`部位：${request.focus}`:"",request.equipment?`本次器械：${request.equipment}`:"",
      request.split?`结构：${({auto:"自动安排",full_body:"全身训练",upper_lower:"上下肢交替",ppl:"三分化",four:"四分化",five:"五分化"})[request.split] || request.split}`:"",
      request.weekly?"安排未来七天":""
    ].filter(Boolean).map(escapeHtml).join(" · ")}</p>`;
  }
  function mount(root,reference,options={}) {
    dispose();
    if(!root) return;
    root.innerHTML=`<h3>当天基础有氧</h3><form class="training-plan-form"><fieldset>
      <div class="form-grid"><label>日期<input type="date" name="day" value="${escapeHtml(state.day)}" required></label><label>可用训练时间（分钟）<input type="number" name="daily_minutes" min="15" max="120" step="1" required></label><label>时间口径<select name="time_basis" required><option value="">请选择</option><option value="session">本次接下来可用</option><option value="daily" selected>全天总共（含已完成）</option></select></label><label>主活动<select name="activity"><option value="walk">平地步行</option><option value="cycle">平地骑行</option><option value="either">两者均可</option></select></label></div>
      <dl class="plan-profile"><dt>训练经验</dt><dd>${state.profile.experience==="experienced"?"有训练经验":"初学"}</dd><dt>档案器械</dt><dd>${escapeHtml(state.profile.equipment || "未填写")}</dd></dl>
      <label class="plan-check"><input type="checkbox" name="bicycle_available">本次有自行车、头盔及安全平地路线，能够正常骑行</label>
      <label class="plan-check"><input type="checkbox" name="general_adult" required>我为18至64岁成人，非孕哺期，无伤病、不适、慢性疾病或专业运动限制，能够正常步行</label>
      <label class="plan-check"><input type="checkbox" name="constraints_reviewed" required>已如实填写训练限制，并核对这一天的训练记录与当前身体状态</label>
      <p class="small muted">仅基础有氧，不是完整力量、增肌或分化计划。时间为待核对安排，不是必须完成的目标；不适时停止活动。</p>
      <p class="small muted">生成时向阿里云发送目标、训练经验、已完成分钟数、本次时间及器械选项和公开依据，并计入 AI 使用次数。不发送姓名。</p>
      <div class="plan-actions"><button type="submit" class="primary training-generate">${icon("sparkles")}生成有氧建议</button><button type="button" class="icon-button training-reload" aria-label="刷新训练建议" title="刷新训练建议">${icon("refresh-cw")}</button></div>
      </fieldset><button type="button" class="training-stop" hidden>${icon("square")}停止等待</button><p class="training-status small muted" role="status"></p><p class="training-error form-error" role="alert" hidden></p></form><div class="training-results" aria-live="polite"></div>`;
    active={root,reference,options,user:state.user,ticket:0,busy:false,controller:null};
    const request=options.context || {},fields=root.querySelector("form").elements;
    if(request.minutes){fields.daily_minutes.value=request.minutes;fields.time_basis.value=["daily","session"].includes(request.time_basis)?request.time_basis:"";}
    if(request.activity)fields.activity.value=request.activity;
    if(options.day){root.querySelector('[name="day"]').value=options.day;root.querySelector('[name="day"]').readOnly=true;}
    controls(active,false);
    root.querySelector("form").addEventListener("submit",event=>{event.preventDefault();generate();});
    root.querySelector(".training-stop").addEventListener("click",cancel);
    root.querySelector(".training-reload").addEventListener("click",activate);
    root.querySelector("fieldset").addEventListener("change",event=>{
      cancel();root.querySelector(".training-results").replaceChildren();
      if(event.target.name==="day") activate();
    });
  }
  async function operation(work,waiting,success) {
    const s=active;
    if(!s || s.busy) return;
    const ticket=++s.ticket;s.busy=true;s.controller=new AbortController();controls(s,true);
    const error=s.root.querySelector(".training-error"),status=s.root.querySelector(".training-status");
    error.hidden=true;status.textContent=waiting;
    const controller=s.controller,timeout=setTimeout(()=>controller.abort(),40000);
    try {
      const result=await work(s,controller.signal);
      if(!current(s,ticket)) return;
      success(s,result);icons();
    } catch(failure) {
      if(!current(s,ticket)) return;
      error.hidden=false;error.textContent=failure.name==="AbortError"?"等待超时，未自动重试；可刷新建议记录核对，已提交的 AI 请求仍计入使用次数。":failure.message;
      status.textContent="未完成本次操作";
      if(failure.code==="PLAN_CONTEXT_CHANGED") s.root.querySelector(".training-results").replaceChildren();
    } finally {clearTimeout(timeout);if(current(s,ticket)){s.busy=false;controls(s,false);}}
  }
  function markup(s,plans,readonly=false) {
    return plans.length?plans.map(plan=>{
      const sameConversation=!s.options.binding || (plan.coach_id===s.options.binding.coach_id && plan.coach_version===s.options.binding.coach_version);
      const label=plan.stale?"已失效，不再作为当前建议":plan.status==="draft"?"待核对草稿":plan.current?`已采纳 · 第${plan.version}版`:`历史 · 第${plan.version}版`;
      return `<article class="plan-result"><div class="section-heading"><h3>${escapeHtml(plan.day)} 基础有氧</h3><span class="small ${plan.stale?"nutrition-question":"muted"}">${label}</span></div>
        <p class="small">${plan.time_basis==="session"?"本次可用":"全天总共"} ${plan.daily_minutes} 分钟 · 当天已完成 ${plan.completed_minutes} 分钟 · 本次可安排上限 ${plan.remaining_minutes} 分钟</p>
        ${!sameConversation?'<p class="small muted">其他对话或旧版建议，仅供查看</p>':""}
        <ul class="plan-portions"><li><div><strong>轻松步行热身</strong><span class="small muted">缓慢开始，留意身体状态</span></div><b>${plan.warmup_minutes} 分钟</b></li><li><div><strong>${escapeHtml(plan.activity_name)}</strong><span class="small muted">以能交谈、不能唱歌为中等强度参考；太吃力就放慢</span></div><b>${plan.main_minutes} 分钟</b></li><li><div><strong>轻松步行收尾</strong><span class="small muted">逐渐放慢，不必用满剩余时间</span></div><b>${plan.cooldown_minutes} 分钟</b></li></ul>
        <p><strong>本次共 ${plan.total_minutes} 分钟</strong></p><p class="small muted">具体时间为建议草稿，资料不规定你的个人分钟数。头晕、胸部不适或疼痛时停止活动并寻求专业帮助。${plan.activity_id==="cycle"?"骑行需头盔与安全路线，不能用车辆速度代替相对强度判断。":""}</p>
        <details class="plan-evidence"><summary>活动原则与出处</summary>${plan.sources.map(s.reference).join("")}</details><p class="small muted">AI 建议（阿里云千问） · ${escapeHtml(plan.generated_at.replace("T"," ").slice(0,16))} UTC</p>
        ${!readonly && plan.status==="draft" && !plan.stale && sameConversation?`<label class="plan-check"><input type="checkbox" class="training-reviewed">已核对时长、器械与当前身体状态；采纳不代表已经完成</label><button type="button" class="primary training-accept" data-id="${escapeHtml(plan.id)}">${icon("check")}采纳这份建议</button>`:""}
        ${!readonly && sameConversation?`<div class="plan-actions">${plan.execution_review?.status==="committed"?'<span class="small muted">已确认入账，后续修改请到训练记录</span>':`<button type="button" class="training-record" data-id="${escapeHtml(plan.id)}">${icon("clipboard-check")}${plan.execution_review?.status==="draft"?"继续核对实际训练":"记录实际训练"}</button>`}</div>`:""}</article>`;
    }).join(""):'<p class="empty">这一天还没有训练建议</p>';
  }
  function history(root,plans,reference) {
    root.innerHTML=`<details class="plan-evidence"><summary>这轮的训练建议 · ${plans.length}份</summary>${markup({reference,options:{}},plans,true)}</details>`;
  }
  function draw(s,plans) {
    const selected=s.options.binding?plans.filter(plan=>plan.coach_id===s.options.binding.coach_id && plan.coach_version===s.options.binding.coach_version):plans;
    s.root.querySelector(".training-results").innerHTML=markup(s,selected);
    s.root.querySelectorAll(".training-accept").forEach(button=>button.addEventListener("click",()=>{
      if(!button.closest("article").querySelector(".training-reviewed").checked){const error=s.root.querySelector(".training-error");error.hidden=false;error.textContent="请先核对并勾选确认";return;}
      operation(async(session,signal)=>{
        await api(`/training-plans/${button.dataset.id}/accept`,{method:"POST",signal,body:JSON.stringify({reviewed:true})});
        return api(`/training-plans?day=${session.root.querySelector('[name="day"]').value}`,{signal});
      },"正在采纳…",(session,result)=>{draw(session,result);session.root.querySelector(".training-status").textContent="已保存建议版本，实际训练记录未改变";});
    }));
    s.root.querySelectorAll(".training-record").forEach(button=>button.addEventListener("click",()=>{
      operation((session,signal)=>api(`/training-plans/${button.dataset.id}/execution`,{method:"POST",signal,body:JSON.stringify({reviewed:true})}),
        "正在读取实际训练草稿…",(session,result)=>{
          if(result.status==="committed") {session.root.querySelector(".training-status").textContent="这份建议已确认入账，请到训练记录查看或修改";button.remove();return;}
          session.root.querySelector(".training-status").textContent="实际训练草稿已保留，尚未入账";
          window.WorkoutEditor.openExecution(result);
        });
    }));
  }
  function activate() {
    if(!active || active.busy) return;
    active.root.querySelector(".training-results").replaceChildren();
    operation((s,signal)=>api(`/training-plans?day=${s.root.querySelector('[name="day"]').value}`,{signal}),"正在读取训练建议…",(s,result)=>{
      const plan=s.options.binding && result.find(item=>!item.stale && item.coach_id===s.options.binding.coach_id && item.coach_version===s.options.binding.coach_version);
      if(plan){const fields=s.root.querySelector("form").elements;fields.daily_minutes.value=plan.daily_minutes;fields.time_basis.value=plan.time_basis || "daily";fields.activity.value=plan.activity_id;}
      draw(s,result);s.root.querySelector(".training-status").textContent=state.trainingPlanStatus==="configured_unverified"?"基础有氧草稿 · 尚不含力量与分化安排":"训练建议尚未启用，历史仍可查看";
    });
  }
  function generate() {
    const s=active;
    if(!s || s.busy || state.trainingPlanStatus!=="configured_unverified") return;
    const form=s.root.querySelector("form");if(!form.reportValidity()) return;
    const body={client_id:crypto.randomUUID(),day:form.elements.day.value,daily_minutes:Number(form.elements.daily_minutes.value),time_basis:form.elements.time_basis.value,activity:form.elements.activity.value,bicycle_available:form.elements.bicycle_available.checked,general_adult:form.elements.general_adult.checked,constraints_reviewed:form.elements.constraints_reviewed.checked,...(s.options.binding || {})};
    s.root.querySelector(".training-results").replaceChildren();
    operation((session,signal)=>api("/training-plans",{method:"POST",signal,body:JSON.stringify(body)}),"正在生成基础有氧草稿…",(session,result)=>{draw(session,[result]);session.root.querySelector(".training-status").textContent="草稿已暂存，尚未采纳或记为完成";});
  }
  return {mount,activate,cancel,dispose,history,summary};
})();
