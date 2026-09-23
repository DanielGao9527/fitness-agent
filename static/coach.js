"use strict";

window.CoachView = (() => {
  let active=null;
  const questions=["今天剩下怎么吃？","结合前几天的训练，今天练什么？"];
  const goals={fat_loss:"减脂",muscle_gain:"增肌",maintain:"维持"};
  function closePlan(s) {
    window.MealPlans?.dispose();window.TrainingPlans?.dispose();
    s.root.querySelector(".coach-plan").replaceChildren();
    s.root.querySelectorAll(".coach-inline-plan").forEach(node=>node.replaceChildren());
  }
  function current(s,ticket) {return active===s && s.ticket===ticket && state.user===s.user && s.root.isConnected;}
  function controls(s) {
    s.root.querySelectorAll(".coach-toolbar button,.coach-toolbar select,.coach-toolbar input,.coach-compose button,.coach-compose textarea,.coach-quick button,.coach-open-plan,.coach-review button,.coach-review input,.quick-consent button,.quick-consent input,.coach-recommend,.coach-recommend-training,.coach-meal-selection button,.coach-meal-selection input,.training-recommendation select")
      .forEach(node=>node.disabled=s.busy);
    s.root.querySelector(".coach-stop").hidden=!s.busy;
    s.root.querySelector(".coach-delete").disabled=s.busy || !s.data;
    s.root.querySelector(".coach-day").readOnly=Boolean(s.data);
    s.root.querySelectorAll('.coach-compose button,.coach-compose textarea,.coach-quick button,.coach-understand,.coach-confirm-understanding,.coach-review input,.coach-recommend,.coach-recommend-training,.coach-meal-selection input,.coach-meal-selection button')
      .forEach(node=>node.disabled=s.busy || !s.consent?.confirmed);
  }
  function cancel() {
    const s=active;if(!s || !s.busy) return;
    ++s.ticket;s.controller?.abort();s.busy=false;controls(s);
    s.root.querySelector(".coach-status").textContent="已停止等待；提交可能已保存，请刷新对话核对。";
  }
  function renderTraining(value, latest) {
    if(value.stale && latest)return '<p class="training-stale">档案、实际训练或参考资料已变化，旧建议不再适用。</p>';
    const history=value.history.completed.slice(0,8), cardio=value.weekly_cardio;
    const content=`<section class="training-recommendation" aria-label="训练建议"><h3>${escapeHtml(value.title)}</h3><p>${escapeHtml(value.message)}</p>
      ${history.length?`<details class="training-records"><summary>近七天实际记录 · ${value.history.completed.length}项</summary><p class="small muted">${history.map(item=>`${escapeHtml(item.day.slice(5))} ${escapeHtml(item.name)} ${item.minutes}分钟`).join('；')}${value.history.completed.length>8?'；另有更多记录参与核对':''}</p></details>`:'<p class="small muted">近七天暂无已保存的完成记录，不代表没有训练。</p>'}
      ${value.structure?.reason?`<p class="small">${escapeHtml(value.structure.title)} · ${escapeHtml(value.structure.reason)}</p>`:''}
      ${value.equipment_note?`<p class="small training-gap">${escapeHtml(value.equipment_note)}</p>`:''}
      ${value.exercises.length?`<ol class="training-exercises">${value.exercises.map(item=>window.TrainingProgram.exercise(item)).join('')}</ol>`:''}
      ${value.gaps?.length?`<p class="small training-gap">本次安排${value.exercises.length}个动作；受可用时间、已确认器械或近期负荷限制，没有加入其他方向。</p>`:''}
      ${value.aerobic_options.length?`<h4>${value.aerobic_is_alternative?'也可以改选有氧（替代上面的力量，选一项）':'有氧项目（选一项）'}</h4><ul class="training-aerobic">${value.aerobic_options.map(item=>`<li><strong>${escapeHtml(item.name)} ${item.minutes}分钟</strong><p class="small">${escapeHtml(item.preparation)}，本次共约${item.total_minutes}分钟。</p><p class="small muted">${escapeHtml(item.condition)}</p></li>`).join('')}</ul>`:''}
      ${cardio?`<p class="small muted">本周已记录有氧${cardio.cardio_days}天、${cardio.cardio_minutes}分钟；强度和记录完整性未确认，不据此判定每周达标。</p>`:''}
      <p class="small muted">仅为今天的建议，未写入训练记录；不要求力竭，有不适时停止。</p>
      ${(value.warnings || []).length?`<details class="training-notes"><summary>安排说明</summary>${value.warnings.map(text=>`<p class="small muted">${escapeHtml(text)}</p>`).join('')}</details>`:''}
      ${value.sources.length?`<details class="training-sources"><summary>参考依据</summary><ul>${value.sources.map(source=>`<li>${source.origin==='curated_text'||!source.url?`${escapeHtml(source.title)} · 训练参考笔记，作者归属未核实`:`<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.publisher)} · ${escapeHtml(source.title)}</a>`}</li>`).join('')}</ul><p class="small muted">动作依据下列资料整理；组次与时间为一般健身建议，可按实际能力调整，不是恢复判断或个体处方。</p></details>`:''}</section>`;
    return latest?content:`<details class="training-history"><summary>当时的训练建议（不代表实际完成）</summary>${content}</details>`;
  }
  function dispose() {const s=active;cancel();if(s)closePlan(s);active=null;}
  function replyText(data,turn) {
    const response=turn.response,request=response.quick_request;
    if(request?.status==='failed' && request.error)return `本次餐单未完整生成。${apiError(request.error).message}`;
    if(response.constraints?.diet_caution)return response.text;
    if(response.intent==='meal' && !request && response.status!=='needs_review' && !response.meal_context?.selection_required && !response.meal_context?.target_required){
      const latest=turn.version===data.version,calculation=latest?data.meal_calculation:response.meal_readiness;
      if(calculation?.ready===false)return `这次还没有生成餐单。${calculation.message}`;
      return latest?'已收到餐次请求，餐单尚未生成。':'这条请求没有生成餐单。需要时请重新提出本次餐次要求，按最新记录核对。';
    }
    return response.text;
  }
  async function operation(work,success) {
    if(window.MealPlanActions && !window.MealPlanActions.canLeave())return;
    const s=active;if(!s || s.busy)return;
    const ticket=++s.ticket;s.busy=true;s.controller=new AbortController();controls(s);
    const error=s.root.querySelector(".coach-error"),status=s.root.querySelector(".coach-status");
    error.hidden=true;status.textContent="正在读取与保存对话…";
    const controller=s.controller,timeout=setTimeout(()=>controller.abort(),150000);
    try {
      const result=await work(s,controller.signal);
      if(!current(s,ticket))return;
      success(s,result);status.textContent="对话已保存 · 建议请结合实际情况核对";icons();
    } catch(failure) {
      if(!current(s,ticket))return;
      error.textContent=failure.name==="AbortError"?"等待超时，未自动重试。输入已保留，请刷新对话核对。":failure.message;
      error.hidden=false;status.textContent="操作未确认完成";
    } finally {clearTimeout(timeout);if(current(s,ticket)){s.busy=false;controls(s);}}
  }
  function draw(s) {
    closePlan(s);
    const data=s.data;
    if(data?.meal_consent)s.consent=data.meal_consent;
    const gate=s.root.querySelector('.coach-consent');
    gate.innerHTML=s.consent && !s.consent.confirmed?window.QuickMeals.consent({meal_consent:s.consent}):'';
    gate.querySelector('form')?.addEventListener('submit',event=>{
      event.preventDefault();if(!event.target.reportValidity())return;
      operation(async(session,signal)=>{
        await api('/meal-plans/consent',{method:'PUT',signal,body:JSON.stringify({context_hash:session.consent.context_hash,adult_general_diet:true,constraints_reviewed:true})});
        const consent=await api('/meal-plans/consent',{signal});
        const refreshed=session.data?await api(`/coach/conversations/${session.data.id}`,{signal}):null;
        return {consent,data:refreshed};
      },(session,result)=>{session.consent=result.consent;session.data=result.data;draw(session);});
    });
    const select=s.root.querySelector(".coach-history");
    select.innerHTML='<option value="">新对话</option>'+s.list.map(item=>`<option value="${escapeHtml(item.id)}">${escapeHtml(item.day)} · ${escapeHtml(item.title)}</option>`).join("");
    select.value=data?.id || "";
    s.root.querySelector(".coach-day").value=data?.day || s.day;
    const feed=s.root.querySelector(".coach-messages");
    feed.innerHTML=data?.turns.length?data.turns.map(turn=>`<article class="coach-turn" data-version="${turn.version}"><h3>你</h3><p>${escapeHtml(turn.message)}</p><h3>助手</h3>${turn.response.training_recommendation?'':`<p>${escapeHtml(replyText(data,turn))}</p>`}<div class="coach-meal-history"></div></article>`).join(""):'<p class="coach-empty">今天想安排什么？</p>';
    const context=s.root.querySelector(".coach-context");
    if(data) {
      const c=data.context,p=c.profile;
      state.profile={...state.profile,...p};
      context.innerHTML=`<details><summary>本次参考记录 · ${escapeHtml(c.day)}</summary><dl class="plan-profile"><dt>目标</dt><dd>${goals[p.goal]}</dd><dt>食物禁忌</dt><dd>${escapeHtml(p.food_allergies || "未填写")}</dd><dt>饮食偏好</dt><dd>${escapeHtml(p.preferences || "未填写")}</dd></dl><p class="small">当天${c.meal_count}条饮食 · 完成训练${c.completed_minutes_today}分钟</p><p class="small muted">${escapeHtml(c.window_start)} 至 ${escapeHtml(c.day)} · ${c.recent_completed_count}项已完成训练。未记录不代表没训练，不据此判断恢复。</p>
        <ul class="coach-facts">${c.meals.map(item=>`<li>${escapeHtml(meals[item.meal_type] || item.meal_type)} · ${escapeHtml(item.name)} · ${escapeHtml(item.grams==null?item.amount_description:`${item.grams}g`)}</li>`).join("")}${c.recent_workouts.map(item=>`<li>${escapeHtml(item.day)} · ${escapeHtml(item.name)} · ${item.minutes}分钟</li>`).join("")}</ul>${c.truncated?'<p class="small muted">记录较多，仅展示前100项；并非完整明细。</p>':""}</details>`;
      const limits=data.constraints || {};
      if(limits.food_avoid?.length || limits.preferences?.length || limits.training_caution || limits.diet_caution){
        const names={rice:"米饭","brown-rice":"糙米饭",oats:"燕麦",potato:"土豆",corn:"玉米","sweet-potato":"红薯",chicken:"鸡肉",beef:"牛肉",shrimp:"虾",egg:"鸡蛋",tofu:"豆腐",broccoli:"西兰花",carrot:"胡萝卜",spinach:"菠菜"};
        context.insertAdjacentHTML("beforeend",`<p class="small coach-limits">本对话临时限制：${[
          limits.food_avoid?.length?`避开${limits.food_avoid.map(key=>names[key] || key).map(escapeHtml).join("、")}`:"",
          limits.preferences?.length?`偏好${limits.preferences.map(escapeHtml).join("、")}`:"",
          limits.training_caution?"本次训练请求超出一般健身适用范围":"",
          limits.diet_caution?"特殊健康注意事项仍保留，一般建议暂停":""
        ].filter(Boolean).join("；")}</p>`);
      }
    } else context.replaceChildren();
    const action=s.root.querySelector(".coach-action");
    action.replaceChildren();
    feed.querySelectorAll(".coach-turn").forEach(turn=>{
      const version=Number(turn.dataset.version),root=turn.querySelector(".coach-meal-history");
      if(data.action==="meal" && version===data.version && !data.pending && data.meal_question){
        const names={breakfast:"早餐",lunch:"午餐",dinner:"晚餐"};
        root.innerHTML=data.meal_context.selection_required?`<form class="coach-meal-selection"><fieldset><legend>安排哪几餐</legend>${Object.values(names).map(name=>`<label class="plan-check"><input type="checkbox" value="${name}">${name}</label>`).join("")}<button type="submit" class="primary">${icon("check")}确认餐次</button></fieldset></form>`:"";
        root.querySelector("form")?.addEventListener("submit",event=>{
          event.preventDefault();const selected=[...root.querySelectorAll("input:checked")].map(node=>node.value);
          if(!selected.length){s.root.querySelector(".coach-error").hidden=false;s.root.querySelector(".coach-error").textContent="请至少选择一餐";return;}
          s.root.querySelector(".coach-input").value="安排"+selected.join("和");send();
        });
        if(data.meal_context.target_required && data.meal_context.pending_command){
          root.innerHTML=Object.entries(names).filter(([key])=>data.meal_context.meal_types.includes(key)).map(([key,name])=>`<button type="button" data-meal="${key}">${name}</button>`).join(" ");
          root.querySelectorAll("button").forEach(button=>button.addEventListener("click",()=>{s.root.querySelector(".coach-input").value=names[button.dataset.meal]+"："+data.meal_context.pending_command;send();}));
        }
      } else if(data.action==="meal" && version===data.version && !data.pending){
        root.classList.add("coach-inline-plan");
        const plans=window.QuickMeals.selected(data),count=data.meal_context?.meal_types?.length || 1;
        root.innerHTML=window.QuickMeals.render(plans,{complete:plans.length===count,current:true});
        const calculation=data.meal_calculation;
        if(calculation && !calculation.ready){
          root.insertAdjacentHTML("beforeend",`<div class="coach-calculation"><p>${escapeHtml(calculation.message)}</p>${calculation.reason==='target_required' || calculation.reason==='macro_required'?`<button type="button" data-view="profile">${icon('target')}设置长期营养标准</button> <button type="button" data-view="today">核对本日目标</button>`:calculation.reason==='confirm_empty'?`<button type="button" class="coach-empty-intake">${icon('check')}今天还没吃东西</button> <button type="button" data-view="meals">补录已吃内容</button>`:calculation.reason==='unknown_intake'?`<button type="button" data-view="meals">核对饮食记录</button>`:calculation.reason==='target_reached'?`<button type="button" data-view="today">核对本日目标与摄入</button>`:''}</div>`);
          root.querySelector('.coach-empty-intake')?.addEventListener('click',()=>{
            const schedule=data.meal_context.meal_types || [data.meal_context.meal_type || 'dinner'];
            s.root.querySelector('.coach-input').value='今天还没吃东西，安排'+schedule.map(key=>meals[key]).join('和');send();
          });
          if(calculation.reason==='macro_balance')root.querySelector('.coach-calculation').insertAdjacentHTML('beforeend','<button type="button" data-view="meals">核对饮食记录</button>');
        }else if(data.meal_consent.confirmed && (!plans.length || plans.some(plan=>plan.stale) || data.turns.at(-1).response.quick_request?.status!=="ready")){
          const request=data.turns.at(-1).response.quick_request;
          root.insertAdjacentHTML("beforeend",`<p class="small muted">${escapeHtml(request?.error ? apiError(request.error).message : request?.status==="generating"?"餐单正在生成，稍后刷新查看。":"餐单尚未完成。")}</p><button type="button" class="coach-recommend">${icon("refresh-cw")}重新推荐</button>`);
          root.querySelector(".coach-recommend").addEventListener("click",()=>operation((session,signal)=>recommend(session,data,signal,crypto.randomUUID()),showRecommendation));
        }
      } else {
        const plans=(data.meal_plans || []).filter(plan=>plan.coach_version===version);
        if(plans.length)root.innerHTML=window.QuickMeals.render(plans);
      }
      const training=document.createElement("div");turn.append(training);
      const recommendation=data.turns.find(item=>item.version===version)?.response.training_recommendation;
      if(recommendation)training.innerHTML=renderTraining(recommendation,version===data.version);
      if(version===data.version && data.intent==="training"){
        if(!data.pending && (!recommendation || recommendation.stale)){
          training.insertAdjacentHTML('beforeend',`<button type="button" class="coach-recommend-training">${icon('refresh-cw')}${recommendation?'按最新记录重新建议':'生成训练建议'}</button>`);
          training.querySelector('button').addEventListener('click',()=>operation((session,signal)=>recommend(session,data,signal,crypto.randomUUID()),showRecommendation));
        }
      }
    });
    drawReview(s);feed.scrollTop=feed.scrollHeight;controls(s);icons();
  }
  function drawReview(s) {
    const root=s.root.querySelector(".coach-review"),data=s.data,review=data?.review;
    root.replaceChildren();
    if(!data?.pending)return;
    const enabled=data.understanding_status==="configured_unverified";
    const scopes={meal:"餐次安排 / 食材调整",aerobic:"基础有氧",strength:"力量训练需求",other:"其他请求（尚未开放）",unclear:"需要补充"};
    if(review && !review.stale && ["ready","needs_input"].includes(review.status)){
      root.innerHTML=`<h3>核对这次理解</h3><p>${escapeHtml(scopes[review.scope])}${review.command?` · ${escapeHtml(review.command)}`:""}</p>
        ${review.meal_types?.length?`<p>餐次：${review.meal_types.map(key=>({breakfast:"早餐",lunch:"午餐",dinner:"晚餐"}[key])).join("、")}</p>`:""}
        ${review.commands?.length?`<h4>本餐修改</h4><ul class="coach-review-changes">${review.commands.map(command=>`<li>${escapeHtml(command)}</li>`).join("")}</ul>`:""}
        ${review.meal_changes?.length?`<h4>分别修改</h4><ul class="coach-review-changes">${review.meal_changes.map(change=>`<li>${({breakfast:'早餐',lunch:'午餐',dinner:'晚餐'})[change.meal_type]}：${change.commands.map(escapeHtml).join('；')}</li>`).join('')}</ul>`:''}
        ${window.TrainingPlans.summary(review.training)}
        ${review.training?.replacements?`<ul class="coach-review-changes">${Object.entries(review.training.replacements).map(([from,to])=>`<li>动作替换：${escapeHtml(review.replacement_names?.[from] || from)} → ${escapeHtml(review.replacement_names?.[to] || to)}</li>`).join('')}</ul>`:''}
        <ol class="coach-review-notes">${review.notes.map(note=>`<li><blockquote>${escapeHtml(note.message)}</blockquote><ul>
          ${note.avoid_names.length?`<li>避开：${note.avoid_names.map(escapeHtml).join("、")}</li>`:""}
          ${note.preferences.length?`<li>偏好：${note.preferences.map(escapeHtml).join("、")}</li>`:""}
          ${note.training_caution?"<li>本次训练请求超出一般健身适用范围</li>":""}${note.diet_caution?"<li>涉及特殊健康管理，一般建议暂停</li>":""}
          ${note.unresolved?"<li>还有未明确的限制</li>":""}${!note.avoid_names.length&&!note.preferences.length&&!note.training_caution&&!note.diet_caution&&!note.unresolved?"<li>未提取到额外限制，请核对是否遗漏</li>":""}
        </ul></li>`).join("")}</ol><p class="small muted">仅本对话生效，不修改长期档案</p>
        ${review.status==="ready"?`<label class="plan-check"><input type="checkbox" class="coach-review-checked">以上请求与限制完整、准确</label><button class="primary coach-confirm-understanding" type="button">${icon("check")}确认这次理解</button>`:`<p class="nutrition-question">${escapeHtml(review.question)}</p>`}`;
    } else {
      root.innerHTML=`<p class="small muted">${escapeHtml(review?.stale?"对话或档案已变化，旧理解不能沿用。":review?.error ? apiError({code:review.code,message:review.error}).message : review?.status==="generating"?"理解请求尚未完成，可刷新查看。":"补充尚待理解与核对。")}</p>`;
    }
    if(enabled)root.insertAdjacentHTML("beforeend",`<button type="button" class="coach-understand">${icon("sparkles")}${review?"重新理解":"理解待处理补充"}</button>`);
    else root.insertAdjacentHTML("beforeend",'<p class="small muted">自由描述理解尚未启用</p>');
    root.querySelector(".coach-understand")?.addEventListener("click",()=>understand(s));
    root.querySelector(".coach-confirm-understanding")?.addEventListener("click",()=>{
      if(!root.querySelector(".coach-review-checked").checked){s.root.querySelector(".coach-error").hidden=false;s.root.querySelector(".coach-error").textContent="请先核对本次理解是否完整";return;}
      operation(async(session,signal)=>recommend(session,await api(`/coach/conversations/${data.id}/confirm-understanding`,{method:"POST",signal,body:JSON.stringify({version:data.version,review_id:review.id,reviewed:true})}),signal,crypto.randomUUID()),showRecommendation);
    });
  }
  function understand(s) {
    if(active!==s || s.busy || !s.data?.pending)return;
    const id=s.data.id,version=s.data.version,client_id=crypto.randomUUID();
    operation(async(session,signal)=>{
      try{return await recommend(session,await api(`/coach/conversations/${id}/understand`,{method:"POST",signal,body:JSON.stringify({version,client_id,auto_apply:Boolean(session.consent?.confirmed)})}),signal,client_id);}
      catch(error){if(signal.aborted)throw error;return {data:await api(`/coach/conversations/${id}`,{signal}),error:error.message};}
    },(session,result)=>{session.data=result.data;draw(session);if(result.error){session.root.querySelector(".coach-error").textContent=result.error;session.root.querySelector(".coach-error").hidden=false;}});
  }
  async function recommend(session,data,signal,client_id) {
    if(data.intent==='training' && !data.pending){
      if(signal.aborted || active!==session || state.user!==session.user || !session.root.isConnected)throw new DOMException('Aborted','AbortError');
      session.root.querySelector('.coach-status').textContent='正在核对实际训练与可用动作…';
      try{return {data:await api(`/coach/conversations/${data.id}/recommend-training`,{method:'POST',signal,body:JSON.stringify({version:data.version,client_id})})};}
      catch(error){if(signal.aborted)throw error;return {data:await api(`/coach/conversations/${data.id}`,{signal}),error:error.message};}
    }
    if(!window.QuickMeals.eligible(data) || !data.meal_consent.confirmed || data.meal_calculation?.ready===false)return {data};
    if(signal.aborted || active!==session || state.user!==session.user || !session.root.isConnected)throw new DOMException("Aborted","AbortError");
    session.root.querySelector(".coach-status").textContent="正在安排食材并核算份量…";
    try{return {data:await api(`/coach/conversations/${data.id}/recommend-meals`,{method:"POST",signal,body:JSON.stringify({version:data.version,client_id})})};}
    catch(error){if(signal.aborted)throw error;return {data:await api(`/coach/conversations/${data.id}`,{signal}),error:error.message};}
  }
  function showRecommendation(session,result) {
    session.data=result.data;draw(session);
    if(result.error){session.root.querySelector(".coach-error").textContent=result.error;session.root.querySelector(".coach-error").hidden=false;}
  }
  function reset(s) {
    s.data=null;s.day=state.day;s.createId=crypto.randomUUID();s.pendingSend=null;
    s.root.querySelector(".coach-input").value="";draw(s);
  }
  function safeToLeave(s) {
    const limits=s.data?.constraints || {};
    return !(s.data?.pending || limits.food_avoid?.length || limits.preferences?.length || limits.training_caution || limits.diet_caution || s.root.querySelector(".coach-input").value.trim()) || window.confirm("当前有临时限制、待处理补充或未发送文字。新对话不会继承这些内容；长期食物过敏请先保存到个人档案。仍要切换吗？");
  }
  function reload(selected) {
    operation(async(s,signal)=>{
      const list=await api("/coach/conversations",{signal});
      const consent=await api('/meal-plans/consent',{signal});
      const id=selected===undefined?(s.data?.id || list.find(item=>item.day===s.day)?.id):selected;
      const data=id?await api(`/coach/conversations/${id}`,{signal}):null;
      return {list,data,consent};
    },(s,result)=>{s.list=result.list;s.data=result.data;s.consent=result.consent;draw(s);});
  }
  function send() {
    const s=active;if(!s || s.busy || !s.consent?.confirmed)return;
    const input=s.root.querySelector(".coach-input");
    const message=input.value.trim();if(!message || !s.root.querySelector(".coach-compose").reportValidity())return;
    const day=s.root.querySelector(".coach-day").value;
    if(!day){s.root.querySelector(".coach-day").reportValidity();return;}
    if(s.pendingSend?.message!==message || s.pendingSend?.version!==(s.data?.version || 0))
      s.pendingSend={message,version:s.data?.version || 0,client_id:crypto.randomUUID()};
    const body={...s.pendingSend},createId=s.createId,conversation=s.data?.id;
    operation(async(session,signal)=>{
      const consent=await api('/meal-plans/consent',{signal});
      if(!consent.confirmed)return {needsConsent:true,consent};
      const created=conversation?{id:conversation}:await api("/coach/conversations",{method:"POST",signal,body:JSON.stringify({client_id:createId,day})});
      const data=await api(`/coach/conversations/${created.id}/messages`,{method:"POST",signal,body:JSON.stringify(body)});
      let updated=data,error=null;
      if(data.pending && data.understanding_status==="configured_unverified"){
        if(signal.aborted || active!==session || state.user!==session.user || !session.root.isConnected)throw new DOMException("Aborted","AbortError");
        session.root.querySelector(".coach-status").textContent="正在理解你的请求与补充…";
        try{updated=await api(`/coach/conversations/${data.id}/understand`,{method:"POST",signal,body:JSON.stringify({version:data.version,client_id:body.client_id,auto_apply:true})});}
        catch(failure){if(signal.aborted)throw failure;error=failure.message;updated=await api(`/coach/conversations/${data.id}`,{signal});}
      }
      if(!error){const suggested=await recommend(session,updated,signal,body.client_id);updated=suggested.data;error=suggested.error;}
      return {data:updated,error,list:await api("/coach/conversations",{signal})};
    },(session,result)=>{if(result.needsConsent){session.consent=result.consent;if(session.data)session.data.meal_consent=result.consent;draw(session);return;}session.data=result.data;session.list=result.list;session.pendingSend=null;input.value="";draw(session);if(result.error){session.root.querySelector(".coach-error").textContent=result.error;session.root.querySelector(".coach-error").hidden=false;}});
  }
  function mount(root) {
    dispose();
    root.innerHTML=`<section class="coach-view" aria-label="个人助手"><div class="coach-toolbar"><label>对话<select class="coach-history" aria-label="历史对话"><option value="">新对话</option></select></label><label>记录日期<input type="date" class="coach-day" value="${escapeHtml(state.day)}" required></label><div class="coach-tools"><button type="button" class="icon-button coach-new" title="新对话" aria-label="新对话">${icon("plus")}</button><button type="button" class="icon-button coach-refresh" title="刷新对话" aria-label="刷新对话">${icon("refresh-cw")}</button><button type="button" class="icon-button coach-delete" title="删除对话" aria-label="删除对话">${icon("trash-2")}</button></div></div>
      <div class="coach-context"></div><div class="coach-consent"></div><div class="coach-messages" role="log" aria-label="对话消息" aria-live="polite"></div><section class="coach-review" aria-live="polite"></section><div class="coach-action"></div><div class="coach-plan"></div>
      <p class="small muted">仅适用于无伤病的一般成人健身，不提供伤病或康复训练方案。</p>
      <div class="coach-quick">${questions.map((question,index)=>`<button type="button" data-question="${index}">${escapeHtml(question)}</button>`).join("")}</div>
      <form class="coach-compose"><label for="coach-message" class="sr-only">给助手的消息</label><textarea id="coach-message" class="coach-input" rows="2" maxlength="2000" placeholder="你想安排什么？" required></textarea><button type="submit" class="primary icon-button" title="发送消息" aria-label="发送消息">${icon("send")}</button></form>
      ${state.coachStatus==="configured_unverified"?'<p class="coach-privacy small muted">AI 助手会结合档案和近期记录提供建议；相关内容交由阿里云处理，并计入 AI 使用次数。建议不会自动写入实际饮食或训练记录。</p>':""}
      <button type="button" class="coach-stop" hidden>${icon("square")}停止等待</button><p class="coach-status small muted" role="status"></p><p class="coach-error form-error" role="alert" hidden></p></section>`;
    active={root,user:state.user,ticket:0,busy:false,data:null,list:[],day:state.day,createId:crypto.randomUUID(),pendingSend:null};
    const s=active;
    root.querySelector(".coach-compose").addEventListener("submit",event=>{event.preventDefault();send();});
    root.querySelectorAll("[data-question]").forEach(button=>button.addEventListener("click",()=>{root.querySelector(".coach-input").value=questions[Number(button.dataset.question)];root.querySelector(".coach-input").focus();}));
    root.querySelector(".coach-stop").addEventListener("click",cancel);
    root.querySelector(".coach-day").addEventListener("change",()=>{if(!s.data){s.day=root.querySelector(".coach-day").value;s.createId=crypto.randomUUID();s.pendingSend=null;}});
    root.querySelector(".coach-refresh").addEventListener("click",()=>reload());
    root.querySelector(".coach-new").addEventListener("click",()=>{if(safeToLeave(s))reset(s);});
    root.querySelector(".coach-history").addEventListener("change",event=>{
      const id=event.target.value;
      if(!safeToLeave(s)){event.target.value=s.data?.id || "";return;}
      closePlan(s);if(id)reload(id);else reset(s);
    });
    root.querySelector(".coach-delete").addEventListener("click",()=>{
      if(!s.data || !window.confirm("删除这段对话？关联建议将失效，实际饮食、训练记录不会删除。长期限制请先保存在个人档案。"))return;
      closePlan(s);
      operation(async(session,signal)=>{
        await api(`/coach/conversations/${session.data.id}`,{method:"DELETE",signal,body:JSON.stringify({version:session.data.version})});
        return api("/coach/conversations",{signal});
      },(session,list)=>{session.list=list;reset(session);});
    });
    reload();icons();
  }
  return {mount,dispose};
})();
