// Synthetic records and local reviewed rules on the isolated 8767 fixture.
const {chromium}=require('playwright');
const consent=require('./browser-consent.cjs');
const assert=require('node:assert/strict'),path=require('node:path');
async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN',reducedMotion:'reduce'});
  const page=await context.newPage(),errors=[],posts=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  page.on('request',r=>{if(r.method()==='POST')posts.push(r.url());});
  const base='http://127.0.0.1:8767',day='2026-09-17';
  const ready=()=>page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/));
  const send=async(text)=>{await page.locator('.coach-input').fill(text);await page.getByRole('button',{name:'发送消息',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.coach-input')?.value==='');await ready();};
  const coach=async()=>{await page.locator('.nav-item[data-view="assistant"]').click();await consent(page);await ready();};
  const shot=async(name)=>{
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,name);
    assert.deepEqual(await page.locator('.meal-week button,.training-recommendation button').evaluateAll(nodes=>nodes.filter(n=>n.scrollWidth>n.clientWidth+2).map(n=>n.textContent)),[],name);
    await page.screenshot({path:path.resolve(__dirname,`../artifacts/r114-${name}.png`),fullPage:true});
  };
  try{
    await page.goto(base);await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill(`suggest_${Date.now()}`);await page.getByLabel('密码',{exact:true}).fill('Synthetic-training-2026');
    await page.getByRole('button',{name:'注册并开始',exact:true}).click();await page.locator('#workspace').waitFor({state:'visible'});
    assert.equal((await page.request.put(`${base}/api/profile`,{data:{equipment:'在家，只有哑铃',experience:'experienced',minutes_per_session:40}})).status(),200);
    for(const [name,recordDay,status]of [['肩','2026-09-16','completed'],['手臂和背',day,'completed'],['腿',day,'planned']]){
      assert.equal((await page.request.post(`${base}/api/workouts`,{data:{client_id:crypto.randomUUID(),day:recordDay,name,minutes:30,status}})).status(),201);
    }
    for(const [name,recordDay,kcal]of [['测试早餐',day,100],['测试午餐',day,null],['昨日饮食','2026-09-16',150]]){
      assert.equal((await page.request.post(`${base}/api/meals`,{data:{client_id:crypto.randomUUID(),day:recordDay,name,meal_type:'breakfast',grams:100,kcal_per_100g:kcal,source:kcal?'Synthetic label':''}})).status(),201);
    }
    await page.locator('#day').fill(day);await page.locator('#day').dispatchEvent('change');
    await page.locator('.nav-item[data-view="meals"]').click();await page.locator(`[data-meal-day="${day}"][aria-pressed="true"]`).waitFor();
    assert.match(await page.locator('.meal-week .week-energy').textContent(),/250 kcal.*1项未知/);
    await page.locator('[data-meal-day="2026-09-16"]').click();await page.locator('.record').filter({hasText:'昨日饮食'}).waitFor();
    await page.getByRole('button',{name:'下一周',exact:true}).click();await page.locator('[data-meal-day="2026-09-23"][aria-pressed="true"]').waitFor();
    assert.match(await page.locator('.meal-week .week-energy').textContent(),/无记录/);
    await page.getByRole('button',{name:'上一周',exact:true}).click();await page.locator('[data-meal-day="2026-09-16"][aria-pressed="true"]').waitFor();
    for(const[width,height]of [[1440,1000],[390,844],[320,740]]){await page.setViewportSize({width,height});await shot(`meal-week-${width}`);}
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('[data-meal-day="2026-09-17"]').click();await page.locator('[data-meal-day="2026-09-17"][aria-pressed="true"]').waitFor();
    await page.locator('.nav-item[data-view="workouts"]').click();await page.locator('.training-week-grid').waitFor();
    assert.equal(await page.getByRole('button',{name:'安排训练',exact:true}).count(),0);
    await page.getByRole('button',{name:'记录训练',exact:true}).click();
    assert.equal(await page.locator('.workout-item [name="status"]').inputValue(),'completed');
    assert.equal(await page.locator('.workout-item [name="status"] option[value="planned"]').count(),0);
    await page.getByRole('button',{name:'取消',exact:true}).click();
    await coach();assert.equal(await page.locator('.coach-quick button').count(),2);
    await send('结合前几天的训练，今天练什么？');
    const latest=()=>page.locator('.coach-turn').last();
    assert.match(await latest().textContent(),/本次建议：下肢与核心/);
    assert.match(await latest().locator('.training-recommendation > .training-exercises').textContent(),/哑铃高脚杯深蹲/);
    assert.match(await latest().locator('.training-recommendation > .training-exercises').textContent(),/仰卧交替伸展/);
    assert.equal(await page.locator('.training-accept,.training-record,.training-plan-form').count(),0);
    for(const[width,height]of [[1440,1000],[390,844],[320,740]]){await page.setViewportSize({width,height});await shot(`training-suggestion-${width}`);}
    await page.setViewportSize({width:1440,height:1000});
    const before=posts.filter(url=>url.endsWith('/recommend-training')).length;
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('#day').fill(day);await page.locator('#day').dispatchEvent('change');await coach();
    assert.match(await latest().textContent(),/本次建议：下肢与核心/);
    assert.equal(posts.filter(url=>url.endsWith('/recommend-training')).length,before);
    // Suggestions do not write workouts; a later real leg session invalidates them.
    assert.equal((await(await page.request.get(`${base}/api/workouts?day=${day}`)).json()).length,2);
    assert.equal((await page.request.post(`${base}/api/workouts`,{data:{client_id:crypto.randomUUID(),day,name:'腿臀',minutes:35,status:'completed'}})).status(),201);
    await page.getByRole('button',{name:'刷新对话',exact:true}).click();await page.locator('.training-stale').waitFor();
    await page.getByRole('button',{name:'按最新记录重新建议',exact:true}).click();await ready();
    await page.waitForFunction(()=>document.querySelector('.coach-turn:last-child .training-recommendation > .training-exercises')?.textContent.includes('仰卧交替伸展'));
    assert.match(await latest().textContent(),/仰卧交替伸展/);
    // Venue substitution is local and explicit, cardio never turns into completed minutes.
    await send('有氧建议');assert.match(await latest().textContent(),/尚无匹配/);
    await send('健身房和泳池');assert.match(await latest().textContent(),/休闲游泳/);
    assert.match(await latest().textContent(),/本周已记录有氧0天/);
    assert.equal(posts.filter(url=>url.endsWith('/understand')).length,0);
    // A failed response is not automatically retried and the sent question stays visible.
    await page.route('**/recommend-training',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'模拟建议暂不可用'})}));
    await send('接下来还有30分钟');await page.locator('.coach-error').filter({hasText:'模拟建议暂不可用'}).waitFor();
    await page.unrouteAll({behavior:'wait'});await page.getByRole('button',{name:'生成训练建议',exact:true}).click();await ready();
    await page.locator('.coach-turn').last().getByText(/休闲游泳/).first().waitFor();
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['meal calendar','unknown calories','date navigation','actual-only entry','two shortcuts','history and equipment','sets and reps','no workout writes','actual changes invalidate','cardio alternatives','reload no generation','failure/retry','1440/390/320']}));
  }finally{await page.unrouteAll({behavior:'ignoreErrors'});await context.close();await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
