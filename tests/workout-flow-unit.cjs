// DOM and request substitutes only; no browser, microphone or provider calls.
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const source = fs.readFileSync(path.join(__dirname, "../static/workouts.js"), "utf8");
const flush = () => new Promise(setImmediate);

function setup({deferred=false, saveError=null, estimateError=null, parseResult=null, record=null, unknown=false}={}) {
  const listeners={}, requests=[], items=[], buttons={};
  let release, confirmResult=false, confirmations=0, speechTarget, closed=false;
  const node = (props={}) => ({value:"",disabled:false,hidden:false,isConnected:true,nodes:{},
    setAttribute(){},classList:{toggle(){}},focus(){},...props});
  const field = (name,value="") => node({name,value:String(value ?? "")});
  const form=node({id:"workout-form",reportValidity:()=>true});
  const list=node({
    replaceChildren(){items.splice(0);},
    insertAdjacentHTML(_position,html){
      const item=node({remove(){items.splice(items.indexOf(item),1);}});
      for(const name of ["name","minutes","status","intensity","details"]) {
        const match=html.match(new RegExp(`name="${name}"[^>]*value="([^"]*)"`));
        const selected=html.match(new RegExp(`name="${name}">[\\s\\S]*?<option value="([^"]*)" selected`));
        item.nodes[`[name="${name}"]`]=field(name,match?.[1] ?? selected?.[1] ?? "");
      }
      for(const selector of ["h3",'[data-workout="remove"]',".workout-item-status",".workout-calories"]) item.nodes[selector]=node();
      Object.values(item.nodes).forEach(el=>{el.closest=selector=>selector==="#workout-form" ? form : selector===".workout-item" ? item : null;});
      items.push(item);this.lastElementChild=item;
    }
  });
  for(const name of ["day","weight_kg","text","notes"]) form.nodes[`[name="${name}"]`]=field(name,{day:"2026-09-17",weight_kg:70}[name] ?? "");
  for(const action of ["add","estimate","clear","parse","manual","text"]) {
    const button=node({dataset:{workout:action},nodes:{span:node()}});
    button.closest=()=>button;buttons[action]=button;form.nodes[`[data-workout="${action}"]`]=button;
  }
  for(const selector of ["#workout-text","#workout-speech",".workout-state",".workout-weight-state",".workout-questions",".form-error",'[type="submit"]']) form.nodes[selector]=node();
  form.nodes['[type="submit"]'].nodes.span=node();
  form.nodes["#workout-items"]=list;
  form.querySelectorAll=selector=>selector===".workout-item" ? items : [...Object.values(form.nodes),...items.flatMap(row=>Object.values(row.nodes))];
  Object.values(form.nodes).filter(el=>el.name).forEach(el=>{el.closest=selector=>selector==="#workout-form" ? form : null;});
  const roots={"#workout-form":form,"#dialog-title":node(),"#dialog-content":node(),"#record-dialog":node({showModal(){},close(){closed=true;}})};
  const context={window:{confirm(){confirmations++;return confirmResult;},FitnessSpeech:{dispose(){},mount(_root,target){speechTarget=target;}}},
    state:{day:"2026-09-17",profile:{weight_kg:70},user:{id:1},workoutStatus:"configured_unverified",speechStatus:"configured_unverified"},
    document:{addEventListener:(type,fn)=>{listeners[type]=fn;}},crypto:{randomUUID},AbortController,setTimeout,clearTimeout,
    $:(selector,root)=>root ? root.nodes[selector] : roots[selector],
    icon:()=>"",icons(){},escapeHtml:value=>String(value ?? ""),nutritionRange:range=>`${range.lower} ~ ${range.upper}`,
    field:(_label,name,value)=>`<input name="${name}" value="${value ?? ""}">`,
    options:(choices,selected)=>Object.entries(choices).map(([key,label])=>`<option value="${key}"${key===selected?' selected':''}>${label}</option>`).join(""),
    canLeaveDraft:()=>true,notify(){},refresh:async()=>{},
    api:async(url,options)=>{
      const body=JSON.parse(options.body);requests.push({url,body});
      if(url.endsWith("/batch") && saveError) throw Object.assign(new Error(saveError.message),{status:saveError.status});
      if(url.endsWith("preview-batch") && estimateError) throw Object.assign(new Error(estimateError.message),{status:estimateError.status});
      let result={};
      if(url.endsWith("parse-text")) result=parseResult || {items:[{name:"Walk",minutes:30,status:"completed",intensity:"normal_assumed",details:""}],questions:[]};
      if(url.endsWith("preview-batch")) result={items:body.items.map(()=>({id:randomUUID(),estimate:unknown ? {status:"unknown",question:"Unknown fixture"} : {status:"estimated",kcal:{lower:90,upper:150},assumptions:["Fixture"],model:"fixture",generated_at:"2026-09-17",question:""}}))};
      return deferred ? await new Promise(resolve=>{release=()=>resolve(result);}) : result;
    }
  };
  vm.runInNewContext(source,context);
  const module=context.window.WorkoutEditor;
  module.open(record);
  const change=(name,value,index=null)=>{
    const target=(index===null ? form : items[index]).nodes[`[name="${name}"]`];
    target.value=String(value);listeners.input({target});
  };
  return {module,form,items,requests,buttons,context,change,
    click:action=>listeners.click({target:buttons[action]}),
    submit:()=>listeners.submit({target:form,preventDefault(){}}),
    fill:(index=0)=>{change("name",`Walk ${index}`,index);change("minutes",30,index);},
    release:()=>release(),confirm:value=>{confirmResult=value;},confirmations:()=>confirmations,
    speechTarget:()=>speechTarget,closed:()=>closed};
}

