// Isolated 8767 fixture. Synthetic accounts/models only, no provider calls.
const {chromium}=require('playwright');
const consent=require('./browser-consent.cjs');
const assert=require('node:assert/strict'),path=require('node:path');
async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN',reducedMotion:'reduce'});
  const errors=[],requests=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  page.on('request',r=>{if(r.url().endsWith('/recommend-meals'))requests.push(r);});
  const ready=()=>page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/) && !document.querySelector('.coach-input').disabled);
  const enter=async()=>{await page.locator('[data-view="assistant"]').click();await consent(page);await ready();};
  const send=async message=>{await page.locator('.coach-input').fill(message);await page.getByRole('button',{name:'发送消息',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.coach-input')?.value==='');await ready();};
  const current=()=>page.locator('.coach-turn').last();
  const rows=async()=>current().locator('.quick-foods').allTextContents();
  const freshData=()=>page.evaluate(async()=>{const id=document.querySelector('.coach-history').value;return (await fetch(`/api/coach/conversations/${id}`)).json();});
  try{
    await page.goto('http://127.0.0.1:8767');await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill(`quick_${Date.now()}`);await page.getByLabel('密码',{exact:true}).fill('Synthetic-quick-2026');
    await page.getByRole('button',{name:'注册并开始',exact:true}).click();await page.locator('#workspace').waitFor({state:'visible'});
    const day=await page.locator('#day').inputValue();
    assert.equal((await page.request.put('http://127.0.0.1:8767/api/profile',{data:{height_cm:175,weight_kg:70,nutrition_reference:'regular_training'}})).status(),200);
    const target=await (await page.request.get(`http://127.0.0.1:8767/api/intake-target?day=${day}`)).json();
    assert.equal((await page.request.post('http://127.0.0.1:8767/api/intake-target',{data:{client_id:crypto.randomUUID(),version:target.version,context_hash:target.context_hash,effective_from:day,kcal:2200,source:'Synthetic target',confirmed:true,general_adult:true}})).status(),200);
    await enter();await send('今天剩下怎么吃');
    assert.equal(requests.length,0);assert.equal(await page.locator('.coach-meal-selection').count(),1);
    await send('安排午餐和晚餐');assert.equal(requests.length,0);
    await page.getByRole('button',{name:'今天还没吃东西',exact:true}).click();await ready();
    assert.equal(await current().locator('.quick-meal').count(),2);
    assert.equal(await current().locator('.quick-total').count(),1);
    assert.ok((await current().locator('.quick-balance').textContent()).includes('三项营养范围核对通过'));
    assert.equal((await current().locator('.quick-reference').textContent()).includes('距参考下沿'),false);
    assert.ok((await current().locator('.quick-reference').textContent()).includes('优先参考约 140 g'));
    assert.ok((await current().textContent()).includes('本日配餐目标：2200 kcal'));
    assert.equal((await current().textContent()).includes('未强行凑数'),false);
    assert.equal(await page.locator('.plan-generate,.plan-accept,.plan-portions-edit,.plan-record-actual').count(),0);
    assert.equal(await current().locator('button').count(),0);
    const first=await rows();assert.equal(first.length,2);
    assert.ok((await current().textContent()).includes('蛋白质'));assert.ok((await current().textContent()).includes('已确认完整'));
    await send('今天还没吃东西，安排午餐和晚餐');
    assert.equal(await page.locator('.quick-consent').count(),0);
    assert.ok((await current().textContent()).includes('已确认完整'));
    const before=await rows();
    const initialData=await freshData();
    const originalProtein=initialData.meal_plans.find(plan=>plan.meal_type==='dinner' && !plan.stale).items.find(item=>item.group==='protein').name;
    await send(`晚餐${originalProtein}换成牛肉`);
    const after=await rows();assert.equal(after[0],before[0]);assert.ok(after[1].includes('牛肉'));if(originalProtein!=='牛肉')assert.ok(!after[1].includes(originalProtein));
    assert.equal(await current().locator('button').count(),0);
    const count=requests.length;
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});await enter();
    assert.equal(requests.length,count);assert.deepEqual(await rows(),after);
    for(const [width,height] of [[1440,1000],[390,844],[320,740]]){
      await page.setViewportSize({width,height});await current().scrollIntoViewIfNeeded();
      const overflow=await page.evaluate(()=>({page:document.documentElement.scrollWidth>innerWidth+1,clipped:[...document.querySelectorAll('.quick-recommendation *')].filter(el=>el.getBoundingClientRect().width>0 && el.scrollWidth>el.clientWidth+2 && getComputedStyle(el).display!=='inline').map(el=>el.className)}));
      assert.deepEqual(overflow,{page:false,clipped:[]});
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/r111-quick-${width}.png`),fullPage:true});
      if(width===320)await current().screenshot({path:path.resolve(__dirname,'../artifacts/r125-quick-current-320.png')});
    }
    await page.setViewportSize({width:1440,height:1000});
    let blocked=true;
    await page.route('**/recommend-meals',async route=>{if(blocked){blocked=false;return route.fulfill({status:504,contentType:'application/json',body:JSON.stringify({detail:{message:'Synthetic network timeout'}})});}return route.continue();});
    await send('晚餐牛肉换成虾');await page.locator('.coach-recommend').waitFor();
    assert.ok((await page.locator('.coach-error').textContent()).includes('timeout'));
    await page.locator('.coach-recommend').click();await ready();assert.ok((await rows())[1].includes('虾仁'));
    const lastData=await freshData();assert.equal(lastData.turns.at(-1).response.quick_request.status,'ready');
    const meals=await (await page.request.get(`http://127.0.0.1:8767/api/meals?day=${day}`)).json();assert.equal(meals.length,0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,provider_calls:0,checks:['one reply/two meals','automatic calculation','no meal action buttons','once-only consent','remaining scope','followup keeps lunch','refresh no calls','explicit retry','no actual writes','1440/390/320']}));
  }catch(error){await page.screenshot({path:path.resolve(__dirname,'../artifacts/r111-quick-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
