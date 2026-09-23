const vm=require("node:vm"),fs=require("node:fs"),path=require("node:path"),assert=require("node:assert/strict");
const source=fs.readFileSync(path.join(__dirname,"../static/training-plans.js"),"utf8");
const flush=()=>new Promise(setImmediate);
function setup(enabled=true,options={}) {
  const node=()=>({value:"",checked:false,disabled:false,hidden:false,innerHTML:"",textContent:"",listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},replaceChildren(){this.innerHTML="";}});
  const nodes=Object.fromEntries(['form','fieldset','.training-stop','.training-reload','.training-status','.training-error','.training-results','.training-generate','[name="day"]'].map(key=>[key,node()]));
  nodes['[name="day"]'].value="2026-09-18";
  nodes.form.elements={day:nodes['[name="day"]'],daily_minutes:{value:"30"},activity:{value:"walk"},bicycle_available:{checked:false},general_adult:{checked:true},constraints_reviewed:{checked:true}};
  nodes.form.elements.time_basis={value:"daily"};
  nodes.form.reportValidity=()=>true;
  const root={isConnected:true,innerHTML:"",querySelector:key=>nodes[key],querySelectorAll:()=>[]},requests=[];
  const context={window:{},state:{day:"2026-09-18",user:{id:1},profile:{},trainingPlanStatus:enabled?"configured_unverified":"not_configured"},AbortController,setTimeout,clearTimeout,crypto:require('node:crypto'),escapeHtml:value=>String(value??"").replaceAll("<","&lt;"),icon:()=>"",icons(){},api:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))};
  vm.runInNewContext(source,context);const module=context.window.TrainingPlans;module.mount(root,()=>"source",options);
  const submit=()=>nodes.form.listeners.submit({preventDefault(){}});
  return {module,nodes,requests,context,root,submit};
}
const draft={id:"test",day:"2026-09-18",status:"draft",stale:false,daily_minutes:30,completed_minutes:0,remaining_minutes:30,warmup_minutes:5,cooldown_minutes:5,main_minutes:10,total_minutes:20,activity_name:"<unsafe>",activity_id:"walk",sources:[],model:"fixture",generated_at:"2026-09-18T10:00:00Z"};
async function main() {
  const v=setup();assert.equal(v.requests.length,0);v.module.activate();v.requests[0].resolve([]);await flush();
  v.submit();v.submit();assert.equal(v.requests.length,2);
  const body=JSON.parse(v.requests[1].options.body);assert.equal(body.daily_minutes,30);assert.equal(body.general_adult,true);assert.ok(!('user_id' in body));
  v.requests[1].resolve(draft);await flush();assert.ok(v.nodes['.training-results'].innerHTML.includes('&lt;unsafe>'));assert.ok(!v.nodes['.training-results'].innerHTML.includes('<unsafe>'));assert.ok(v.nodes['.training-status'].textContent.includes('尚未采纳'));
  v.nodes.fieldset.listeners.change({target:{name:'daily_minutes'}});assert.equal(v.nodes['.training-results'].innerHTML,"");
  v.submit();v.module.cancel();assert.ok(v.requests[2].options.signal.aborted);v.requests[2].resolve(draft);await flush();assert.equal(v.nodes['.training-results'].innerHTML,"");
  v.submit();v.requests[3].reject(new Error('Offline'));await flush();assert.equal(v.nodes['.training-error'].textContent,'Offline');assert.equal(v.nodes.form.elements.daily_minutes.value,'30');
  v.submit();v.context.state.user={id:2};v.requests[4].resolve(draft);await flush();assert.equal(v.nodes['.training-results'].innerHTML,"");v.module.dispose();
  const gone=setup();gone.submit();gone.module.dispose();gone.requests[0].resolve(draft);await flush();assert.equal(gone.nodes['.training-results'].innerHTML,"");
  const off=setup(false);off.module.activate();off.requests[0].resolve([{...draft,stale:true}]);await flush();assert.ok(off.nodes['.training-results'].innerHTML.includes('已失效'));assert.ok(!off.nodes['.training-results'].innerHTML.includes('training-accept'));off.submit();assert.equal(off.requests.length,1);off.module.dispose();
  const request=setup(true,{context:{minutes:20,time_basis:'unspecified',activity:'cycle'}});
  assert.equal(request.nodes.form.elements.daily_minutes.value,20);assert.equal(request.nodes.form.elements.time_basis.value,'');assert.equal(request.nodes.form.elements.activity.value,'cycle');assert.equal(request.nodes.form.elements.bicycle_available.checked,false);
  assert.ok(request.module.summary({kind:'strength',focus:'<unsafe>'}).includes('&lt;unsafe>'));
  request.nodes.form.elements.time_basis.value='session';request.submit();assert.equal(JSON.parse(request.requests[0].options.body).time_basis,'session');request.module.dispose();
  const history={innerHTML:''};request.module.history(history,[draft],()=>"source");assert.ok(history.innerHTML.includes('这轮的训练建议'));assert.ok(!history.innerHTML.includes('training-accept'));
  console.log('Training plan UI: explicit generation, duplicate guard, escaping, cancellation, input changes, errors, identity and stale history passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
