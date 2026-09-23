// Current simple entry UI, plus preservation of legacy performance data.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const {randomUUID}=require('node:crypto');

async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN'});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('dialog',dialog=>dialog.accept());
  const call=(url,method='GET',body)=>page.evaluate(async({url,method,body})=>{
    const r=await fetch('/api'+url,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
    if(!r.ok)throw new Error(await r.text());return r.status===204?null:r.json();
  },{url,method,body});
  const review=async()=>{
    await page.getByRole('button',{name:'核对训练',exact:true}).click();
    await page.waitForFunction(()=>{
      const button=document.querySelector('#workout-form [type="submit"]');
      return button && !button.disabled && button.textContent.includes('保存训练');
    });
  };
  const save=async()=>{
    await page.getByRole('button',{name:'保存训练',exact:true}).click();
    await page.getByRole('dialog').waitFor({state:'hidden'});
  };
  try{
    await page.goto('http://127.0.0.1:8767');
    await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill('simple_'+Date.now());
    await page.getByLabel('密码',{exact:true}).fill('Synthetic-test-only-2026');
    await page.getByRole('button',{name:'注册并开始'}).click();
    await page.locator('#workspace').waitFor({state:'visible'});
    await call('/profile','PUT',{weight_kg:80});
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('[data-view="workouts"]').click();
    await page.getByRole('button',{name:'记录训练',exact:true}).click();
    assert.equal(await page.getByLabel('本次体重（kg）',{exact:true}).inputValue(),'80');
    assert.equal(await page.getByText('动作表现（可选）',{exact:true}).count(),0);
    assert.equal(await page.getByText('强度与细节（可选）',{exact:true}).count(),0);
    await page.getByLabel('训练项目或部位',{exact:true}).fill('游泳');
    await page.getByLabel('总时长（分钟）',{exact:true}).fill('30');
    await page.getByLabel('本次体重（kg）',{exact:true}).fill('85');
    await review();assert.equal((await call('/profile')).weight_kg,80);
    for(const width of [1440,390,320]){
      await page.setViewportSize({width,height:900});
      const check=await page.evaluate(()=>({page:document.documentElement.scrollWidth>innerWidth+1,
        dialog:document.querySelector('#record-dialog').scrollWidth>document.querySelector('#record-dialog').clientWidth+1}));
      assert.equal(check.page,false);assert.equal(check.dialog,false);
      await page.screenshot({path:path.resolve(__dirname,'../artifacts/r127-simple-training-'+width+'.png'),fullPage:true});
    }
    await save();assert.equal((await call('/profile')).weight_kg,85);
    await page.getByRole('button',{name:'记录训练',exact:true}).click();
    assert.equal(await page.getByLabel('本次体重（kg）',{exact:true}).inputValue(),'85');
    await page.getByLabel('本次体重（kg）',{exact:true}).fill('90');
    await page.getByRole('button',{name:'取消',exact:true}).click();
    await page.getByRole('dialog').waitFor({state:'hidden'});
    assert.equal((await call('/profile')).weight_kg,85);
    const day=await page.locator('#day').inputValue();
    const performance={exercise_id:'db-bench',load_basis:'each',equipment_label:'Synthetic bench',
      increment_kg:1,technique_stable:true,sets:[{load_kg:20,reps:13,rir:2}]};
    const legacy=await call('/workouts','POST',{client_id:randomUUID(),day,name:'哑铃平板卧推',minutes:20,
      status:'completed',weight_kg:80,intensity:'light',details:'旧记录细节',performance});
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('[data-view="workouts"]').click();
    const record=page.locator('.record').filter({hasText:'哑铃平板卧推'});
    await record.getByRole('button',{name:'编辑训练',exact:true}).click();
    await page.getByLabel('备注',{exact:true}).fill('只改备注');
    await review();await save();
    let saved=(await call('/workouts?day='+day)).find(row=>row.id===legacy.id);
    assert.deepEqual(saved.performance,performance);assert.equal(saved.intensity,'light');assert.equal(saved.details,'旧记录细节');
    assert.equal((await call('/profile')).weight_kg,85,'old estimation weight must not overwrite profile');
    await record.getByRole('button',{name:'编辑训练',exact:true}).click();
    await page.getByLabel('训练项目或部位',{exact:true}).fill('胸和肩');
    await review();await save();
    saved=(await call('/workouts?day='+day)).find(row=>row.id===legacy.id);
    assert.equal(saved.performance,null);assert.equal(saved.intensity,'normal_assumed');assert.equal(saved.details,'');
    assert.deepEqual(errors,[]);
    console.log('simple training, weight sync, cancel, legacy preservation and 1440/390/320 layout: passed');
  }finally{await context.close();await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
