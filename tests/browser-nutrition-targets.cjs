// Real UI flow, isolated synthetic accounts and provider substitutes on 8767.
const {chromium}=require('playwright');
const consent=require('./browser-consent.cjs');
const assert=require('node:assert/strict'),path=require('node:path');
const base='http://127.0.0.1:8767';
async function main(){
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN',reducedMotion:'reduce'});
  const page=await context.newPage(),errors=[],calls=[];
  page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>dialog.accept());
  page.on('request',request=>{if(/recommend-meals$|\/understand$/.test(request.url()))calls.push(request.url());});
  const nav=async view=>{await page.locator(`.nav-item[data-view="${view}"]`).click();};
  const ready=()=>page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/) && !document.querySelector('.coach-input').disabled);
  const send=async text=>{await page.locator('.coach-input').fill(text);await page.getByRole('button',{name:'发送消息',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.coach-input')?.value==='');await ready();};
  const targetReady=()=>page.waitForFunction(()=>document.querySelector('.nutrition-current')?.textContent.trim() && !document.querySelector('.nutrition-edit').disabled);
  const saveProfile=async()=>{const done=page.waitForResponse(r=>r.url().includes('/api/summary?') && r.status()===200);await page.getByRole('button',{name:'保存档案',exact:true}).click();await done;await targetReady();};
  const calc=day=>page.request.get(`${base}/api/summary?day=${day}`).then(r=>r.json());
  const data=()=>page.evaluate(async()=>{const id=document.querySelector('.coach-history').value;return (await fetch(`/api/coach/conversations/${id}`)).json();});
  const currentPlans=d=>d.meal_plans.filter(p=>!p.stale && p.coach_version===d.meal_context.meal_versions[p.meal_type]);
  try{
    const username=`targets_${Date.now()}`;
    assert.equal((await context.request.post(base+'/api/auth/register',{data:{username,password:'Synthetic-targets-2026'}})).status(),201);
    await page.goto(base);await page.locator('#workspace').waitFor({state:'visible'});await targetReady();
    const day=await page.locator('#day').inputValue();
    await nav('assistant');await consent(page);await ready();await send('安排午餐和晚餐');
    assert.ok((await page.locator('.coach-calculation').textContent()).includes('每日目标未就绪'));assert.equal(calls.length,0);
    await page.getByRole('button',{name:'设置长期营养标准',exact:true}).click();await targetReady();
    await page.locator('#profile-form [name="height_cm"]').fill('175');await page.locator('#profile-form [name="weight_kg"]').fill('70');
    await page.locator('#profile-form [name="age"]').fill('30');await page.locator('#profile-form [name="equation_sex"]').selectOption('male');await page.locator('#profile-form [name="activity"]').selectOption('inactive');
    await saveProfile();
    await page.getByRole('button',{name:'设置长期标准',exact:true}).click();
    const form=page.locator('.nutrition-standard-form');
    assert.equal(await form.locator('[name="age"],[name="equation_sex"],[name="activity"],[name="mode"],[name="fixed_kcal"]').count(),0);await form.locator('[name="scope"]').check();
    await page.getByRole('button',{name:'预览每日目标',exact:true}).click();await page.getByRole('button',{name:'确认长期标准',exact:true}).waitFor();
    assert.equal((await calc(day)).intake_target.status,'unset');
    await page.getByRole('button',{name:'确认长期标准',exact:true}).click();await page.getByText('长期营养标准已保存',{exact:true}).waitFor();
    const maintenance=(await calc(day)).intake_target.target.kcal;assert.equal(maintenance,2550);assert.equal(calls.length,0);
    await page.locator('#profile-form [name="goal"]').selectOption('fat_loss');await saveProfile();
    assert.equal((await calc(day)).intake_target.target.kcal,2300);
    await page.locator('#profile-form [name="goal"]').selectOption('muscle_gain');await saveProfile();
    assert.equal((await calc(day)).intake_target.target.kcal,2700);
    await nav('today');await targetReady();await page.getByRole('button',{name:'仅调整本日',exact:true}).click();
    await page.locator('.nutrition-day-form [name="kcal"]').fill('3000');await page.locator('.nutrition-day-form [name="confirmed"]').check();await page.getByRole('button',{name:'保存本日目标',exact:true}).click();await page.locator('.nutrition-restore').waitFor();
    assert.ok((await page.locator('.nutrition-current').textContent()).includes('盈余 450 kcal'));
    await page.locator('.nutrition-restore').click();await page.waitForFunction(()=>!document.querySelector('.nutrition-restore'));
    await nav('profile');await targetReady();
    await page.getByRole('button',{name:'修改长期标准',exact:true}).click();await form.locator('[name="offset_kcal"]').fill('100');await form.locator('[name="scope"]').check();
    await page.getByRole('button',{name:'预览每日目标',exact:true}).click();await page.getByRole('button',{name:'确认长期标准',exact:true}).click();await page.getByText('长期营养标准已保存',{exact:true}).waitFor();
    await nav('today');await targetReady();await page.getByRole('button',{name:'仅调整本日',exact:true}).click();
    await page.locator('.nutrition-day-form [name="kcal"]').fill('3500');await page.locator('.nutrition-day-form [name="confirmed"]').check();await page.getByRole('button',{name:'保存本日目标',exact:true}).click();await page.getByText('本日目标已更新，其他日期不变',{exact:true}).waitFor();
    const tomorrow=new Date(`${day}T12:00:00Z`);tomorrow.setUTCDate(tomorrow.getUTCDate()+1);const tomorrowString=tomorrow.toISOString().slice(0,10);
    assert.equal((await calc(day)).intake_target.target.kcal,3500);assert.equal((await calc(tomorrowString)).intake_target.target.kcal,2800);
    await page.getByRole('button',{name:'恢复长期标准',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.nutrition-restore'));assert.equal((await calc(day)).intake_target.target.kcal,2800);
    // Seed a labelled meal, then exercise its actual edit screen.
    const food={client_id:crypto.randomUUID(),day,meal_type:'breakfast',name:'Synthetic breakfast',grams:100,kcal_per_100g:400,protein_per_100g:20,carbs_per_100g:50,fat_per_100g:10,source:'synthetic label'};
    const saved=await (await context.request.post(base+'/api/meals',{data:food})).json();assert.ok(saved.id);
    await nav('assistant');await ready();await send('安排午餐和晚餐');
    const before=currentPlans(await data());assert.equal(before.length,2);
    assert.ok(Math.abs(before.reduce((n,p)=>n+p.quick_nutrition.portion_reference.kcal,0)-2400)<.2);
    await nav('today');await targetReady();await page.locator(`[data-action="edit"][data-kind="meals"][data-id="${saved.id}"]`).click();
    await page.locator('#record-form [name="grams"]').fill('225');await page.locator('#record-form [type="submit"]').click();await page.locator('#record-dialog').waitFor({state:'hidden'});
    await nav('assistant');await ready();assert.ok((await page.locator('.coach-turn').last().textContent()).includes('旧餐单暂不作为本次建议'));
    const count=calls.length;await page.locator('.coach-recommend').click();await ready();
    const after=currentPlans(await data());assert.ok(Math.abs(after.reduce((n,p)=>n+p.quick_nutrition.portion_reference.kcal,0)-1900)<.2);assert.notDeepEqual(after.map(p=>p.items),before.map(p=>p.items));assert.equal(calls.length,count+1);
    await nav('profile');await targetReady();const general=(await calc(day)).meal_calculation.macro.ranges;
    await page.locator('#profile-form [name="regular_training"]').check();await saveProfile();
    assert.notDeepEqual((await calc(day)).meal_calculation.macro.ranges,general);
    assert.equal((await calc(day)).meal_calculation.macro.center.protein,140);
    // Training nutrition changes only macros, not the saved activity classification.
    assert.equal((await calc(day)).intake_target.target.kcal,2800);
    // Synthetic response to check the derived-target explanation; no real history is modified.
    await page.route('**/api/summary?*',async route=>{
      const response=await route.fetch(),value=await response.json();
      const feedback={adjustment_kcal:100,eligible_days:2,status:'adjusted'};
      value.intake_target.target.base_kcal=2800;value.intake_target.target.kcal=2900;
      value.intake_target.energy_feedback=feedback;
      value.meal_calculation.base_target_kcal=2800;value.meal_calculation.target_kcal=2900;
      value.meal_calculation.energy_feedback=feedback;
      await route.fulfill({response,json:value});
    });
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:width===1440?1000:844});await nav('today');await targetReady();
      assert.ok((await page.locator('.nutrition-current').textContent()).includes('标准加近期调整'));
      assert.ok((await page.locator('.nutrition-current').textContent()).includes('今天小幅增加 100 kcal'));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/r113-targets-${width}.png`),fullPage:true});
      await nav('profile');await targetReady();await page.getByRole('button',{name:'修改长期标准',exact:true}).click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/r116-standard-${width}.png`),fullPage:true});
      await page.locator('.nutrition-cancel').click();
    }
    const callsBefore=calls.length;await page.reload();await page.locator('#workspace').waitFor({state:'visible'});await targetReady();
    assert.equal((await calc(day)).intake_target.target.kcal,2800);assert.equal(calls.length,callsBefore);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['missing target blocks before model','profile inputs / three adjustments only','goal changes','formula offset','day only / tomorrow restore','edit breakfast changes portions','nutrition mode linked','read does not call model','1440/390/320']}));
  }catch(error){await page.screenshot({path:path.resolve(__dirname,'../artifacts/r113-targets-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
