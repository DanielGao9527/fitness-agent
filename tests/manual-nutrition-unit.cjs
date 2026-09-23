// Isolated event/DOM substitutes; no real browser, records or provider calls.
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const source = fs.readFileSync(path.join(__dirname, "../static/manual-nutrition.js"), "utf8");

function setup({count=2, saved=false, deferred=false, fail=false, unknown=false}={}) {
  const listeners = {}, requests = [];
  let release, confirmations=0;
  const node = (extra={}) => ({disabled:false, hidden:false, isConnected:true, value:"", ...extra});
  const estimate = node({dataset:{action:"estimate-manual-nutrition"}, nodes:{span:node()}});
  const clear = node({dataset:{action:"clear-manual-nutrition"}});
  estimate.closest = () => estimate; clear.closest = () => clear;
  const items = Array.from({length:count}, (_, index) => {
    const item = {nodes:{}};
    for (const name of ["name","grams","amount_description","kcal_per_100g","protein_per_100g","carbs_per_100g","fat_per_100g"])
      item.nodes[`[name="${name}"]`] = node({name, closest:()=>item});
    item.nodes['[name="name"]'].value = `Eggs ${index}`;
    item.nodes['[name="amount_description"]'].value = "2 eggs";
    item.nodes['.manual-nutrition-status'] = node();
    item.nodes['.manual-nutrition-result'] = node();
    return item;
  });
  const form = {isConnected:true,nodes:{
    '[data-action="estimate-manual-nutrition"]':estimate,
    '[data-action="clear-manual-nutrition"]':clear,
    '.manual-batch-status':node(),
  }, querySelectorAll:selector=>selector===".meal-item" ? items : [estimate,clear,...items.flatMap(item=>Object.values(item.nodes))]};
  const context = {window:{confirm(){confirmations++;return false;}}, state:{editing:{row:saved?{}:null},nutritionStatus:"configured_unverified"},
    document:{addEventListener:(type,fn)=>{listeners[type]=fn;}}, crypto:{randomUUID}, AbortController,setTimeout,clearTimeout,
    $:(selector,root)=>root.nodes[selector], icon:()=>"",icons(){},nutritionDetails:value=>JSON.stringify(value),
    portionIssue:item=>!item.name || !item.amount_description ? "missing" : "",
    api:async(_url,options)=>{
      requests.push(JSON.parse(options.body));
      if(fail) throw new Error("Synthetic failure");
      const result={items:requests.at(-1).items.map(food=>({id:randomUUID(), food, nutrition:{model:"fixture",generated_at:"test",items:[unknown && requests.length===1?{status:'unknown',question:'Synthetic question'}:{status:"estimated",kcal:{lower:100,upper:200}}]}}))};
      return deferred ? await new Promise(resolve=>{release=()=>resolve(result);}) : result;
    }};
  vm.runInNewContext(source, context);
  const module=context.window.ManualNutrition;
  module.mount(form,saved?{nutrition_estimate:{status:"estimated",kcal:{lower:100,upper:200}}}:null);
  return {module,items,form,requests,estimate,clear,
    click:button=>listeners.click({target:button}),
    change:(index,name,value)=>{const target=items[index].nodes[`[name="${name}"]`];target.value=value;listeners.input({target});},
    release:()=>release(), confirmations:()=>confirmations};
}
const flush = () => new Promise(setImmediate);

async function main() {
  const retry=setup({count:1,unknown:true});
  retry.click(retry.estimate);await flush();
  assert.equal(retry.estimate.disabled,false);
  retry.click(retry.estimate);await flush();
  assert.equal(retry.requests.length,2);
  assert.notEqual(retry.requests[0].items[0].client_id,retry.requests[1].items[0].client_id);
  assert.equal(retry.estimate.disabled,true);
  const batch=setup({count:8});
  assert.doesNotMatch(batch.module.markup(),/data-action=/);
  assert.equal((batch.module.toolbar(false).match(/estimate-manual-nutrition/g)||[]).length,1);
  batch.click(batch.estimate); await flush();
  assert.equal(batch.requests[0].items.length,8);
  const retained=batch.module.payload(batch.items[1]).nutrition_preview_id;
  batch.change(0,"amount_description","3 eggs");
  assert.equal(batch.module.allowSave(batch.form),false);
  assert.equal(batch.confirmations(),1);
  assert.equal(batch.module.payload(batch.items[1]).nutrition_preview_id,retained);
  batch.click(batch.estimate);await flush();
  assert.equal(batch.requests[1].items.length,1);
  assert.equal(batch.requests[1].items[0].amount_description,"3 eggs");
  assert.equal(batch.module.allowSave(batch.form),true);
  assert.equal(batch.module.payload(batch.items[1]).nutrition_preview_id,retained);

  const edit=setup({count:1,saved:true});
  edit.change(0,"amount_description","5 eggs");
  assert.equal(edit.module.payload(edit.items[0]).clear_nutrition_estimate,true);
  edit.click(edit.estimate);await flush();
  assert.equal(edit.requests[0].items.length,1);
  assert.ok(edit.module.payload(edit.items[0]).nutrition_preview_id);
  assert.equal(edit.module.allowSave(edit.form),true);

  const mixed=setup();mixed.change(1,"kcal_per_100g","100");
  mixed.click(mixed.estimate);await flush();
  assert.equal(mixed.requests[0].items.length,1);
  mixed.click(mixed.clear);
  assert.equal(mixed.module.payload(mixed.items[0]).clear_nutrition_estimate,true);
  assert.equal(mixed.items[1].nodes['[name="kcal_per_100g"]'].value,"100");

  const failed=setup({fail:true});failed.click(failed.estimate);await flush();
  assert.equal(failed.estimate.disabled,false);
  assert.equal(failed.items[0].nodes['[name="amount_description"]'].value,"2 eggs");
  assert.match(failed.items[0].nodes['.manual-nutrition-status'].textContent,/Synthetic failure/);
  const cancelled=setup({deferred:true});cancelled.click(cancelled.estimate);
  assert.equal(cancelled.module.allowSave(cancelled.form),false);
  cancelled.module.cancel();cancelled.release();await flush();
  assert.equal(cancelled.module.payload(cancelled.items[0]).nutrition_preview_id,undefined);
  assert.equal(cancelled.items[0].nodes['[name="name"]'].disabled,false);
  console.log("PASS: batch and changed-row reuse; edit and stale-save guard; manual values and clear; errors; cancellation/late response. No real provider calls.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
