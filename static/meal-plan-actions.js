"use strict";

window.MealPlanActions=(()=>{
  function markup(plan,foods){
    return `<div class="plan-actions">${!plan.stale?`<button type="button" class="plan-edit-toggle">${icon('pencil')}调整份量</button>`:''}<button type="button" class="plan-record-food">${icon('clipboard-pen')}记录实际食用</button></div>
      ${plan.stale?'':`<form class="plan-portion-edit" hidden><h4>计划份量</h4>${plan.items.map(item=>{
        const food=foods.find(food=>food.id===item.food_id);
        return `<div class="plan-portion-row" data-food="${escapeHtml(item.food_id)}"><span>${escapeHtml(item.name)}<small class="muted"> · ${escapeHtml(item.unit)}</small></span>
          <label>下限<input aria-label="${escapeHtml(item.name)}份量下限" name="lower" type="number" min="${food?.min||1}" max="${food?.max||300}" step="1" value="${item.lower}" required></label>
          <label>上限<input aria-label="${escapeHtml(item.name)}份量上限" name="upper" type="number" min="${food?.min||1}" max="${food?.max||300}" step="1" value="${item.upper}" required></label></div>`;
      }).join('')}<label class="plan-check"><input type="checkbox" name="portion_reviewed" required>已核对计划份量与生熟口径；不是实际吃过记录</label>
      <div class="plan-actions"><button type="button" class="plan-edit-cancel">取消</button><button type="submit" class="primary">${icon('save')}保存新份量</button></div></form>`}`;
  }
  function mount(root,plan,{onSave,onRecord}){
    const form=root.querySelector('.plan-portion-edit');
    root.querySelector('.plan-record-food').onclick=onRecord;
    if(!form)return;
    let clientId=crypto.randomUUID();
    root.querySelector('.plan-edit-toggle').onclick=()=>{form.hidden=!form.hidden;};
    root.querySelector('.plan-edit-cancel').onclick=()=>{form.reset();form.hidden=true;form.dataset.dirty='false';};
    form.oninput=event=>{if(event.target.name!=='portion_reviewed'){clientId=crypto.randomUUID();form.elements.portion_reviewed.checked=false;}form.dataset.dirty='true';};
    form.onsubmit=event=>{
      event.preventDefault();if(!form.reportValidity())return;
      const items=[...form.querySelectorAll('.plan-portion-row')].map(row=>({food_id:row.dataset.food,lower:Number(row.querySelector('[name=lower]').value),upper:Number(row.querySelector('[name=upper]').value)}));
      if(items.some(item=>item.lower>item.upper)){notify('份量下限不能大于上限',true);return;}
      onSave({client_id:clientId,items,reviewed:true},()=>{form.dataset.dirty='false';});
    };
  }
  function canLeave(root=document){
    const forms=[...root.querySelectorAll('.plan-portion-edit[data-dirty="true"]')];
    if(!forms.length)return true;
    if(!window.confirm('有尚未保存的餐单份量修改，仍要离开吗？'))return false;
    forms.forEach(form=>{form.reset();form.hidden=true;form.dataset.dirty='false';});return true;
  }
  window.addEventListener?.('beforeunload',event=>{
    if(document.querySelector('.plan-portion-edit[data-dirty="true"]')){event.preventDefault();event.returnValue='';}
  });
  return {markup,mount,canLeave};
})();
