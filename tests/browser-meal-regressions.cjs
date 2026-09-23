// Synthetic accounts on the isolated fixture. No real provider requests.
const {chromium}=require('playwright');
const consent=require('./browser-consent.cjs');
const assert=require('node:assert/strict'),path=require('node:path');
const base='http://127.0.0.1:8767';
async function main(){
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN'});
  const page=await context.newPage(),errors=[],requests=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  page.on('request',r=>{if(r.url().endsWith('/recommend-meals'))requests.push(r);});
  const ready=()=>page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/) && !document.querySelector('.coach-input').disabled);
  const send=async message=>{await page.locator('.coach-input').fill(message);await page.getByRole('button',{name:'发送消息',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.coach-input')?.value==='');await ready();};
  const checked=async response=>{assert.ok(response.ok(),await response.text());return response.json();};
  try{
    await checked(await context.request.post(base+'/api/auth/register',{data:{username:`mr_${Date.now()}`,password:'Synthetic-meals-2026'}}));
    await checked(await context.request.put(base+'/api/profile',{data:{height_cm:175,weight_kg:70,nutrition_reference:'regular_training',equipment:'哑铃'}}));
    await page.goto(base);await page.locator('#workspace').waitFor({state:'visible'});
    const day=await page.locator('#day').inputValue();
    const target=await checked(await context.request.get(`${base}/api/intake-target?day=${day}`));
    await checked(await context.request.post(base+'/api/intake-target',{data:{client_id:crypto.randomUUID(),version:target.version,context_hash:target.context_hash,effective_from:day,kcal:2200,source:'Synthetic target',confirmed:true,general_adult:true}}));
    const rows=[];
    for(const name of ['羊肉','牛肉'])rows.push(await checked(await context.request.post(base+'/api/meals',{data:{client_id:crypto.randomUUID(),day,meal_type:'lunch',name,grams:100,amount_description:'半盘，火锅涮煮'}})));
    await page.locator('[data-view="assistant"]').click();await consent(page);await ready();
    await send('今天剩下怎么吃');await send('安排晚餐');
    assert.equal(requests.length,0);
    const blocked=await page.locator('.coach-turn').last().innerText();
    assert.ok(blocked.includes('没有生成餐单') && blocked.includes('羊肉') && blocked.includes('牛肉'));
    assert.ok(!blocked.includes('已整理'));
    const mealVersion=await page.locator('.coach-turn').last().getAttribute('data-version');
    await send('今天练什么');
    assert.ok(await page.locator('.training-recommendation > .training-exercises').last().isVisible());
    assert.ok((await page.locator(`.coach-turn[data-version="${mealVersion}"]`).innerText()).includes('热量未知'));
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});await page.locator('[data-view="assistant"]').click();await ready();
    assert.equal(requests.length,0);
    assert.ok((await page.locator(`.coach-turn[data-version="${mealVersion}"]`).innerText()).includes('羊肉'));
    for(const row of rows){const {id,...body}=row;await checked(await context.request.put(`${base}/api/meals/${id}`,{data:{...body,kcal_per_100g:150,protein_per_100g:20,carbs_per_100g:0,fat_per_100g:8,source:'Synthetic values'}}));}
    // Switching back from ordinary training keeps the meal calculation context.
    await send('安排晚餐');
    assert.equal(await page.locator('.coach-turn').last().locator('.quick-meal').count(),1);
    const menu=await page.locator('.coach-turn').last().locator('.quick-foods').innerText();
    await page.route('**/messages',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Synthetic send failure'})}));
    await page.locator('.coach-input').fill('米饭少一点');await page.getByRole('button',{name:'发送消息',exact:true}).click();
    await page.locator('.coach-error').waitFor({state:'visible'});
    assert.equal(await page.locator('.coach-turn').last().locator('.quick-foods').innerText(),menu);
    await page.unroute('**/messages');await page.locator('.coach-input').fill('');
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/r121-meal-${width}.png`),fullPage:true});
    }
    await page.locator('.nav-item[data-view="meals"]').click();await page.getByRole('button',{name:'记录饮食',exact:true}).click();
    await page.getByRole('button',{name:'文字描述',exact:true}).click();
    const form=page.locator('#draft-form');
    await form.locator('[name="text"]').fill('早餐吃了两个鸡蛋，中午海底捞火锅吃了半盘羊肉');
    await form.locator('[data-action="parse-draft"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-form .form-error')?.textContent.includes('一次只录一餐'));
    assert.equal(await form.locator('.draft-item').count(),0);
    assert.ok((await form.locator('[name="text"]').inputValue()).includes('早餐'));
    await form.locator('[name="text"]').fill('午餐火锅样本，我吃了半盘羊肉和半盘牛肉');
    await form.locator('[data-action="parse-draft"]').click();
    await page.waitForFunction(()=>document.querySelector('#draft-form .form-error')?.textContent.includes('当前选择的是早餐'));
    await form.locator('[name="meal_type"]').selectOption('lunch');
    await form.locator('[data-action="parse-draft"]').click();await form.locator('.draft-item').nth(1).waitFor();
    assert.equal(await form.locator('.draft-item').count(),2);
    assert.ok((await form.locator('[name="amount_description"]').first().inputValue()).includes('火锅'));
    await form.locator('[data-action="estimate-nutrition"]').click();await form.locator('.nutrition-result').nth(1).waitFor();
    await form.locator('[name="reviewed"]').check();await form.locator('[data-action="confirm-draft"]').click();
    await page.locator('#record-dialog').waitFor({state:'hidden'});
    const saved=await checked(await context.request.get(`${base}/api/meals?day=${day}`));
    assert.equal(saved.length,4);assert.ok(saved.every(row=>row.meal_type==='lunch'));
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,provider_calls:0,checks:['unknown food reason in reply/history/reload','ordinary training','food resumes after correction','failed send keeps existing menu','mixed-meal/mismatch blocked','half portions no weighing','1440/390/320']}));
  }catch(error){await page.screenshot({path:path.resolve(__dirname,'../artifacts/r121-meal-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
