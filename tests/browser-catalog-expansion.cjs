// Synthetic account and model on 8767 only. No real provider calls or private records.
const {chromium}=require('playwright');
const consent=require('./browser-consent.cjs');
const assert=require('node:assert/strict'),path=require('node:path');
async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN',reducedMotion:'reduce'});
  const base='http://127.0.0.1:8767',errors=[],calls=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',r=>{if(r.method()==='POST')calls.push(r.url());});
  const ready=()=>page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/)&&!document.querySelector('.coach-input').disabled);
  const send=async text=>{await page.locator('.coach-input').fill(text);await page.getByRole('button',{name:'发送消息',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.coach-input')?.value==='');await ready();};
  const latest=()=>page.locator('.coach-turn').last();
  const shot=async name=>{for(const [width,height]of [[1440,1000],[390,844],[320,740]]){
    await page.setViewportSize({width,height});await latest().scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    await page.screenshot({path:path.resolve(__dirname,`../artifacts/r117-${name}-${width}.png`),fullPage:true});
  }await page.setViewportSize({width:1440,height:1000});};
  try{
    await page.goto(base);await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill(`catalog_${Date.now()}`);
    await page.getByLabel('密码',{exact:true}).fill('Synthetic-catalog-2026');
    await page.getByRole('button',{name:'注册并开始',exact:true}).click();await page.locator('#workspace').waitFor({state:'visible'});
    const day=await page.locator('#day').inputValue();
    assert.equal((await page.request.put(`${base}/api/profile`,{data:{height_cm:175,weight_kg:70,nutrition_reference:'regular_training',equipment:'在家，只有哑铃',experience:'experienced',minutes_per_session:40}})).status(),200);
    const target=await(await page.request.get(`${base}/api/intake-target?day=${day}`)).json();
    assert.equal((await page.request.post(`${base}/api/intake-target`,{data:{client_id:crypto.randomUUID(),version:target.version,context_hash:target.context_hash,effective_from:day,kcal:2200,source:'Synthetic',confirmed:true,general_adult:true}})).status(),200);
    await page.locator('[data-view="assistant"]').click();await consent(page);await ready();
    await send('今天还没吃东西，安排午餐和晚餐');
    await send('晚餐我想吃三文鱼');assert.match(await latest().textContent(),/三文鱼/);
    await send('晚餐西兰花换成番茄');assert.match(await latest().textContent(),/番茄/);
    await send('晚餐我想吃苹果');assert.match(await latest().textContent(),/苹果/);
    await send('晚餐我想吃牛奶');assert.match(await latest().textContent(),/低脂纯牛奶/);
    assert.match(await latest().textContent(),/蛋白质/);
    for(const meal of await latest().locator('.quick-meal').all())assert.ok(await meal.locator('.quick-foods li').count()<=7);
    await send('晚餐三文鱼换成瘦羊肉');assert.match(await latest().textContent(),/瘦羊肉/);
    await send('晚餐苹果换成猕猴桃');assert.match(await latest().textContent(),/猕猴桃/);
    await shot('expanded-meal');
    await send('练二头');assert.match(await latest().textContent(),/哑铃二头弯举/);
    assert.doesNotMatch(await latest().locator('.training-recommendation > .training-exercises').textContent(),/哑铃仰卧臂屈伸/);
    await send('练三头');assert.match(await latest().textContent(),/哑铃仰卧臂屈伸/);
    await send('把哑铃仰卧臂屈伸换成哑铃俯身臂后伸');
    assert.match(await latest().locator('.training-recommendation > .training-exercises').textContent(),/哑铃俯身臂后伸/);
    await send('练肩');assert.match(await latest().textContent(),/哑铃侧平举/);
    assert.doesNotMatch(await latest().locator('.training-recommendation > .training-exercises').textContent(),/反向飞鸟/);
    await shot('expanded-training');
    assert.equal(calls.filter(url=>url.endsWith('/understand')).length,0);
    assert.equal((await(await page.request.get(`${base}/api/meals?day=${day}`)).json()).length,0);
    assert.equal((await(await page.request.get(`${base}/api/workouts?day=${day}`)).json()).length,0);
    const count=calls.filter(url=>url.endsWith('/recommend-training')).length;
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});await page.locator('[data-view="assistant"]').click();await ready();
    assert.match(await latest().textContent(),/哑铃侧平举/);
    assert.equal(calls.filter(url=>url.endsWith('/recommend-training')).length,count);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['new foods','fruit and milk addition','nutrition rendering','biceps/triceps/shoulders','equipment filtering','no actual writes','reload no calls','1440/390/320']}));
  }catch(error){await page.screenshot({path:path.resolve(__dirname,'../artifacts/r117-expansion-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
