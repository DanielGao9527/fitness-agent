"use strict";

window.KnowledgeView = (() => {
  let active = null;
  const categories = {all:"全部",nutrition:"饮食",training:"训练"};

  function dispose() {
    active?.controller?.abort();
    window.KnowledgeAsk?.dispose();
    window.MealPlans?.dispose();
    window.TrainingPlans?.dispose();
    active=null;
  }

  function reference(hit) {
    return `<article class="knowledge-result">
      <div class="knowledge-result-heading"><span class="knowledge-topic">${categories[hit.topic]}</span><h3>${escapeHtml(hit.title)}</h3></div>
      <p class="knowledge-excerpt">${escapeHtml(hit.excerpt)}</p>
      <p class="muted small">${escapeHtml(hit.scope)}</p>
      <details><summary>来源与适用范围</summary><dl class="knowledge-meta">
        <dt>机构</dt><dd>${escapeHtml(hit.publisher)}</dd><dt>原始资料</dt><dd>${escapeHtml(hit.original_title)}</dd>
        <dt>定位</dt><dd>${escapeHtml(hit.locator)}</dd><dt>资料版本</dt><dd>${escapeHtml(hit.source_version)}</dd>
        <dt>本次核对</dt><dd>${escapeHtml(hit.reviewed_on)} · 复核期限 ${escapeHtml(hit.review_due)}</dd>
        <dt>内容类型</dt><dd>${hit.origin==='curated_text'?'训练参考笔记 · 作者归属未核实':'项目中文摘要 · 非官方译文'}</dd></dl>
        <p class="small">${escapeHtml(hit.notice)}</p>
        ${hit.origin==='curated_text'||!hit.url?'<p class="small">未提供可核实的原文链接，不代表完整课程或原作者授权。</p>':`<div class="knowledge-links"><a href="${escapeHtml(hit.url)}" target="_blank" rel="noopener noreferrer">${icon("external-link")}来源原文</a><a href="${escapeHtml(hit.license_url)}" target="_blank" rel="noopener noreferrer">使用条款</a></div>`}
      </details>
      <p class="knowledge-attribution small">${escapeHtml(hit.publisher)} · ${escapeHtml(hit.reviewed_on)}核对 · 非机构背书</p>
    </article>`;
  }

  function mount(root) {
    dispose();
    root.innerHTML=`<section class="knowledge-view" aria-label="知识资料">
      <div class="section-heading"><h2>${icon("book-open")}饮食与训练助手</h2></div>
      <div class="segmented knowledge-modes" role="tablist" aria-label="资料模式"><button type="button" role="tab" data-knowledge-mode="browse" aria-selected="true" aria-controls="knowledge-browse" class="selected">查资料</button><button type="button" role="tab" data-knowledge-mode="ask" aria-selected="false" aria-controls="knowledge-ask-panel">问资料</button></div>
      <div id="knowledge-ask-panel" role="tabpanel" aria-label="问资料" hidden></div>
      <div id="meal-plan-panel" role="tabpanel" aria-label="下一餐" hidden></div>
      <div id="training-plan-panel" role="tabpanel" aria-label="训练建议" hidden></div>
      <div id="knowledge-browse" role="tabpanel" aria-label="查资料">
      <form id="knowledge-search" role="search"><label class="knowledge-query">资料检索<input name="query" maxlength="300" placeholder="例如：力量训练频率、饮食搭配" autocomplete="off"></label><button type="submit" class="primary icon-button" aria-label="检索资料" title="检索资料">${icon("search")}</button></form>
      <div class="segmented knowledge-topics" aria-label="资料分类">${Object.entries(categories).map(([value,label])=>`<button type="button" data-topic="${value}" aria-pressed="${value==="all"}" class="${value==="all" ? "selected" : ""}">${label}</button>`).join("")}</div>
      <p class="knowledge-state muted small" role="status"></p><p class="knowledge-error form-error" role="alert" hidden></p>
      <p class="knowledge-notice small">一般知识资料，不是个人处方。项目摘要非官方译文；原文免费可读，引用不表示原机构或政府认可本应用。</p>
      <div class="knowledge-results" aria-live="polite"></div>
      </div>
    </section>`;
    active={root,user:state.user,topic:"all",ticket:0,controller:null};
    const modes=root.querySelector(".knowledge-modes");
    if(modes) modes.insertAdjacentHTML("beforeend", '<button type="button" role="tab" data-knowledge-mode="plan" aria-selected="false" aria-controls="meal-plan-panel">下一餐</button>');
    if(modes) modes.insertAdjacentHTML("beforeend", '<button type="button" role="tab" data-knowledge-mode="training" aria-selected="false" aria-controls="training-plan-panel">训练建议</button>');
    window.KnowledgeAsk?.mount(root.querySelector("#knowledge-ask-panel"), reference);
    window.MealPlans?.mount(root.querySelector("#meal-plan-panel"), reference);
    window.TrainingPlans?.mount(root.querySelector("#training-plan-panel"), reference);
    root.querySelectorAll("[data-knowledge-mode]").forEach(button=>button.addEventListener("click",()=>{
      const asking=button.dataset.knowledgeMode==="ask";
      const planning=button.dataset.knowledgeMode==="plan";
      const training=button.dataset.knowledgeMode==="training";
      root.querySelector("#knowledge-ask-panel").hidden=!asking;
      root.querySelector("#knowledge-browse").hidden=asking || planning || training;
      root.querySelector("#meal-plan-panel").hidden=!planning;
      const trainingPanel=root.querySelector("#training-plan-panel");
      if(trainingPanel) trainingPanel.hidden=!training;
      root.querySelectorAll("[data-knowledge-mode]").forEach(node=>{
        node.setAttribute("aria-selected",String(node===button));node.classList.toggle("selected",node===button);
      });
      if(!asking) window.KnowledgeAsk?.cancel();
      if(planning) window.MealPlans?.activate();
      else window.MealPlans?.cancel();
      if(training) window.TrainingPlans?.activate();
      else window.TrainingPlans?.cancel();
    }));
    root.querySelector("#knowledge-search").addEventListener("submit",event=>{event.preventDefault();search();});
    root.querySelectorAll("[data-topic]").forEach(button=>button.addEventListener("click",()=>{
      if(!active || active.root!==root) return;
      active.topic=button.dataset.topic;
      root.querySelectorAll("[data-topic]").forEach(node=>{
        node.setAttribute("aria-pressed",String(node===button));node.classList.toggle("selected",node===button);
      });
      search();
    }));
    search();
  }

  async function search() {
    const s=active;
    if(!s) return;
    s.controller?.abort();s.controller=new AbortController();
    const ticket=++s.ticket;
    const current=()=>active===s && ticket===s.ticket && state.user===s.user && s.root.isConnected;
    const error=s.root.querySelector(".knowledge-error"), status=s.root.querySelector(".knowledge-state"), results=s.root.querySelector(".knowledge-results");
    error.hidden=true;error.textContent="";status.textContent="正在检索…";results.replaceChildren();
    const controller=s.controller, timeout=setTimeout(()=>controller.abort(),15000);
    try {
      const data=await api("/knowledge/search",{method:"POST",signal:controller.signal,body:JSON.stringify({query:s.root.querySelector('[name="query"]').value.trim(),topic:s.topic,limit:20})});
      if(!current()) return;
      status.textContent=`找到 ${data.hits.length} 条相关资料`;
      results.innerHTML=data.hits.length ? data.hits.map(reference).join("") : `<div class="empty">${icon("search-x")}<h3>${data.status==="empty" ? "暂无可用资料" : "未找到匹配资料"}</h3></div>`;
      icons();
    } catch(failure) {
      if(!current()) return;
      error.hidden=false;error.textContent=failure.name==="AbortError" ? "检索超时，输入已保留" : failure.message;
      status.textContent="资料暂不可用";
    } finally {clearTimeout(timeout);}
  }
  return {mount,dispose,reference};
})();
