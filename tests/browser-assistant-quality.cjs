// Synthetic server only; provider behavior is verified separately by the capped live harness.
const {chromium}=require('playwright');
const consent=require('./browser-consent.cjs');
const assert=require('node:assert/strict'),path=require('node:path');
async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN',reducedMotion:'reduce'});
  const base='http://127.0.0.1:8767',errors=[],requests=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  page.on('request',r=>{if(r.method()==='POST')requests.push(r.url());});
  const ready=()=>page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/)&&!document.querySelector('.coach-input')?.disabled);
  const latest=()=>page.locator('.coach-turn').last();
  const send=async text=>{await page.locator('.coach-input').fill(text);await page.getByRole('button',{name:'发送消息',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.coach-input')?.value==='');await ready();};
  const shot=async name=>{for(const [width,height]of [[1440,1000],[390,844],[320,740]]){
    await page.setViewportSize({width,height});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    await page.screenshot({path:path.resolve(__dirname,`../artifacts/r119-${name}-${width}.png`),fullPage:true});
  }await page.setViewportSize({width:1440,height:1000});};
  try{
    await page.goto(base);await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill(`quality_${Date.now()}`);await page.getByLabel('密码',{exact:true}).fill('Synthetic-quality-2026');
    await page.getByRole('button',{name:'注册并开始',exact:true}).click();await page.locator('#workspace').waitFor({state:'visible'});
    assert.equal((await page.request.put(`${base}/api/profile`,{data:{training_split:'five',training_days:5,minutes_per_session:60,experience:'experienced',equipment:'哑铃，可调训练凳，杠铃，卧推架，保护杠，泳池'}})).status(),200);
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});const day=await page.locator('#day').inputValue();
    await page.locator('[data-view="assistant"]').click();await consent(page);await ready();await send('练胸');
    const count=()=>requests.filter(url=>url.endsWith('/recommend-training')).length;
    const before=count();await send('刚才的哑铃平板卧推我想改用杠铃平板卧推，其他动作保持');
    assert.equal(count(),before+1,'validated request can return a suggestion after daily consent');
    assert.equal(await page.locator('.coach-review-checked').count(),0);
    await shot('replacement-answer');
    assert.match(await latest().locator('.training-recommendation > .training-exercises').textContent(),/杠铃平板卧推/);
    await send('我还是想试试三分化，你帮我把这一周重新排一下');
    assert.equal(await page.locator('.coach-review-checked').count(),0);
    assert.match(await latest().textContent(),/三分化/);
    assert.equal((await(await page.request.get(`${base}/api/profile`)).json()).training_split,'five');
    await send('今天是在家练，只有哑铃，没有训练凳，帮我调整一下');
    assert.equal(await page.locator('.coach-review-checked').count(),0);
    await shot('equipment-answer');
    assert.doesNotMatch(await latest().locator('.training-recommendation > .training-exercises').textContent(),/杠铃平板卧推|哑铃平板卧推|上斜哑铃卧推/);
    await send('器械改为泳池');
    await send('这次先不练力量了，我想去泳池游一会儿，接下来还有30分钟');
    assert.equal(await page.locator('.coach-review-checked').count(),0);
    assert.match(await latest().textContent(),/休闲游泳/);await shot('swim');
    const understanding=requests.filter(url=>url.endsWith('/understand')).length;
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});await page.locator('[data-view="assistant"]').click();await ready();
    assert.equal(requests.filter(url=>url.endsWith('/understand')).length,understanding);
    const todayConversation=await page.locator('.coach-history').inputValue();
    const old=await(await page.request.post(`${base}/api/coach/conversations`,{data:{client_id:crypto.randomUUID(),day:'2026-09-01'}})).json();
    assert.equal((await page.request.post(`${base}/api/coach/conversations/${old.id}/messages`,{data:{client_id:crypto.randomUUID(),version:0,message:'今天练什么'}})).status(),200);
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});await page.locator('[data-view="assistant"]').click();await ready();
    assert.equal(await page.locator('.coach-history').inputValue(),todayConversation,'older dates must not replace current-date conversation on entry');
    const beforeRevoke=requests.length;
    assert.equal((await page.request.delete(`${base}/api/meal-plans/consent`)).status(),200);
    await page.locator('.coach-input').fill('接下来还有20分钟');
    await page.getByRole('button',{name:'发送消息',exact:true}).click();
    await page.locator('.coach-consent .quick-consent').waitFor();
    assert.equal(await page.locator('.coach-input').inputValue(),'接下来还有20分钟');
    assert.equal(requests.length,beforeRevoke,'revocation must stop sending before storing a turn');
    await consent(page);await send('接下来还有20分钟');
    assert.equal(await page.locator('.coach-consent input:checked').count(),0);
    assert.equal((await(await page.request.get(`${base}/api/workouts?day=${day}`)).json()).length,0);
    assert.equal((await(await page.request.get(`${base}/api/training-plans?day=${day}`)).json()).length,0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,provider_calls:0,checks:['automatic read-only suggestions after daily consent','no repeated review','temporary split','complete equipment negation','swim answer','reload no calls','no actual writes','1440/390/320']}));
  }catch(error){await page.screenshot({path:path.resolve(__dirname,'../artifacts/r119-browser-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
