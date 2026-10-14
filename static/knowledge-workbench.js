"use strict";

window.KnowledgeWorkbenchView=(()=>{
  let active=null;
  const endpoint='/knowledge-workbench',labels={knowledge_issue:'知识资料问题',personal_correction:'个人信息纠正'};
  const live=s=>active===s&&state.user===s.user&&s.root.isConnected;
  const q=(s,selector)=>s.root.querySelector(selector);
  function dispose(){active?.controller?.abort();active=null;}
  function message(s,text){q(s,'.workbench-status').textContent=text;}
  function busy(s,value){s.busy=value;s.root.querySelectorAll('button,input,select,textarea').forEach(node=>node.disabled=value);}
  function reviewed(s){q(s,'.candidate-reviewed').checked=false;s.pendingCandidate=null;}
  function setConversation(data){
    const s=active;if(!s)return;
    const field=q(s,'.feedback-target'),previous=field.value;
    const old=s.targets.find(item=>String(item.version)===previous);
    const changed=s.conversation!==data?.id;
    s.conversation=data?.id||null;
    s.targets=(data?.turns||[]).filter(turn=>turn.feedback_target);
    field.innerHTML='<option value="">选择要反馈的知识回答</option>'+s.targets.map(turn=>`<option value="${turn.version}">${turn.version}. ${escapeHtml(Array.from(turn.message).slice(0,70).join(''))}</option>`).join('');
    const target=s.targets.find(item=>String(item.version)===previous);
    if(!changed&&target&&target.feedback_target.answer_hash===old?.feedback_target.answer_hash)field.value=previous;
    else if(previous){q(s,'.feedback-form').reset();s.pendingFeedback=null;message(s,'来源对话或回答已变化，请重新选择并核对反馈。');}
  }
  function render(s,data){
    const [feedback,candidates,sources]=data;
    s.feedback=feedback.items;s.candidates=candidates.items;s.sources=sources.items;s.sourceUnavailable=Boolean(sources.unavailable);
    const old=q(s,'.candidate-source').value,pinned=s.sourceHash;
    q(s,'.candidate-source').innerHTML='<option value="">选择有效公共资料</option>'+s.sources.map(item=>`<option value="${escapeHtml(item.source.chunk_id)}">${escapeHtml(item.source.title)}</option>`).join('');
    const selected=s.sources.find(item=>item.source.chunk_id===old);
    if(selected&&selected.source_hash===pinned)q(s,'.candidate-source').value=old;
    else if(old){s.sourceHash=null;reviewed(s);q(s,'.candidate-reference').textContent='来源已变化，请重新选择并核对。';}
    q(s,'.feedback-list').innerHTML=s.feedback.length?s.feedback.map(item=>`<article class="workbench-item"><strong>${escapeHtml(labels[item.kind])}</strong><p>${escapeHtml(item.question)}</p><p class="workbench-text">${escapeHtml(item.comment)}</p><p class="small muted">${item.origin_stale?'原回答已变化，不能继续准备候选':'来源仍可核对'} · 仅本人可见</p><div class="workbench-actions">${item.kind==='knowledge_issue'&&!item.origin_stale?`<button type="button" data-prepare="${escapeHtml(item.id)}">准备摘录候选</button>`:'<span class="small muted">个人纠正请在档案或个人记忆中另行确认。</span>'}<button type="button" data-delete-feedback="${escapeHtml(item.id)}">删除反馈及关联候选</button></div></article>`).join(''):'<p class="small muted">暂无反馈。</p>';
    q(s,'.candidate-list').innerHTML=s.candidates.length?s.candidates.map(item=>`<article class="workbench-item"><strong>${escapeHtml(item.source.title)} · ${item.status==='draft'?'待核对':'已搁置'}</strong><p class="workbench-text">${escapeHtml(item.content)}</p><p class="small muted">${escapeHtml(item.source.scope)} · 版本 ${item.version} · 未发布${item.source_stale?' · 公共来源失效':''}${item.origin_stale?' · 原回答已变化':''}</p><div class="workbench-actions"><button type="button" data-edit-candidate="${escapeHtml(item.id)}">修订</button><button type="button" data-delete-candidate="${escapeHtml(item.id)}">删除候选</button></div><label>搁置原因<input class="candidate-note" data-note="${escapeHtml(item.id)}" maxlength="300" value="${escapeHtml(item.review_note)}"></label><button type="button" data-reject="${escapeHtml(item.id)}">搁置这版候选</button><details><summary>来源与变更记录</summary><p>${escapeHtml(item.source.publisher)} · ${escapeHtml(item.source.source_version)}</p><p>${escapeHtml(item.source.url)}</p>${item.events.map(event=>`<p class="small">v${event.version} · ${escapeHtml(event.action)} · ${escapeHtml(event.note)}</p>`).join('')}</details></article>`).join(''):'<p class="small muted">暂无知识候选。候选不会参与检索。</p>';
  }
  async function load(signal){return Promise.all([api(endpoint+'/feedback',{signal}),api(endpoint+'/candidates',{signal}),api(endpoint+'/sources',{signal}).catch(error=>{if(error.name==='AbortError')throw error;return {items:[],unavailable:true};})]);}
  async function operation(request,apply){
    const s=active;if(!s||s.busy)return;
    s.controller?.abort();s.controller=new AbortController();busy(s,true);
    try{const value=await request(s,s.controller.signal);if(!live(s))return;if(apply)apply(s,value);}
    catch(error){if(live(s)&&error.name!=='AbortError')message(s,error.message);}
    finally{if(live(s))busy(s,false);}
  }
  function refresh(){operation((s,signal)=>load(signal),(s,data)=>{render(s,data);message(s,s.sourceUnavailable?'公共来源暂不可用；仍可查看、搁置或删除已有私人条目。':'已刷新；反馈和候选仅本人可见，未进入知识库。');});}
  function resetCandidate(s){s.edit=null;s.feedbackId=null;s.sourceHash=null;s.pendingCandidate=null;q(s,'.candidate-form').reset();q(s,'.candidate-reference').textContent='';q(s,'.candidate-editor').hidden=true;}
  function selectSource(s){
    const item=s.sources.find(item=>item.source.chunk_id===q(s,'.candidate-source').value);
    s.sourceHash=item?.source_hash||null;reviewed(s);
    q(s,'.candidate-reference').textContent=item?`${item.source.publisher} · ${item.source.scope}\n${item.source.excerpt}`:'';
    if(item)q(s,'.candidate-content').value=Array.from(item.source.excerpt).slice(0,1000).join('');
  }
  function mount(root){
    dispose();if(!root)return;
    root.innerHTML=`<details class="knowledge-workbench"><summary>反馈与知识候选</summary><p class="small muted">反馈仅本人可见。个人纠正不会自动修改档案；知识候选只接受已审核公共资料的连续摘录，仍需后续评测、审批和发布。</p><form class="feedback-form"><label>反馈来源<select class="feedback-target" required><option value="">选择要反馈的知识回答</option></select></label><label>反馈类型<select class="feedback-kind"><option value="knowledge_issue">知识资料问题</option><option value="personal_correction">个人信息纠正</option></select></label><label>需要核对的问题<textarea class="feedback-comment" maxlength="1000" rows="2" required></textarea></label><button type="submit">保存反馈</button></form><button type="button" class="workbench-refresh">刷新工作台</button><p class="workbench-status small muted" role="status"></p><div class="feedback-list workbench-list"></div><section class="candidate-editor" hidden><h3>准备公共资料摘录候选</h3><form class="candidate-form"><label>公共出处<select class="candidate-source" required><option value="">选择有效公共资料</option></select></label><p class="candidate-reference workbench-text small"></p><label>连续摘录<textarea class="candidate-content" maxlength="1000" rows="3" required></textarea></label><label class="candidate-confirm"><input class="candidate-reviewed" type="checkbox" required>已核对出处与摘录，仅使用所选公共摘要，不包含私人信息</label><div class="workbench-actions"><button type="submit">保存候选</button><button class="candidate-cancel" type="button">取消修订</button></div></form></section><div class="candidate-list workbench-list"></div></details>`;
    const s=active={root,user:state.user,busy:false,targets:[],feedback:[],candidates:[],sources:[],conversation:null,sourceHash:null,edit:null,feedbackId:null};
    q(s,'.workbench-refresh').addEventListener('click',refresh);
    q(s,'.feedback-form').addEventListener('input',()=>{s.pendingFeedback=null;});
    q(s,'.feedback-form').addEventListener('submit',event=>{
      event.preventDefault();if(s.busy||!event.target.reportValidity())return;
      const target=s.targets.find(item=>String(item.version)===q(s,'.feedback-target').value);if(!target)return;
      const body={conversation_id:s.conversation,turn_version:target.version,answer_hash:target.feedback_target.answer_hash,kind:q(s,'.feedback-kind').value,comment:q(s,'.feedback-comment').value.trim()};
      s.pendingFeedback=s.pendingFeedback||{...body,client_id:crypto.randomUUID()};
      operation(async(session,signal)=>{await api(endpoint+'/feedback',{method:'POST',signal,body:JSON.stringify(s.pendingFeedback)});return load(signal);},(session,data)=>{render(session,data);q(session,'.feedback-form').reset();session.pendingFeedback=null;message(session,'反馈已保存；没有修改档案或公共知识。');});
    });
    q(s,'.candidate-source').addEventListener('change',()=>selectSource(s));
    q(s,'.candidate-content').addEventListener('input',()=>reviewed(s));
    q(s,'.candidate-cancel').addEventListener('click',()=>resetCandidate(s));
    q(s,'.candidate-form').addEventListener('submit',event=>{
      event.preventDefault();if(s.busy||!event.target.reportValidity()||!s.sourceHash)return;
      const body={content:q(s,'.candidate-content').value.trim(),source_chunk_id:q(s,'.candidate-source').value,source_hash:s.sourceHash,reviewed:q(s,'.candidate-reviewed').checked};
      const edit=s.edit;s.pendingCandidate=s.pendingCandidate||(edit?{...body,version:edit.version}:{...body,feedback_id:s.feedbackId,client_id:crypto.randomUUID()});
      operation(async(session,signal)=>{await api(endpoint+'/candidates'+(edit?'/'+edit.id:''),{method:edit?'PUT':'POST',signal,body:JSON.stringify(s.pendingCandidate)});return load(signal);},(session,data)=>{render(session,data);resetCandidate(session);message(session,'候选已保存，仍未发布或参与检索。');});
    });
    root.addEventListener('click',event=>{
      const button=event.target.closest('button');if(!button||s.busy)return;
      if(button.dataset.prepare){resetCandidate(s);s.feedbackId=button.dataset.prepare;q(s,'.candidate-editor').hidden=false;q(s,'.candidate-source').focus();}
      if(button.dataset.editCandidate){const item=s.candidates.find(item=>item.id===button.dataset.editCandidate);if(!item)return;resetCandidate(s);s.edit=item;s.feedbackId=item.feedback_id;q(s,'.candidate-editor').hidden=false;
        const source=s.sources.find(entry=>entry.source.chunk_id===item.source.chunk_id&&entry.source_hash===item.source_hash);if(source){q(s,'.candidate-source').value=source.source.chunk_id;selectSource(s);}q(s,'.candidate-content').value=item.content;message(s,'修订后需重新核对。保存将生成新版本并清除旧搁置意见。');}
      const kind=button.dataset.deleteFeedback?'feedback':button.dataset.deleteCandidate?'candidates':null;
      if(kind){const id=button.dataset.deleteFeedback||button.dataset.deleteCandidate,item=(kind==='feedback'?s.feedback:s.candidates).find(entry=>entry.id===id);if(!item||!window.confirm(kind==='feedback'?'删除反馈及其关联候选和记录？':'删除这个未发布候选及其变更记录？'))return;
        operation(async(session,signal)=>{await api(endpoint+'/'+kind+'/'+id,{method:'DELETE',signal,body:JSON.stringify({version:item.version})});return load(signal);},(session,data)=>{render(session,data);resetCandidate(session);message(session,'已删除。');});}
      if(button.dataset.reject){const item=s.candidates.find(entry=>entry.id===button.dataset.reject),note=s.root.querySelector(`[data-note="${button.dataset.reject}"]`);if(!item||!note.value.trim()){note?.focus();message(s,'请填写搁置原因。');return;}
        operation(async(session,signal)=>{await api(endpoint+'/candidates/'+item.id+'/reject',{method:'POST',signal,body:JSON.stringify({version:item.version,note:note.value.trim()})});return load(signal);},(session,data)=>{render(session,data);resetCandidate(session);message(session,'这版候选已搁置，未发布。');});}
    });
    refresh();
  }
  return {mount,setConversation,dispose};
})();
