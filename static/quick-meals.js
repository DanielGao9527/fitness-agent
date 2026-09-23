"use strict";

window.QuickMeals = (() => {
  const nutrients={kcal:"热量",protein:"蛋白质",carbs:"碳水",fat:"脂肪"};
  const names={breakfast:"早餐",lunch:"午餐",dinner:"晚餐"};
  function range(value,key) {
    if(!value)return "未知";
    const round=number=>Number(number.toFixed(key==="kcal"?0:1));
    const lower=round(value.lower),upper=round(value.upper);
    return `${lower===upper?lower:`${lower}–${upper}`}${key==="kcal"?" kcal":" g"}`;
  }
  function summary(values) {return Object.keys(nutrients).map(key=>`${nutrients[key]}约${range(values[key],key)}`).join(" · ");}
  function assessment(value,target,key) {
    if(!value || !target)return "未知，暂不判断";
    if(value.upper<target.lower)return `距参考下沿还差 ${range({lower:target.lower-value.upper,upper:target.lower-value.lower},key)}`;
    if(value.lower>target.upper)return `高于参考上沿 ${range({lower:value.lower-target.upper,upper:value.upper-target.upper},key)}`;
    if(value.lower>=target.lower && value.upper<=target.upper)return "在参考范围内";
    return "估算区间部分超出参考范围，未全部满足";
  }
  function feedbackText(info) {
    const feedback=info?.energy_feedback;
    if(!feedback?.adjustment_kcal)return "";
    const change=feedback.adjustment_kcal;
    return `长期标准 ${info.base_target_kcal} kcal，参考近期 ${feedback.eligible_days} 天已核对记录，今天小幅${change>0?"增加":"减少"} ${Math.abs(change)} kcal。消耗仍为估算，不要求追平。`;
  }
  function eligible(data) {return data?.action==="meal" && !data.pending && !data.meal_question;}
  function selected(data) {
    const context=data.meal_context || {},schedule=context.meal_types || [context.meal_type || "dinner"];
    return schedule.map(meal=>(data.meal_plans || []).find(plan=>plan.meal_type===meal && plan.coach_version===(context.meal_versions?.[meal] || data.version))).filter(Boolean);
  }
  function render(plans,{complete=true,current=false}={}) {
    if(!plans.length)return "";
    plans=[...plans].sort((a,b)=>Object.keys(names).indexOf(a.meal_type)-Object.keys(names).indexOf(b.meal_type));
    const totals=Object.fromEntries(Object.keys(nutrients).map(key=>[key,{lower:0,upper:0}]));
    let known=complete;
    const body=plans.map(plan=>{
      if(current && plan.stale){known=false;return `<section class="quick-meal"><h4>${names[plan.meal_type]}</h4><p class="small muted">档案、记录或依据已变化，旧餐单暂不作为本次建议。</p></section>`;}
      const nutrition=plan.quick_nutrition;
      if(!nutrition || plan.stale)known=false;
      if(nutrition)for(const key in totals)for(const side of ["lower","upper"])totals[key][side]+=nutrition.totals[key][side];
      return `<section class="quick-meal"><h4>${names[plan.meal_type]}${plan.stale?' <span class="muted small">历史参考，依据或记录已变化</span>':""}</h4><ul class="quick-foods">${plan.items.map(item=>`<li><span>${escapeHtml(item.name)}</span><strong>${item.lower===item.upper?item.lower:`${item.lower}–${item.upper}`}${escapeHtml(item.unit)}</strong><small>${escapeHtml(item.basis)}</small></li>`).join("")}</ul>
        <p class="quick-macros">${nutrition?summary(nutrition.totals):"旧版餐单：营养尚未核算"}</p>
        </section>`;
    }).join("");
    const info=plans.find(plan=>plan.quick_nutrition)?.quick_nutrition;
    let comparison="";
    if(info && known){
      const context=info.context,macro=context.macro;
      const projected=Object.fromEntries(Object.keys(totals).map(key=>[key,context.recorded[key]?{lower:context.recorded[key].lower+totals[key].lower,upper:context.recorded[key].upper+totals[key].upper}:null]));
      comparison=`<p class="quick-total"><strong>本次建议合计</strong><br>${summary(totals)}</p>`;
      if(context.target_kcal){
        comparison+=`<p class="small">本日配餐目标：${context.target_kcal} kcal。已记录＋本次建议：${range(projected.kcal,"kcal")}。</p><p class="small muted">${escapeHtml(feedbackText(context))}</p>`;
        if(projected.kcal){
          const gap={lower:context.target_kcal-projected.kcal.upper,upper:context.target_kcal-projected.kcal.lower};
          comparison+=`<p class="small muted">与每日目标差额：${range(gap,"kcal")}（负值表示超过；不是实际能量缺口）。</p>`;
        }
      }
      if(macro.ranges){
        const within=["protein","carbs","fat"].every(key=>projected[key] && projected[key].lower>=macro.ranges[key].lower-1e-7 && projected[key].upper<=macro.ranges[key].upper+1e-7);
        if(plans.every(plan=>plan.quick_nutrition?.balance_policy))comparison+=`<p class="quick-balance"><strong>${within?"三项营养范围核对通过":"三项营养范围尚未全部满足"}</strong><br><span class="small">${within?"按已记录摄入，蛋白质、碳水、脂肪的完整估算区间均在参考范围内。":"此餐单不能视为满足全天参考。"}</span></p>`;
        comparison+=`<dl class="quick-reference">${["protein","carbs","fat"].map(key=>`<div><dt>${nutrients[key]}</dt><dd>全天参考 ${range(macro.ranges[key],key)}${Number.isFinite(macro.center?.[key])?`<br>优先参考约 ${Number(macro.center[key].toFixed(1))} g`:""}<br><span class="muted">已记录＋建议 ${range(projected[key],key)}</span><br>${assessment(projected[key],macro.ranges[key],key)}</dd></div>`).join("")}</dl>`;
      }
      comparison+=`<p class="small muted">${escapeHtml(macro.note)} ${escapeHtml(info.allocation_note)}</p>`;
    }else if(plans.length>1)comparison=`<p class="small muted">${plans.some(plan=>plan.stale)?"历史建议不再与当前目标合计。":"餐次、营养或依据尚不完整，暂不显示完整合计。"}</p>`;
    const source=info?`<details class="quick-sources"><summary>估算依据与假设</summary><p class="small">${escapeHtml(info.notice)}</p><a target="_blank" rel="noopener noreferrer" href="${escapeHtml(info.source.url)}">${escapeHtml(info.source.title)}</a><ul>${(info.context.macro.sources || []).map(source=>`<li><a target="_blank" rel="noopener noreferrer" href="${escapeHtml(source.url)}">${escapeHtml(source.title)}</a></li>`).join("")}</ul><ul>${plans.flatMap(plan=>(plan.quick_nutrition?.rows || []).map(row=>`<li class="small">${escapeHtml(plan.items.find(item=>item.food_id===row.food_id)?.name || row.food_id)} · ${escapeHtml(row.code)} · ${escapeHtml(row.assumption)}<br>${summary(row.nutrients)}</li>`)).join("")}</ul></details>`:"";
    return `<div class="quick-recommendation">${body}${comparison}${source}<p class="small muted">仅为饮食建议，未写入实际饮食记录。</p></div>`;
  }
  function consent(data) {
    const p=data.meal_consent.profile;
    return `<form class="quick-consent"><h3>本日首次确认</h3><p class="small">过敏与禁忌：${escapeHtml(p.food_allergies || "未填写")}；偏好：${escapeHtml(p.preferences || "未填写")}</p><label class="plan-check"><input type="checkbox" name="adult" required>我是19岁及以上、无伤病的一般健康成人，不涉及孕哺期或医疗饮食管理</label><label class="plan-check"><input type="checkbox" name="reviewed" required>已如实核对档案中的食物过敏、禁忌与偏好</label><p class="small muted">发送后，必要档案、请求和记录可能交由阿里云理解；明确的请求直接用于生成建议，有歧义时再询问。建议不写入实际记录，不修改长期档案。</p><button type="submit" class="primary">${icon("check")}确认并开始</button></form>`;
  }
  return {eligible,selected,render,consent,range,assessment,feedbackText};
})();
