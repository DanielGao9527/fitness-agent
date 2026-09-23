const assert=require('node:assert/strict');
async function consent(page){
  await page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/));
  const form=page.locator('.coach-consent .quick-consent');
  if(await form.count()){
    assert.equal(await form.locator('input:checked').count(),0,'consent is never preselected');
    assert.equal(await page.locator('.coach-input').isDisabled(),true);
    await form.locator('[name="adult"]').check();
    await form.locator('[name="reviewed"]').check();
    await form.getByRole('button',{name:'确认并开始',exact:true}).click();
    await form.waitFor({state:'detached'});
  }
  await page.waitForFunction(()=>!document.querySelector('.coach-input')?.disabled);
}
module.exports=consent;
