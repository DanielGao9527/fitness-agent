// DOM/API substitutes only; confirms escaping, ownership guard and explicit writes.
const vm=require("node:vm"),fs=require("node:fs"),path=require("node:path"),assert=require("node:assert/strict");
const source=fs.readFileSync(path.join(__dirname,"../static/coach-memory.js"),"utf8");
const flush=()=>new Promise(setImmediate);
function node(extra={}) {return {innerHTML:"",textContent:"",value:"",disabled:false,hidden:false,listeners:{},
  addEventListener(type,handler){this.listeners[type]=handler;},focus(){},...extra};}
async function main() {
  const owner={id:1},status=node(),list=node(),digestsList=node(),create=node(),content=node({value:""}),kind=node({value:"user_preference"}),cancel=node(),submit=node();
  const sourcePicker=node(),reviewed=node({checked:false}),sourceReview=node(),origin=node();
  const form=node({reportValidity:()=>true,reset(){content.value="";sourcePicker.value="";reviewed.checked=false;},querySelector:key=>({".coach-memory-content":content,".coach-memory-kind":kind,".coach-memory-cancel":cancel,'.coach-memory-source':sourcePicker,'.coach-memory-reviewed':reviewed,'.coach-memory-source-review':sourceReview,'.coach-memory-origin':origin,'button[type="submit"]':submit})[key],querySelectorAll:()=>[content,kind,cancel,submit,sourcePicker,reviewed]});
  const nodes={".coach-memory-status":status,".coach-memory-form":form,".coach-memory-list":list,".coach-digest-create":create,".coach-digest-list":digestsList};
  const root=node({isConnected:true,querySelector:key=>nodes[key]});
  const calls=[];
  let entries=[{id:"a",kind:"assistant_note",content:'<img src=x onerror="evil()">',version:1}],digests=[],revision=1;
  const context={window:{confirm:()=>true},state:{user:owner},
    escapeHtml:value=>String(value).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;"),
    api:async(url,options={})=>{calls.push({url,options});
      if(url.endsWith("/memory/entries")&&(!options.method||options.method==="GET"))return {items:entries,revision};
      if(url.endsWith("/memory/digests")&&(!options.method||options.method==="GET"))return {items:digests};
      if(url.endsWith("/from-conversation/conv-1")){assert.deepEqual(JSON.parse(options.body),{});digests=[{id:"d",day:"2026-09-15",summary:"旧对话",status:"pending",version:1,stale:false}];return digests[0];}
      if(url.endsWith("/d/approve")){assert.equal(JSON.parse(options.body).version,1);digests[0]={...digests[0],status:"approved",version:2};return digests[0];}
      if(url.endsWith("/memory/entries/from-turn")){
        const body=JSON.parse(options.body);assert.equal(body.reviewed,true);assert.equal(body.source_version,1);
        assert.equal(body.memory_revision,revision);assert.equal(body.conversation_version,2);assert.equal(body.category,"personal_term");
        entries.push({id:"quoted",kind:"assistant_note",content:body.content,version:1,source_mode:"conversation",source:{version:1,day:"2026-10-14"}});revision++;return entries.at(-1);
      }
      if(url.endsWith("/memory/entries")&&options.method==="POST"){entries.push({id:"b",...JSON.parse(options.body),version:1});revision++;return entries.at(-1);}
      throw new Error("Unexpected request " + url);
    }};
  vm.runInNewContext(source,context);
  context.window.CoachMemoryView.mount(root);await flush();await flush();
  assert.ok(list.innerHTML.includes("&lt;img"));assert.ok(!list.innerHTML.includes("<img"));
  context.window.CoachMemoryView.setConversation("conv-1");assert.equal(create.disabled,false);
  await create.listeners.click();await flush();await flush();
  assert.ok(digestsList.innerHTML.includes("待核对"));
  await digestsList.listeners.click({target:{closest:selector=>selector==="button[data-action]"?{dataset:{action:"approve"},disabled:false,closest:()=>({dataset:{id:"d"}})}:null}});await flush();await flush();
  assert.ok(digestsList.innerHTML.includes("已核对"));
  content.value="请简短回答";await form.listeners.submit({preventDefault(){}});await flush();await flush();
  assert.ok(entries.some(entry=>entry.content==="请简短回答"));
  const quoted='我把动作A叫作术语<script>evil()</script>';
  const data={id:"conv-2",version:2,turns:[{version:1,message:quoted}]};
  context.window.CoachMemoryView.setConversation(data);await flush();await flush();
  assert.ok(sourcePicker.innerHTML.includes("&lt;script>"));assert.ok(!sourcePicker.innerHTML.includes("<script>"));
  sourcePicker.value="1";sourcePicker.listeners.change();kind.value="assistant_note";
  assert.equal(content.value,quoted);assert.equal(reviewed.checked,false);
  const posts=()=>calls.filter(call=>call.options.method==="POST" && call.url.endsWith("/entries/from-turn")).length;
  await form.listeners.submit({preventDefault(){}});assert.equal(posts(),0);
  reviewed.checked=true;content.listeners.input();assert.equal(reviewed.checked,false);
  reviewed.checked=true;kind.listeners.change();assert.equal(reviewed.checked,false);
  reviewed.checked=true;content.value="不在原话里的新解释";await form.listeners.submit({preventDefault(){}});assert.equal(posts(),0);
  content.value=quoted;reviewed.checked=true;await form.listeners.submit({preventDefault(){}});await flush();await flush();assert.equal(posts(),1);
  assert.ok(list.innerHTML.includes("来自2026-10-14第1条原话"));
  sourcePicker.value="1";sourcePicker.listeners.change();reviewed.checked=true;
  context.window.CoachMemoryView.setConversation({...data,version:3});await flush();await flush();
  reviewed.checked=true;await form.listeners.submit({preventDefault(){}});assert.equal(posts(),1);
  entries.find(entry=>entry.id==="quoted").stale=true;
  context.window.CoachMemoryView.setConversation({...data,version:4});await flush();await flush();
  assert.ok(list.innerHTML.includes("来源已变化或删除，暂不使用"));
  const oldHtml=list.innerHTML;context.state.user={id:2};entries.push({id:"c",kind:"user_preference",content:"another user",version:1});
  context.window.CoachMemoryView.setConversation("conv-2");assert.equal(list.innerHTML,oldHtml);
  console.log("Coach memory UI: escaped source text, explicit review, stale source rejection, correction and account guard passed.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