async function main() {
  const batch=setup();batch.fill();
  for(let i=1;i<8;i++){batch.click("add");batch.fill(i);}
  assert.equal(batch.requests.length,0);assert.equal(batch.buttons.estimate.hidden,true);
  batch.submit();await flush();assert.equal(batch.requests[0].body.items.length,8);assert.equal(batch.closed(),false);
  assert.ok(batch.requests[0].body.items.every(item=>item.intensity==="normal_assumed"));
  batch.change("minutes",40,0);
  batch.submit();await flush();assert.equal(batch.requests[1].body.items.length,1);assert.equal(batch.confirmations(),0);
  assert.equal(batch.requests[1].body.items[0].minutes,40);
  batch.submit();await flush();assert.equal(batch.requests[2].body.items.length,8);
  assert.ok(batch.requests[2].body.items.every(item=>item.calorie_preview_id && !item.calorie_estimate));
  assert.equal(batch.closed(),true);

  const text=setup();text.click("text");assert.equal(text.speechTarget(),text.form.nodes['[name="text"]']);
  text.change("text","Walk 30 minutes");assert.equal(text.requests.length,0);
  text.click("parse");await flush();assert.equal(text.items.length,1);assert.equal(text.requests.length,2);
  assert.ok(text.requests[1].url.endsWith("preview-batch"));assert.equal(text.closed(),false);
  text.change("text","Walk 20 minutes");assert.equal(text.form.nodes['[type="submit"]'].disabled,true);
  text.click("manual");assert.equal(text.form.nodes['[type="submit"]'].disabled,false);
  assert.equal(text.module.canLeave(),false);text.confirm(true);assert.equal(text.module.canLeave(),true);

  const cancelled=setup({deferred:true});cancelled.fill();cancelled.submit();
  cancelled.module.dispose();cancelled.release();await flush();
  assert.equal(cancelled.items[0].nodes['.workout-calories'].innerHTML,"");
  assert.equal(cancelled.items[0].nodes['[name="name"]'].disabled,false);

  const expired=setup({saveError:{status:409,message:"预览过期，请重新估算"}});expired.fill();
  expired.submit();await flush();expired.submit();await flush();
  assert.equal(expired.closed(),false);
  expired.submit();await flush();assert.equal(expired.requests[2].body.items.length,1);
  assert.notEqual(expired.requests[0].body.items[0].client_id,expired.requests[2].body.items[0].client_id);

  const weight=setup();weight.fill();weight.submit();await flush();
  weight.change("status","planned",0);weight.submit();await flush();assert.equal(weight.requests.length,1);
  weight.change("weight_kg",71);weight.submit();await flush();assert.equal(weight.requests.length,2);
  weight.change("weight_kg","");weight.submit();await flush();assert.equal(weight.requests.length,2);
  assert.equal(weight.form.nodes['[type="submit"]'].disabled,false);
  weight.submit();await flush();assert.equal(weight.closed(),true);
  assert.ok(weight.requests[2].body.items.every(item=>!item.calorie_preview_id));

  const failed=setup({estimateError:{status:504,message:"Fixture timeout"}});failed.fill();
  failed.submit();await flush();assert.equal(failed.requests.length,1);assert.equal(failed.closed(),false);
  assert.match(failed.form.nodes['.form-error'].textContent,/消耗未知保存/);
  failed.submit();await flush();assert.equal(failed.requests.length,2);assert.equal(failed.closed(),true);
  assert.equal(failed.requests[1].body.items[0].calorie_preview_id,undefined);

  const retry=setup({unknown:true});retry.fill();retry.submit();await flush();
  retry.change("notes","Note only");retry.submit();await flush();assert.equal(retry.requests.length,1);
  retry.click("estimate");await flush();assert.equal(retry.requests.length,2);
  assert.notEqual(retry.requests[0].body.items[0].client_id,retry.requests[1].body.items[0].client_id);

  const disabled=setup();disabled.context.state.workoutStatus="not_configured";disabled.fill();
  disabled.submit();await flush();assert.equal(disabled.requests.length,0);
  disabled.submit();await flush();assert.equal(disabled.closed(),true);

  const single=setup({record:{id:42,name:"Chest and triceps",minutes:40,status:"completed",intensity:"unknown",calorie_estimate:{kcal:{lower:100,upper:200},assumptions:["Old"],model:"old",generated_at:"2026-09-17"}}});
  single.submit();await flush();assert.equal(single.requests.length,0);
  single.change("minutes",50,0);single.submit();await flush();assert.equal(single.requests[0].body.items.length,1);
  assert.equal(single.requests[0].body.items[0].intensity,"unknown");
  single.submit();await flush();assert.equal(single.requests[1].url,"/workouts/42");

  const cleared=setup();cleared.fill();cleared.submit();await flush();cleared.click("clear");
  cleared.submit();await flush();assert.equal(cleared.requests.length,2);assert.equal(cleared.closed(),true);
  assert.equal(cleared.requests[1].body.items[0].clear_calorie_estimate,true);

  const late=setup({deferred:true});late.click("text");late.change("text","Walk 30 minutes");late.click("parse");
  late.context.state.user={id:2};late.release();await flush();assert.equal(late.requests.length,1);
  assert.equal(late.items[0].nodes['[name="name"]'].value,"");

  const incomplete=setup({parseResult:{items:[{name:"Chest and triceps",minutes:null,status:"completed"}],questions:["Duration?"]}});
  incomplete.click("text");incomplete.change("text","Trained chest and triceps");incomplete.click("parse");await flush();
  assert.equal(incomplete.requests.length,1);assert.equal(incomplete.form.nodes['[type="submit"]'].disabled,true);
  incomplete.change("minutes",40,0);incomplete.submit();await flush();assert.equal(incomplete.requests.length,2);
  incomplete.submit();await flush();assert.equal(incomplete.closed(),true);
  console.log("PASS: automatic batch/changed-row preview, explicit save, no keystroke/voice auto-parse, failure/unknown/manual save, retry/cache, single edit, clear, cancellation, account switch and expiry. No live provider calls.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
