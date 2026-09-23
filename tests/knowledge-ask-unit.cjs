const vm=require("node:vm"), fs=require("node:fs"), path=require("node:path"), assert=require("node:assert/strict");
const source=fs.readFileSync(path.join(__dirname,"../static/knowledge-ask.js"),"utf8");
const flush=()=>new Promise(setImmediate);
function setup(enabled=true) {
  const node=()=>({value:"",innerHTML:"",textContent:"",hidden:false,disabled:false,listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},replaceChildren(){this.innerHTML="";}});
  const nodes=Object.fromEntries(['form','[name="question"]','[type="submit"]','.knowledge-ask-cancel','.knowledge-ask-status','.knowledge-ask-error','.knowledge-answer'].map(key=>[key,node()]));
  const root={innerHTML:"",isConnected:true,querySelector:key=>nodes[key]}, requests=[];
  const context={window:{},state:{user:{id:1},knowledgeQaStatus:enabled?"configured_unverified":"not_configured"},AbortController,setTimeout,clearTimeout,
    icons(){},icon:()=>"",escapeHtml:value=>String(value).replaceAll("<","&lt;"),
    api:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))};
  vm.runInNewContext(source,context);const module=context.window.KnowledgeAsk;
  module.mount(root,hit=>`<article>${hit.chunk_id}</article>`);
  const input=value=>{nodes['[name="question"]'].value=value;nodes['[name="question"]'].listeners.input();};
  const submit=()=>nodes.form.listeners.submit({preventDefault(){}});
  return {nodes,root,context,requests,module,input,submit};
}
const response={answer:"<unsafe>",sources:[{chunk_id:"cdc-adults:strength"}],model_called:true,model:"fixture"};
async function main() {
  const v=setup();v.input("question");v.submit();v.submit();assert.equal(v.requests.length,1);
  v.requests[0].resolve(response);await flush();
  assert.ok(v.nodes['.knowledge-answer'].innerHTML.includes("&lt;unsafe>"));
  assert.ok(v.nodes['.knowledge-answer'].innerHTML.includes("cdc-adults:strength"));
  assert.equal(v.nodes['[type="submit"]'].disabled,false);
  v.input("timeout");assert.equal(v.nodes['.knowledge-answer'].innerHTML,"");v.submit();
  v.requests[1].reject(new Error("Upstream failed"));await flush();
  assert.equal(v.nodes['[name="question"]'].value,"timeout");assert.equal(v.nodes['.knowledge-ask-error'].textContent,"Upstream failed");
  v.submit();v.input("edited while waiting");assert.ok(v.requests[2].options.signal.aborted);
  v.requests[2].resolve(response);await flush();assert.equal(v.nodes['.knowledge-answer'].innerHTML,"");
  v.submit();v.module.cancel();assert.ok(v.requests[3].options.signal.aborted);
  v.requests[3].resolve(response);await flush();assert.equal(v.nodes['.knowledge-answer'].innerHTML,"");
  v.submit();v.context.state.user={id:2};v.requests[4].resolve(response);await flush();
  assert.equal(v.nodes['.knowledge-answer'].innerHTML,"");v.module.dispose();
  const removed=setup();removed.input("late");removed.submit();removed.module.dispose();
  removed.requests[0].resolve(response);await flush();assert.equal(removed.nodes['.knowledge-answer'].innerHTML,"");
  const disabled=setup(false);disabled.input("question");disabled.submit();assert.equal(disabled.requests.length,0);disabled.module.dispose();
  console.log("Knowledge Q&A: explicit submit, duplicate guard, edit/cancel/logout late results, escaping, disabled and errors passed.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
