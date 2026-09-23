"use strict";

window.NutritionTargets = (() => {
  let active=null;
  const goals={maintain:"维持",fat_loss:"减脂",muscle_gain:"增肌"};
  const activity={inactive:"日常活动为主",low_active:"较活跃",active:"活跃",very_active:"非常活跃"};
  const statuses={unset:"尚未设置",paused:"已停用",needs_review:"档案已变化，请复核",needs_input:"需要补充资料",out_of_scope:"特殊健康情况，暂停数值建议",review_due:"目标已到复核日期"};
  const names={kcal:"热量",protein:"蛋白质",carbs:"碳水",fat:"脂肪"};
  const range=(value,key)=>window.QuickMeals.range(value,key);
  function dispose(){active?.controller?.abort();active=null;}
  function canLeave(){return !active?.dirty || window.confirm("营养目标尚未保存，离开并放弃这次输入？");}
  function current(s,ticket){return active===s && s.ticket===ticket && state.user===s.user && s.root.isConnected;}
  function calculation(value){
    if(!value)return "";
    const base=value.maintenance?.kcal,delta=base==null?null:value.kcal-base;
    return `<p class="target-equation">${base==null?"自定义固定标准":`维持参考 ${base} ${delta<0?"−":"+"} ${Math.abs(delta)} =`} <strong>${value.kcal} kcal / 天</strong></p>
      <p class="small muted">${value.mode==="fixed"?"固定值优先，不随健身目标自动改变。":`${value.mode==='day'?'本日自定义':goals[value.goal]} · 相对维持参考${delta<0?"缺口":delta>0?"盈余":"持平"}${delta?` ${Math.abs(delta)} kcal`:""}，不是实测能量平衡。`}</p>`;
  }
  function dailyComparison(info){
    if(!info?.macro?.ranges)return `<p class="small muted">${escapeHtml(info?.message || "营养参考尚未就绪")}</p>`;
    const limits={kcal:{lower:info.target_kcal,upper:info.target_kcal},...info.macro.ranges};
    return `<div class="nutrition-comparison"><table><caption>已录入与全天参考</caption><thead><tr><th>营养</th><th>全天参考</th><th>已录入</th><th>对照</th></tr></thead><tbody>${Object.keys(names).map(key=>`<tr><th scope="row">${names[key]}</th><td>${range(limits[key],key)}</td><td>${range(info.recorded[key],key)}</td><td>${window.QuickMeals.assessment(info.recorded[key],limits[key],key)}</td></tr>`).join("")}</tbody></table></div>
      <p class="small muted">${escapeHtml(info.macro.note)} 只比较已录入内容，未知不算0；不包含建议餐单和未记录饮食，不把训练总消耗直接叠加到活动水平上。</p>`;
  }
  function skeleton(s){
    s.root.innerHTML=`<div class="section-heading"><h2>${icon("target")}${s.mode==="standard"?"长期营养标准":"本日营养目标"}</h2><div class="target-actions"><button type="button" class="icon-button nutrition-edit" title="${s.mode==="standard"?"修改长期标准":"仅调整本日"}" aria-label="${s.mode==="standard"?"修改长期标准":"仅调整本日"}">${icon("pencil")}</button><button type="button" class="icon-button nutrition-refresh" title="刷新营养目标" aria-label="刷新营养目标">${icon("refresh-cw")}</button></div></div><div class="nutrition-current"></div><div class="nutrition-editor"></div><p class="nutrition-status small" role="status"></p><p class="nutrition-error form-error" role="alert" hidden></p>`;
    s.root.querySelector('.nutrition-edit').onclick=()=>{if(canLeave())edit(s);};
    s.root.querySelector('.nutrition-refresh').onclick=()=>{if(canLeave()){s.dirty=false;load(s);}};
    icons();
  }
  async function operation(s,work,done){
    if(active!==s || s.busy)return;
    const ticket=++s.ticket;s.busy=true;s.controller=new AbortController();
    const timer=setTimeout(()=>s.controller.abort(),15000),disabled=new Map();
    s.root.querySelectorAll('button,fieldset').forEach(node=>{disabled.set(node,node.disabled);node.disabled=true;});
    s.root.querySelector('.nutrition-error').hidden=true;s.root.querySelector('.nutrition-status').textContent="正在核对…";
    try{const data=await work(s.controller.signal);if(current(s,ticket))done(data);}
    catch(error){if(current(s,ticket)){s.root.querySelector('.nutrition-error').textContent=error.name==='AbortError'?"等待超时，输入保留；请刷新核对是否已保存，再操作。":error.message;s.root.querySelector('.nutrition-error').hidden=false;}}
    finally{clearTimeout(timer);if(current(s,ticket)){s.busy=false;for(const [node,value] of disabled)if(node.isConnected)node.disabled=value;s.root.querySelector('.nutrition-status').textContent="";icons();}}
  }
  function draw(s,data){
    s.data=data;s.dirty=false;
    const t=data.intake_target,base=t.baseline,saved=t.target,info=data.meal_calculation;
    const host=s.root.querySelector('.nutrition-current');
    if(s.mode==='standard'){
      host.innerHTML=base?.standard?calculation(base.calculation):`<p>${base?.kcal?`原有每日目标 ${base.kcal} kcal`:'尚未设置长期标准'}</p>`;
      if(base?.error)host.insertAdjacentHTML('beforeend',`<p class="form-error">${escapeHtml(base.error)}</p>`);
      if(t.status==='out_of_scope')host.insertAdjacentHTML('beforeend',`<p class="form-error">${statuses.out_of_scope}</p>`);
      host.insertAdjacentHTML('beforeend',`<p class="small muted">${base?.effective_from?`${escapeHtml(base.effective_from)} 起 · `:''}长期标准；本日临时调整不改变这里的设置。</p>`);
      if(!base?.standard)host.insertAdjacentHTML('beforeend',`<button type="button" class="nutrition-setup">${icon('calculator')}设置长期标准</button>`);
    }else{
      const equation=t.day_override && base?.calculation?.maintenance?{...base.calculation,mode:'day',kcal:saved.kcal}:saved?.calculation;
      host.innerHTML=`<div class="target-values"><div><span>${t.day_override?'仅本日覆盖':t.energy_feedback?.adjustment_kcal?'标准加近期调整':'沿用每日标准'} · ${escapeHtml(s.day)}</span><strong>${t.status==='active'?saved.kcal:'未就绪'}<small>kcal / 天</small></strong></div><div><span>长期标准</span><strong>${base?.kcal ?? '未设置'}<small>kcal / 天</small></strong></div></div>${calculation(equation)}<p class="small muted">${escapeHtml(window.QuickMeals.feedbackText(info))}</p>
        ${t.status!=='active'?`<p class="form-error">${escapeHtml(t.error || statuses[t.status] || '目标未就绪')}</p>`:''}
        ${dailyComparison(info)}<div class="form-actions"><button type="button" data-view="profile">${icon('user')}长期标准</button>${t.day_override?`<button type="button" class="nutrition-restore">${icon('rotate-ccw')}恢复长期标准</button>`:''}</div>`;
      host.querySelector('.nutrition-restore')?.addEventListener('click',()=>{
        if(!window.confirm(`取消 ${s.day} 的临时目标，恢复该日长期标准？`))return;
        saveDay(s,null);
      });
    }
    host.querySelector('.nutrition-setup')?.addEventListener('click',()=>edit(s));
    s.root.querySelector('.nutrition-editor').replaceChildren();icons();
  }
  function load(s){operation(s,signal=>api(`/summary?day=${s.day}`,{signal}),data=>draw(s,data));}
  function saveDay(s,kcal){
    const target=s.data.intake_target;
    const body={day:s.day,kcal,version:target.version,context_hash:target.context_hash,client_id:s.saveId || crypto.randomUUID(),confirmed:true,general_adult:true};
    s.saveId=body.client_id;
    operation(s,async signal=>{await api('/nutrition-target/day',{method:'POST',signal,body:JSON.stringify(body)});return api(`/summary?day=${s.day}`,{signal});},data=>{s.saveId=null;draw(s,data);notify('本日目标已更新，其他日期不变');});
  }
  function edit(s){
    if(s.busy || !s.data)return;
    const t=s.data.intake_target,host=s.root.querySelector('.nutrition-editor');s.saveId=null;
    if(s.mode==='day'){
      host.innerHTML=`<form class="nutrition-day-form"><fieldset><h3>仅调整 ${escapeHtml(s.day)}</h3><label>本日热量（kcal）<input type="number" name="kcal" min="1201" max="5000" step="1" value="${t.target?.kcal || ''}" required></label><p class="small muted">仅这个日期生效，下一日恢复长期标准；1 kcal（千卡）= 4.184 kJ（千焦）。</p><label class="plan-check"><input type="checkbox" name="confirmed" required>我已核对本日目标，属于一般健康成人，不涉及孕哺期或医疗饮食管理</label><div class="dialog-actions"><button type="button" class="nutrition-cancel">取消</button><button type="submit" class="primary">${icon('save')}保存本日目标</button></div></fieldset></form>`;
      const form=host.querySelector('form');form.oninput=()=>{s.dirty=true;s.saveId=null;};
      form.onsubmit=event=>{event.preventDefault();if(form.reportValidity())saveDay(s,Number(form.elements.kcal.value));};
    }else{
      const policy=t.baseline?.standard || {},inputs=t.profile_inputs || {},m=t.measurements;
      host.innerHTML=`<form class="nutrition-standard-form"><fieldset><h3>长期标准 · ${goals[m.goal]}</h3><p class="small">已保存档案：${escapeHtml(m.height_cm ?? '未填')} cm · ${escapeHtml(m.weight_kg ?? '未填')} kg</p>
        <p class="small">${inputs.age == null ? '年龄未填' : `${escapeHtml(inputs.age)}岁`} · ${escapeHtml({male:'男性公式',female:'女性公式'}[inputs.equation_sex] || '性别未填')} · ${escapeHtml(activity[inputs.activity] || '活动水平未填')}</p>
        ${policy.mode==='fixed'?'<p class="small muted">当前保留原固定目标。预览并确认后改为按档案计算；历史目标不变。</p>':''}
        <div class="form-grid"><label>减脂缺口（kcal，留空用初始参考）<input type="number" name="fat_loss_kcal" min="0" max="500" step="1" value="${policy.fat_loss_kcal ?? ''}" placeholder="维持参考的10%左右"></label><label>增肌盈余（kcal，留空用初始参考）<input type="number" name="muscle_gain_kcal" min="0" max="500" step="1" value="${policy.muscle_gain_kcal ?? ''}" placeholder="维持参考的5%左右"></label><label>整体微调（kcal）<input type="number" name="offset_kcal" min="-500" max="500" step="1" required value="${policy.offset_kcal || 0}"></label></div>
        <p class="small muted">按档案目标选用缺口、盈余或维持，再加整体微调。初始比例是可修改的产品起点，不是个人处方；总调整最多500 kcal且不超过维持参考的20%。</p>
        <label class="plan-check"><input name="scope" type="checkbox" required>已核对档案及适用范围；非孕哺期，无需疾病相关特殊饮食管理</label>
        <div class="dialog-actions"><button type="button" class="nutrition-cancel">取消</button><button type="submit">${icon('calculator')}预览每日目标</button></div><div class="nutrition-preview" aria-live="polite"></div></fieldset></form>`;
      const form=host.querySelector('form'),preview=host.querySelector('.nutrition-preview');
      form.oninput=()=>{s.dirty=true;preview.replaceChildren();s.saveId=null;};
      form.onsubmit=event=>{
        event.preventDefault();if(!form.reportValidity())return;
        if(document.querySelector('#profile-form')?.dataset.dirty==='true'){notify('请先保存上面的个人档案，再预览长期标准',true);return;}
        const value=Object.fromEntries(new FormData(form));
        const optional=name=>value[name]===''?null:Number(value[name]);
        const standard={mode:'calculated',use_profile:true,fat_loss_kcal:optional('fat_loss_kcal'),muscle_gain_kcal:optional('muscle_gain_kcal'),offset_kcal:Number(value.offset_kcal)};
        const request={day:s.day,context_hash:t.context_hash,standard};
        preview.replaceChildren();
        operation(s,signal=>api('/nutrition-target/preview',{method:'POST',signal,body:JSON.stringify(request)}),result=>{
          s.dirty=true;const c=result.calculation;
          preview.innerHTML=`<h3>待确认</h3>${calculation(c)}<p class="small">从 ${escapeHtml(s.day)} 起，每日沿用${c.mode==='calculated'?'此规则；以后保存新体重或目标会重算':'此固定数值'}。本日已有临时覆盖时，临时值仍优先。</p><details class="small"><summary>依据与边界</summary><p>${escapeHtml(c.notice)}</p>${c.sources.map(source=>`<p><a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title)}</a></p>`).join('')}</details><button type="button" class="primary nutrition-confirm">${icon('check')}确认长期标准</button>`;
          const body={...request,version:result.target_state.version,kcal:c.kcal,client_id:crypto.randomUUID(),confirmed:true,general_adult:true};
          preview.querySelector('.nutrition-confirm').onclick=()=>operation(s,async signal=>{await api('/nutrition-target/standard',{method:'POST',signal,body:JSON.stringify(body)});return api(`/summary?day=${s.day}`,{signal});},data=>{draw(s,data);notify('长期营养标准已保存');});
        });
      };
    }
    host.querySelector('.nutrition-cancel').onclick=()=>{if(canLeave()){s.dirty=false;host.replaceChildren();}};icons();
  }
  function mount(root,mode,summary){
    dispose();const s=active={root,mode,day:mode==='standard'?localDate():state.day,user:state.user,ticket:0,busy:false,dirty:false,data:null};
    skeleton(s);if(summary && summary.day===s.day)draw(s,summary);else load(s);
  }
  function profileSaved(){if(active?.mode==='standard')mount(active.root,'standard');}
  return {mount,dispose,canLeave,profileSaved,dailyComparison};
})();
