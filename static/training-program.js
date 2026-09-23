"use strict";

window.TrainingProgram = (() => {
  function exercise(item) {
    if(item.load_guidance && ['insufficient','unavailable'].includes(item.load_guidance.status))item={...item,load_guidance:null};
    return `<li><strong>${escapeHtml(item.name)}</strong><span>${item.sets}组 · 每组${escapeHtml(item.repetitions)}${item.rest_seconds?` · 组间约${item.rest_seconds}秒`:''}</span>
      ${item.target?`<p class="small">${escapeHtml(item.target)}</p>`:''}<p class="small muted">${escapeHtml(item.cue)}</p>
      ${item.load_guidance?`<div class="load-guidance"><strong>${item.load_guidance.status==='consider'?`可考虑 ${escapeHtml(String(item.load_guidance.suggested_kg))} kg`:'负重参考'}</strong><p class="small">${escapeHtml(item.load_guidance.message)}</p>${item.load_guidance.current_kg?`<p class="small muted">上次 ${escapeHtml(String(item.load_guidance.current_kg))} kg · ${escapeHtml(item.load_guidance.basis)} · ${escapeHtml(item.load_guidance.equipment_label)}</p>`:''}${item.load_guidance.reference_days.length?`<p class="small muted">实际记录：${item.load_guidance.reference_days.map(escapeHtml).join('、')}</p>`:''}${item.load_guidance.rule?`<details><summary>进阶依据</summary><p class="small">${escapeHtml(item.load_guidance.rule)}</p></details>`:''}</div>`:''}
      </li>`;
  }
  return {exercise};
})();
