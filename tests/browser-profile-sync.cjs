// Synthetic profiles only; no provider calls or production database writes.
const {chromium}=require('playwright');
const assert=require('node:assert/strict'), path=require('node:path');
const base='http://127.0.0.1:8767';
async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const context=await browser.newContext({viewport:{width:1440,height:1000}}), page=await context.newPage(), errors=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  const nav=async view=>page.locator(`.nav-item[data-view="${view}"]`).click();
  const targetReady=()=>page.waitForFunction(()=>document.querySelector('.nutrition-current')?.textContent.trim() && !document.querySelector('.nutrition-edit').disabled);
  const bodyReady=()=>page.waitForFunction(()=>document.querySelector('.body-period') && !document.querySelector('.body-status').textContent);
  const get=async route=>(await context.request.get(base+'/api'+route)).json();
  try{
    assert.equal((await context.request.post(base+'/api/auth/register',{data:{username:`sync_${Date.now()}`,password:'Synthetic-sync-2026'}})).status(),201);
    await page.goto(base);await page.locator('#workspace').waitFor({state:'visible'});await targetReady();
    const day=await page.locator('#day').inputValue();
    await nav('profile');await targetReady();
    const form=page.locator('#profile-form');
    await form.locator('[name="height_cm"]').fill('175');await form.locator('[name="weight_kg"]').fill('70');
    await form.locator('[name="body_fat_percent"]').fill('22');await form.locator('[name="age"]').fill('30');
    await form.locator('[name="equation_sex"]').selectOption('male');await form.locator('[name="activity"]').selectOption('inactive');
    await page.getByRole('button',{name:'设置长期标准',exact:true}).click();
    await page.locator('.nutrition-standard-form [name="scope"]').check();
    await page.getByRole('button',{name:'预览每日目标',exact:true}).click();
    await page.getByText('请先保存上面的个人档案，再预览长期标准',{exact:true}).waitFor();
    assert.equal(await page.locator('.nutrition-confirm').count(),0);
    const saved=page.waitForResponse(r=>r.url().includes('/api/summary?') && r.status()===200);
    await page.getByRole('button',{name:'保存档案',exact:true}).click();await saved;await targetReady();
    await page.getByRole('button',{name:'设置长期标准',exact:true}).click();
    await page.locator('.nutrition-standard-form [name="scope"]').check();
    await page.getByRole('button',{name:'预览每日目标',exact:true}).click();
    await page.getByRole('button',{name:'确认长期标准',exact:true}).click();
    await page.locator('.nutrition-standard-form').waitFor({state:'detached'});await targetReady();
    const initial=await get(`/intake-target?day=${day}`);
    await nav('body');await bodyReady();
    assert.match(await page.locator('.body-metrics').textContent(),/沿用档案值/);
    assert.equal(await page.locator('.body-table tbody tr').count(),0);
    await page.getByRole('button',{name:'记录体测',exact:true}).click();
    await page.getByLabel('体重（kg）',{exact:true}).fill('80');
    await page.getByRole('button',{name:'保存体测',exact:true}).click();
    await page.locator('.body-form').waitFor({state:'detached'});await bodyReady();
    assert.equal((await get('/profile')).weight_kg,80);assert.equal((await get('/profile')).body_fat_percent,22);
    assert.ok((await get(`/intake-target?day=${day}`)).target.kcal>initial.target.kcal);
    assert.match(await page.locator('.body-metrics').textContent(),new RegExp(`沿用 ${day} 测量值`));
    const older=new Date(`${day}T12:00:00Z`);older.setUTCDate(older.getUTCDate()-7);
    assert.equal((await context.request.post(base+'/api/body-measurements',{data:{client_id:crypto.randomUUID(),day:older.toISOString().slice(0,10),weight_kg:90}})).status(),201);
    assert.equal((await get('/profile')).weight_kg,80);
    await nav('profile');await targetReady();assert.equal(await form.locator('[name="weight_kg"]').inputValue(),'80');
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});await nav('profile');await targetReady();
    assert.equal(await form.locator('[name="age"]').inputValue(),'30');assert.equal(await form.locator('[name="weight_kg"]').inputValue(),'80');
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:900});await nav('body');await bodyReady();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/r116-body-${width}.png`),fullPage:true});
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['unsaved profile guard','formula fields only once','profile-only carried value','latest measurement sync and recalc','partial fields','older backfill no overwrite','reload','1440/390/320']}));
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
