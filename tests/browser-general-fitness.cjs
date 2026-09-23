// Synthetic fixture only; the removed injury feature must not reappear in the UI.
const {chromium}=require('playwright');
const consent=require('./browser-consent.cjs');
const assert=require('node:assert/strict'),path=require('node:path');
const base='http://127.0.0.1:8767';
async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN'});
  const page=await context.newPage(),errors=[],calls=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  page.on('request',r=>{if(r.url().endsWith('/understand'))calls.push(r.url());});
  const ready=()=>page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/) && !document.querySelector('.coach-input').disabled);
  const send=async message=>{await page.locator('.coach-input').fill(message);await page.getByRole('button',{name:'发送消息',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.coach-input')?.value==='');await ready();};
  try{
    assert.equal((await context.request.post(base+'/api/auth/register',{data:{username:`general_${Date.now()}`,password:'Synthetic-General-2026'}})).status(),201);
    await page.goto(base);await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('.nav-item[data-view="profile"]').click();
    assert.equal(await page.locator('[name="training_limitations"]').count(),0);
    assert.ok((await page.locator('#profile-form').innerText()).includes('仅适用于无伤病'));
    await page.getByLabel('器械与场地',{exact:true}).fill('在家，只有哑铃');
    await page.getByLabel('食物过敏与禁忌',{exact:true}).fill('西兰花过敏');
    const saved=page.waitForResponse(r=>r.url().endsWith('/api/profile') && r.request().method()==='PUT');
    await page.getByRole('button',{name:'保存档案',exact:true}).click();
    assert.equal((await saved).status(),200);
    await page.locator('.nav-item[data-view="assistant"]').click();await consent(page);await ready();
    await send('今天练什么');
    assert.ok(await page.locator('.training-recommendation > .training-exercises').isVisible());
    assert.equal(calls.length,0);
    assert.ok(!(await page.locator('.coach-context').innerText()).includes('训练限制'));
    assert.ok((await page.locator('.coach-context').textContent()).includes('西兰花过敏'));
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/r122-general-${width}.png`),fullPage:true});
    }
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('.nav-item[data-view="profile"]').click();
    assert.equal(await page.locator('[name="training_limitations"]').count(),0);
    assert.equal(await page.getByLabel('食物过敏与禁忌',{exact:true}).inputValue(),'西兰花过敏');
    await page.screenshot({path:path.resolve(__dirname,'../artifacts/r122-profile-320.png'),fullPage:true});
    const profile=await(await context.request.get(base+'/api/profile')).json();
    assert.ok(!('training_limitations' in profile));
    // Explicit unsupported requests receive a scope notice, not an injury intake form.
    await page.locator('.nav-item[data-view="assistant"]').click();await ready();
    await send('我肩疼，还想安排一点有氧');
    assert.equal(await page.locator('.coach-review-checked').count(),0);
    const final=page.locator('.coach-turn').last();
    assert.ok((await final.textContent()).includes('不提供医疗、伤病或康复'));
    assert.equal(await final.locator('.training-exercises').count(),0);
    assert.equal(await page.locator('[name="guidance"],[name="warning_signs"]').count(),0);
    const day=await page.locator('#day').inputValue();
    assert.deepEqual(await(await context.request.get(`${base}/api/workouts?day=${day}`)).json(),[]);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['removed profile field','saved allergies','normal training','no automatic recording','reload','unsupported request notice','1440/390/320']}));
  }finally{await context.close();await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
