const vm=require("node:vm"),fs=require("node:fs"),path=require("node:path"),assert=require("node:assert/strict");
const source=fs.readFileSync(path.join(__dirname,"../static/meal-plans.js"),"utf8");
const flush=()=>new Promise(setImmediate);
function setup(enabled=true) {
  const node=()=>({value:"",checked:false,disabled:false,hidden:false,innerHTML:"",textContent:"",listeners:{},
    addEventListener(type,fn){this.listeners[type]=fn;},replaceChildren(){this.innerHTML="";},querySelector(){return null;}});
  const selectors=['form','fieldset','.plan-stop','.plan-reload','.plan-status','.plan-error','.plan-results','.plan-foods','.plan-generate','.plan-consent','.plan-consent-saved','.plan-consent-reset','.plan-profile','[name="day"]','[name="meal_type"]'];
  const nodes=Object.fromEntries(selectors.map(key=>[key,node()]));
  nodes['.plan-intake']=node();
  nodes['[name="day"]'].value="2026-09-17";nodes['[name="meal_type"]'].value="dinner";
  nodes.form.elements={day:nodes['[name="day"]'],meal_type:nodes['[name="meal_type"]'],adult_general_diet:{checked:true},constraints_reviewed:{checked:true},plant_only:{checked:false}};
  nodes.form.reportValidity=()=>true;
  const root={isConnected:true,innerHTML:"",querySelector:key=>nodes[key],querySelectorAll:()=>[]},requests=[];
  const context={window:{},state:{day:"2026-09-17",user:{id:1},profile:{},mealPlanStatus:enabled?"configured_unverified":"not_configured"},AbortController,DOMException,setTimeout,clearTimeout,crypto:require('node:crypto'),
    escapeHtml:value=>String(value??"").replaceAll("<","&lt;"),icon:()=>"",icons(){},
    api:(url,options)=>url.startsWith('/meal-plans/intake-context?')?Promise.resolve({status:'unset',target_kcal:null,record_count:0,estimated_count:0,unknown_count:0}):new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))};
  vm.runInNewContext(source,context);const module=context.window.MealPlans;module.mount(root,()=>"source");
  const submit=()=>nodes.form.listeners.submit({preventDefault(){}});
  return {module,nodes,requests,context,root,submit};
}
const foods=[{id:"rice",name:"米饭"}], draft={id:"test",day:"2026-09-17",meal_type:"dinner",status:"draft",stale:false,
  meal_count:2,completed_minutes:20,items:[{name:"<unsafe>",basis:"熟重",lower:100,upper:150,unit:"g"}],
  sources:[],excluded_foods:[],model:"fixture",generated_at:"2026-09-17T10:00:00Z",portion_basis:"估算"};
async function load(v,confirmed=true) {
  v.module.activate();v.requests[0].resolve(foods);await flush();v.requests[1].resolve([]);await flush();
  v.requests[2].resolve({confirmed,context_hash:'fixture'});await flush();
}
async function main() {
  const v=setup();assert.equal(v.requests.length,0);await load(v);
  assert.equal(v.nodes['.plan-consent'].hidden,true);
  v.submit();v.submit();assert.equal(v.requests.length,4);
  const body=JSON.parse(v.requests[3].options.body);assert.ok(!('adult_general_diet' in body));assert.equal(body.meal_type,"dinner");assert.ok(!('user_id' in body));
  v.requests[3].resolve(draft);await flush();assert.ok(v.nodes['.plan-results'].innerHTML.includes('&lt;unsafe>'));assert.ok(!v.nodes['.plan-results'].innerHTML.includes('<unsafe>'));
  assert.ok(v.nodes['.plan-status'].textContent.includes('尚未采纳'));assert.equal(v.nodes.fieldset.disabled,false);
  v.nodes.fieldset.listeners.change({target:{name:'plant_only'}});assert.equal(v.nodes['.plan-results'].innerHTML,"");
  v.submit();v.module.cancel();assert.ok(v.requests[4].options.signal.aborted);v.requests[4].resolve(draft);await flush();assert.equal(v.nodes['.plan-results'].innerHTML,"");
  v.submit();v.requests[5].reject(new Error('Offline'));await flush();assert.equal(v.nodes['.plan-error'].textContent,'Offline');assert.equal(v.nodes.form.elements.day.value,'2026-09-17');
  v.submit();v.context.state.user={id:2};v.requests[6].resolve(draft);await flush();assert.equal(v.nodes['.plan-results'].innerHTML,"");v.module.dispose();
  const gone=setup();await load(gone);gone.submit();gone.module.dispose();gone.requests[3].resolve(draft);await flush();assert.equal(gone.nodes['.plan-results'].innerHTML,"");
  const off=setup(false);await load(off);off.submit();assert.equal(off.requests.length,3);off.module.dispose();
  const fresh=setup();await load(fresh,false);assert.equal(fresh.nodes['.plan-consent'].hidden,false);
  fresh.nodes.form.elements.adult_general_diet.checked=true;fresh.nodes.form.elements.constraints_reviewed.checked=true;
  fresh.submit();assert.equal(fresh.requests[3].url,'/meal-plans/consent');assert.equal(fresh.requests[3].options.method,'PUT');
  fresh.requests[3].resolve({confirmed:true,context_hash:'fixture'});await flush();
  assert.equal(fresh.requests[4].url,'/meal-plans');fresh.requests[4].resolve(draft);await flush();
  assert.equal(fresh.nodes['.plan-consent'].hidden,true);fresh.module.dispose();
  const stopped=setup();await load(stopped,false);stopped.submit();stopped.module.cancel();
  stopped.requests[3].resolve({confirmed:true,context_hash:'fixture'});await flush();
  assert.equal(stopped.requests.length,4,'cancel between consent and generation must not call model');stopped.module.dispose();
  console.log('Meal plan UI: explicit generation, duplicate guard, escaping, cancellation, exclusions, errors, identity and disposal passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
