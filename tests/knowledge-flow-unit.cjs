// DOM/request substitutes only. No provider calls or business data writes.
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const source = fs.readFileSync(path.join(__dirname, "../static/knowledge.js"), "utf8");
const flush = () => new Promise(setImmediate);

function setup() {
  const node = (props={}) => ({value:"",innerHTML:"",textContent:"",hidden:false,listeners:{},
    addEventListener(type,fn){this.listeners[type]=fn;},setAttribute(){},classList:{toggle(){}},
    replaceChildren(){this.innerHTML="";},...props});
  const nodes = Object.fromEntries(["#knowledge-search",'[name="query"]',".knowledge-error",".knowledge-state",".knowledge-results"].map(key=>[key,node()]));
  const buttons=["all","training","nutrition"].map(topic=>node({dataset:{topic}}));
  const root=node({isConnected:true,querySelector:key=>nodes[key],querySelectorAll:selector=>selector==="[data-topic]" ? buttons : []});
  const requests=[];
  const context={window:{},state:{user:{id:1}},AbortController,setTimeout,clearTimeout,
    icons(){},icon:()=>"",escapeHtml:value=>String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;"),
    api:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))};
  vm.runInNewContext(source,context);
  const module=context.window.KnowledgeView;
  module.mount(root);
  const submit=query=>{nodes['[name="query"]'].value=query;nodes["#knowledge-search"].listeners.submit({preventDefault(){}});};
  return {module,root,nodes,buttons,context,requests,submit};
}

const result = (title="Fresh") => ({status:"ready",source_count:4,chunk_count:12,unavailable_source_count:0,hits:[{
  title,topic:"training",excerpt:"Summary",url:"https://www.cdc.gov/",license_url:"https://www.cdc.gov/other/agencymaterials.html"
}]});

async function main() {
  const view=setup();
  assert.equal(JSON.parse(view.requests[0].options.body).query,"");
  view.submit("strength");
  assert.ok(view.requests[0].options.signal.aborted);
  view.requests[1].resolve(result('<img src=x onerror="evil()">'));await flush();
  assert.ok(view.nodes[".knowledge-results"].innerHTML.includes("&lt;img"));
  assert.ok(!view.nodes[".knowledge-results"].innerHTML.includes("<img"));
  assert.ok(view.nodes[".knowledge-results"].innerHTML.includes('rel="noopener noreferrer"'));
  view.requests[0].resolve(result("Stale"));await flush();
  assert.ok(!view.nodes[".knowledge-results"].innerHTML.includes("Stale"));
  view.buttons[2].listeners.click();
  assert.equal(JSON.parse(view.requests[2].options.body).topic,"nutrition");
  view.requests[2].reject(new Error("Offline"));await flush();
  assert.equal(view.nodes['[name="query"]'].value,"strength");
  assert.equal(view.nodes[".knowledge-error"].textContent,"Offline");
  view.submit("unknown");view.requests[3].resolve({...result(),hits:[]});await flush();
  assert.ok(view.nodes[".knowledge-results"].innerHTML.includes("未找到匹配资料"));
  view.submit("empty");view.requests[4].resolve({...result(),status:"empty",hits:[]});await flush();
  assert.ok(view.nodes[".knowledge-results"].innerHTML.includes("暂无可用资料"));
  view.submit("late");view.module.dispose();
  assert.ok(view.requests[5].options.signal.aborted);
  view.requests[5].resolve(result("Disposed"));await flush();
  assert.ok(!view.nodes[".knowledge-results"].innerHTML.includes("Disposed"));
  const logout=setup();logout.context.state.user={id:2};
  logout.requests[0].resolve(result("Old user"));await flush();
  assert.equal(logout.nodes[".knowledge-results"].innerHTML,"");
  logout.module.dispose();
  const notes=setup(), notesResult=result('<script>source</script>');
  Object.assign(notesResult.hits[0],{origin:'curated_text',url:'',license_url:''});
  notes.requests[0].resolve(notesResult);await flush();
  const html=notes.nodes['.knowledge-results'].innerHTML;
  assert.ok(html.includes('训练参考笔记'));
  assert.ok(html.includes('作者归属未核实'));
  assert.ok(html.includes('&lt;script>'));
  assert.ok(!html.includes('<a ')&&!html.includes('<script>'));
  notes.module.dispose();
  console.log("Knowledge flow: cancellation, stale responses, account switch, escaping, filters and errors passed.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
