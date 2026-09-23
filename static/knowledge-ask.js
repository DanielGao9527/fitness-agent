"use strict";

window.KnowledgeAsk = (() => {
  let active=null;
  function cancel() {
    const s=active;
    if(!s || !s.busy) return;
    s.ticket++;s.controller.abort();s.busy=false;
    s.root.querySelector('[type="submit"]').disabled=false;
    s.root.querySelector('.knowledge-ask-cancel').hidden=true;
    s.root.querySelector('.knowledge-ask-status').textContent="已停止等待，已提交的 AI 请求仍计入使用次数";
  }
  function dispose() {cancel();active=null;}

  function mount(root, reference) {
    dispose();
    const enabled=state.knowledgeQaStatus==="configured_unverified";
    root.innerHTML=`<form class="knowledge-ask-form"><label>你的问题<textarea name="question" maxlength="1000" rows="3" required placeholder="例如：肌肉强化活动一般每周几天？"></textarea></label>
      <p class="muted small">一般知识摘答，不是个人计划。提交的问题将发送至阿里云千问，并计入 AI 使用次数；不上传完整档案或历史记录。</p>
      <div class="knowledge-ask-actions"><button class="primary" type="submit" ${enabled ? "" : "disabled"}>${icon("send")}提问</button><button class="knowledge-ask-cancel" type="button" hidden>${icon("square")}停止等待</button><span class="knowledge-ask-status muted small" role="status">${enabled ? "" : "资料问答尚未启用，可继续查资料"}</span></div>
      <p class="knowledge-ask-error form-error" role="alert" hidden></p></form><div class="knowledge-answer" aria-live="polite"></div>`;
    active={root,reference,user:state.user,ticket:0,busy:false,controller:null};
    root.querySelector("form").addEventListener("submit",event=>{event.preventDefault();ask();});
    root.querySelector(".knowledge-ask-cancel").addEventListener("click",cancel);
    root.querySelector('[name="question"]').addEventListener("input",()=>{
      if(active?.root!==root) return;
      const wasWaiting=active.busy;
      cancel();root.querySelector(".knowledge-answer").replaceChildren();
      root.querySelector(".knowledge-ask-error").hidden=true;
      if(!wasWaiting && enabled) root.querySelector(".knowledge-ask-status").textContent="问题尚未提交";
    });
  }

  async function ask() {
    const s=active;
    if(!s || s.busy || state.knowledgeQaStatus!=="configured_unverified") return;
    const input=s.root.querySelector('[name="question"]'), question=input.value.trim();
    if(!question) return;
    const ticket=++s.ticket;
    const current=()=>active===s && ticket===s.ticket && state.user===s.user && s.root.isConnected;
    const button=s.root.querySelector('[type="submit"]'), stop=s.root.querySelector('.knowledge-ask-cancel'),
      status=s.root.querySelector('.knowledge-ask-status'), error=s.root.querySelector('.knowledge-ask-error'), answer=s.root.querySelector('.knowledge-answer');
    s.busy=true;s.controller=new AbortController();button.disabled=true;stop.hidden=false;
    error.hidden=true;error.textContent="";answer.replaceChildren();status.textContent="正在核对资料…";
    const controller=s.controller, timeout=setTimeout(()=>controller.abort(),35000);
    try {
      const result=await api("/knowledge/ask",{method:"POST",signal:controller.signal,body:JSON.stringify({question})});
      if(!current()) return;
      status.textContent="回答仅供参考，未写入实际记录";
      answer.innerHTML=`<p class="knowledge-answer-message">${escapeHtml(result.answer)}</p>${result.sources.length ? '<p class="knowledge-notice small">项目摘要非官方译文，原文免费可读；引用不表示原机构或政府认可本应用。</p>' : ""}${result.sources.map(s.reference).join("")}`;
      icons();
    } catch(failure) {
      if(!current()) return;
      status.textContent="未得到回答，问题已保留";error.hidden=false;
      error.textContent=failure.name==="AbortError" ? "等待超时，未自动重试；已提交的 AI 请求仍计入使用次数" : failure.message;
    } finally {
      clearTimeout(timeout);
      if(current()){s.busy=false;button.disabled=false;stop.hidden=true;}
    }
  }
  return {mount,cancel,dispose};
})();
