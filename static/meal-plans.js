"use strict";

window.MealPlans = (() => {
  let active=null;
  let selected=null;
  const labels={breakfast:"早餐",lunch:"午餐",dinner:"晚餐"};
  const intakeMessages={unset:'未设置每日目标，本次仅参考搭配',paused:'每日目标已停用',needs_review:'档案已变化，每日目标待复核',review_due:'每日目标已到复核日期',out_of_scope:'特殊饮食情况，暂停数值参考',unknown_intake:'有热量未知的记录，暂不使用数值参考',confirm_records:'尚未核对截至现在的饮食是否记全',confirm_empty:'没有饮食记录，不能直接当作还没吃',ready:'已核对截至确认时的饮食记录',target_reached:'已录入摄入达到或超过目标，不据此建议停吃',uncertain_remaining:'估算差额跨过零，暂不据此安排份量'};
  function kcalRange(value) {return value ? `${value.lower===value.upper?value.lower:`${value.lower} ~ ${value.upper}`} kcal` : '未知';}
  function intakeDetails(value) {
    return `<p class="small">每日目标 ${escapeHtml(value.daily_target_kcal)} kcal · 已吃 ${escapeHtml(kcalRange(value.recorded_kcal))}</p>
      <p class="small">全日目标减已吃 ${escapeHtml(kcalRange(value.day_difference_kcal))} · 本次安排 ${value.requested_meals.map(key=>labels[key]).join('、')}</p>
      <p class="small muted">这个范围尚未分配给各餐，不是本餐配额；餐单份量尚未经过目标热量匹配校验，训练消耗不加回。</p>`;
  }
  function binding(s,mealType) {
    return s.options.binding?{...s.options.binding,coach_version:s.options.mealVersions?.[mealType] || s.options.binding.coach_version}:null;
  }
  function cancel() {
    const s=active;
    if(!s || !s.busy) return;
    s.ticket++;s.controller?.abort();s.busy=false;
    controls(s,false);
    s.root.querySelector(".plan-status").textContent="已停止等待，已提交的 AI 请求仍计入使用次数；重新进入可查看已生成草稿";
  }
  function dispose() {
    if(active?.options.binding)selected={user:active.user.id,day:active.options.day,...active.options.binding,
      meal:active.root.querySelector('[name="meal_type"]')?.value};
    cancel();active=null;
  }
  function current(s,ticket) {return active===s && s.ticket===ticket && state.user===s.user && s.root.isConnected;}
  function controls(s,busy) {
    s.root.querySelector("fieldset").disabled=busy;
    s.root.querySelector(".plan-stop").hidden=!busy;
    s.root.querySelector(".plan-generate").disabled=busy || !s.ready || state.mealPlanStatus!=="configured_unverified";
    s.root.querySelectorAll(".plan-accept").forEach(button=>button.disabled=busy);
    s.root.querySelectorAll('.plan-result .plan-actions button,.plan-portion-edit input,.plan-portion-edit button').forEach(element=>element.disabled=busy);
    s.root.querySelectorAll('.plan-intake button').forEach(button=>button.disabled=busy);
    s.root.querySelectorAll('.plan-nutrition-estimate').forEach(button=>button.disabled=busy||state.nutritionStatus!=='configured_unverified'||button.dataset.pending==='true');
    const toggle=s.root.querySelector('[name="use_intake"]');
    if(toggle)toggle.disabled=busy || s.intake?.status!=='ready';
  }
  function mount(root,reference,options={}) {
    dispose();
    if(selected && selected.user===state.user.id && selected.day===options.day &&
       selected.coach_id===options.binding?.coach_id && selected.coach_version===options.binding?.coach_version && options.mealTypes?.includes(selected.meal))
      options={...options,mealType:selected.meal};
    root.innerHTML=`<div class="plan-meal-tabs" role="group" aria-label="餐次" ${options.mealTypes?"":"hidden"}>${(options.mealTypes || []).map(key=>`<button type="button" data-meal="${key}" aria-pressed="false">${labels[key]}</button>`).join("")}</div><section class="plan-intake" aria-label="每日目标参考"></section><form class="plan-form"><fieldset><div class="form-grid"><label>日期<input type="date" name="day" value="${escapeHtml(state.day)}" required></label><label>下一餐<select name="meal_type">${Object.entries(labels).map(([key,value])=>`<option value="${key}" ${key==="dinner"?"selected":""}>${value}</option>`).join("")}</select></label></div>
      <dl class="plan-profile"><dt>食物禁忌</dt><dd>${escapeHtml(state.profile.food_allergies || "未填写")}</dd><dt>饮食偏好</dt><dd>${escapeHtml(state.profile.preferences || "未填写")}</dd></dl>
      <div class="plan-consent"><label class="plan-check"><input type="checkbox" name="adult_general_diet" required>我已满18岁，非孕哺期，无需疾病相关的特殊饮食管理</label>
      <label class="plan-check"><input type="checkbox" name="constraints_reviewed" required>档案已如实填写禁忌；食用前会核对实际配料与交叉接触风险</label>
      <p class="small muted">确认在本次登录期间有效；相关档案变化后重新核对。</p></div>
      <div class="plan-consent-saved" hidden><span class="small muted">饮食适用条件已确认</span><button class="plan-consent-reset icon-button" type="button" title="重新核对适用条件" aria-label="重新核对适用条件">${icon("pencil")}</button></div>
      <p class="small muted">生成时将向阿里云发送健身方向、身高体重、饮食偏好/禁忌、当天饮食和训练分钟数，并计入 AI 使用次数。开启每日目标参考时还会发送目标数值、已吃及差额范围、本次餐次；不发送目标来源备注或公式输入。</p>
      <div class="plan-actions"><button class="primary plan-generate" type="submit" disabled>${icon("sparkles")}生成下一餐</button><button class="plan-reload icon-button" type="button" title="刷新建议记录" aria-label="刷新建议记录">${icon("refresh-cw")}</button></div></fieldset>
      <button type="button" class="plan-stop" hidden>${icon("square")}停止等待</button><p class="plan-status small muted" role="status"></p><p class="plan-error form-error" role="alert" hidden></p></form>
      <section class="plan-nutrition-panel" aria-label="餐单营养估算"></section><div class="plan-results" aria-live="polite"></div>`;
    active={root,reference,options,user:state.user,ticket:0,busy:false,ready:false,foods:[],controller:null};
    if(options.day){root.querySelector('[name="day"]').value=options.day;root.querySelector('[name="day"]').readOnly=true;}
    if(options.binding)root.querySelector('[name="day"]').closest("label").hidden=true;
    if(options.mealType){root.querySelector('[name="meal_type"]').value=options.mealType;root.querySelector('[name="meal_type"]').disabled=true;}
    if(options.mealTypes){
      root.querySelector('[name="meal_type"]').closest("label").hidden=true;
      root.querySelectorAll(".plan-meal-tabs button").forEach(button=>button.addEventListener("click",()=>{
        if(window.MealPlanActions && !window.MealPlanActions.canLeave(root))return;
        cancel();root.querySelector('[name="meal_type"]').value=button.dataset.meal;activate();
      }));
    }
    root.querySelector("form").addEventListener("submit",event=>{event.preventDefault();generate();});
    root.querySelector(".plan-stop").addEventListener("click",cancel);
    root.querySelector(".plan-reload").addEventListener("click",activate);
    root.querySelector(".plan-consent-reset").addEventListener("click",()=>operation(
      async(s,signal)=>{await api("/meal-plans/consent",{method:"DELETE",signal});return api("/meal-plans/consent",{signal});},"正在重新核对…",
      (s,consent)=>{setConsent(s,consent);s.root.querySelector(".plan-status").textContent="请重新核对两项适用条件";}
    ));
    root.querySelector('[name="day"]').addEventListener("change",()=>{cancel();activate();});
    root.querySelector('[name="meal_type"]').addEventListener("change",()=>{cancel();activate();});
    root.querySelector("fieldset").addEventListener("change",event=>{
      if(active?.root===root && active.busy) cancel();
      if(["excluded_foods","plant_only"].includes(event.target.name)) root.querySelector(".plan-results").replaceChildren();
    });
  }
  function setConsent(s,consent) {
    s.consent=consent;
    s.root.querySelector(".plan-consent").hidden=consent.confirmed;
    s.root.querySelector(".plan-consent-saved").hidden=!consent.confirmed;
    const form=s.root.querySelector("form");
    for(const name of ["adult_general_diet","constraints_reviewed"]){
      form.elements[name].required=!consent.confirmed;
      form.elements[name].checked=false;
    }
    if(consent.profile){
      const p=consent.profile;
      s.root.querySelector(".plan-profile").innerHTML=`<dt>食物禁忌</dt><dd>${escapeHtml(p.food_allergies || "未填写")}</dd><dt>饮食偏好</dt><dd>${escapeHtml(p.preferences || "未填写")}</dd>`;
    }
  }
  function drawIntake(s,value) {
    s.intake=value;
    const host=s.root.querySelector('.plan-intake');
    if(!value){host.innerHTML='<p class="small muted">每日目标参考暂不可用</p>';return;}
    const confirmable=['confirm_records','confirm_empty'].includes(value.status);
    host.innerHTML=`<div class="section-heading"><h4>每日目标参考</h4>${value.record_state?`<button type="button" class="intake-revoke icon-button" title="撤回饮食完整性确认" aria-label="撤回饮食完整性确认">${icon('pencil')}</button>`:''}</div>
      <p class="small">目标 ${value.target_kcal==null?'未设置':escapeHtml(value.target_kcal)+' kcal'} · 已录入 ${escapeHtml(kcalRange(value.recorded_kcal))}</p>
      <p class="small muted">${value.record_count}条饮食 · ${value.estimated_count}条含估算 · ${value.unknown_count}条热量未知</p>
      <p class="small intake-state">${intakeMessages[value.status] || '数值参考暂不可用'}</p>
      ${confirmable?`<p class="small muted">核对范围包含截至现在已吃的食物、饮料和零食；未吃的餐单不算记录。</p><button type="button" class="intake-confirm">${icon('check')}${value.record_count?'截至现在，已吃的都记下了':'确认今天截至现在尚未进食'}</button>`:''}
      ${value.status==='ready'?`<p class="small">全日目标减已吃：${escapeHtml(kcalRange(value.remaining_kcal))}</p><label class="plan-check"><input type="checkbox" name="use_intake" ${s.useIntake===false?'':'checked'}>本次参考已确认的每日目标</label>`:''}
      ${value.confirmed_at?`<p class="small muted">完整性确认 ${escapeHtml(value.confirmed_at)} UTC</p>`:''}
      <p class="small muted">尚未分配各餐热量，不能将全日差额当作这一餐的份量；未知或未确认时仅参考搭配。</p>`;
    host.querySelector('[name="use_intake"]')?.addEventListener('change',event=>{s.useIntake=event.target.checked;drawNutrition(s);});
    host.querySelector('.intake-confirm')?.addEventListener('click',()=>reviewIntake(s,false));
    host.querySelector('.intake-revoke')?.addEventListener('click',()=>reviewIntake(s,true));
  }
  function reviewIntake(s,revoke) {
    if(active!==s || !s.intake)return;
    const value=s.intake;
    const body={day:value.day,version:value.version,...(revoke?{}:{context_hash:value.context_hash,record_state:value.record_count?'complete':'none_yet',confirmed:true})};
    operation(async(session,signal)=>{
      const intake=await api(`/meal-plans/intake-context${revoke?'/revoke':''}`,{method:revoke?'POST':'PUT',signal,body:JSON.stringify(body)});
      const plans=await api(`/meal-plans?day=${value.day}`,{signal});
      return {intake,plans};
    },revoke?'正在撤回核对…':'正在保存核对…',(session,result)=>{
      drawIntake(session,result.intake);draw(session,result.plans);
      session.root.querySelector('.plan-status').textContent=revoke?'已撤回，原饮食记录未改变':'核对已保存，未生成餐单；记录或目标变化后需重新核对';
    });
  }
  async function operation(work, waiting, success, {editing=false,timeoutMs=40000}={}) {
    const s=active;
    if(!s || s.busy) return;
    if(!editing && window.MealPlanActions && !window.MealPlanActions.canLeave(s.root))return;
    const ticket=++s.ticket;s.busy=true;s.controller=new AbortController();controls(s,true);
    const error=s.root.querySelector(".plan-error"),status=s.root.querySelector(".plan-status");
    error.hidden=true;status.textContent=waiting;
    const controller=s.controller, timeout=setTimeout(()=>controller.abort(),timeoutMs);
    try {
      const result=await work(s,s.controller.signal);
      if(!current(s,ticket)) return;
      success(s,result);icons();
    } catch(failure) {
      if(!current(s,ticket)) return;
      error.hidden=false;error.textContent=failure.name==="AbortError" ? "等待超时，未自动重试。可刷新建议记录核对；已提交的 AI 请求仍计入使用次数。" : failure.message;
      status.textContent="未完成本次操作";
    } finally {
      clearTimeout(timeout);
      if(current(s,ticket)){s.busy=false;controls(s,false);}
    }
  }
  function draw(s,plans) {
    s.plans=plans;
    const mealType=s.root.querySelector('[name="meal_type"]')?.value;
    const selectedBinding=binding(s,mealType);
    const matching=plans.filter(plan=>(!mealType || plan.meal_type===mealType) && (!selectedBinding || (plan.coach_id===selectedBinding.coach_id && plan.coach_version===selectedBinding.coach_version)));
    const selected=s.readonly?matching:matching.slice(0,1);
    s.root.querySelectorAll(".plan-meal-tabs button").forEach(button=>{
      const key=button.dataset.meal,bound=binding(s,key);
      const plan=plans.find(item=>item.meal_type===key && item.coach_id===bound?.coach_id && item.coach_version===bound?.coach_version && !item.stale);
      button.setAttribute("aria-pressed",String(key===mealType));
      const status=plan?plan.status==="accepted"?"已采纳":"草稿":"待生成";
      button.innerHTML=`<span>${labels[key]}</span> <span class="small">${status}</span>`;
    });
    s.root.querySelector(".plan-results").innerHTML=selected.length ? selected.map(plan=>{
      const sameConversation=!selectedBinding || (plan.coach_id===selectedBinding.coach_id && plan.coach_version===selectedBinding.coach_version);
      const caption=plan.stale ? "已失效，不再作为当前建议" : plan.status==="draft" ? "待核对草稿" : plan.current ? `已采纳 · 第${plan.version}版` : `历史 · 第${plan.version}版`;
      return `<article class="plan-result" data-plan-id="${escapeHtml(plan.id)}"><div class="section-heading"><h3>${escapeHtml(plan.day)} ${labels[plan.meal_type]}</h3><span class="small ${plan.stale?"nutrition-question":"muted"}">${caption}</span></div>
        <p class="small muted">参考当日${plan.meal_count}条饮食记录 · 已完成训练${plan.completed_minutes}分钟</p>
        ${!sameConversation?'<p class="small muted">其他对话或旧版建议，仅供查看</p>':""}
        ${plan.requested_changes?.length?`<p class="small plan-requested-changes">本次调整：${plan.requested_changes.map(escapeHtml).join("；")}</p>`:""}
        <ul class="plan-portions">${plan.items.map(item=>`<li><div><strong>${escapeHtml(item.name)}</strong><span class="small muted">${escapeHtml(item.basis)}</span></div><b>${item.lower===item.upper?item.lower:`${item.lower}–${item.upper}`} ${escapeHtml(item.unit)}</b></li>`).join("")}</ul>
        <p class="small muted">${escapeHtml(plan.portion_basis)}</p>
        ${window.MealPlanNutrition?.details(plan)||''}
        ${plan.intake_reference?`<details class="plan-intake-reference"><summary>本次使用的目标与已吃范围</summary>${intakeDetails(plan.intake_reference)}</details>`:''}
        ${plan.excluded_foods.length?`<p class="small">已排除：${plan.excluded_foods.map(id=>escapeHtml(s.foods.find(food=>food.id===id)?.name || id)).join("、")}</p>`:""}
        <details class="plan-evidence"><summary>搭配与食材核对依据 · 非个人克数依据</summary>${plan.sources.map(s.reference).join("")}</details>
        <p class="small muted">${plan.portion_source==='user'?'用户调整份量':'AI 建议（阿里云千问）'} · ${escapeHtml(plan.generated_at.replace("T"," ").slice(0,16))} UTC</p>
        ${plan.status==="draft" && !plan.stale && sameConversation && !s.readonly?`<label class="plan-check"><input class="plan-reviewed" type="checkbox">已核对食材、份量和实际配料；采纳不代表已经吃过</label><button type="button" class="primary plan-accept" data-id="${escapeHtml(plan.id)}">${icon("check")}采纳这份建议</button>`:""}
        ${!s.readonly&&sameConversation?window.MealPlanActions?.markup(plan,s.foods)||'':''}
      </article>`;
    }).join("") : "";
    if(s.options.binding) s.root.querySelector("fieldset").hidden=selected.some(plan=>!plan.stale);
    drawNutrition(s);
    if(!s.readonly&&window.MealPlanActions)selected.forEach(plan=>{
      const article=s.root.querySelector(`[data-plan-id="${plan.id}"]`);if(!article?.querySelector('.plan-record-food'))return;
      window.MealPlanActions.mount(article,plan,{
        onSave:(body,clearDirty)=>operation(async(session,signal)=>{
          const updated=await api(`/meal-plans/${plan.id}/portions`,{method:'POST',signal,body:JSON.stringify(body)});
          return [updated,...session.plans.filter(item=>item.id!==updated.id)];
        },'正在保存新份量…',(session,plans)=>{clearDirty();draw(session,plans);session.root.querySelector('.plan-status').textContent='新份量已保存为草稿，营养待重新估算；实际饮食未改变';},{editing:true}),
        onRecord:()=>operation((session,signal)=>api(`/meal-plans/${plan.id}/record-draft`,{method:'POST',signal,body:JSON.stringify({reviewed:true})}),
          '正在打开实际食用核对…',(session,draft)=>{
            if(draft.status==='committed'){notify('这份餐单已有确认的饮食记录，请到饮食记录中核对或修改');return;}
            if(draft.status==='cancelled'){notify('这份餐单的记录草稿已放弃，需要时可手动添加实际食用记录');return;}
            openTextDraft(draft);session.root.querySelector('.plan-status').textContent='请核对实际吃过的食物与份量，确认前不会入账';
          })
      });
    });
    s.root.querySelectorAll(".plan-accept").forEach(button=>button.addEventListener("click",()=>{
      const checked=button.closest("article").querySelector(".plan-reviewed").checked;
      if(!checked){const error=s.root.querySelector(".plan-error");error.hidden=false;error.textContent="请先核对并勾选确认";return;}
      operation(async(session,signal)=>{
        await api(`/meal-plans/${button.dataset.id}/accept`,{method:"POST",signal,body:JSON.stringify({reviewed:true})});
        return api(`/meal-plans?day=${session.root.querySelector('[name="day"]').value}`,{signal});
      },"正在采纳…",(session,result)=>{draw(session,result);session.root.querySelector(".plan-status").textContent="已保存建议版本，实际饮食记录未改变";});
    }));
  }
  function drawNutrition(s) {
    if(s.readonly||!window.MealPlanNutrition)return;
    window.MealPlanNutrition.render(s.root.querySelector('.plan-nutrition-panel'),{
      plans:s.plans||[],mealTypes:s.options.mealTypes||[s.root.querySelector('[name="meal_type"]').value],
      bindingFor:key=>binding(s,key),intake:s.intake,useIntake:s.useIntake!==false,
      enabled:state.nutritionStatus==='configured_unverified',
      onEstimate:items=>operation(async(session,signal)=>{
        try{return await api('/meal-plans/nutrition',{method:'POST',signal,body:JSON.stringify({client_id:crypto.randomUUID(),items})});}
        catch(error){
          if(!signal.aborted&&active===session&&state.user===session.user&&session.root.isConnected){
            try{const plans=await api(`/meal-plans?day=${session.root.querySelector('[name="day"]').value}`,{signal});
              if(!signal.aborted&&active===session&&state.user===session.user&&session.root.isConnected)draw(session,plans);
            }catch(ignored){}
          }
          throw error;
        }
      },'正在估算餐单营养，原餐单保留…',(session,plans)=>{
        draw(session,plans);session.root.querySelector('.plan-status').textContent='餐单估算已保存，实际饮食未改变；未知项不计为零';
      },{timeoutMs:90000})
    });
  }
  function activate() {
    if(!active || active.busy) return;
    if(window.MealPlanActions && !window.MealPlanActions.canLeave(active.root))return;
    active.ready=false;active.intake=null;
    active.root.querySelector(".plan-results").replaceChildren();
    active.root.querySelector('.plan-nutrition-panel')?.replaceChildren();
    operation(async(s,signal)=>{
      const foods=await api("/meal-plans/foods",{signal});
      const plans=await api(`/meal-plans?day=${s.root.querySelector('[name="day"]').value}`,{signal});
      const consent=await api("/meal-plans/consent",{signal});
      const intake=await api(`/meal-plans/intake-context?day=${s.root.querySelector('[name="day"]').value}`,{signal});
      return {foods,plans,consent,intake};
    },"正在读取建议记录…",(s,result)=>{
      s.ready=true;s.foods=result.foods;setConsent(s,result.consent);drawIntake(s,result.intake);draw(s,result.plans);
      s.root.querySelector(".plan-generate").innerHTML=icon("sparkles")+(s.options.mealTypes?"生成"+labels[s.root.querySelector('[name="meal_type"]').value]:"生成下一餐");
      s.root.querySelector(".plan-status").textContent=state.mealPlanStatus==="configured_unverified" ? (s.options.mealTypes?"每次只生成当前餐，份量为估算；尚未分配各餐热量":"单餐份量为估算，尚未分配各餐热量") : "下一餐生成尚未启用，已保存的建议仍可查看";
    });
  }
  function generate() {
    const s=active;
    if(!s || !s.ready || s.busy || state.mealPlanStatus!=="configured_unverified") return;
    if(window.MealPlanActions && !window.MealPlanActions.canLeave(s.root))return;
    const form=s.root.querySelector("form");
    if(!form.reportValidity()) return;
    const body={client_id:crypto.randomUUID(),day:form.elements.day.value,meal_type:form.elements.meal_type.value,
      ...(binding(s,form.elements.meal_type.value) || {})};
    if(s.root.querySelector('[name="use_intake"]')?.checked && s.intake?.status==='ready')body.intake_context_hash=s.intake.context_hash;
    const confirmation=s.consent.confirmed?null:{context_hash:s.consent.context_hash,
      adult_general_diet:form.elements.adult_general_diet.checked,constraints_reviewed:form.elements.constraints_reviewed.checked};
    s.root.querySelector(".plan-results").replaceChildren();
    operation(async(session,signal)=>{
      if(confirmation){
        const consent=await api("/meal-plans/consent",{method:"PUT",signal,body:JSON.stringify(confirmation)});
        if(signal.aborted || active!==session || state.user!==session.user || !session.root.isConnected) throw new DOMException("Aborted","AbortError");
        setConsent(session,consent);
      }
      return api("/meal-plans",{method:"POST",signal,body:JSON.stringify(body)});
    },"正在生成食材与估算份量…",(session,result)=>{
      draw(session,[result,...(session.plans || []).filter(plan=>plan.id!==result.id)]);session.root.querySelector(".plan-status").textContent="本餐草稿已暂存，尚未采纳或记为吃过";
    });
  }
  function history(root,plans,reference) {
    root.innerHTML='<details><summary>历史餐单</summary><div class="plan-results"></div></details>';
    draw({root,foods:[],reference,options:{},readonly:true},plans);
  }
  return {mount,activate,cancel,dispose,history};
})();
