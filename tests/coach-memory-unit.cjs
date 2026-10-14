// DOM/API substitutes only; confirms escaping, ownership guard and explicit writes.
const vm=require("node:vm"),fs=require("node:fs"),path=require("node:path"),assert=require("node:assert/strict");
const source=fs.readFileSync(path.join(__dirname,"../static/coach-memory.js"),"utf8");
const flush=()=>new Promise(setImmediate);
function node(extra={}) {return {innerHTML:"",textContent:"",value:"",disabled:false,hidden:false,listeners:{},
  addEventListener(type,handler){this.listeners[type]=handler;},focus(){},...extra};}
async function main() {
  const owner={id:1},status=node(),list=node(),digestsList=node(),create=node(),content=node({value:""}),kind=node({value:"user_preference"}),cancel=node(),submit=node();
  const form=node({reportValidity:()=>true,reset(){content.value="";},querySelector:key=>({".coach-memory-content":content,".coach-memory-kind":kind,".coach-memory-cancel":cancel,'button[type="submit"]':submit})[key],querySelectorAll:()=>[content,kind,cancel,submit]});
  const nodes={".coach-memory-status":status,".coach-memory-form":form,".coach-memory-list":list,".coach-digest-create":create,".coach-digest-list":digestsList};
  const root=node({isConnected:true,querySelector:key=>nodes[key]});
  const calls=[];
  let entries=[{id:"a",kind:"assistant_note",content:'<img src=x onerror="evil()">',version:1}],digests=[];
  const context={window:{confirm:()=>true},state:{user:owner},
    escapeHtml:value=>String(value).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;"),
    api:async(url,options={})=>{calls.push({url,options});
      if(url.endsWith("/memory/entries")&&(!options.method||options.method==="GET"))return {items:entries};
      if(url.endsWith("/memory/digests")&&(!options.method||options.method==="GET"))return {items:digests};
      if(url.endsWith("/from-conversation/conv-1")){assert.deepEqual(JSON.parse(options.body),{});digests=[{id:"d",day:"2026-09-15",summary:"旧对话",status:"pending",version:1,stale:false}];return digests[0];}
      if(url.endsWith("/d/approve")){assert.equal(JSON.parse(options.body).version,1);digests[0]={...digests[0],status:"approved",version:2};return digests[0];}
      if(url.endsWith("/memory/entries")&&options.method==="POST"){entries.push({id:"b",...JSON.parse(options.body),version:1});return entries.at(-1);}
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
  const oldHtml=list.innerHTML;context.state.user={id:2};entries.push({id:"c",kind:"user_preference",content:"another user",version:1});
  context.window.CoachMemoryView.setConversation("conv-2");assert.equal(list.innerHTML,oldHtml);
  console.log("Coach memory UI: escaped content, explicit create/approve, account guard passed.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
