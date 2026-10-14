"use strict";

window.CoachMemoryView = (() => {
  const endpoint="/coach/conversations/memory/entries";
  const digestsEndpoint="/coach/conversations/memory/digests";
  let activeView=null;
  function mount(root) {
    const owner=state.user;
    root.innerHTML=`<details><summary>个人记忆</summary><p class="small muted">只保存你明确确认的交流偏好和个人术语。过敏、目标、器械等长期条件请在个人档案修改；旧对话不会自动变成今天的条件。</p>
      <form class="coach-memory-form"><label>类别<select class="coach-memory-kind"><option value="user_preference">交流偏好</option><option value="assistant_note">个人术语</option></select></label>
      <label>来源<select class="coach-memory-source"><option value="">手工输入</option></select></label>
      <label>记忆内容<textarea class="coach-memory-content" rows="3" maxlength="200" required placeholder="例如：请用简明中文解释"></textarea></label>
      <label class="coach-memory-source-review" hidden><input class="coach-memory-reviewed" type="checkbox">我已核对原话及类型，确认作为长期交流记忆保存</label>
      <p class="coach-memory-origin small muted"></p><button type="submit">保存记忆</button><button type="button" class="coach-memory-cancel" hidden>取消修改</button></form>
      <p class="coach-memory-status small" role="status"></p><ul class="coach-memory-list"></ul>
      <div class="coach-digest"><p class="small muted">也可从当前对话已核对的原话生成摘要候选。核对后才可用于找回旧对话；它不会成为今天的限制。</p><button type="button" class="coach-digest-create" disabled>整理当前对话</button><ul class="coach-digest-list"></ul></div></details>`;
    let items=[],digests=[],editing=null,conversationId=null,conversation=null,revision=null;
    let selectedSource=null,sourceStale=false,busy=false,refreshTicket=0;
    const live=()=>root.isConnected && state.user===owner;
    const status=root.querySelector(".coach-memory-status");
    const form=root.querySelector(".coach-memory-form");
    const content=form.querySelector(".coach-memory-content");
    const kind=form.querySelector(".coach-memory-kind");
    const cancel=form.querySelector(".coach-memory-cancel");
    const source=form.querySelector(".coach-memory-source");
    const reviewed=form.querySelector(".coach-memory-reviewed");
    const sourceReview=form.querySelector(".coach-memory-source-review");
    const origin=form.querySelector(".coach-memory-origin");
    function controls() {
      for(const control of form.querySelectorAll("button,input,select,textarea"))control.disabled=busy;
      kind.disabled=busy || !!editing;
      source.disabled=busy || !!editing || revision===null;
    }
    function sources() {
      const selected=source.value;
      source.innerHTML='<option value="">手工输入</option>'+(conversation?.turns || []).map(turn=>`<option value="${escapeHtml(turn.version)}">当前对话第${escapeHtml(turn.version)}条：${escapeHtml(Array.from(turn.message).slice(0,60).join(""))}</option>`).join("");
      source.value=selected;
      controls();
    }
    function draw() {
      const list=root.querySelector(".coach-memory-list");
      list.innerHTML=items.length?items.map(item=>`<li data-id="${escapeHtml(item.id)}"><span><strong>${item.kind==="assistant_note"?"个人术语":"交流偏好"}：</strong>${escapeHtml(item.content)}<small class="muted">${item.stale?" · 来源已变化或删除，暂不使用":item.source?` · 来自${escapeHtml(item.source.day || "旧对话")}第${escapeHtml(item.source.version)}条原话`:" · 手工保存"}</small></span><button type="button" data-action="edit">修改</button><button type="button" data-action="delete">删除</button></li>`).join(""):'<li class="small muted">还没有保存个人记忆。</li>';
      root.querySelector(".coach-digest-create").disabled=!conversationId;
      root.querySelector(".coach-digest-list").innerHTML=digests.map(item=>`<li data-id="${escapeHtml(item.id)}"><span>${escapeHtml(item.day || "旧对话")} · 第${escapeHtml((item.source_versions || []).join("、"))}条原话 · ${escapeHtml(item.summary)}${item.stale?" · 来源已变化":item.status==="pending"?" · 待核对":" · 已核对"}</span>${item.status==="pending"&&!item.stale?'<button type="button" data-action="approve">核对并启用</button>':''}<button type="button" data-action="delete">删除</button></li>`).join("");
    }
    function reset() {editing=null;selectedSource=null;sourceStale=false;form.reset();source.value="";reviewed.checked=false;reviewed.required=false;sourceReview.hidden=true;origin.textContent="";cancel.hidden=true;form.querySelector('button[type="submit"]').textContent="保存记忆";controls();}
    async function refresh() {
      const ticket=++refreshTicket;
      try {const [data,history]=await Promise.all([api(endpoint),api(digestsEndpoint)]);if(!live() || ticket!==refreshTicket)return;items=data.items;revision=data.revision ?? 0;digests=history.items;
        if(selectedSource && selectedSource.memory_revision!==revision){sourceStale=true;reviewed.checked=false;origin.textContent="个人记忆已变化，请重新选择原话后核对。";}
        draw();controls();}
      catch(error) {if(live())status.textContent=error.message;}
    }
    source.addEventListener("change",()=>{
      if(busy || editing || !live())return;
      const turn=(conversation?.turns || []).find(item=>String(item.version)===source.value);
      sourceStale=false;selectedSource=null;reviewed.checked=false;reviewed.required=!!turn;sourceReview.hidden=!turn;
      if(turn && revision!==null && conversation.version){
        selectedSource={conversation_id:conversation.id,conversation_version:conversation.version,source_version:turn.version,memory_revision:revision,message:turn.message};
        content.value=Array.from(turn.message).slice(0,200).join("");
        origin.textContent="请保留原话中的连续摘录。删除原对话后会停用此条记忆；清除另存的摘录，请在记忆列表删除。";
      }else{origin.textContent="手工保存只作为交流上下文；交流偏好已有条目时请直接修改。";}
    });
    content.addEventListener("input",()=>{if(selectedSource)reviewed.checked=false;});
    kind.addEventListener("change",()=>{if(selectedSource)reviewed.checked=false;});
    form.addEventListener("submit",async event=>{
      event.preventDefault();if(busy || !live() || !form.reportValidity())return;
      if(sourceStale){status.textContent="来源或记忆已变化，请重新选择原话后核对。";return;}
      if(selectedSource && (!reviewed.checked || !selectedSource.message.includes(content.value.trim()))){status.textContent="请核对类型与来源，并保留原话中的连续摘录。";return;}
      const category=kind.value==="assistant_note"?"personal_term":"communication_preference";
      const body=editing?{version:editing.version,content:content.value}:selectedSource?{category,content:content.value,
        conversation_id:selectedSource.conversation_id,conversation_version:selectedSource.conversation_version,
        source_version:selectedSource.source_version,memory_revision:selectedSource.memory_revision,reviewed:true}
        :{kind:kind.value,content:content.value,category};
      const url=editing?`${endpoint}/${editing.id}`:selectedSource?`${endpoint}/from-turn`:endpoint;
      busy=true;controls();
      try {await api(url,{method:editing?"PUT":"POST",body:JSON.stringify(body)});if(!live())return;reset();status.textContent="记忆已保存。";await refresh();}
      catch(error) {if(live()){
        if(selectedSource && (error.status===409 || error.status===404)){sourceStale=true;reviewed.checked=false;origin.textContent="来源或记忆已变化，请刷新当前对话并重新选择原话。";}
        status.textContent=error.message;await refresh();}}
      finally {busy=false;if(live())controls();}
    });
    cancel.addEventListener("click",reset);
    root.querySelector(".coach-memory-list").addEventListener("click",async event=>{
      const button=event.target.closest("button[data-action]");if(!button || busy || !live())return;
      const item=items.find(value=>value.id===button.closest("li")?.dataset.id);if(!item)return;
      if(button.dataset.action==="edit") {reset();editing=item;kind.value=item.kind;content.value=item.content;cancel.hidden=false;origin.textContent="修改后按你手工确认的新内容保存，不再冒充原来的对话摘录。";form.querySelector('button[type="submit"]').textContent="保存修改";controls();content.focus();return;}
      if(!window.confirm("删除这条个人记忆？删除后不会再用于新请求。"))return;
      busy=true;button.disabled=true;controls();
      try {await api(`${endpoint}/${item.id}`,{method:"DELETE",body:JSON.stringify({version:item.version})});if(live()){status.textContent="记忆已删除。";await refresh();}}
      catch(error) {if(live()){status.textContent=error.message;await refresh();}}
      finally {busy=false;if(live()){button.disabled=false;controls();}}
    });
    root.querySelector(".coach-digest-create").addEventListener("click",async()=>{
      if(!conversationId)return;
      const button=root.querySelector(".coach-digest-create");button.disabled=true;
      try {await api(`${digestsEndpoint}/from-conversation/${conversationId}`,{method:"POST",body:"{}"});if(live()){status.textContent="摘要候选已整理，请逐条核对。";await refresh();}}
      catch(error) {if(live())status.textContent=error.message;}
      finally {if(live())button.disabled=false;}
    });
    root.querySelector(".coach-digest-list").addEventListener("click",async event=>{
      const button=event.target.closest("button[data-action]");if(!button)return;
      const item=digests.find(value=>value.id===button.closest("li")?.dataset.id);if(!item)return;
      if(button.dataset.action==="delete"&&!window.confirm("删除这份旧对话摘要？"))return;
      button.disabled=true;
      try {
        await api(`${digestsEndpoint}/${item.id}${button.dataset.action==="approve"?"/approve":""}`,{method:button.dataset.action==="approve"?"POST":"DELETE",body:JSON.stringify({version:item.version})});
        if(live()){status.textContent=button.dataset.action==="approve"?"摘要已核对，可用于旧对话找回。":"摘要已删除。";await refresh();}
      } catch(error) {if(live()){status.textContent=error.message;await refresh();}}
      finally {if(live())button.disabled=false;}
    });
    activeView={root,owner,setConversation(data){
      const next=typeof data==="string"?{id:data,version:null,turns:[]}:data;
      const changed=conversationId!==next?.id || conversation?.version!==next?.version;
      conversation=next;conversationId=next?.id || null;
      if(selectedSource && (selectedSource.conversation_id!==conversationId || selectedSource.conversation_version!==next?.version
          || !(next?.turns || []).some(turn=>turn.version===selectedSource.source_version && turn.message===selectedSource.message))){
        sourceStale=true;reviewed.checked=false;origin.textContent="来源对话已变化，请重新选择原话后核对。";
      }
      sources();draw();if(changed)refresh();
    }};
    refresh();
  }
  function setConversation(id) {if(activeView?.root.isConnected && activeView.owner===state.user)activeView.setConversation(id);}
  return {mount,setConversation};
})();
