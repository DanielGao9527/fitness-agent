"use strict";

window.CoachMemoryView = (() => {
  const endpoint="/coach/conversations/memory/entries";
  const digestsEndpoint="/coach/conversations/memory/digests";
  let activeView=null;
  function mount(root) {
    const owner=state.user;
    root.innerHTML=`<details><summary>个人记忆</summary><p class="small muted">只保存你明确确认的交流偏好和个人术语。过敏、目标、器械等长期条件请在个人档案修改；旧对话不会自动变成今天的条件。</p>
      <form class="coach-memory-form"><label>类别<select class="coach-memory-kind"><option value="user_preference">交流偏好</option><option value="assistant_note">个人术语</option></select></label><label>记忆内容<input class="coach-memory-content" maxlength="200" required placeholder="例如：请用简明中文解释"></label><button type="submit">保存记忆</button><button type="button" class="coach-memory-cancel" hidden>取消修改</button></form>
      <p class="coach-memory-status small" role="status"></p><ul class="coach-memory-list"></ul>
      <div class="coach-digest"><p class="small muted">也可从当前对话已核对的原话生成摘要候选。核对后才可用于找回旧对话；它不会成为今天的限制。</p><button type="button" class="coach-digest-create" disabled>整理当前对话</button><ul class="coach-digest-list"></ul></div></details>`;
    let items=[],digests=[],editing=null,conversationId=null;
    const live=()=>root.isConnected && state.user===owner;
    const status=root.querySelector(".coach-memory-status");
    const form=root.querySelector(".coach-memory-form");
    const content=form.querySelector(".coach-memory-content");
    const kind=form.querySelector(".coach-memory-kind");
    const cancel=form.querySelector(".coach-memory-cancel");
    function draw() {
      const list=root.querySelector(".coach-memory-list");
      list.innerHTML=items.length?items.map(item=>`<li data-id="${escapeHtml(item.id)}"><span><strong>${item.kind==="assistant_note"?"个人术语":"交流偏好"}：</strong>${escapeHtml(item.content)}</span><button type="button" data-action="edit">修改</button><button type="button" data-action="delete">删除</button></li>`).join(""):'<li class="small muted">还没有保存个人记忆。</li>';
      root.querySelector(".coach-digest-create").disabled=!conversationId;
      root.querySelector(".coach-digest-list").innerHTML=digests.map(item=>`<li data-id="${escapeHtml(item.id)}"><span>${escapeHtml(item.day || "旧对话")} · 第${escapeHtml((item.source_versions || []).join("、"))}条原话 · ${escapeHtml(item.summary)}${item.stale?" · 来源已变化":item.status==="pending"?" · 待核对":" · 已核对"}</span>${item.status==="pending"&&!item.stale?'<button type="button" data-action="approve">核对并启用</button>':''}<button type="button" data-action="delete">删除</button></li>`).join("");
    }
    function reset() {editing=null;form.reset();kind.disabled=false;cancel.hidden=true;form.querySelector('button[type="submit"]').textContent="保存记忆";}
    async function refresh() {
      try {const [data,history]=await Promise.all([api(endpoint),api(digestsEndpoint)]);if(!live())return;items=data.items;digests=history.items;draw();}
      catch(error) {if(live())status.textContent=error.message;}
    }
    form.addEventListener("submit",async event=>{
      event.preventDefault();if(!form.reportValidity())return;
      const body=editing?{version:editing.version,content:content.value}:{kind:kind.value,content:content.value};
      const url=editing?`${endpoint}/${editing.id}`:endpoint;
      for(const control of form.querySelectorAll("button,input,select"))control.disabled=true;
      try {await api(url,{method:editing?"PUT":"POST",body:JSON.stringify(body)});if(!live())return;reset();status.textContent="记忆已保存。";await refresh();}
      catch(error) {if(live()){status.textContent=error.message;await refresh();}}
      finally {if(live())for(const control of form.querySelectorAll("button,input,select"))control.disabled=false;}
    });
    cancel.addEventListener("click",reset);
    root.querySelector(".coach-memory-list").addEventListener("click",async event=>{
      const button=event.target.closest("button[data-action]");if(!button)return;
      const item=items.find(value=>value.id===button.closest("li")?.dataset.id);if(!item)return;
      if(button.dataset.action==="edit") {editing=item;kind.value=item.kind;kind.disabled=true;content.value=item.content;cancel.hidden=false;form.querySelector('button[type="submit"]').textContent="保存修改";content.focus();return;}
      if(!window.confirm("删除这条个人记忆？删除后不会再用于新请求。"))return;
      button.disabled=true;
      try {await api(`${endpoint}/${item.id}`,{method:"DELETE",body:JSON.stringify({version:item.version})});if(live()){status.textContent="记忆已删除。";await refresh();}}
      catch(error) {if(live()){status.textContent=error.message;await refresh();}}
      finally {if(live())button.disabled=false;}
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
    activeView={root,owner,setConversation(id){conversationId=id;draw();}};
    refresh();
  }
  function setConversation(id) {if(activeView?.root.isConnected && activeView.owner===state.user)activeView.setConversation(id);}
  return {mount,setConversation};
})();
