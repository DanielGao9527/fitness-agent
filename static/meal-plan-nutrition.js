"use strict";

window.MealPlanNutrition=(()=>{
  const labels={breakfast:'早餐',lunch:'午餐',dinner:'晚餐'};
  function range(value,unit='kcal') {return value?`${value.lower===value.upper?value.lower:`${value.lower} ~ ${value.upper}`} ${unit}`:'未知';}
  function summarize(plans,mealTypes,bindingFor,intake,useIntake) {
    const rows=mealTypes.map(key=>{
      const binding=bindingFor(key);
      const plan=plans.find(p=>p.meal_type===key&&!p.stale&&(!binding||(p.coach_id===binding.coach_id&&p.coach_version===binding.coach_version)));
      const review=plan?.nutrition_review;
      return {key,plan,review,kcal:review?.status==='ready'?review.result?.totals?.kcal:null};
    });
    const complete=rows.length>0&&rows.every(row=>row.kcal);
    const total=complete?Object.fromEntries(['lower','upper'].map(side=>[side,Math.round(rows.reduce((sum,row)=>sum+row.kcal[side],0)*100)/100])):null;
    const comparable=complete&&useIntake&&intake?.status==='ready'&&rows.every(row=>row.plan.intake_reference);
    let comparison=null;
    if(comparable){
      const remaining=intake.remaining_kcal;
      comparison=total.upper<remaining.lower?'below':total.lower>remaining.upper?'above':'overlap';
    }
    return {rows,total,comparison};
  }
  function details(plan) {
    const review=plan.nutrition_review;
    if(review?.status!=='ready')return '';
    const result=review.result;
    return `<details class="plan-nutrition-details"><summary>本餐营养估算${plan.stale?' · 旧餐单，仅供查看':''}</summary>
      <p class="small">${result.totals?`热量 ${escapeHtml(range(result.totals.kcal))} · 蛋白质 ${escapeHtml(range(result.totals.protein,'g'))} · 碳水 ${escapeHtml(range(result.totals.carbs,'g'))} · 脂肪 ${escapeHtml(range(result.totals.fat,'g'))}`:`${result.unknown_count}项未知，本餐总量未知`}</p>
      ${result.items.map(item=>`<div class="plan-nutrient-item"><strong>${escapeHtml(item.name)} · ${escapeHtml(range(item.kcal))}</strong><p class="small">${escapeHtml(item.status==='unknown'?item.question:item.assumptions.join('；'))}</p><p class="small muted">AI 估算（阿里云千问） · ${escapeHtml(item.generated_at.replace('T',' ').slice(0,16))} UTC</p></div>`).join('')}
      <p class="small muted">千问估算，非实测或独立营养认证；覆盖所列份量范围，不含额外用油、调味料及未列食物。</p></details>`;
  }
  function render(host,{plans,mealTypes,bindingFor,intake,useIntake,enabled,onEstimate}) {
    if(!host)return;
    const summary=summarize(plans,mealTypes,bindingFor,intake,useIntake);
    const selected=summary.rows.filter(row=>row.plan);
    if(!selected.length){host.innerHTML='';return;}
    const missing=selected.filter(row=>row.review?.status!=='ready');
    const pending=missing.some(row=>row.review?.status==='generating'&&Date.now()/1000-row.review.started_at<180);
    const status=row=>!row.plan?'尚未生成有效餐单':row.kcal?range(row.kcal):row.review?.status==='ready'?'有未知项，总量未知':row.review?.status==='failed'?'估算未完成，可重试':row.review?.status==='generating'?'估算处理中，可刷新核对':'尚未估算';
    const comparisons={below:'所选餐合计范围低于全日差额',above:'所选餐合计范围高于全日差额',overlap:'所选餐合计与全日差额范围重叠，不代表达标'};
    host.innerHTML=`<h4>餐单营养估算</h4><dl class="plan-nutrition-rows">${summary.rows.map(row=>`<div><dt>${labels[row.key]}</dt><dd>${escapeHtml(status(row))}</dd></div>`).join('')}</dl>
      <p class="small"><strong>所选餐合计：${escapeHtml(range(summary.total))}</strong></p>
      ${summary.comparison?`<p class="small plan-nutrition-comparison">${comparisons[summary.comparison]}（${escapeHtml(range(intake.remaining_kcal))}）</p>`:''}
      <p class="small muted">餐单不是已吃记录，合计不代表全天摄入；未列的餐食、饮料、用油和调味料未计入。不据此要求补吃或停吃。</p>
      ${missing.length?`<button class="plan-nutrition-estimate" type="button" data-pending="${pending}" ${!enabled||pending?'disabled':''}>${icon('calculator')}统一估算餐单</button><p class="small muted">只向阿里云发送待估餐单的食材、份量与生熟口径，并计入 AI 使用次数；已有估算将保留。</p>`:''}
      ${!enabled&&missing.length?'<p class="small muted">营养估算尚未启用，餐单仍可查看和核对采纳。</p>':''}`;
    host.querySelector('.plan-nutrition-estimate')?.addEventListener('click',()=>onEstimate(selected.map(row=>({plan_id:row.plan.id,version:row.review?.version||0}))));
  }
  return {render,details,summarize};
})();
