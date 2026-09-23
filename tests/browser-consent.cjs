const assert=require('node:assert/strict');
async function consent(page){
  await page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/));
  const form=page.locator('.coach-consent .quick-consent');
  if(await form.count()){
    assert.equal(await form.locator('input:checked').count(),0,'consent is never preselected');
    assert.equal(await page.locator('.coach-input').isDisabled(),false,'general questions remain available without meal consent');
    await form.locator('[name="adult"]').check();
    await form.locator('[name="reviewed"]').check();
    await form.getByRole('button',{name:'确认并开始',exact:true}).click();
    await form.waitFor({state:'detached'});
  }else{
    // These existing suites exercise recommendations with explicit meal consent.
    // Knowledge/consent UI gating is covered separately in browser-coach-knowledge.
    const url=new URL('/api/meal-plans/consent',page.url()).href;
    const response=await page.request.get(url);
    const data=await response.json();
    if(!data.confirmed){
      const saved=await page.request.put(url,{data:{context_hash:data.context_hash,adult_general_diet:true,constraints_reviewed:true}});
      assert.equal(saved.status(),200);
      await page.locator('.coach-refresh').click();
      await page.waitForFunction(()=>!document.querySelector('.coach-input')?.disabled);
    }
  }
  await page.waitForFunction(()=>!document.querySelector('.coach-input')?.disabled);
}
module.exports=consent;
